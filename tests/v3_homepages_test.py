"""Acceptance through ordinary CLI sync/note-edit; only synthetic vaults."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_sync import SyncEngine, parse, render
from beyin_v3_homepages import CELL
from v3_package_helpers import inherited_env, isolated_env, install


class HomepageTest(unittest.TestCase):
    installed = False
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.state = Path(self.tmp.name) / 'state'
        self.engine = SyncEngine(self.vault, self.state)
        (self.vault / 'notes').mkdir()
        for name in ('aksam', 'gunduz'):
            (self.vault / ('notes/' + name + '.md')).write_text(render(dict(id=name, kind='note',
                project='kariyer', facts=dict(gorusme='12 Kasım', adres='Eski adres')),
                'Özgün davet: [PDF](davet.pdf)\n'), encoding='utf-8')
        self.home = self.vault / 'notes/Ana Sayfa.md'
        self.env = isolated_env(Path(self.tmp.name)/'home')
        if self.installed:
            installed = install(self.vault, self.state, self.env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
        self.cli('sync')
        request = Path(self.tmp.name)/'homepage-create.json'
        selections = [dict(source='notes/'+name+'.md', fields=[dict(
            field='facts.gorusme', label='Görüşme tarihi', editable=True)])
            for name in ('aksam', 'gunduz')]
        selections.append(dict(source='notes/aksam.md', fields=[dict(
            field='facts.adres', label='Adres', editable=True)]))
        creation = dict(source='notes/Ana Sayfa.md', expected_sha256=None,
            title='Başvurularım', project='kariyer', selections=selections)
        request.write_text(json.dumps(creation), encoding='utf-8')
        self.cli('homepage-create', '--file', str(request))
        self.home.write_text(self.home.read_text(encoding='utf-8') +
            '\nKişisel not: yol süresini ve adresi sor\n\nSerbest metnim.\n', encoding='utf-8')

    def cli(self, *args):
        run = subprocess.run([sys.executable, str(self.vault/'beyin.py' if self.installed else ROOT/'scripts/beyin_v3.py'), '--vault', str(self.vault),
            '--state', str(self.state), '--json', *args], capture_output=True, text=True,
            encoding='utf-8', env=self.env, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return json.loads(run.stdout)

    def source_edit(self, field, value, *, name='aksam', cli=False):
        path = self.vault/('notes/'+name+'.md')
        raw_bytes = path.read_bytes()
        raw = raw_bytes.decode('utf-8')
        metadata, body = parse(raw)
        metadata['facts'][field] = value
        new = render(metadata, body)
        if cli:
            request = Path(self.tmp.name)/'edit.json'
            request.write_text(json.dumps(dict(source=path.relative_to(self.vault).as_posix(),
                expected_sha256=hashlib.sha256(raw_bytes).hexdigest(), edits=[dict(old=raw, new=new)])), encoding='utf-8')
            return self.cli('note-edit', '--file', str(request))
        path.write_text(new, encoding='utf-8')

    def home_edit(self, old, new):
        raw = self.home.read_bytes()
        self.home.write_bytes(raw.replace(old.encode(), new.encode(), 1))

    def values(self, name='aksam'):
        return parse((self.vault/('notes/'+name+'.md')).read_text(encoding='utf-8'))[0]['facts']

    def assert_replay(self):
        before = {p:p.read_bytes() for p in self.vault.rglob('*.md')}
        stats = {p:p.stat().st_mtime_ns for p in before}
        self.cli('sync')
        self.assertEqual(before, {p:p.read_bytes() for p in before})
        self.assertEqual(stats, {p:p.stat().st_mtime_ns for p in before})

    def test_canonical_cli_edit_updates_only_correct_row(self):
        other = (self.vault/'notes/gunduz.md').read_bytes()
        self.source_edit('gorusme', '13 Kasım', cli=True)
        text = self.home.read_text(encoding='utf-8')
        self.assertIn('-->13 Kasım<!--', text)
        self.assertIn('yol süresini ve adresi sor', text)
        self.assertEqual(other, (self.vault/'notes/gunduz.md').read_bytes())
        self.assertIn('Serbest metnim.', text)
        self.assert_replay()

    def test_home_edit_and_independent_source_edit_merge(self):
        self.source_edit('adres', 'Yeni adres')
        self.home_edit('-->12 Kasım<!--', '-->15 Kasım<!--')
        self.cli('sync')
        self.assertEqual(self.values(), dict(gorusme='15 Kasım', adres='Yeni adres'))
        self.assertEqual(self.values('gunduz')['gorusme'], '12 Kasım')
        self.assertIn('yol süresini ve adresi sor', self.home.read_text(encoding='utf-8'))
        self.assert_replay()

    def test_interrupted_canonical_write_keeps_latest_note(self):
        self.source_edit('gorusme', '13 Kasım')
        self.home_edit('yol süresini ve adresi sor', 'adresi ve giriş kapısını sor')
        source = (self.vault/'notes/aksam.md').read_bytes()
        self.cli('sync')
        text = self.home.read_text(encoding='utf-8')
        self.assertIn('-->13 Kasım<!--', text)
        self.assertIn('adresi ve giriş kapısını sor', text)
        self.assertEqual(source, (self.vault/'notes/aksam.md').read_bytes())
        self.assert_replay()

    def test_divergence_is_field_local_and_resolved_by_choice(self):
        self.source_edit('gorusme', '13 Kasım')
        self.source_edit('gorusme', '14 Kasım', name='gunduz')
        self.home_edit('-->12 Kasım<!--', '-->15 Kasım<!--')
        result = self.cli('sync')
        conflicts = result['workspace_homepages']['conflicts']
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['field'], 'facts.gorusme')
        self.assertEqual(conflicts[0]['source_value'], '13 Kasım')
        self.assertEqual(conflicts[0]['homepage_value'], '15 Kasım')
        self.assertIn('-->14 Kasım<!--', self.home.read_text(encoding='utf-8'))
        self.assertEqual(self.values()['gorusme'], '13 Kasım')
        # User chooses source value for this field only.
        self.home_edit('-->15 Kasım<!--', '-->13 Kasım<!--')
        self.cli('sync')
        self.assert_replay()

    def test_conflict_does_not_block_another_fact_on_same_record(self):
        self.source_edit('gorusme', '13 Kasım')
        self.home_edit('-->12 Kasım<!--', '-->15 Kasım<!--')
        self.home_edit('-->Eski adres<!--', '-->Yeni adres<!--')
        result = self.cli('sync')
        self.assertEqual(len(result['workspace_homepages']['conflicts']), 1)
        self.assertEqual(self.values(), dict(gorusme='13 Kasım', adres='Yeni adres'))

    def test_same_concurrent_value_does_not_ask(self):
        self.source_edit('gorusme', '15 Kasım')
        self.home_edit('-->12 Kasım<!--', '-->15 Kasım<!--')
        result = self.cli('sync')
        self.assertFalse(result.get('workspace_homepages', {}).get('conflicts'))
        self.assert_replay()

    def test_crash_after_source_write_then_latest_note_and_resume(self):
        self.home_edit('-->12 Kasım<!--', '-->15 Kasım<!--')
        import beyin_v3_homepages as module
        original = module._edit
        def crash(engine, snapshot, old, new):
            if snapshot['source'] == 'notes/Ana Sayfa.md':
                raise RuntimeError('simulated process interruption after source write')
            return original(engine, snapshot, old, new)
        with patch.object(module, '_edit', crash):
            with self.assertRaises(RuntimeError):
                self.engine.sync()
        self.assertEqual(self.values()['gorusme'], '15 Kasım')
        self.home_edit('yol süresini ve adresi sor', 'adresi ve giriş kapısını sor')
        source = (self.vault/'notes/aksam.md').read_bytes()
        self.cli('sync')
        self.assertEqual(source, (self.vault/'notes/aksam.md').read_bytes())
        self.assertIn('adresi ve giriş kapısını sor', self.home.read_text(encoding='utf-8'))
        self.assert_replay()

    def test_identity_scope_and_readonly_are_preserved(self):
        path = self.vault/'notes/aksam.md'
        raw = path.read_text(encoding='utf-8')
        path.write_text(raw.replace('"aksam"', '"wrong-id"'), encoding='utf-8')
        self.home_edit('-->12 Kasım<!--', '-->15 Kasım<!--')
        self.cli('sync')
        self.assertEqual(self.values()['gorusme'], '12 Kasım')

    def test_readonly_and_arbitrary_numeric_recipe_fact(self):
        text = self.home.read_text(encoding='utf-8')
        first = CELL.search(text)[0]
        binding = json.loads(CELL.search(text)[1])
        binding['editable'] = False
        from beyin_v3_homepages import _cell
        self.home.write_text(text.replace(first, _cell(binding, '15 Kasım')), encoding='utf-8')
        result = self.cli('sync')
        self.assertEqual(result['workspace_homepages']['conflicts'][0]['reason'], 'read_only_field')
        self.assertEqual(self.values()['gorusme'], '12 Kasım')
        # A second purpose uses an arbitrary numeric fact, with no domain/kind catalog.
        recipe = self.vault/'notes/tarif.md'
        recipe.write_text(render(dict(id='tarif', kind='note', project='tarifler',
            facts={'porsiyon': 4}), 'Aile tarifi.\n'), encoding='utf-8')
        recipe_home = self.vault/'notes/Tarifler.md'
        request = Path(self.tmp.name)/'recipe-homepage-create.json'
        request.write_text(json.dumps(dict(source='notes/Tarifler.md', expected_sha256=None,
            title='Tarifler', project='tarifler', selections=[dict(source='notes/tarif.md',
                fields=[dict(field='facts.porsiyon', label='Porsiyon', editable=True)])])), encoding='utf-8')
        self.cli('sync')
        self.cli('homepage-create', '--file', str(request))
        recipe_home.write_text(recipe_home.read_text(encoding='utf-8').replace('-->4<!--', '-->6<!--'), encoding='utf-8')
        self.cli('sync')
        self.assertEqual(parse(recipe.read_text(encoding='utf-8'))[0]['facts']['porsiyon'], 6)
        self.assert_replay()

    def test_private_scope_cannot_flow_to_internal_homepage(self):
        path = self.vault/'notes/aksam.md'
        metadata, body = parse(path.read_text(encoding='utf-8'))
        metadata['visibility'] = 'private'
        metadata['facts']['gorusme'] = 'Gizli tarih'
        path.write_text(render(metadata, body), encoding='utf-8')
        self.cli('sync')
        self.assertNotIn('Gizli tarih', self.home.read_text(encoding='utf-8'))

    def test_homepage_note_edit_cli_returns_current_hash(self):
        raw = self.home.read_bytes()
        request = Path(self.tmp.name)/'home-edit.json'
        request.write_text(json.dumps(dict(source='notes/Ana Sayfa.md', expected_sha256=hashlib.sha256(raw).hexdigest(),
            edits=[dict(old='-->12 Kasım<!--', new='-->15 Kasım<!--')])), encoding='utf-8')
        # Anchor occurs twice: target the complete first cell to keep normal CAS unambiguous.
        data = json.loads(request.read_text())
        first = CELL.search(raw.decode())[0]
        data['edits'] = [dict(old=first, new=first.replace('-->12 Kasım<!--', '-->15 Kasım<!--'))]
        request.write_text(json.dumps(data), encoding='utf-8')
        result = self.cli('note-edit', '--file', str(request))
        self.assertEqual(result['sha256'], hashlib.sha256(self.home.read_bytes()).hexdigest())
        self.assertEqual(self.values()['gorusme'], '15 Kasım')
        self.assert_replay()


class InstalledHomepageTest(HomepageTest):
    installed = True


if __name__ == '__main__':
    unittest.main()
