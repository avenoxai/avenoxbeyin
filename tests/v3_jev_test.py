"""Synthetic offline advisor gates; no credentials or real vault data."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
sys.path.insert(0, str(ROOT / 'extensions/laya'))
import beyin_v3 as runtime
import beyin_v3_jev as advisor


class AdvisorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.store = runtime.MemoryStore(root / 'state', self.vault)
        self.calls = []
        self.env = patch.dict(os.environ, {'TYPESAFE_API_KEY': 'synthetic'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.a = self.record('a')
        self.b = self.record('b')

    def record(self, ident, **extra):
        text = 'Quartz project prefers short notes.'
        path = self.vault / (ident + '.md')
        path.write_text(text, encoding='utf-8')
        return self.store.ingest(dict(id=ident, text=text, source=path.name,
                                     project=extra.pop('project', 'quartz'), **extra))

    def config(self, mode='shadow'):
        (self.store.state_dir / 'jev.json').write_text(json.dumps({'mode': mode}), encoding='utf-8')

    def transport(self, url, body, key, timeout):
        self.calls.append(body)
        return {'answers': {q: {'type': 'score', 'score': 2 if body['state']['candidates'][int(q.split('_c')[1])]['id'] == 'a' else 1}
                            for q in body['questions']}}

    def context(self, **kw):
        return advisor.advise_context(self.store, 'short notes', project='quartz', transport=self.transport, **kw)

    def proposal(self):
        return dict(status='proposed', project='quartz', claim='Use short notes for Quartz.',
                    evidence=[dict(record_id='a', source_sha256=self.a['source_sha256'],
                                   quote='Quartz project prefers short notes.')])

    def test_off_has_no_calls_or_cache(self):
        local = self.store.retrieve('short notes', project='quartz')
        actual = self.context()
        self.assertEqual(actual['records'], local['records'])
        self.assertFalse(self.calls)
        self.assertFalse((self.store.state_dir / '.cache').exists())

    def test_shadow_retains_order_on_reranks_same_set(self):
        self.config()
        shadow = self.context()
        self.assertEqual([r['id'] for r in shadow['records']], ['b', 'a'])
        self.config('on')
        selected = self.context()
        self.assertEqual([r['id'] for r in selected['records']], ['a', 'b'])
        self.assertEqual([r['id'] for r in selected['citations']], ['a', 'b'])
        self.assertEqual(selected['used_chars'], shadow['used_chars'])

    def test_scope_visibility_trust_and_stale_excluded_before_send(self):
        self.record('foreign', project='elsewhere')
        self.record('private', visibility='private')
        self.record('untrusted', trust='untrusted')
        self.record('stale')
        (self.vault / 'stale.md').write_text('changed', encoding='utf-8')
        self.config()
        self.context()
        self.assertEqual({r['id'] for r in self.calls[0]['state']['candidates']}, {'a', 'b'})

    def test_query_secret_stops_call_and_leaves_local_context(self):
        self.config()
        result = advisor.advise_context(self.store, 'short notes password=123456789012',
                                        project='quartz', transport=self.transport)
        self.assertTrue(result['jev']['degraded'])
        self.assertFalse(self.calls)

    def test_private_or_missing_project_rejected(self):
        with self.assertRaises(ValueError):
            self.context(audience='private')
        with self.assertRaises(ValueError):
            advisor.advise_context(self.store, 'notes', project=None)

    def test_source_change_during_request_removes_stale_record(self):
        self.config('on')
        def mutate(*args):
            (self.vault / 'a.md').write_text('changed', encoding='utf-8')
            return self.transport(*args)
        result = advisor.advise_context(self.store, 'short notes', project='quartz', transport=mutate)
        self.assertTrue(result['jev']['degraded'])
        self.assertEqual([r['id'] for r in result['records']], ['b'])
        self.assertEqual(result['jev']['scores'], {})

    def test_mode_change_during_request_ignores_scores(self):
        self.config('on')
        def mutate(*args):
            self.config('off')
            return self.transport(*args)
        result = advisor.advise_context(self.store, 'short notes', project='quartz', transport=mutate)
        self.assertEqual(result['jev']['scores'], {})
        self.assertEqual([r['id'] for r in result['records']], ['b', 'a'])

    def test_provider_failure_falls_back(self):
        self.config('on')
        def fail(*args):
            raise TimeoutError('must not appear')
        result = advisor.advise_context(self.store, 'short notes', project='quartz', transport=fail)
        self.assertEqual([r['id'] for r in result['records']], ['b', 'a'])
        self.assertTrue(result['jev']['degraded'])
        self.assertNotIn('must not appear', json.dumps(result))

    def test_review_never_mutates_database_or_source(self):
        self.config()
        before = self.store.database.read_bytes()
        result = advisor.review_candidate(self.store, self.proposal(), project='quartz', transport=self.transport)
        self.assertFalse(result['memory_written'])
        self.assertFalse(result['approved'])
        self.assertFalse(result['jev']['degraded'])
        self.assertEqual(before, self.store.database.read_bytes())
        self.assertEqual(hashlib.sha256((self.vault/'a.md').read_bytes()).hexdigest(), self.a['source_sha256'])

    def test_review_scope_quote_and_hash_are_exact(self):
        self.config()
        for field, value in [('record_id', 'absent'), ('source_sha256', 'wrong'), ('quote', 'invented')]:
            proposal = self.proposal()
            proposal['evidence'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                advisor.review_candidate(self.store, proposal, project='quartz', transport=self.transport)
        with self.assertRaises(ValueError):
            advisor.review_candidate(self.store, self.proposal(), project='elsewhere', transport=self.transport)
        self.assertFalse(self.calls)

    def test_review_rejects_source_drift_during_io(self):
        self.config()
        def mutate(*args):
            (self.vault/'a.md').write_text('changed', encoding='utf-8')
            return self.transport(*args)
        result = advisor.review_candidate(self.store, self.proposal(), project='quartz', transport=mutate)
        self.assertTrue(result['jev']['degraded'])
        self.assertEqual(result['jev']['scores'], {})

    def long_pool(self, count=advisor.MANUAL_POOL):
        # Ordinary notes longer than the 800-character excerpt: a full manual pool of these
        # is over the default 24,000-character request budget in one request.
        for i in range(count):
            text = f'Release planning note {i:02d}. ' + f'Checklist item {i} covers staging and rollback steps. ' * 30
            path = self.vault / f'n{i:02d}.md'
            path.write_text(text, encoding='utf-8')
            self.store.ingest(dict(id=f'n{i:02d}', text=text, source=path.name, project='quartz'))

    def scoring(self, favourite, fail_on=None):
        def transport(url, body, key, timeout):
            ids = [c['id'] for c in body['state']['candidates']]
            self.calls.append(dict(ids=ids, chars=len(json.dumps(body, ensure_ascii=False))))
            if fail_on in ids:
                raise TimeoutError('synthetic')
            return {'answers': {q: {'type': 'score', 'score': 2 if ids[int(q.split('_c')[1])] == favourite else 1}
                                for q in body['questions']}}
        return transport

    def test_large_pool_is_split_into_packages_that_fit(self):
        self.long_pool()
        self.config('on')
        result = advisor.advise_context(self.store, 'release planning', project='quartz',
                                        transport=self.scoring('n15'))
        jev = result['jev']
        self.assertFalse(jev['degraded'], jev['diagnostics'])
        self.assertNotIn('budget_exceeded', jev['diagnostics'])
        self.assertTrue(2 <= len(self.calls) <= 3, len(self.calls))
        self.assertEqual(jev['packages'], len(self.calls))
        self.assertEqual(jev['http_requests'], len(self.calls))
        self.assertTrue(all(call['chars'] <= 24000 for call in self.calls))
        sent = [ident for call in self.calls for ident in call['ids']]
        self.assertEqual(len(sent), advisor.MANUAL_POOL)
        self.assertEqual(len(set(sent)), len(sent))
        self.assertEqual(set(jev['scores']), set(sent))
        self.assertEqual(result['records'][0]['id'], 'n15')

    def test_failed_package_keeps_local_order_without_retry(self):
        self.long_pool()
        self.config('on')
        local = self.store.retrieve('release planning', project='quartz')
        result = advisor.advise_context(self.store, 'release planning', project='quartz',
                                        transport=self.scoring('n15', fail_on='n15'))
        self.assertTrue(result['jev']['degraded'])
        self.assertEqual(result['jev']['scores'], {})
        self.assertEqual(result['records'], local['records'])
        self.assertEqual(len(self.calls), result['jev']['packages'])
        self.assertEqual(result['jev']['http_requests'], len(self.calls))
        self.assertEqual(sum(call['ids'].count('n15') for call in self.calls), 1)

    def test_pool_needing_more_than_three_packages_makes_no_call(self):
        self.long_pool()
        (self.store.state_dir / 'jev.json').write_text(json.dumps({'mode': 'on', 'max_input_chars': 6000}), encoding='utf-8')
        local = self.store.retrieve('release planning', project='quartz')
        result = advisor.advise_context(self.store, 'release planning', project='quartz',
                                        transport=self.scoring('n15'))
        self.assertTrue(result['jev']['degraded'])
        self.assertIn('budget_exceeded', result['jev']['diagnostics'])
        self.assertFalse(self.calls)
        self.assertEqual(result['records'], local['records'])


class InstalledAdvisorTest(unittest.TestCase):
    def test_installed_cli_keeps_local_default_and_reviews_without_writing_source(self):
        from v3_package_helpers import install, isolated_env, run_python
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / 'vault'
            vault.mkdir()
            state = root / 'state'
            env = isolated_env(root / 'home')
            installed = install(vault, state, env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            import shutil
            shutil.copytree(ROOT / 'extensions', vault / 'extensions')
            source = vault / 'notes' / 'demo.md'
            source.parent.mkdir(exist_ok=True)
            source.write_text('---\n{"id":"demo-note","project":"demo","visibility":"internal"}\n---\nUse short demo notes.\n', encoding='utf-8')
            entry = vault / 'beyin.py'
            def run(*args):
                output = run_python(entry, args, vault, env)
                self.assertEqual(output.returncode, 0, output.stderr)
                return json.loads(output.stdout)
            local = run('context', 'short demo notes', '--project', 'demo', '--json')
            self.assertNotIn('jev', local)
            advised = run('context', 'short demo notes', '--project', 'demo', '--jev', '--json')
            self.assertEqual(advised['records'], local['records'])
            self.assertEqual(advised['jev']['mode'], 'off')
            rec = local['records'][0]
            proposal = root / 'proposal.json'
            proposal.write_text(json.dumps(dict(status='proposed', project='demo', claim='Use short demo notes.',
                evidence=[dict(record_id=rec['id'], source_sha256=rec['source_sha256'], quote='Use short demo notes.')])) , encoding='utf-8')
            before = source.read_bytes()
            review = run('jev-review', '--project', 'demo', '--file', str(proposal), '--json')
            self.assertFalse(review['memory_written'])
            self.assertFalse(review['approved'])
            self.assertEqual(source.read_bytes(), before)
            claims = root / 'claims.json'
            claims.write_text(json.dumps([dict(text='Use short demo notes.', citations=[dict(
                record_id=rec['id'], source_sha256=rec['source_sha256'], quote='Use short demo notes.')])]), encoding='utf-8')
            checked = run('jev-answer', '--project', 'demo', '--file', str(claims), '--json')
            self.assertEqual(checked['claims'][0]['verdict'], 'uncertain')
            self.assertTrue(checked['claims'][0]['mechanical_verified'])
            self.assertFalse(checked['memory_written'])
            self.assertEqual(source.read_bytes(), before)

    def test_installed_cli_without_extension_reports_clear_error(self):
        from v3_package_helpers import install, isolated_env, run_python
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / 'vault'
            vault.mkdir()
            state = root / 'state'
            env = isolated_env(root / 'home')
            installed = install(vault, state, env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            for f in (vault / '.claude/scripts').glob('*jev*'):
                f.unlink()
            entry = vault / 'beyin.py'
            output = run_python(entry, ['context', 'demo', '--project', 'demo', '--jev', '--json'], vault, env)
            self.assertEqual(output.returncode, 1)
            err = json.loads(output.stderr)
            self.assertEqual(err['error'], 'ModuleNotFoundError')
            self.assertIn('extensions/laya', err['message'])


if __name__ == '__main__':
    unittest.main()
