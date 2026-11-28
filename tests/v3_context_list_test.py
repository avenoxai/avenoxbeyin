"""Contract and regression tests for `context --list` compact candidate listing mode (#974)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'tests') not in sys.path:
    sys.path.insert(0, str(ROOT / 'tests'))
if str(ROOT / 'template/.claude/scripts') not in sys.path:
    sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))

from v3_package_helpers import inherited_env, install, isolated_env, run_python, snapshot  # noqa: E402


def _orchard_note(index, *, heading=True, frontmatter=True, extra_meta=None):
    """Build a realistic multi-paragraph >600-char note with the match deep in the body."""
    intro = (
        f"Opening seasonal ledger for southern terrace parcel {index:02d}. "
        "Rainfall gauges, fence repairs, gravel drainage trenches, compost bins, "
        "and tool shed inventory checklists fill the first section before field work starts. "
        "Water line valves stay shut through January while soil moisture settles along the slope."
    )
    middle = (
        f"Late winter orchard pruning tree schedule for parcel {index:02d}: "
        "make clean angled cuts just above outward-facing buds so sunlight reaches the inner canopy."
    )
    outro = (
        "Autumn harvest crates, cold-storage labels, ladder inspections, and mulch delivery dates "
        "are reviewed every October after leaf drop. Keep hand shears oiled and record any frost damage "
        "in the annual maintenance log for future seasons."
    )
    meta = {'title': f'Orchard Note {index:02d}', 'tags': ['garden', 'orchard']}
    if extra_meta:
        meta.update(extra_meta)
    parts = []
    if frontmatter:
        parts.append('---\n' + json.dumps(meta) + '\n---\n')
    if heading:
        parts.append(f'# Orchard Note {index:02d}\n\n{intro}\n\n## Winter Care\n\n{middle}\n\n## Autumn Log\n\n{outro}\n')
    else:
        parts.append(f'{intro}\n\n{middle}\n\n{outro}\n')
    body = ''.join(parts)
    assert len(body) > 600, len(body)
    return body


class ContextListModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-context-list-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        (self.vault / 'notes').mkdir(parents=True)
        self.state = self.root / 'state'
        self.cli = ROOT / 'scripts/beyin_v3.py'
        self.env = inherited_env(**isolated_env(self.root / 'home'), BEYIN_UPDATES_OFF='1')

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(self.cli), '--vault', str(self.vault), '--state', str(self.state), *args],
            capture_output=True, text=True, encoding='utf-8', env=self.env, timeout=60,
        )

    def seed_orchard_notes(self, count):
        for i in range(1, count + 1):
            (self.vault / f'notes/orchard-{i:02d}.md').write_text(_orchard_note(i), encoding='utf-8')

    def test_issue_974_thirty_matching_notes_installed_entrypoint_compact_list(self):
        """Exact #974 acceptance: 30 matching notes via beyin.py context --list --limit 30 stays < 10,000 chars."""
        self.seed_orchard_notes(30)
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)

        # Baseline without --list: default 8,000-char budget truncates 30 matches down to far fewer.
        full_proc = run_python(self.vault / 'beyin.py', ['context', 'orchard pruning tree', '--limit', '30'], self.vault, self.env)
        self.assertEqual(full_proc.returncode, 0, full_proc.stderr)
        full_data = json.loads(full_proc.stdout.decode('utf-8'))
        self.assertLess(len(full_data['records']), 30)
        self.assertGreater(full_data['omitted_count'], 0)

        # Compact --list mode: all 30 candidates returned, omitted_count == 0, stdout < 10,000 chars.
        list_proc = run_python(
            self.vault / 'beyin.py',
            ['context', 'orchard pruning tree', '--list', '--limit', '30'],
            self.vault, self.env,
        )
        self.assertEqual(list_proc.returncode, 0, list_proc.stderr)
        stdout = list_proc.stdout.decode('utf-8')
        self.assertLess(len(stdout), 10000, f'Expected compact JSON stdout < 10,000 chars, got {len(stdout)}')

        data = json.loads(stdout)
        self.assertEqual(len(data['records']), 30)
        self.assertEqual(data['omitted_count'], 0)
        self.assertFalse(data['abstained'])
        self.assertTrue(data['truncated'])

        scores = []
        for record in data['records']:
            for key in ('id', 'source', 'title', 'score', 'text', 'text_truncated'):
                self.assertIn(key, record)
            self.assertTrue(record['text_truncated'])
            self.assertLessEqual(len(record['text']), 206)
            self.assertLess(len(record['text']), 220)
            self.assertIsInstance(record['score'], (int, float))
            scores.append(record['score'])
            lowered = record['text'].lower()
            self.assertTrue(
                any(term in lowered for term in ('orchard', 'pruning', 'tree')),
                f'Snippet should center on matched terms, got: {record["text"]!r}',
            )
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_thirty_five_matches_with_limit_thirty_reports_omitted_count_five(self):
        self.seed_orchard_notes(35)
        proc = self.run_cli('context', 'orchard pruning tree', '--list', '--limit', '30')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(len(proc.stdout), 10000)
        data = json.loads(proc.stdout)
        self.assertEqual(len(data['records']), 30)
        self.assertEqual(data['omitted_count'], 5)
        self.assertTrue(data['truncated'])

    def test_list_default_limit_and_independence_from_budget_chars(self):
        self.seed_orchard_notes(30)
        # Without explicit --limit, --list uses a wider browsing default (20).
        default_proc = self.run_cli('context', 'orchard pruning tree', '--list')
        self.assertEqual(default_proc.returncode, 0, default_proc.stderr)
        default_data = json.loads(default_proc.stdout)
        self.assertEqual(len(default_data['records']), 20)
        self.assertEqual(default_data['omitted_count'], 10)

        # Even with tiny --budget-chars, --list still honors --limit independently of full-text budget.
        small_budget = self.run_cli('context', 'orchard pruning tree', '--list', '--limit', '30', '--budget-chars', '500')
        self.assertEqual(small_budget.returncode, 0, small_budget.stderr)
        small_data = json.loads(small_budget.stdout)
        self.assertEqual(len(small_data['records']), 30)
        self.assertEqual(small_data['omitted_count'], 0)

    def test_snippet_centers_deep_matches_for_headed_headingless_single_paragraph_and_turkish(self):
        # 1) Headingless multi-paragraph note with match deep inside.
        (self.vault / 'notes/headingless.md').write_text(
            _orchard_note(1, heading=False, frontmatter=False), encoding='utf-8'
        )
        # 2) Single long continuous paragraph (>1000 chars) with match near the end.
        long_single = (
            ('Unrelated inventory filler about ceramic tiles and copper pipes for warehouse shelving. ' * 12)
            + 'Special orchard pruning tree cutting protocol for steep terraces. '
            + ('Trailing storage remarks about wooden crates and shipping pallets. ' * 4)
        )
        (self.vault / 'notes/single-para.md').write_text(
            '---\n{"title": "Single Paragraph"}\n---\n' + long_single + '\n', encoding='utf-8'
        )
        # 3) Turkish note with inflected match deep in body.
        tr_body = (
            ('Kuzey yamacindaki sulama kanali ve depo sayimi ile ilgili genel hazirlik notlari burada yer aliyor. ' * 8)
            + 'Kis sonunda meyve agaclarinin budanmasi ve budama makasinin temizligi kritik adimdir. '
            + ('Sonbahar hasat plani ve kasa etiketleme kontrol listesi sonraki bolumde surer. ' * 4)
        )
        (self.vault / 'notes/turkish-note.md').write_text(
            '---\n{"title": "Bahce Rehberi"}\n---\n' + tr_body + '\n', encoding='utf-8'
        )

        en_proc = self.run_cli('context', 'orchard pruning tree', '--list', '--limit', '10')
        self.assertEqual(en_proc.returncode, 0, en_proc.stderr)
        en_by_source = {r['source']: r for r in json.loads(en_proc.stdout)['records']}
        for src in ('notes/headingless.md', 'notes/single-para.md'):
            self.assertIn(src, en_by_source)
            rec = en_by_source[src]
            self.assertTrue(rec['text_truncated'])
            self.assertLessEqual(len(rec['text']), 206)
            self.assertIn('orchard pruning tree', rec['text'].lower())
            self.assertTrue(rec['text'].startswith('...'), rec['text'])
        # Headingless note without frontmatter title still gets a clean fallback title.
        self.assertEqual(en_by_source['notes/headingless.md']['title'], 'headingless')

        tr_proc = self.run_cli('context', 'agac budama', '--list', '--limit', '10')
        self.assertEqual(tr_proc.returncode, 0, tr_proc.stderr)
        tr_by_source = {r['source']: r for r in json.loads(tr_proc.stdout)['records']}
        self.assertIn('notes/turkish-note.md', tr_by_source)
        tr_rec = tr_by_source['notes/turkish-note.md']
        self.assertTrue(tr_rec['text_truncated'])
        self.assertIn('budama', tr_rec['text'].lower())
        self.assertTrue(tr_rec['text'].startswith('...'), tr_rec['text'])

    def test_short_note_is_not_marked_truncated_and_title_fallback_uses_h1(self):
        (self.vault / 'notes/short-h1.md').write_text(
            '# Compact Orchard Tip\n\nOrchard pruning tree tip in one brief line.\n',
            encoding='utf-8',
        )
        (self.vault / 'notes/title-only-match.md').write_text(
            '---\n{"title": "Orchard Pruning Tree Index"}\n---\n'
            + ('General seasonal overview without repeating those keywords in body text. ' * 12) + '\n',
            encoding='utf-8',
        )
        proc = self.run_cli('context', 'orchard pruning tree', '--list', '--limit', '10')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        by_src = {r['source']: r for r in json.loads(proc.stdout)['records']}

        short = by_src['notes/short-h1.md']
        self.assertFalse(short['text_truncated'])
        self.assertEqual(short['title'], 'Compact Orchard Tip')
        self.assertIn('Orchard pruning tree tip', short['text'])

        title_only = by_src['notes/title-only-match.md']
        self.assertTrue(title_only['text_truncated'])
        self.assertEqual(title_only['title'], 'Orchard Pruning Tree Index')
        self.assertLessEqual(len(title_only['text']), 206)
        self.assertTrue(title_only['text'].endswith('...'))

    def test_privacy_visibility_untrusted_rejected_superseded_project_and_stale_gates_enforced(self):
        (self.vault / 'notes/public-ok.md').write_text(
            _orchard_note(1, extra_meta={'id': 'pub-1', 'visibility': 'public', 'project': 'alpha'}), encoding='utf-8'
        )
        (self.vault / 'notes/internal-ok.md').write_text(
            _orchard_note(2, extra_meta={'id': 'int-1', 'visibility': 'internal', 'project': 'beta'}), encoding='utf-8'
        )
        (self.vault / 'notes/private-hidden.md').write_text(
            _orchard_note(3, extra_meta={'id': 'priv-1', 'visibility': 'private'}), encoding='utf-8'
        )
        (self.vault / 'notes/Özel').mkdir(parents=True)
        (self.vault / 'notes/Özel/secret-folder.md').write_text(
            _orchard_note(4, extra_meta={'id': 'folder-priv'}), encoding='utf-8'
        )
        (self.vault / 'notes/untrusted.md').write_text(
            _orchard_note(5, extra_meta={'id': 'untrust-1', 'trust': 'untrusted'}), encoding='utf-8'
        )
        (self.vault / 'notes/rejected.md').write_text(
            _orchard_note(6, extra_meta={
                'id': 'rej-1', 'kind': 'inference', 'validity': 'rejected',
                'rejected_reason': 'Corrected by user.', 'rejected_at': '2026-10-01',
            }), encoding='utf-8'
        )
        (self.vault / 'notes/old-guide.md').write_text(
            _orchard_note(7, extra_meta={'id': 'old-1'}), encoding='utf-8'
        )
        (self.vault / 'notes/new-guide.md').write_text(
            _orchard_note(8, extra_meta={'id': 'new-1', 'supersedes': ['old-1']}), encoding='utf-8'
        )

        # Default internal audience: excludes private, sensitive folder, untrusted, rejected, superseded.
        internal = self.run_cli('context', 'orchard pruning tree', '--list', '--limit', '20')
        self.assertEqual(internal.returncode, 0, internal.stderr)
        ids = {r['id'] for r in json.loads(internal.stdout)['records']}
        self.assertEqual(ids, {'pub-1', 'int-1', 'new-1'})

        # Public audience sees only public.
        pub = self.run_cli('context', 'orchard pruning tree', '--list', '--audience', 'public', '--limit', '20')
        self.assertEqual(pub.returncode, 0, pub.stderr)
        self.assertEqual({r['id'] for r in json.loads(pub.stdout)['records']}, {'pub-1'})

        # Project scoping sees only that project.
        scoped = self.run_cli('context', 'orchard pruning tree', '--list', '--project', 'alpha', '--limit', '20')
        self.assertEqual(scoped.returncode, 0, scoped.stderr)
        self.assertEqual({r['id'] for r in json.loads(scoped.stdout)['records']}, {'pub-1'})

        # Stale source with --no-sync is excluded and counted in stale_count without modifying state.
        (self.vault / 'notes/internal-ok.md').write_text('Changed externally after sync.\n', encoding='utf-8')
        before = snapshot(self.state)
        stale_proc = self.run_cli('context', 'orchard pruning tree', '--list', '--no-sync', '--limit', '20')
        self.assertEqual(stale_proc.returncode, 0, stale_proc.stderr)
        stale_data = json.loads(stale_proc.stdout)
        self.assertEqual({r['id'] for r in stale_data['records']}, {'pub-1', 'new-1'})
        self.assertEqual(stale_data['stale_count'], 1)
        self.assertEqual(snapshot(self.state), before)

    def test_list_with_jev_raises_clear_value_error_without_creating_state(self):
        self.seed_orchard_notes(2)
        before_vault = snapshot(self.vault)
        proc = self.run_cli('context', 'orchard pruning tree', '--list', '--jev')
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, '')
        err = json.loads(proc.stderr)
        self.assertEqual(err['error'], 'ValueError')
        self.assertEqual(err['message'], '--list is only available for local context')
        self.assertFalse(self.state.exists())
        self.assertEqual(snapshot(self.vault), before_vault)

    def test_limit_zero_and_negative_and_strip_fillers_and_empty_matches(self):
        self.seed_orchard_notes(5)
        zero = self.run_cli('context', 'orchard pruning tree', '--list', '--limit', '0')
        self.assertEqual(zero.returncode, 0, zero.stderr)
        zero_data = json.loads(zero.stdout)
        self.assertEqual(zero_data['records'], [])
        self.assertTrue(zero_data['abstained'])
        self.assertEqual(zero_data['omitted_count'], 5)

        neg = self.run_cli('context', 'orchard pruning tree', '--list', '--limit', '-1')
        self.assertNotEqual(neg.returncode, 0)
        self.assertEqual(json.loads(neg.stderr)['error'], 'ValueError')

        miss = self.run_cli('context', 'nonexistent_zebra_token_974', '--list', '--limit', '20')
        self.assertEqual(miss.returncode, 0, miss.stderr)
        miss_data = json.loads(miss.stdout)
        self.assertEqual(miss_data['records'], [])
        self.assertTrue(miss_data['abstained'])
        self.assertEqual(miss_data['omitted_count'], 0)
        self.assertFalse(miss_data['truncated'])

        fillers = self.run_cli('context', 'orchard pruning hakkinda ne biliyoruz?', '--list', '--strip-fillers', '--limit', '5')
        self.assertEqual(fillers.returncode, 0, fillers.stderr)
        self.assertEqual(len(json.loads(fillers.stdout)['records']), 5)

    def test_default_context_without_list_is_unchanged(self):
        self.seed_orchard_notes(6)
        proc = self.run_cli('context', 'orchard pruning tree')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Default CLI output remains pretty-printed JSON with standard full-record metadata and citations.
        self.assertIn('\n  "records": [', proc.stdout)
        data = json.loads(proc.stdout)
        self.assertNotIn('list_mode', data)
        self.assertIn('citations', data)
        self.assertEqual(len(data['records']), 5)
        self.assertEqual(len(data['citations']), 5)
        self.assertEqual(data['omitted_count'], 1)
        for key in ('id', 'source', 'source_sha256', 'revision', 'facts', 'visibility', 'kind', 'text'):
            self.assertIn(key, data['records'][0])

    def test_python_api_list_mode_on_retrieve_pack_context_and_pack_widened(self):
        import beyin_v3 as runtime
        store = runtime.MemoryStore(self.state, self.vault)
        self.addCleanup(store.close)
        for i in range(1, 7):
            rel = f'notes/orchard-{i:02d}.md'
            body = _orchard_note(i, frontmatter=False)
            (self.vault / rel).write_text(body, encoding='utf-8')
            store.ingest({
                'id': f'orch-{i:02d}', 'source': rel, 'kind': 'note',
                'title': f'Orchard Note {i:02d}', 'visibility': 'internal',
                'revision': 1, 'text': body, 'facts': {'large_blob': 'z' * 500},
                'aliases': ['alias-' + str(i)],
            })
        res = store.retrieve('orchard pruning tree', limit=6, budget_chars=500, list_mode=True)
        self.assertEqual(len(res['records']), 6)
        self.assertEqual(res['omitted_count'], 0)
        self.assertTrue(res['list_mode'])
        for rec in res['records']:
            self.assertNotIn('facts', rec)
            self.assertNotIn('aliases', rec)
            self.assertLessEqual(len(rec['text']), 206)
            self.assertTrue(rec['text_truncated'])
            self.assertIn('score', rec)

        raw_records = [
            {'id': f'r{i}', 'source': f'notes/r{i}.md', 'title': f'R{i}', 'text': 'orchard pruning tree ' * 40,
             'facts': {'big': 'y' * 400}}
            for i in range(4)
        ]
        packed = runtime.pack_context(raw_records, limit=3, budget_chars=100, query='orchard pruning', list_mode=True)
        self.assertEqual(len(packed['records']), 3)
        self.assertEqual(packed['omitted_count'], 1)
        widened = runtime.pack_widened(raw_records, {}, limit=3, budget_chars=100, query='orchard pruning', list_mode=True)
        self.assertEqual(len(widened['records']), 3)
        self.assertEqual(widened['omitted_count'], 1)
        with self.assertRaises(ValueError):
            store.retrieve('orchard', list_mode='yes')
        with self.assertRaises(ValueError):
            runtime.pack_context(raw_records, list_mode='yes')
        with self.assertRaises(ValueError):
            runtime.pack_widened(raw_records, {}, list_mode='yes')

    def test_snippet_edge_cases_unbroken_token_whitespace_and_task_metadata(self):
        import beyin_v3 as runtime
        # Giant unbroken token without whitespace stays bounded to max_chars.
        unbroken, trunc = runtime._list_snippet('a' * 500, query='missing', max_chars=150)
        self.assertTrue(trunc)
        self.assertLessEqual(len(unbroken), 150)
        self.assertTrue(unbroken.endswith('...'))

        # Whitespace-only long string never crashes and stays bounded.
        blank, trunc_blank = runtime._list_snippet('   \n\t  ' * 50, query='orchard', max_chars=150)
        self.assertTrue(trunc_blank)
        self.assertLessEqual(len(blank), 150)

        # Task record keeps compact non-default kind/status/project while stripping heavy bookkeeping.
        task_rec = {
            'id': 'task-1', 'source': 'tasks/prune.md', 'title': 'Prune Orchard',
            'kind': 'task', 'status': 'active', 'project': 'garden', 'visibility': 'internal',
            'revision': 3, 'source_sha256': 'a' * 64, 'facts': {'huge': 'x' * 600},
            'aliases': ['old-prune'], 'score': 4.25,
            'text': 'Prune orchard trees before spring bloom. ' * 15,
        }
        packed = runtime.pack_list_context([task_rec], limit=5, query='prune orchard')
        item = packed['records'][0]
        self.assertEqual(item['kind'], 'task')
        self.assertEqual(item['status'], 'active')
        self.assertEqual(item['project'], 'garden')
        self.assertEqual(item['score'], 4.25)
        for stripped in ('facts', 'aliases', 'source_sha256', 'revision', 'visibility'):
            self.assertNotIn(stripped, item)

    def test_file_input_and_human_mode_with_list(self):
        self.seed_orchard_notes(6)
        req = self.root / 'req.json'
        req.write_text(json.dumps({'query': 'orchard pruning tree'}), encoding='utf-8')
        proc = self.run_cli('context', '--file', str(req), '--list', '--limit', '4')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(len(data['records']), 4)
        self.assertEqual(data['omitted_count'], 2)

        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        human = run_python(self.vault / 'beyin.py', ['context', 'orchard pruning tree', '--list', '--limit', '3', '--human'], self.vault, self.env)
        self.assertEqual(human.returncode, 0, human.stderr)
        text = human.stdout.decode('utf-8')
        self.assertIn('notes/orchard-', text)
        self.assertIn('orchard pruning tree', text.lower())

    def test_skill_doc_mentions_context_list_two_step_workflow(self):
        import re
        skill = (ROOT / 'template/.agents/skills/beyin/SKILL.md').read_text(encoding='utf-8')
        self.assertRegex(skill, r'context[^\n]*--list')
        self.assertIn('note-read', skill)


if __name__ == '__main__':
    unittest.main()
