#!/usr/bin/env python3
"""Directory names the user keeps out of the index (state/index.json), synthetic local fixtures only.

Nothing is pruned by name unless the user lists it: "Vendor" or "External" is a vendored code
tree in one vault and a notes folder in another. Every active name, a file that is not applied
and an indexed tree that looks vendored are visible in sync and doctor.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from unittest import mock

from v3_package_helpers import inherited_env, install

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
MODULE = SCRIPTS / 'beyin_v3_sync.py'


def load_module():
    if not MODULE.is_file():
        raise AssertionError('SyncEngine not implemented')
    spec = importlib.util.spec_from_file_location('beyin_v3_exclude_subject', MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    scripts = str(MODULE.parent)
    sys.path.insert(0, scripts)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old
        sys.path.remove(scripts)
    return module


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    scripts = str(SCRIPTS)  # adjacent modules import each other by name
    sys.path.insert(0, scripts)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old
        sys.path.remove(scripts)
    return module


index_settings = load_file('beyin_v3_index_subject', SCRIPTS / 'beyin_v3_index.py')
WITHHELD_SUMMARY = load_file('beyin_v3_projections_subject', SCRIPTS / 'beyin_v3_projections.py').WITHHELD_SUMMARY


class EngineFixture(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-exclude-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'Synthetic Beyin Çalışma'
        self.vault.mkdir()
        self.state = self.root / 'runtime'
        self.state.mkdir()
        self.engine = self.module.SyncEngine(self.vault, self.state)
        self.addCleanup(lambda: getattr(self.engine.store, 'close', lambda: None)())

    def note(self, relative, body, metadata=True):
        front = {'id': 'synthetic-' + relative.replace('/', '-').replace('.', '-'),
                 'kind': 'note', 'project': 'nebula', 'revision': 1,
                 'visibility': 'internal', 'facts': {'owner': 'Synthetic Reviewer'}}
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        text = ('---\n' + json.dumps(front, ensure_ascii=False) + '\n---\n' if metadata else '') + body
        path.write_bytes(text.encode('utf-8'))
        return path

    def config(self, value):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
        (self.state / 'index.json').write_bytes(data)

    def sources(self):
        with self.engine.store._connect() as db:
            return sorted(row[0] for row in db.execute('SELECT source FROM markdown_sources'))


class DefaultWalkTest(EngineFixture):
    def test_vendored_looking_names_stay_indexed_without_a_list(self):
        # A business vault keeps supplier notes under Vendor/ and meeting notes under External/.
        expected = [self.note(relative, 'Nebula calibration note.\n').relative_to(self.vault).as_posix()
                    for relative in ('Vendor/Acme Tedarik.md', 'External/Toplanti.md', 'isler/vendored/karar.md',
                                     'Third_Party/Denetim.md', 'proje/3rdparty/SDL2/README.md', 'notlar/gercek.md')]
        result = self.engine.sync()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.sources(), sorted(expected))
        self.assertNotIn('index_exclusions', result, 'a vault that never set the list keeps its exact sync output')

    def test_existing_excluded_dirs_still_apply(self):
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/node_modules/pkg/README.md', 'Nebula calibration node module.\n')
        self.note('proje/__pycache__/x.md', 'Nebula calibration cache.\n')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(self.sources(), ['notlar/gercek.md'])


class UserListTest(EngineFixture):
    def test_listed_name_is_pruned_at_any_depth_and_case_as_a_whole_segment(self):
        self.config({'excluded_dirs': ['3rdParty', 'target']})
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/3RDPARTY/lib/SECURITY.md', 'Nebula calibration vendored security.\n')
        self.note('proje/a/b/c/3rdparty/CONTRIBUTING.md', 'Nebula calibration vendored deep.\n')
        self.note('rust/target/debug/notes.md', 'Nebula calibration cargo tree.\n')
        # Only a whole path segment matches: a longer name or a file is not the listed directory.
        self.note('proje/3rdparty-notlari/karar.md', 'Nebula calibration own decision.\n')
        self.note('proje/my3rdparty/karar.md', 'Nebula calibration own decision two.\n')
        self.note('notlar/target.md', 'Nebula calibration note named like a directory.\n')
        # A vendored-looking name the user did not list stays indexed.
        self.note('Vendor/Acme Tedarik.md', 'Nebula calibration supplier.\n')
        result = self.engine.sync()
        self.assertEqual(result['status'], 'succeeded', 'the user chose these names: reported, not degraded')
        self.assertEqual(self.sources(), ['Vendor/Acme Tedarik.md', 'notlar/gercek.md', 'notlar/target.md',
                                          'proje/3rdparty-notlari/karar.md', 'proje/my3rdparty/karar.md'])
        self.assertEqual(result['index_exclusions'], {
            'excluded_dirs': ['3rdParty', 'target'], 'pruned': 3,
            'pruned_dirs': ['proje/3RDPARTY', 'proje/a/b/c/3rdparty', 'rust/target']})

    def test_unicode_form_and_turkish_case_match_on_both_sides(self):
        nfd = unicodedata.normalize('NFD', 'Önbellek Çıktısı')
        self.assertNotEqual(nfd, unicodedata.normalize('NFC', nfd))
        self.note(nfd + '/a.md', 'Nebula calibration decomposed folder.\n')
        self.note('Işık/b.md', 'Nebula calibration dotless capital.\n')
        self.note('İşler/c.md', 'Nebula calibration dotted capital.\n')
        self.note('Çıktı/d.md', 'Nebula calibration composed folder.\n')
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        # Typed in another case and, for the last one, decomposed.
        self.config({'excluded_dirs': ['önbellek çıktısı', 'ışık', 'işler', unicodedata.normalize('NFD', 'ÇIKTI')]})
        result = self.engine.sync()
        self.assertEqual(self.sources(), ['notlar/gercek.md'])
        self.assertEqual(result['index_exclusions']['pruned'], 4)
        key = index_settings.dir_key
        self.assertEqual({key('Işık'), key('IŞIK'), key('ışık')}, {key('ışık')})
        self.assertEqual({key('İşler'), key('İŞLER'), key('işler'), key('i\u0307şler')}, {key('işler')})
        self.assertNotEqual(key('Şifre'), key('Sifre'), 'only the two i letters fold; no other letter is merged')

    def test_excluded_tree_is_never_listed(self):
        self.config({'excluded_dirs': ['3rdparty']})
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/3rdparty/SDL2/docs/deep/README.md', 'Nebula calibration vendored readme.\n')
        with mock.patch('os.scandir', wraps=os.scandir) as scandir:
            self.engine.sync()
        listed = [os.fspath(call.args[0]) for call in scandir.call_args_list]
        self.assertIn(str(self.vault / 'proje'), listed, 'the recorder sees the walk')
        self.assertEqual([path for path in listed if '3rdparty' in path], [],
                         'pruned from the walk, not filtered after it')

    def test_adding_a_name_removes_its_records_and_removing_it_restores_them(self):
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/3rdparty/SDL2/README.md', 'Zirkonvendored upstream shader readme.\n')
        self.assertEqual(self.engine.sync()['indexed'], 2)
        found = self.engine.store.context_for('claude', 'zirkonvendored upstream shader', strict=True)['records']
        self.assertEqual([record['source'] for record in found], ['proje/3rdparty/SDL2/README.md'])
        self.config({'excluded_dirs': ['3rdparty']})
        result = self.engine.sync()
        self.assertEqual((result['indexed'], result['deleted']), (1, 1))
        self.assertEqual(self.sources(), ['notlar/gercek.md'])
        with self.engine.store._connect() as db:
            last = db.execute('SELECT event_type, record_id FROM events ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertEqual(tuple(last), ('delete', 'synthetic-proje-3rdparty-SDL2-README-md'))
        for strict in (True, False):
            found = self.engine.store.context_for('claude', 'zirkonvendored upstream shader', strict=strict)['records']
            self.assertEqual(found, [], 'gone from the passage path and the note-level path')
        self.config({'excluded_dirs': []})
        result = self.engine.sync()
        self.assertEqual((result['indexed'], result['deleted']), (2, 0))
        self.assertEqual(result['index_exclusions'], {'excluded_dirs': [], 'pruned': 0, 'pruned_dirs': []})
        found = self.engine.store.context_for('claude', 'zirkonvendored upstream shader', strict=True)['records']
        self.assertEqual([record['source'] for record in found], ['proje/3rdparty/SDL2/README.md'])
        (self.state / 'index.json').unlink()
        self.assertNotIn('index_exclusions', self.engine.sync())

    def test_a_new_source_under_a_listed_name_is_refused_before_it_is_written(self):
        # Without the refusal the file is written, never indexed, and the read-back reports a
        # projection conflict that does not name the cause.
        self.config({'excluded_dirs': ['Arşiv']})
        self.note('notes/kaynak.md', 'Nebula calibration real note.\n')
        self.engine.sync()
        with self.assertRaisesRegex(ValueError, 'excluded from the index'):
            self.engine.note_create('knowledge/arşiv/yeni.md', 'Nebula calibration created note.')
        with self.assertRaisesRegex(ValueError, 'excluded from the index'):
            self.engine.task_create('tasks/ARŞİV/gorev.md', 'Nebula calibration task.',
                                    {'id': 'synthetic-task', 'status': 'active', 'owner': 'Synthetic Reviewer'})
        self.assertFalse((self.vault / 'knowledge').exists())
        self.assertFalse((self.vault / 'tasks').exists())
        self.assertEqual(self.engine.note_create('knowledge/yeni.md', 'Nebula calibration created note.')['status'], 'succeeded')

    def test_a_listed_built_in_name_prunes_nothing_not_even_its_turkish_spelling(self):
        # The walk's own rule does not take RECE\u0130PTS for receipts; the list's Turkish folding
        # would. What the list prunes and what the receipt readers withhold must be one set, and
        # receipts cannot be in it, so a listed built-in name is a no-op here as well.
        folder = 'RECE\u0130PTS'
        self.config({'excluded_dirs': ['receipts', '.gizli']})
        self.note(folder + '/fis.md', 'Nebula calibration user folder.\n')
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        result = self.engine.sync()
        self.assertEqual(self.sources(), [folder + '/fis.md', 'notlar/gercek.md'])
        self.assertEqual((result['index_exclusions']['excluded_dirs'], result['index_exclusions']['pruned']),
                         (['receipts', '.gizli'], 0))
        self.assertEqual(self.engine.excluded_dir_keys, frozenset())

    def test_built_in_and_hidden_names_are_harmless_and_receipts_keep_working(self):
        self.config({'excluded_dirs': ['node_modules', 'receipts', '.venv', 'target']})
        self.note('notes/kaynak.md', 'Nebula calibration receipt source.\n')
        self.note('proje/.venv/lib/README.md', 'Nebula calibration venv.\n')
        self.note('proje/node_modules/pkg/README.md', 'Nebula calibration node module.\n')
        result = self.engine.sync()
        self.assertNotIn('error', result['index_exclusions'])
        self.assertEqual(result['index_exclusions']['pruned'], 0, 'the walk skipped these before the list was asked')
        receipt = self.engine.receipt('synthetic-event-1', 'Nebula calibration receipt.', ['notes/kaynak.md'], 'codex')
        self.assertEqual(receipt['status'], 'succeeded')
        with self.engine.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)
        self.assertEqual(self.sources(), ['notes/kaynak.md'])


class RefusedConfigTest(EngineFixture):
    """A file that cannot be applied never breaks the sync: everything is indexed and the reason is shown."""

    def setUp(self):
        super().setUp()
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/3rdparty/SDL2/README.md', 'Nebula calibration vendored readme.\n')
        self.everything = ['notlar/gercek.md', 'proje/3rdparty/SDL2/README.md']

    def refused(self, reason):
        result = self.engine.sync()
        self.assertEqual(result['status'], 'succeeded', 'a refused file is reported, not a degraded scan')
        self.assertEqual(self.sources(), self.everything)
        report = result['index_exclusions']
        self.assertEqual((report['excluded_dirs'], report['pruned'], report['pruned_dirs']), ([], 0, []))
        self.assertIn(reason, report['error'])
        return report

    def test_malformed_json(self):
        self.config(b'{not json')
        self.refused('not valid JSON')
        self.config(b'\xff\xfe\x00broken')
        self.refused('not valid JSON')

    def test_one_bad_entry_refuses_the_whole_list(self):
        # 3rdparty is a valid entry and a real directory: a half-applied list would prune it.
        for bad in ('../gizli', 'a/b', 'a\\b', '', ' 3rdparty ', '.', '..', 7, None, 'x' * 256, 'satir\nsonu'):
            with self.subTest(entry=bad):
                self.config({'excluded_dirs': ['3rdparty', bad]})
                self.refused('excluded_dirs')

    def test_wrong_shape_and_unknown_keys(self):
        for value in (['3rdparty'], {'excluded_dirs': '3rdparty'}, {'exclude_dirs': ['3rdparty']},
                      {'excluded_dirs': ['3rdparty'], 'schema': 2}):
            with self.subTest(value=value):
                self.config(value)
                self.refused('excluded_dirs')

    def test_entry_count_is_capped(self):
        self.config({'excluded_dirs': ['3rdparty'] + ['d%d' % i for i in range(64)]})
        self.refused('at most 64')
        self.config({'excluded_dirs': ['3rdparty'] + ['d%d' % i for i in range(63)]})
        self.assertEqual(self.engine.sync()['index_exclusions']['pruned_dirs'], ['proje/3rdparty'])

    def test_oversized_file_and_directory(self):
        self.config(json.dumps({'excluded_dirs': ['3rdparty']}).encode() + b' ' * 70000)
        self.refused('larger than')
        (self.state / 'index.json').unlink()
        (self.state / 'index.json').mkdir()
        self.refused('regular file')

    def test_symlinked_config(self):
        outside = self.root / 'outside.json'
        outside.write_bytes(json.dumps({'excluded_dirs': ['3rdparty']}).encode())
        try:
            (self.state / 'index.json').symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are not available here')
        self.refused('regular file')

    def test_bom_written_by_windows_editors_is_read(self):
        self.config(b'\xef\xbb\xbf' + json.dumps({'excluded_dirs': ['3rdparty']}).encode())
        result = self.engine.sync()
        self.assertNotIn('error', result['index_exclusions'])
        self.assertEqual(self.sources(), ['notlar/gercek.md'])

    def test_folders_read_back_through_the_index_cannot_be_excluded(self):
        self.note('🔮 850-Companion/Core.md', 'Nebula calibration companion.\n', metadata=False)
        self.everything = sorted(self.everything + ['🔮 850-Companion/Core.md'])
        for name in ('Knowledge', 'TASKS', '🔮 850-Companion', 'notes'):
            with self.subTest(name=name):
                self.config({'excluded_dirs': ['3rdparty', name]})
                self.refused('read back through the index')
        # With notes still listed, the refused file leaves note-create and its read-back intact.
        created = self.engine.note_create('notes/yeni.md', 'Nebula calibration created note.')
        self.assertEqual(created['status'], 'succeeded')

    def test_a_personalized_companion_folder_is_protected_too(self):
        self.note('Hafıza Echo/Core.md', 'Nebula calibration companion.\n', metadata=False)
        self.everything = sorted(self.everything + ['Hafıza Echo/Core.md'])
        self.config({'excluded_dirs': ['hafıza echo']})
        self.refused('read back through the index')
        # Another top-level folder beside the companion is the user's to exclude.
        self.note('Vendor/Acme Tedarik.md', 'Nebula calibration supplier.\n')
        self.config({'excluded_dirs': ['vendor']})
        report = self.engine.sync()['index_exclusions']
        self.assertEqual((report.get('error'), report['pruned_dirs']), (None, ['Vendor']))


class SettingsFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='beyin-index-settings-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()

    def test_save_adds_removes_and_keeps_the_first_spelling(self):
        self.assertEqual(index_settings.read(self.state, self.vault), ([], None, False))
        self.assertEqual(index_settings.save(self.state, self.vault, ['3rdparty', 'Target']), ['3rdparty', 'Target'])
        self.assertEqual(index_settings.save(self.state, self.vault, ['TARGET', unicodedata.normalize('NFD', 'Önbellek')]),
                         ['3rdparty', 'Target', 'Önbellek'])
        self.assertEqual(json.loads((self.state / 'index.json').read_bytes().decode('utf-8')),
                         {'schema': 1, 'excluded_dirs': ['3rdparty', 'Target', 'Önbellek']})
        self.assertEqual(index_settings.save(self.state, self.vault, remove=['3RDPARTY', 'absent']), ['Target', 'Önbellek'])
        self.assertEqual(index_settings.read(self.state, self.vault), (['Target', 'Önbellek'], None, True))

    def test_a_refused_change_writes_nothing(self):
        index_settings.save(self.state, self.vault, ['3rdparty'])
        before = (self.state / 'index.json').read_bytes()
        for name in ('notes', 'knowledge', 'tasks', '🔮 850-Companion', 'a/b', '..', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                index_settings.save(self.state, self.vault, [name])
        with self.assertRaises(ValueError):
            index_settings.save(self.state, self.vault, ['d%d' % i for i in range(64)])
        self.assertEqual((self.state / 'index.json').read_bytes(), before)
        self.assertEqual([entry.name for entry in self.state.iterdir()], ['index.json'], 'no temporary file is left')

    def test_an_invalid_file_is_never_rewritten(self):
        self.state.mkdir()
        (self.state / 'index.json').write_bytes(b'{not json')
        with self.assertRaisesRegex(ValueError, 'fix or remove it first'):
            index_settings.save(self.state, self.vault, ['3rdparty'])
        self.assertEqual((self.state / 'index.json').read_bytes(), b'{not json')


class VendoredHintTest(unittest.TestCase):
    def test_only_trees_with_upstream_documents_are_pointed_at(self):
        sources = ['Vendor/Acme Tedarik.md', 'Vendor/Sozlesmeler/2026.md', 'External/Toplanti.md',  # notes folders
                   'proje/3rdparty/SDL2/README.md', 'proje/3rdparty/SDL2/SECURITY.md', 'proje/3rdparty/SDL2/docs/x.md',
                   'go/Vendor/pkg/CHANGELOG.md', 'notlar/vendor.md', 'notlar/gercek.md']
        self.assertEqual(index_settings.vendored_hint(sources), {
            'notes': 4, 'dirs': [{'dir': 'proje/3rdparty', 'notes': 3}, {'dir': 'go/Vendor', 'notes': 1}],
            'names': ['3rdparty', 'Vendor'], 'truncated': False})
        self.assertIsNone(index_settings.vendored_hint(sources[:3] + sources[-2:]))

    def test_doctor_leaves_out_what_the_list_already_covers(self):
        with tempfile.TemporaryDirectory(prefix='beyin-index-hint-') as tmp:
            state = Path(tmp)
            sources = ['proje/3rdparty/SDL2/README.md', 'go/vendor/pkg/README.md']
            self.assertEqual(index_settings.doctor(state, None, sources)['vendored_hint']['names'], ['3rdparty', 'vendor'])
            (state / 'index.json').write_bytes(b'{"excluded_dirs": ["3RDPARTY"]}')
            report = index_settings.doctor(state, None, sources)
            self.assertEqual((report['status'], report['excluded_dirs']), ('active', ['3RDPARTY']))
            self.assertEqual(report['vendored_hint']['names'], ['vendor'])
            (state / 'index.json').write_bytes(b'{"excluded_dirs": ["3rdparty", "vendor"]}')
            self.assertNotIn('vendored_hint', index_settings.doctor(state, None, sources))
            (state / 'index.json').write_bytes(b'[]')
            report = index_settings.doctor(state, None, sources)
            self.assertEqual((report['status'], report['excluded_dirs']), ('invalid', []))
            self.assertIn('error', report)


class ArchiveHintTest(unittest.TestCase):
    def test_unretired_notes_under_archive_directories_are_pointed_at(self):
        records = [
            {'source': '📦 900-Archive/eski-arac.md'},  # no status -> active
            {'source': '📦 900-Archive/tamamlanan.md', 'status': 'completed'},  # completed is not retired
            {'source': '📦 900-Archive/terk-edilen.md', 'status': 'abandoned'},  # abandoned is not retired
            {'source': '📦 900-Archive/arsivlenmis.md', 'status': 'archived'},  # retired!
            {'source': '📦 900-Archive/emekli.md', 'status': 'retired'},  # retired!
            {'source': '📦 900-Archive/sub/derin.md', 'status': 'active'},  # unretired
            {'source': 'archive/legacy.md', 'status': 'waiting'},  # unretired
            {'source': 'arsiv/gecmis.md', 'status': 'eski'},  # retired!
            {'source': 'knowledge/canli.md', 'status': 'active'},  # not in archive dir
        ]
        hint = index_settings.archive_hint(records)
        self.assertEqual(hint, {
            'notes': 5,
            'dirs': [{'dir': '📦 900-Archive', 'notes': 4}, {'dir': 'archive', 'notes': 1}],
            'names': ['archive', '📦 900-Archive'],
            'truncated': False,
        })

    def test_all_retired_or_no_archive_notes_returns_none(self):
        records = [
            {'source': '📦 900-Archive/bir.md', 'status': 'archived'},
            {'source': '📦 900-Archive/iki.md', 'status': 'superseded'},
            {'source': 'knowledge/canli.md', 'status': 'active'},
        ]
        self.assertIsNone(index_settings.archive_hint(records))
        self.assertIsNone(index_settings.archive_hint([]))

    def test_doctor_leaves_out_what_the_list_already_covers(self):
        with tempfile.TemporaryDirectory(prefix='beyin-index-archive-hint-') as tmp:
            state = Path(tmp)
            records = [
                {'source': '📦 900-Archive/not1.md'},
                {'source': 'archive/not2.md'},
            ]
            report = index_settings.doctor(state, None, records=records)
            self.assertEqual(report['archive_hint']['names'], ['archive', '📦 900-Archive'])
            (state / 'index.json').write_bytes(json.dumps({'excluded_dirs': ['📦 900-Archive']}).encode('utf-8'))
            report = index_settings.doctor(state, None, records=records)
            self.assertEqual(report['archive_hint']['names'], ['archive'])
            (state / 'index.json').write_bytes(json.dumps({'excluded_dirs': ['📦 900-Archive', 'archive']}).encode('utf-8'))
            self.assertNotIn('archive_hint', index_settings.doctor(state, None, records=records))

    def test_doctor_human_formatting_includes_archive_hint(self):
        entry = load_file('beyin_entry_archive_probe', ROOT / 'scripts/beyin_entry.py')
        result = {
            'status': 'observed_metadata',
            'index_exclusions': {
                'file': 'index.json',
                'excluded_dirs': [],
                'status': 'none',
                'archive_hint': {
                    'notes': 4,
                    'dirs': [{'dir': '📦 900-Archive', 'notes': 4}],
                    'names': ['📦 900-Archive'],
                    'truncated': False,
                }
            }
        }
        human = entry.human_result(result, 'doctor')
        self.assertIn('Arsiv kapsami (bilgi): arsiv klasorlerinde emekli edilmemis (canli sayilan) 4 not indeksli (📦 900-Archive: 4).', human)
        self.assertIn('--index-exclude-dir "📦 900-Archive"', human)
        self.assertIn('status: archived', human)


class WordCapTest(unittest.TestCase):
    def test_word_cap_leaves_an_excluded_tree_alone(self):
        hygiene = load_file('beyin_v3_hygiene_exclude_subject', SCRIPTS / 'beyin_v3_hygiene.py')
        with tempfile.TemporaryDirectory(prefix='beyin-index-cap-') as tmp:
            root = Path(tmp).resolve()
            vault, state = root / 'vault', root / 'state'
            for relative in ('Makaleler/uzun.md', 'proje/3rdparty/SDL2/uzun.md'):
                path = vault / relative
                path.parent.mkdir(parents=True)
                path.write_bytes(('kelime ' * 600).encode('utf-8'))
            state.mkdir()
            vendored = vault / 'proje/3rdparty/SDL2/uzun.md'
            self.assertEqual([entry['file'] for entry in hygiene.cap_scan(vault, state=state)['over']],
                             ['Makaleler/uzun.md', 'proje/3rdparty/SDL2/uzun.md'])
            self.assertEqual(hygiene.file_over_cap(vault, vendored, state=state), (600, True))
            (state / 'index.json').write_bytes(b'{"excluded_dirs": ["3rdParty"]}')
            self.assertEqual([entry['file'] for entry in hygiene.cap_scan(vault, state=state)['over']], ['Makaleler/uzun.md'])
            self.assertIsNone(hygiene.file_over_cap(vault, vendored, state=state))
            self.assertEqual(hygiene.file_over_cap(vault, vault / 'Makaleler/uzun.md', state=state), (600, True))


class InstalledCliTest(unittest.TestCase):
    """The real install: preferences sets the list, sync applies and shows it, doctor reads it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='beyin-index-cli-')
        root = Path(cls.tmp.name).resolve()
        cls.vault, cls.state, home = root / 'vault', root / 'state', root / 'home'
        cls.vault.mkdir()
        home.mkdir()
        cls.env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                                LOCALAPPDATA=str(home / 'localappdata'), BEYIN_V3_NO_SPAWN='1',
                                PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        for relative in ('notlar/gercek.md', 'Vendor/Acme Tedarik.md', 'proje/3rdparty/SDL2/README.md',
                         'proje/3rdparty/SDL2/SECURITY.md', 'proje/3rdparty/SPIRV/docs/guide.md'):
            path = cls.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('Nebula calibration ' + relative + '\n').encode('utf-8'))
        installed = install(cls.vault, cls.state, cls.env)
        assert installed.returncode == 0, installed.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def beyin(self, *arguments, human=False):
        done = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *arguments, '--human' if human else '--json'],
                              cwd=self.vault, env=self.env, capture_output=True, timeout=120)
        text = (done.stdout if not done.returncode else done.stderr).decode('utf-8', errors='replace')
        return done.returncode, text if human else json.loads(text)

    def indexed(self):
        db = sqlite3.connect(str(self.state / 'memory.sqlite3'))
        try:
            return sorted(row[0] for row in db.execute('SELECT source FROM markdown_sources'))
        finally:
            db.close()

    def test_hint_then_list_then_agreement_between_doctor_and_index(self):
        code, synced = self.beyin('sync')
        self.assertEqual(code, 0, synced)
        self.assertNotIn('index_exclusions', synced)
        self.assertNotIn('index_exclusions', self.beyin('preferences')[1])
        vendored = [source for source in self.indexed() if source.startswith('proje/3rdparty/')]
        self.assertEqual(len(vendored), 3)
        report = self.beyin('doctor')[1]['index_exclusions']
        self.assertEqual(report, {'file': 'index.json', 'excluded_dirs': [], 'status': 'none',
                                  'always_excluded': ['__pycache__', 'node_modules', 'receipts'],
                                  'vendored_hint': {'notes': 3, 'dirs': [{'dir': 'proje/3rdparty', 'notes': 3}],
                                                    'names': ['3rdparty'], 'truncated': False}})
        human = self.beyin('doctor', human=True)[1]
        self.assertIn('Indeks kapsami (bilgi): baska projelerin kod kopyasi gibi gorunen klasorlerde 3 not indeksli '
                      '(proje/3rdparty: 3).', human)
        self.assertIn('beyin.py preferences --index-exclude-dir 3rdparty', human)
        self.assertNotIn('Vendor', human, 'a notes folder without upstream documents is not pointed at')
        self.assertFalse((self.state / 'index.json').exists(), 'doctor only reads')

        # A refused name changes nothing and says why.
        code, refused = self.beyin('preferences', '--index-exclude-dir', '3rdparty', '--index-exclude-dir', 'notes')
        self.assertEqual(code, 1)
        self.assertIn('read back through the index', refused['message'])
        self.assertFalse((self.state / 'index.json').exists())

        code, saved = self.beyin('preferences', '--index-exclude-dir', '3rdparty')
        self.assertEqual((code, saved['status'], saved['index_exclusions']), (0, 'saved', {'excluded_dirs': ['3rdparty']}))
        self.assertIn('Indeks disi klasor adlari (senin listen, her derinlikte): 3rdparty.', self.beyin('preferences', human=True)[1])
        code, synced = self.beyin('sync')
        self.assertEqual((synced['status'], synced['deleted']), ('succeeded', 3))
        self.assertEqual(synced['index_exclusions'], {'excluded_dirs': ['3rdparty'], 'pruned': 1, 'pruned_dirs': ['proje/3rdparty']})
        self.assertIn('3rdparty (bu taramada 1 klasor atlandi).', self.beyin('sync', human=True)[1])
        self.assertEqual([source for source in self.indexed() if '3rdparty' in source], [])
        self.assertIn('Vendor/Acme Tedarik.md', self.indexed())
        report = self.beyin('doctor')[1]['index_exclusions']
        self.assertEqual((report['status'], report['excluded_dirs'], 'vendored_hint' in report), ('active', ['3rdparty'], False))
        self.assertIn('Indeks disi klasor adlari (senin listen, her derinlikte): 3rdparty.', self.beyin('doctor', human=True)[1])

        # A damaged file is shown by all three commands and refused by the next change.
        (self.state / 'index.json').write_bytes(b'{not json')
        code, synced = self.beyin('sync')
        self.assertEqual((code, synced['status'], synced['index_exclusions']['error']), (0, 'succeeded', 'not valid JSON'))
        self.assertEqual(len([source for source in self.indexed() if '3rdparty' in source]), 3)
        code, doctor = self.beyin('doctor')
        self.assertEqual((doctor['index_exclusions']['status'], doctor['index_exclusions']['error']), ('invalid', 'not valid JSON'))
        self.assertIn('index.json gecersiz (not valid JSON); klasor haric tutma listesi uygulanmiyor', self.beyin('doctor', human=True)[1])
        self.assertIn('index.json gecersiz', self.beyin('preferences', human=True)[1])
        code, refused = self.beyin('preferences', '--index-exclude-dir', 'target')
        self.assertEqual(code, 1)
        self.assertIn('fix or remove it first', refused['message'])
        self.assertEqual((self.state / 'index.json').read_bytes(), b'{not json')

        (self.state / 'index.json').unlink()
        code, saved = self.beyin('preferences', '--index-exclude-dir', '3rdparty', '--index-exclude-dir', 'target')
        self.assertEqual(saved['index_exclusions'], {'excluded_dirs': ['3rdparty', 'target']})
        code, saved = self.beyin('preferences', '--index-include-dir', '3RDPARTY')
        self.assertEqual(saved['index_exclusions'], {'excluded_dirs': ['target']})


# Reads the real database the way the SessionStart hook does: read-only, without a sync first.
RECEIPT_READER = '''import json, sqlite3, sys
from pathlib import Path
vault, state = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(vault / '.claude/scripts'))
from beyin_v3_projections import latest_receipts, recent_receipts
db = sqlite3.connect((state / 'memory.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
try:
    latest = next((event['summary'] for _, event in latest_receipts(db, vault)), None)
    recap = {item['source']: [item['summary'], item['refs'], item.get('refs_withheld')] for item in recent_receipts(db, vault=vault)['items']}
finally:
    db.close()
print(json.dumps({'latest': latest, 'recap': recap}))
'''


class ExcludedRefTest(unittest.TestCase):
    """Excluding a directory must not weaken the gate that withholds private refs.

    A ref under an excluded directory has no record, so its visibility is unknown; recap
    withholds it like a private one and the SessionStart receipt gate skips its receipt.
    Real install, real database rows.
    """

    GENERAL, OPEN, PRIVATE = 'Zirkongenel karar kaydi.', 'Zirkonacik ozet kaydi.', 'Zirkongizli ayrinti kaydi.'

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='beyin-index-refs-')
        root = Path(cls.tmp.name).resolve()
        cls.vault, cls.state, home = root / 'vault', root / 'state', root / 'home'
        cls.vault.mkdir()
        home.mkdir()
        cls.env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                                LOCALAPPDATA=str(home / 'localappdata'), BEYIN_V3_NO_SPAWN='1',
                                PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        for relative, text in (('notlar/genel.md', 'Genel not.\n'), ('Çalışma/acik.md', 'Acik not.\n'),
                               ('Çalışma/gizli.md', '---\nvisibility: private\n---\nGizli not.\n')):
            path = cls.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode('utf-8'))
        installed = install(cls.vault, cls.state, cls.env)
        assert installed.returncode == 0, installed.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def beyin(self, *arguments):
        done = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *arguments, '--json'], cwd=self.vault,
                              env=self.env, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return json.loads(done.stdout.decode('utf-8'))

    def rows(self, query):
        db = sqlite3.connect(str(self.state / 'memory.sqlite3'))
        try:
            return [row[0] for row in db.execute(query)]
        finally:
            db.close()

    EVENTS = {'synthetic-general': GENERAL, 'synthetic-open': OPEN, 'synthetic-private': PRIVATE}

    def by_summary(self, rows):
        """Recap rows keyed by the summary each receipt was written with, via its source.

        A receipt with a private ref shows a placeholder in place of its summary; any other shows it.
        """
        named = {'receipts/' + hashlib.sha256(ident.encode()).hexdigest() + '.md': summary
                 for ident, summary in self.EVENTS.items()}
        keyed = {}
        for source, (summary, refs, withheld) in rows.items():
            written = named[source]
            self.assertEqual(summary, WITHHELD_SUMMARY if withheld and withheld['private'] else written)
            keyed[written] = (refs, withheld)
        return keyed

    def recap(self):
        return self.by_summary({item['source']: (item['summary'], item['refs'], item.get('refs_withheld'))
                                for item in self.beyin('recap')['items']})

    def unsynced(self):
        done = subprocess.run([sys.executable, '-c', RECEIPT_READER, str(self.vault), str(self.state)],
                              cwd=self.vault, env=self.env, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return json.loads(done.stdout.decode('utf-8'))

    def session_start(self):
        done = subprocess.run([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'), '--vault', str(self.vault),
                               '--state', str(self.state), '--harness', 'claude'], cwd=self.vault, env=self.env,
                              input=json.dumps({'hook_event_name': 'SessionStart', 'session_id': 'synthetic-refs',
                                                'prompt': 'Zirkon kayitlari'}).encode('utf-8'),
                              capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return done.stdout.decode('utf-8')

    def test_a_ref_under_an_excluded_directory_stays_withheld(self):
        self.beyin('sync')
        for event, summary, refs in (('synthetic-general', self.GENERAL, ['notlar/genel.md']),
                                     ('synthetic-open', self.OPEN, ['Çalışma/acik.md']),
                                     ('synthetic-private', self.PRIVATE, ['Çalışma/gizli.md', 'notlar/genel.md'])):
            self.beyin('receipt', '--event-id', event, '--summary', summary, *[part for ref in refs for part in ('--ref', ref)])
        withheld = {'private': 1, 'missing': 0}

        # Indexed: the private record itself withholds its ref, and its receipt never opens a session.
        self.assertEqual(sorted(self.rows("SELECT source FROM markdown_sources WHERE source LIKE 'Çalışma/%'")),
                         ['Çalışma/acik.md', 'Çalışma/gizli.md'])
        recap = self.recap()
        self.assertEqual(recap[self.PRIVATE], (['notlar/genel.md'], withheld))
        self.assertEqual(recap[self.OPEN], (['Çalışma/acik.md'], None))
        self.assertEqual(self.unsynced()['latest'], self.OPEN)
        opening = self.session_start()
        self.assertIn(self.OPEN, opening)
        self.assertNotIn(self.PRIVATE, opening)

        # Excluded: the records are gone, the gate holds, and it now covers every path below.
        self.beyin('preferences', '--index-exclude-dir', 'ÇALIŞMA')  # typed in capitals
        self.assertEqual(self.beyin('sync')['index_exclusions']['pruned_dirs'], ['Çalışma'])
        self.assertEqual(self.rows("SELECT source FROM markdown_sources WHERE source LIKE 'Çalışma/%'"), [])
        recap = self.recap()
        self.assertEqual(recap[self.PRIVATE], (['notlar/genel.md'], withheld), 'the private path must not start to show')
        self.assertEqual(recap[self.OPEN], ([], withheld), 'visibility is unknown without a record: withheld')
        self.assertEqual(recap[self.GENERAL], (['notlar/genel.md'], None))
        # The folded name is stored with the index, in the transaction that removed the records.
        self.assertEqual(self.rows("SELECT value FROM metadata WHERE key='index_excluded_dirs'"),
                         [json.dumps([index_settings.dir_key('Çalışma')], ensure_ascii=False)])
        self.assertEqual(self.unsynced()['latest'], self.GENERAL)
        opening = self.session_start()
        self.assertIn(self.GENERAL, opening)
        self.assertNotIn(self.PRIVATE, opening, 'the receipt about a private note must not open a session')
        self.assertNotIn('gizli.md', opening)

        # The name leaves index.json but no sync has run: the records are still missing, so the
        # stored list (committed with the index) must keep withholding. index.json alone would not.
        self.beyin('preferences', '--index-include-dir', 'çalışma')
        self.assertEqual(json.loads((self.state / 'index.json').read_bytes().decode('utf-8'))['excluded_dirs'], [])
        self.assertEqual(self.rows("SELECT source FROM markdown_sources WHERE source LIKE 'Çalışma/%'"), [])
        unsynced = self.unsynced()
        self.assertEqual(self.by_summary(unsynced['recap'])[self.PRIVATE], (['notlar/genel.md'], withheld))
        self.assertEqual(unsynced['latest'], self.GENERAL)

        # Included again: the records are back and decide for themselves.
        self.beyin('sync')
        self.assertEqual(sorted(self.rows("SELECT source FROM markdown_sources WHERE source LIKE 'Çalışma/%'")),
                         ['Çalışma/acik.md', 'Çalışma/gizli.md'])
        self.assertEqual(self.rows("SELECT value FROM metadata WHERE key='index_excluded_dirs'"), [])
        recap = self.recap()
        self.assertEqual(recap[self.PRIVATE], (['notlar/genel.md'], withheld))
        self.assertEqual(recap[self.OPEN], (['Çalışma/acik.md'], None))
        self.assertEqual(self.unsynced()['latest'], self.OPEN)

        # Names the walk skips anyway stay no-ops here too: listing receipts must not hide every receipt.
        self.beyin('preferences', '--index-exclude-dir', 'receipts', '--index-exclude-dir', '.claude')
        self.beyin('sync')
        self.assertEqual(self.rows("SELECT value FROM metadata WHERE key='index_excluded_dirs'"), [])
        self.assertEqual(self.unsynced()['latest'], self.OPEN)
        self.assertEqual(self.recap()[self.PRIVATE], (['notlar/genel.md'], withheld))

        # The stored names follow the list from one name to another, not only to and from empty.
        self.beyin('preferences', '--index-exclude-dir', 'Çalışma')
        self.beyin('sync')
        self.beyin('preferences', '--index-include-dir', 'Çalışma', '--index-exclude-dir', 'notlar')
        self.beyin('sync')
        self.assertEqual(self.rows("SELECT value FROM metadata WHERE key='index_excluded_dirs'"), ['["notlar"]'])
        recap = self.recap()
        self.assertEqual(recap[self.OPEN], (['Çalışma/acik.md'], None))
        self.assertEqual(recap[self.GENERAL], ([], withheld))
        self.assertEqual(recap[self.PRIVATE], ([], {'private': 2, 'missing': 0}))


if __name__ == '__main__':
    unittest.main()
