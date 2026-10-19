"""Opt-in folder project scopes (.beyin-projects.json) for explicit-project gates."""
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unicodedata
import unittest
from unittest.mock import patch
from v3_package_helpers import clean_environ, inherited_env

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
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
        client = unicodedata.normalize('NFD', 'Projeler/Paylaşım X')
        self.note('client', client + '/Teklif.md')
        self.note('rules', 'Companion/Kurallar.md')
        self.scopes({'folders': {'projeler/paylaşım x': 'musteri'}, 'shared_unscoped': True})
        self.assertEqual(self.ids('musteri'), ['client', 'rules'])
        self.assertEqual(self.ids('oyun'), ['rules'])

    def test_missing_or_renamed_folder_is_skipped_and_warns(self):
        # A renamed or deleted project folder is skipped with a warning; other
        # projects in the file remain fully functional and do not crash with ValueError (#317).
        self.note('archive', 'Projeler/Arşiv 2025/Plan.md')
        self.note('game', GAME + '/Plan.md')
        self.scopes({'folders': {ARCHIVE: 'arsiv', GAME: 'oyun'}, 'shared_unscoped': False})
        # ARCHIVE folder doesn't exist, but 'oyun' folder exists: 'oyun' continues to work!
        self.assertEqual(self.ids('oyun'), ['game'])
        # Querying the missing project returns empty folder scope rather than crashing
        self.assertEqual(self.ids('arsiv'), [])
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

    def test_missing_folder_keeps_stale_index_rows_out_of_shared_scope(self):
        note = self.note('private-project', ARCHIVE + '/Plan.md')
        self.note('game', GAME + '/Plan.md')
        self.scopes({'folders': {ARCHIVE: 'arsiv', GAME: 'oyun'}, 'shared_unscoped': True})
        (self.vault / ARCHIVE / 'Plan.md').unlink()
        (self.vault / ARCHIVE).rmdir()
        # Exercise actual persisted rows with the deleted folder, as --no-sync does.
        self.assertEqual(self.ids('oyun'), ['game'])
        import beyin_v3_projects as projects
        self.assertIn((runtime._scope_key(ARCHIVE), 'arsiv'), projects.read_project_scopes(self.vault)['folders'])

    def test_management_preserves_invalid_or_symlinked_user_configuration(self):
        import beyin_v3_projects as projects
        (self.vault / 'Projeler/Alpha').mkdir(parents=True)
        path = self.vault / runtime.PROJECT_SCOPES_FILE
        for raw in ('[]', '{"folders": []}', '{"shared_unscoped": "yes"}'):
            path.write_text(raw, encoding='utf-8')
            for action in ('add', 'remove', 'prune'):
                with self.assertRaises(ValueError):
                    projects.manage(self.vault, action, folder='Projeler/Alpha', project='alpha')
                self.assertEqual(path.read_text(encoding='utf-8'), raw)
        path.unlink()
        outside = self.vault.parent / 'outside-scopes.json'
        outside.write_text('{"folders": {}}', encoding='utf-8')
        try:
            path.symlink_to(outside)
        except OSError:
            return
        with self.assertRaises(ValueError):
            projects.manage(self.vault, 'add', folder='Projeler/Alpha', project='alpha')
        self.assertTrue(path.is_symlink())
        self.assertEqual(outside.read_text(encoding='utf-8'), '{"folders": {}}')

    def test_cli_scope_list_does_not_initialize_runtime(self):
        state = self.vault.parent / 'uninitialized-runtime'
        before = sorted(path.relative_to(self.vault).as_posix() for path in self.vault.rglob('*'))
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(state), 'project-scope', 'list'], env=inherited_env(),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'absent')
        self.assertFalse(state.exists())
        self.assertEqual(before, sorted(path.relative_to(self.vault).as_posix() for path in self.vault.rglob('*')))

    def test_invalid_files_fail_closed_for_explicit_projects(self):
        self.vault_notes()
        for bad in ('{broken', '[]', {'folders': [], 'shared_unscoped': False},
                    {'folders': {GAME: 'oyun'}, 'shared_unscoped': 'yes'},
                    {'folders': {GAME: ''}}, {'folders': {GAME: 3}},
                    {'extra': True},
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

    def test_doctor_reports_project_scopes_health(self):
        import beyin_v3_projects as projects
        # 1. Absent
        scopes_file = self.vault / runtime.PROJECT_SCOPES_FILE
        if scopes_file.exists():
            scopes_file.unlink()
        report = projects.check_project_scopes(self.vault)
        self.assertEqual(report['status'], 'absent')

        # 2. OK (all configured folders exist)
        (self.vault / 'Projeler/A').mkdir(parents=True, exist_ok=True)
        (self.vault / 'Projeler/B').mkdir(parents=True, exist_ok=True)
        self.scopes({'folders': {'Projeler/A': 'a', 'Projeler/B': 'b'}, 'shared_unscoped': True})
        report = projects.check_project_scopes(self.vault)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['missing_folders'], [])

        # 3. Warning (missing folder)
        import shutil
        shutil.rmtree(self.vault / 'Projeler/B')
        report = projects.check_project_scopes(self.vault)
        self.assertEqual(report['status'], 'warning')
        self.assertEqual(report['missing_folders'], ['Projeler/B'])

        # 4. Error (duplicate folder assignment)
        self.scopes({'folders': {'Projeler/A': 'a', 'projeler/a': 'other'}})
        report = projects.check_project_scopes(self.vault)
        self.assertEqual(report['status'], 'error')
        self.assertTrue(report['duplicate_folders'])

    def test_project_scope_cli_manage_actions(self):
        import beyin_v3_projects as projects
        (self.vault / 'Projeler/Alpha').mkdir(parents=True, exist_ok=True)
        (self.vault / 'Projeler/Beta').mkdir(parents=True, exist_ok=True)

        # 1. Add
        res = projects.manage(self.vault, 'add', folder='Projeler/Alpha', project='alpha', shared_unscoped='on')
        self.assertEqual(res['status'], 'saved')
        self.assertEqual(res['folders'], {'Projeler/Alpha': 'alpha'})
        self.assertTrue(res['shared_unscoped'])

        # Add second folder
        projects.manage(self.vault, 'add', folder='Projeler/Beta', project='beta')

        # 2. List
        listed = projects.manage(self.vault, 'list')
        self.assertEqual(listed['status'], 'ok')
        self.assertIn('Projeler/Alpha', listed['folders'])
        self.assertIn('Projeler/Beta', listed['folders'])

        # 3. Prune (remove Beta from disk, prune should remove it from .beyin-projects.json)
        import shutil
        shutil.rmtree(self.vault / 'Projeler/Beta')
        pruned = projects.manage(self.vault, 'prune')
        self.assertEqual(pruned['status'], 'saved')
        self.assertEqual(pruned['pruned'], ['Projeler/Beta'])
        self.assertEqual(pruned['folders'], {'Projeler/Alpha': 'alpha'})

        # 4. Remove
        removed = projects.manage(self.vault, 'remove', folder='Projeler/Alpha')
        self.assertEqual(removed['status'], 'saved')
        self.assertEqual(removed['folders'], {})

    def test_utf8_bom_project_scopes_resilience(self):
        """Scope files and JSON files starting with UTF-8 BOM are loaded cleanly without JSONDecodeError."""
        import importlib.util
        import beyin_v3_projects as projects
        scopes_file = self.vault / runtime.PROJECT_SCOPES_FILE
        (self.vault / 'Projeler/Alpha').mkdir(parents=True, exist_ok=True)
        raw = '\ufeff{\n  "folders": {"Projeler/Alpha": "alpha"},\n  "shared_unscoped": false\n}'
        scopes_file.write_bytes(raw.encode('utf-8'))

        read = projects.read_project_scopes(self.vault)
        self.assertEqual(read['folders'], [('projeler/alpha', 'alpha')])

        checked = projects.check_project_scopes(self.vault)
        self.assertEqual(checked['status'], 'ok')
        self.assertEqual(checked['folders'], {'Projeler/Alpha': 'alpha'})

        listed = projects.manage(self.vault, 'list')
        self.assertEqual(listed['status'], 'ok')
        self.assertEqual(listed['folders'], {'Projeler/Alpha': 'alpha'})

        (self.vault / 'Projeler/Gamma').mkdir(parents=True, exist_ok=True)
        added = projects.manage(self.vault, 'add', folder='Projeler/Gamma', project='gamma')
        self.assertEqual(added['status'], 'saved')
        self.assertIn('Projeler/Gamma', added['folders'])

        spec = importlib.util.spec_from_file_location('beyin_v3_cli', ROOT / 'scripts' / 'beyin_v3.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        json_file = self.vault / 'bom_sample.json'
        json_file.write_bytes('\ufeff{"hello": "world"}'.encode('utf-8'))
        self.assertEqual(cli.read_json(str(json_file)), {'hello': 'world'})

    def test_prune_recovers_from_invalid_and_outside_folder_paths(self):
        """Invalid or outside-the-vault folder paths do not deadlock project-scope prune."""
        import beyin_v3_projects as projects
        scopes_file = self.vault / runtime.PROJECT_SCOPES_FILE
        (self.vault / 'Projeler/Alpha').mkdir(parents=True, exist_ok=True)
        raw = json.dumps({
            'folders': {
                'Projeler/Alpha': 'alpha',
                '../outside': 'bad_scope',
                '/absolute/path': 'bad_abs'
            },
            'shared_unscoped': False
        }, indent=2)
        scopes_file.write_text(raw, encoding='utf-8')

        # Any normal manage action (like add) fails on invalid paths
        with self.assertRaises(ValueError):
            projects.manage(self.vault, 'add', folder='Projeler/Beta', project='beta')

        # But prune succeeds and removes the invalid folder keys
        pruned = projects.manage(self.vault, 'prune')
        self.assertEqual(pruned['status'], 'saved')
        self.assertIn('../outside', pruned['pruned'])
        self.assertIn('/absolute/path', pruned['pruned'])
        self.assertEqual(pruned['folders'], {'Projeler/Alpha': 'alpha'})

        # Now that invalid paths are pruned, subsequent manage actions work
        (self.vault / 'Projeler/Beta').mkdir(parents=True, exist_ok=True)
        added = projects.manage(self.vault, 'add', folder='Projeler/Beta', project='beta')
        self.assertEqual(added['status'], 'saved')
        self.assertIn('Projeler/Beta', added['folders'])


if __name__ == '__main__':
    unittest.main()
