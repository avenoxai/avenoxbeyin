"""Opt-in folder project scopes (.beyin-projects.json) for explicit-project gates."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unicodedata
import unittest
from unittest.mock import patch
from v3_package_helpers import clean_environ

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
sys.path.insert(0, str(ROOT / 'extensions/laya'))
import beyin_v3 as runtime
import beyin_v3_jev as advisor

GAME = 'Projeler/Oyun Kanalları'
ARCHIVE = 'Projeler/Arşiv'


class ProjectScopesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.store = runtime.MemoryStore(Path(self.tmp.name) / 'state', self.vault)
        self.env = clean_environ(TYPESAFE_API_KEY='synthetic-test-key')
        self.env.start()
        self.addCleanup(self.env.stop)

    def note(self, ident, source, text=None, **fields):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        text = text or 'Kaynak notu ' + ident + ' kısa tercih kaydı içerir.'
        path.write_text(text, encoding='utf-8')
        return self.store.ingest(dict(id=ident, source=source, text=text,
                                      updated_at='2026-09-28T00:00:00Z', **fields))

    def scopes(self, data):
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        (self.vault / runtime.PROJECT_SCOPES_FILE).write_text(text, encoding='utf-8')

    def ids(self, project):
        records, _ = self.store._eligible('internal', project)
        return sorted(r['id'] for r in records)

    def vault_notes(self):
        self.note('game', GAME + '/Plan.md')
        self.note('archive', ARCHIVE + '/Plan.md')
        self.note('rules', 'Companion/Kurallar.md')
        self.note('tagged', 'Inbox/Etiketli.md', project='oyun')

    def test_without_file_explicit_projects_keep_the_exact_field_gate(self):
        self.vault_notes()
        self.assertEqual(self.ids('oyun'), ['tagged'])
        self.assertEqual(self.ids('arsiv'), [])
        self.assertEqual(self.ids(None), ['archive', 'game', 'rules', 'tagged'])

    def test_folders_assign_untagged_notes_and_isolate_projects(self):
        self.vault_notes()
        self.scopes({'folders': {GAME: 'oyun', ARCHIVE: 'arsiv'}})
        self.assertEqual(self.ids('oyun'), ['game', 'tagged'])
        self.assertEqual(self.ids('arsiv'), ['archive'])
        self.assertEqual(self.ids('baska'), [])

    def test_shared_unscoped_adds_only_notes_without_any_project(self):
        self.vault_notes()
        self.scopes({'folders': {GAME: 'oyun', ARCHIVE: 'arsiv'}, 'shared_unscoped': True})
        self.assertEqual(self.ids('oyun'), ['game', 'rules', 'tagged'])
        self.assertEqual(self.ids('arsiv'), ['archive', 'rules'])
        # A project unknown to the file still sees the shared notes, never another project.
        self.assertEqual(self.ids('baska'), ['rules'])

    def test_record_field_wins_over_folder(self):
        self.note('moved', GAME + '/Eski.md', project='arsiv')
        self.scopes({'folders': {GAME: 'oyun'}, 'shared_unscoped': True})
        self.assertEqual(self.ids('oyun'), [])
        self.assertEqual(self.ids('arsiv'), ['moved'])

    def test_nested_folder_prefers_the_longest_mapping(self):
        self.note('outer', 'Projeler/Plan.md')
        self.note('inner', GAME + '/Plan.md')
        self.scopes({'folders': {'Projeler': 'genel', GAME: 'oyun'}})
        self.assertEqual(self.ids('genel'), ['outer'])
        self.assertEqual(self.ids('oyun'), ['inner'])

    def test_folder_match_stops_at_a_path_boundary(self):
        self.note('sibling', 'Projeler/Oyun Kanalları Eski/Plan.md')
        self.note('root', 'Oyun.md')
        # Both mapped folders exist, so only the path boundary keeps them apart.
        (self.vault / GAME).mkdir(parents=True)
        (self.vault / 'Oyun').mkdir()
        self.scopes({'folders': {GAME: 'oyun', 'Oyun': 'kok'}})
        self.assertEqual(self.ids('oyun'), [])
        self.assertEqual(self.ids('kok'), [])

    def test_folder_spelling_is_normalized(self):
        self.note('game', GAME + '/Plan.md')
        decomposed = unicodedata.normalize('NFD', GAME).replace('/', '\\') + '/'
        self.scopes({'folders': {decomposed: 'oyun'}})
        self.assertEqual(self.ids('oyun'), ['game'])

    def test_folder_case_and_decomposed_disk_names_still_claim_their_notes(self):
        # macOS keeps names as typed (often NFD) and matches case-insensitively; a folder
        # typed in another case must not leave its notes in the shared scope.
        client = unicodedata.normalize('NFD', 'Projeler/Müşteri X')
        self.note('client', client + '/Teklif.md')
        self.note('rules', 'Companion/Kurallar.md')
        self.scopes({'folders': {'projeler/müşteri x': 'musteri'}, 'shared_unscoped': True})
        self.assertEqual(self.ids('musteri'), ['client', 'rules'])
        self.assertEqual(self.ids('oyun'), ['rules'])

    def test_missing_or_renamed_folder_fails_closed(self):
        # A renamed project folder matches nothing; with shared_unscoped its notes would
        # otherwise join every other project's scope.
        self.note('archive', 'Projeler/Arşiv 2025/Plan.md')
        self.note('game', GAME + '/Plan.md')
        for folders in ({ARCHIVE: 'arsiv', GAME: 'oyun'}, {'Projeler/Arsiv 2025': 'arsiv'}):
            with self.subTest(folders=folders):
                self.scopes({'folders': folders, 'shared_unscoped': True})
                with self.assertRaisesRegex(ValueError, 'not found'):
                    self.store._eligible('internal', 'oyun')
        self.scopes({'folders': {GAME: 'oyun', 'projeler/oyun kanalları/': 'oyun'}})
        self.assertEqual(self.ids('oyun'), ['game'])
        self.scopes({'folders': {GAME: 'oyun', 'projeler/oyun kanalları': 'arsiv'}})
        with self.assertRaisesRegex(ValueError, 'assigned twice'):
            self.store._eligible('internal', 'oyun')

    def test_no_project_path_ignores_the_file_even_when_invalid(self):
        self.vault_notes()
        self.scopes('{broken')
        self.assertEqual(self.ids(None), ['archive', 'game', 'rules', 'tagged'])
        self.assertTrue(self.store.retrieve('kaynak notu', limit=10)['records'])

    def test_invalid_files_fail_closed_for_explicit_projects(self):
        self.vault_notes()
        for bad in ('{broken', '[]', {'folders': [], 'shared_unscoped': False},
                    {'folders': {GAME: 'oyun'}, 'shared_unscoped': 'yes'},
                    {'folders': {GAME: ''}}, {'folders': {GAME: 3}},
                    {'folders': {'../Disari': 'oyun'}}, {'folders': {'/mutlak': 'oyun'}},
                    {'folders': {'C:/Users': 'oyun'}}, {'folders': {'': 'oyun'}},
                    {'folders': {'Projeler/./Oyun': 'oyun'}}, {'extra': True},
                    {'folders': {GAME: 'oyun', GAME + '/': 'arsiv'}}):
            with self.subTest(bad=bad):
                self.scopes(bad)
                with self.assertRaises(ValueError):
                    self.store._eligible('internal', 'oyun')

    def test_visibility_and_freshness_gates_still_apply_inside_a_scope(self):
        self.note('secret', GAME + '/Gizli.md', visibility='private')
        stale = self.note('stale', GAME + '/Eski.md')
        (self.vault / stale['source']).write_text('changed', encoding='utf-8')
        self.scopes({'folders': {GAME: 'oyun'}, 'shared_unscoped': True})
        self.assertEqual(self.ids('oyun'), [])

    def test_answer_check_accepts_shared_evidence_and_rejects_other_projects(self):
        self.vault_notes()
        self.scopes({'folders': {GAME: 'oyun', ARCHIVE: 'arsiv'}, 'shared_unscoped': True})

        def cite(record):
            return dict(record_id=record, source_sha256=next(
                r['source_sha256'] for r in self.store._eligible('internal', None)[0] if r['id'] == record),
                quote='kısa tercih kaydı')

        def offline(*args):
            raise AssertionError('jev is off; no provider call expected')
        result = advisor.verify_answer(self.store, [
            dict(text='Oyun planı kısa.', citations=[cite('game')]),
            dict(text='Kural kısa.', citations=[cite('rules')]),
            dict(text='Arşiv planı kısa.', citations=[cite('archive')]),
        ], project='oyun', transport=offline)
        verified = [c['mechanical_verified'] for c in result['claims']]
        self.assertEqual(verified, [True, True, False])
        self.assertEqual(result['claims'][2]['diagnostics'], ['citation_not_current_or_exact'])

    def test_review_evidence_from_another_project_is_rejected(self):
        self.vault_notes()
        self.scopes({'folders': {GAME: 'oyun', ARCHIVE: 'arsiv'}, 'shared_unscoped': True})
        archive = next(r for r in self.store._eligible('internal', None)[0] if r['id'] == 'archive')
        proposal = dict(status='proposed', project='oyun', claim='Plan kısa.',
                        evidence=[dict(record_id='archive', source_sha256=archive['source_sha256'],
                                       quote='kısa tercih kaydı')])
        with self.assertRaisesRegex(ValueError, 'evidence_not_current_or_exact'):
            advisor.review_candidate(self.store, proposal, project='oyun')

    def test_rejected_matches_follow_the_same_scope(self):
        claim = 'Kullanıcı amber diyagramları tercih ediyor.'
        fields = dict(kind='inference', validity='rejected', rejected_reason='duzeltildi',
                      rejected_at='2026-09-24')
        self.note('shared-old', 'Companion/Cikarim.md', claim, **fields)
        self.note('archive-old', ARCHIVE + '/Cikarim.md', claim, **fields)
        self.scopes({'folders': {ARCHIVE: 'arsiv'}, 'shared_unscoped': True})
        found = [m['record_id'] for m in runtime.rejected_matches(self.store, claim, 'oyun')]
        self.assertEqual(found, ['shared-old'])


if __name__ == '__main__':
    unittest.main()
