"""Black-box acceptance for creating a homepage from ordinary indexed notes."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_homepages import create as create_homepage
from beyin_v3_sync import SyncEngine
from v3_package_helpers import install, isolated_env


class HomepageCreateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.state = root / 'state'
        self.env = isolated_env(root / 'home')
        (self.vault / 'notes').mkdir()
        self.note('aksam', 'Akşam başvurusu', '12 Kasım', 'Eski adres')
        self.note('gunduz', 'Gündüz başvurusu', '12 Kasım', 'Diğer adres')
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr.decode(errors='replace'))
        self.run_cli('sync', ok=True)

    def note(self, name, title, date, address, **metadata):
        path = self.vault / 'notes' / (name + '.md')
        front = {'id': name, 'kind': 'note', 'project': 'kariyer',
                 'facts': {'gorusme': date, 'adres': address}, **metadata}
        path.write_text('---\n' + json.dumps(front, ensure_ascii=False) +
                        '\n---\n# ' + title + '\nÖzgün davet: [PDF](davet.pdf)\n', encoding='utf-8')
        return path

    def run_cli(self, *args, ok=False):
        run = subprocess.run([sys.executable, str(self.vault / 'beyin.py'),
                              '--vault', str(self.vault), '--state', str(self.state),
                              '--json', *args], env=self.env, capture_output=True,
                             text=True, encoding='utf-8', timeout=40)
        if ok:
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return run

    def request(self, selections=None, **changes):
        request = {'source': 'notes/Ana Sayfa.md', 'expected_sha256': None,
                   'title': 'Başvurularım', 'project': 'kariyer',
                   'selections': selections if selections is not None else [
                       {'source': 'notes/aksam.md', 'fields': [
                           {'field': 'facts.gorusme', 'label': 'Görüşme tarihi', 'editable': True}]},
                       {'source': 'notes/gunduz.md', 'fields': [
                           {'field': 'facts.gorusme', 'label': 'Görüşme tarihi', 'editable': True}]}]}
        request.update(changes)
        return request

    def create(self, request, ok=True):
        path = Path(self.tmp.name) / 'request.json'
        path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        run = self.run_cli('homepage-create', '--file', str(path), ok=ok)
        return json.loads(run.stdout) if ok else run

    def rejected(self, request, reason=None):
        run = self.create(request, ok=False)
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("invalid choice: 'homepage-create'", run.stdout + run.stderr)
        if reason:
            self.assertIn(reason, run.stdout + run.stderr)
        return run

    def home(self):
        return self.vault / 'notes/Ana Sayfa.md'

    def linked_sources(self):
        text = self.home().read_text(encoding='utf-8')
        targets = re.findall(r'\]\(([^)\n]+)\)', text)
        return {(self.home().parent / unquote(target)).resolve() for target in targets}

    def snapshot(self):
        return {p.relative_to(self.vault).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.vault.rglob('*.md')}

    def field(self, name, field):
        raw = (self.vault / 'notes' / (name + '.md')).read_text(encoding='utf-8')
        return json.loads(raw.split('---\n', 2)[1])['facts'][field]

    def edit_source(self, name, old, new):
        path = self.vault / 'notes' / (name + '.md')
        raw_bytes = path.read_bytes()
        raw = raw_bytes.decode('utf-8')
        req = {'source': path.relative_to(self.vault).as_posix(),
               'expected_sha256': hashlib.sha256(raw_bytes).hexdigest(),
               'edits': [{'old': old, 'new': new}]}
        edit = Path(self.tmp.name) / 'edit.json'
        edit.write_text(json.dumps(req, ensure_ascii=False), encoding='utf-8')
        self.run_cli('note-edit', '--file', str(edit), ok=True)

    def external_source_edit(self, name, old, new):
        path = self.vault / 'notes' / (name + '.md')
        path.write_text(path.read_text(encoding='utf-8').replace(old, new, 1), encoding='utf-8')

    def test_new_page_two_similar_notes_forward_reverse_and_no_write_repeat(self):
        result = self.create(self.request())
        self.assertEqual(result['status'], 'written')
        text = self.home().read_text(encoding='utf-8')
        self.assertIn('Başvurularım', text)
        self.assertIn('aksam', text)
        self.assertIn('gunduz', text)
        self.assertEqual(text.count('-->12 Kasım<!--'), 2)
        self.edit_source('aksam', '12 Kasım', '13 Kasım')
        text = self.home().read_text(encoding='utf-8')
        self.assertIn('13 Kasım', text)
        self.assertIn('12 Kasım', text)
        self.assertEqual(self.field('gunduz', 'gorusme'), '12 Kasım')
        self.home().write_text(text.replace('-->13 Kasım<!--', '-->15 Kasım<!--', 1), encoding='utf-8')
        self.run_cli('sync', ok=True)
        self.assertEqual(self.field('aksam', 'gorusme'), '15 Kasım')
        self.assertEqual(self.field('gunduz', 'gorusme'), '12 Kasım')
        before = self.snapshot()
        self.run_cli('sync', ok=True)
        self.assertEqual(before, self.snapshot())

    def test_attach_existing_text_exact_sha_and_idempotence_with_personal_edit(self):
        self.home().write_text('# Başvurularım\n\nKişisel not: yol süresini sor.\n', encoding='utf-8')
        original = self.home().read_bytes()
        request = self.request(expected_sha256=hashlib.sha256(original).hexdigest())
        self.assertEqual(self.create(request)['status'], 'written')
        self.assertIn('Kişisel not: yol süresini sor.', self.home().read_text(encoding='utf-8'))
        self.home().write_text(self.home().read_text(encoding='utf-8').replace(
            'yol süresini sor', 'adresi ve giriş kapısını sor'), encoding='utf-8')
        self.edit_source('aksam', '12 Kasım', '13 Kasım')
        before = self.snapshot()
        self.assertEqual(self.create(request)['status'], 'unchanged')
        self.assertEqual(before, self.snapshot())
        changed = self.request(selections=[request['selections'][0]],
                               expected_sha256=request['expected_sha256'])
        self.rejected(changed)
        self.assertEqual(before, self.snapshot())

    def test_same_basename_paths_and_implicit_source_id(self):
        for folder, date in [('aksam', '12 Kasım'), ('gunduz', '14 Kasım')]:
            directory = self.vault / 'notes' / folder
            directory.mkdir()
            metadata = {'kind': 'note', 'project': 'kariyer', 'facts': {'gorusme': date}}
            (directory / 'ilan.md').write_text('---\n' + json.dumps(metadata) +
                '\n---\n# İlan\n', encoding='utf-8')
        self.run_cli('sync', ok=True)
        req = self.request(selections=[{'source': 'notes/' + folder + '/ilan.md',
            'fields': [{'field': 'facts.gorusme', 'label': 'Tarih', 'editable': True}]}
            for folder in ('aksam', 'gunduz')])
        self.create(req)
        self.assertEqual(self.linked_sources(), {
            (self.vault / 'notes/aksam/ilan.md').resolve(),
            (self.vault / 'notes/gunduz/ilan.md').resolve()})
        text = self.home().read_text(encoding='utf-8')
        self.home().write_text(text.replace('-->12 Kasım<!--', '-->15 Kasım<!--', 1), encoding='utf-8')
        self.run_cli('sync', ok=True)
        evening = (self.vault / 'notes/aksam/ilan.md').read_text(encoding='utf-8')
        daylight = (self.vault / 'notes/gunduz/ilan.md').read_text(encoding='utf-8')
        self.assertEqual(json.loads(evening.split('---\n', 2)[1])['facts']['gorusme'], '15 Kasım')
        self.assertEqual(json.loads(daylight.split('---\n', 2)[1])['facts']['gorusme'], '14 Kasım')

    def test_special_source_filename_link_opens_exact_note(self):
        directory = self.vault / 'notes/Kariyer Başvuru'
        directory.mkdir()
        source = directory / 'source#fragment].md'
        source.write_text('---\n' + json.dumps({'kind': 'note', 'project': 'kariyer',
            'facts': {'gorusme': '12 Kasım'}}, ensure_ascii=False) +
            '\n---\n# Özel adlı başvuru\n', encoding='utf-8')
        self.run_cli('sync', ok=True)
        request = self.request(selections=[{'source': source.relative_to(self.vault).as_posix(),
            'fields': [{'field': 'facts.gorusme', 'label': 'Görüşme', 'editable': True}]}])
        self.create(request)
        self.assertEqual(self.linked_sources(), {source.resolve()})
        text = self.home().read_text(encoding='utf-8')
        self.assertIn('%23', text)
        self.assertIn('%5D', text)
        self.home().write_text(text.replace('-->12 Kasım<!--', '-->15 Kasım<!--'), encoding='utf-8')
        self.run_cli('sync', ok=True)
        self.assertEqual(json.loads(source.read_text(encoding='utf-8').split('---\n', 2)[1])
                         ['facts']['gorusme'], '15 Kasım')
        self.assertEqual(self.field('aksam', 'gorusme'), '12 Kasım')

    def test_repeat_rejects_rebound_cell_or_changed_source_scope(self):
        request = self.request()
        self.create(request)
        raw = self.home().read_text(encoding='utf-8')
        self.home().write_text(raw.replace('notes/aksam.md', 'notes/gunduz.md', 1), encoding='utf-8')
        before = self.snapshot()
        self.rejected(request)
        self.assertEqual(before, self.snapshot())
        self.home().write_text(raw, encoding='utf-8')
        path = self.vault / 'notes/aksam.md'
        path.write_text(path.read_text(encoding='utf-8').replace('"aksam"', '"other-id"', 1), encoding='utf-8')
        before = self.snapshot()
        self.rejected(request)
        self.assertEqual(before, self.snapshot())
        path.write_text(path.read_text(encoding='utf-8').replace('"other-id"', '"aksam"', 1)
                        .replace('"project": "kariyer"', '"project": "tarifler"', 1), encoding='utf-8')
        before = self.snapshot()
        self.rejected(request)
        self.assertEqual(before, self.snapshot())

    def test_repeat_rejects_private_or_untrusted_page(self):
        request = self.request()
        self.create(request)
        for needle, replacement in [('"visibility": "internal"', '"visibility": "private"'),
                                    ('"workspace_homepage": true', '"workspace_homepage": false'),
                                    ('"workspace_homepage": true', '"workspace_homepage": true, "trust": "untrusted"')]:
            raw = self.home().read_text(encoding='utf-8')
            self.assertIn(needle, raw)
            self.home().write_text(raw.replace(needle, replacement, 1), encoding='utf-8')
            before = self.snapshot()
            self.rejected(request)
            self.assertEqual(before, self.snapshot())
            self.home().write_text(raw, encoding='utf-8')

    def test_wrong_existing_sha_and_invalid_late_selection_write_nothing(self):
        self.home().write_text('# İnsan yazısı\n', encoding='utf-8')
        before = self.snapshot()
        self.rejected(self.request(expected_sha256='0' * 64), 'changed')
        self.assertEqual(before, self.snapshot())
        request = self.request(expected_sha256=hashlib.sha256(self.home().read_bytes()).hexdigest())
        request['selections'].append({'source': 'notes/missing.md', 'fields': [
            {'field': 'facts.gorusme', 'label': 'Tarih', 'editable': True}]})
        self.rejected(request)
        self.assertEqual(before, self.snapshot())

    def test_malformed_late_selection_is_rejected_before_sync_preflight(self):
        request = self.request()
        request['selections'][1]['fields'][0]['editable'] = 'yes'
        engine = SyncEngine(self.vault, self.state)
        before = self.snapshot()
        with patch.object(engine, 'sync', side_effect=AssertionError('sync ran before validation')) as sync:
            with self.assertRaises(ValueError):
                create_homepage(engine, request)
        sync.assert_not_called()
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.home().exists())

    def test_independent_fact_merge_same_value_and_field_local_conflict(self):
        request = self.request(selections=[{'source': 'notes/aksam.md', 'fields': [
            {'field': 'facts.gorusme', 'label': 'Tarih', 'editable': True},
            {'field': 'facts.adres', 'label': 'Adres', 'editable': True}]},
            self.request()['selections'][1]])
        self.create(request)
        home = self.home()
        self.edit_source('aksam', 'Eski adres', 'Yeni adres')
        home.write_text(home.read_text(encoding='utf-8').replace('-->12 Kasım<!--', '-->15 Kasım<!--', 1), encoding='utf-8')
        self.run_cli('sync', ok=True)
        self.assertEqual((self.field('aksam', 'gorusme'), self.field('aksam', 'adres')),
                         ('15 Kasım', 'Yeni adres'))
        # Equal edits on both sides settle without a question.
        self.external_source_edit('aksam', '15 Kasım', '16 Kasım')
        home.write_text(home.read_text(encoding='utf-8').replace('-->15 Kasım<!--', '-->16 Kasım<!--', 1), encoding='utf-8')
        result = json.loads(self.run_cli('sync', ok=True).stdout)
        self.assertFalse(result.get('workspace_homepages', {}).get('conflicts'))
        # Different values conflict in one field while the other row advances.
        self.external_source_edit('aksam', '16 Kasım', '17 Kasım')
        self.external_source_edit('gunduz', '12 Kasım', '14 Kasım')
        home.write_text(home.read_text(encoding='utf-8').replace('-->16 Kasım<!--', '-->18 Kasım<!--', 1), encoding='utf-8')
        result = json.loads(self.run_cli('sync', ok=True).stdout)
        conflicts = result['workspace_homepages']['conflicts']
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['field'], 'facts.gorusme')
        self.assertEqual(self.field('aksam', 'gorusme'), '17 Kasım')
        self.assertIn('14 Kasım', home.read_text(encoding='utf-8'))

    def test_recipe_number_without_domain_catalog(self):
        recipe = self.vault / 'notes/tarif.md'
        recipe.write_text('---\n' + json.dumps({'id': 'tarif', 'kind': 'note',
            'project': 'tarifler', 'facts': {'porsiyon': 4}}, ensure_ascii=False) +
            '\n---\n# Aile tarifi\n', encoding='utf-8')
        self.run_cli('sync', ok=True)
        req = self.request(title='Tariflerim', project='tarifler', selections=[
            {'source': 'notes/tarif.md', 'fields': [
                {'field': 'facts.porsiyon', 'label': 'Porsiyon', 'editable': True}]}])
        self.create(req)
        self.home().write_text(self.home().read_text(encoding='utf-8').replace('-->4<!--', '-->6<!--'), encoding='utf-8')
        self.run_cli('sync', ok=True)
        self.assertEqual(json.loads(recipe.read_text(encoding='utf-8').split('---\n', 2)[1])['facts']['porsiyon'], 6)

    def test_reject_unsupported_private_untrusted_identity_and_symlink(self):
        bad = self.vault / 'notes/bad.md'
        cases = [
            {'id': 'bad', 'kind': 'task', 'project': 'kariyer', 'facts': {'gorusme': 'X'}},
            {'id': 'bad', 'kind': 'note', 'project': 'kariyer', 'visibility': 'private', 'facts': {'gorusme': 'X'}},
            {'id': 'bad', 'kind': 'note', 'project': 'kariyer', 'trust': 'untrusted', 'facts': {'gorusme': 'X'}},
            {'id': 'bad', 'kind': 'note', 'project': 'kariyer', 'generated': True, 'facts': {'gorusme': 'X'}},
            {'id': 'bad', 'kind': 'note', 'project': 'kariyer', 'facts': {'gorusme': ['X']}},
        ]
        for metadata in cases:
            with self.subTest(metadata=metadata):
                bad.write_text('---\n' + json.dumps(metadata) + '\n---\n# Bad\n', encoding='utf-8')
                self.run_cli('sync', ok=True)
                before = self.snapshot()
                req = self.request(selections=[self.request()['selections'][0],
                    {'source': 'notes/bad.md', 'fields': [
                        {'field': 'facts.gorusme', 'label': 'Tarih', 'editable': True}]}])
                self.rejected(req)
                self.assertEqual(before, self.snapshot())
        bad.unlink()
        link = self.vault / 'notes/link.md'
        link.symlink_to(self.vault / 'notes/aksam.md')
        before = self.snapshot()
        req = self.request(selections=[{'source': 'notes/link.md', 'fields': [
            {'field': 'facts.gorusme', 'label': 'Tarih', 'editable': True}]}])
        self.rejected(req)
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
