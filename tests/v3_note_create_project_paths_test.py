"""Tests for #958: note-create allowed project/workspace paths and safety boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))

import beyin_v3_companion as companion
import beyin_v3_index as index_settings
import beyin_v3_projects as projects
import beyin_v3_workspaces as workspaces
from beyin_v3_sync import SyncEngine
from v3_package_helpers import inherited_env


class NoteCreateProjectPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-note-create-proj-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.engine = SyncEngine(self.vault, self.state)
        self.addCleanup(self.engine.store.close)

    def assert_no_journal(self):
        self.assertEqual(list((self.state / 'markdown-journal').glob('*')), [])

    def indexed_record(self, source):
        with self.engine.store._connect() as db:
            row = db.execute(
                'SELECT r.payload FROM records r JOIN markdown_sources m ON r.id=m.id WHERE m.source=?',
                (source,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def test_note_create_projects_folder_kurallar_succeeds_and_indexes(self):
        (self.vault / 'projects').mkdir()
        source = 'projects/kitchen/Kurallar.md'
        result = self.engine.note_create(source, '# Kitchen Rules\n- Keep counters clear.\n', {})
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['source'], source)
        self.assertTrue((self.vault / source).is_file())
        record = self.indexed_record(source)
        self.assertIsNotNone(record)
        self.assertEqual(record['source'], source)
        self.assertIn('Keep counters clear.', record['text'])
        self.assert_no_journal()

        # Existing file still cannot be overwritten by note-create.
        with self.assertRaisesRegex(ValueError, 'source exists'):
            self.engine.note_create(source, 'Overwrite attempt.\n', {})

    def test_note_create_project_kurallar_is_delivered_by_companion_context(self):
        comp_dir = self.vault / companion.DEFAULT_DIRECTORY
        comp_dir.mkdir(parents=True)
        (comp_dir / 'Core.md').write_text('# Core\nIdentity.\n', encoding='utf-8')
        (comp_dir / 'Kurallar.md').write_text('# Kurallar\n- Global rule.\n', encoding='utf-8')
        (comp_dir / 'Last-Session.md').write_text('## 2026-11-28 09:00 · init · abcd1234\nInit.\n', encoding='utf-8')
        (comp_dir / 'Threads.md').write_text('## Active Threads\n', encoding='utf-8')
        (comp_dir / 'Journal.md').write_text('# Journal\n', encoding='utf-8')
        (self.vault / 'projects').mkdir()

        created = self.engine.note_create(
            'projects/kitchen/Kurallar.md',
            '# Kitchen Rules\n- Kitchen specific rule marker.\n',
            {},
        )
        self.assertEqual(created['status'], 'succeeded')
        ctx = companion.context(self.engine.store, 8000, 'session-1', 'claude', project='kitchen')
        self.assertIn('[projects/kitchen/Kurallar.md]', ctx)
        self.assertIn('Kitchen specific rule marker.', ctx)

    def test_note_create_official_template_and_standard_project_folders_succeed(self):
        cases = (
            '🏰 300-Projects/kitchen/Kurallar.md',
            '🏰 300-Projects/kitchen/specs/checklist.md',
            '300-Projects/kitchen/Kurallar.md',
            'Projeler/kitchen/Kurallar.md',
        )
        for name in ('🏰 300-Projects', '300-Projects', 'Projeler'):
            (self.vault / name).mkdir()
        for index, source in enumerate(cases):
            with self.subTest(source=source):
                res = self.engine.note_create(
                    source,
                    f'Project rule body {index}.\n',
                    {'project': 'kitchen', 'kind': 'rule'},
                )
                self.assertEqual(res['status'], 'succeeded')
                self.assertEqual(res['source'], source)
                self.assertTrue((self.vault / source).is_file())
                record = self.indexed_record(source)
                self.assertIsNotNone(record)
                self.assertEqual(record['project'], 'kitchen')

    def test_note_create_discovered_project_parent_folder_succeeds_only_when_present(self):
        custom_parent = '🚀 200-Projeler'
        (self.vault / custom_parent).mkdir()
        source = f'{custom_parent}/kitchen/Kurallar.md'
        res = self.engine.note_create(source, 'Discovered project parent rule.\n', {})
        self.assertEqual(res['status'], 'succeeded')
        self.assertIsNotNone(self.indexed_record(source))

        # A non-standard project-like folder name that does not exist in the vault is rejected.
        missing_parent = 'ghost-projects/kitchen/Kurallar.md'
        with self.assertRaisesRegex(ValueError, 'note-edit'):
            self.engine.note_create(missing_parent, 'Should not be created.\n', {})
        self.assertFalse((self.vault / 'ghost-projects').exists())
        self.assert_no_journal()

    def test_note_create_inside_registered_workspace_source_folder_succeeds(self):
        (self.vault / 'areas/kitchen').mkdir(parents=True)
        workspaces.create_workspace(self.vault, 'kitchen', sources=['areas/kitchen'])

        for source in ('areas/kitchen/Kurallar.md', 'areas/kitchen/sub/design.md'):
            with self.subTest(source=source):
                res = self.engine.note_create(source, 'Workspace note body.\n', {'project': 'kitchen'})
                self.assertEqual(res['status'], 'succeeded')
                self.assertIsNotNone(self.indexed_record(source))

        # Path boundary must hold: sibling prefix and sibling file are rejected.
        for outside in ('areas/kitchen-extra/Kurallar.md', 'areas/kitchen.md'):
            with self.subTest(outside=outside):
                with self.assertRaisesRegex(ValueError, 'note-edit'):
                    self.engine.note_create(outside, 'Outside workspace.\n', {})
                self.assertFalse((self.vault / outside).exists())

    def test_note_create_inside_single_segment_registered_workspace_folder_succeeds(self):
        (self.vault / 'team').mkdir()
        workspaces.create_workspace(self.vault, 'team-ws', sources=['team'])

        res = self.engine.note_create('team/Kurallar.md', 'Team rules.\n', {})
        self.assertEqual(res['status'], 'succeeded')
        self.assertIsNotNone(self.indexed_record('team/Kurallar.md'))

    def test_note_create_inside_registered_workspace_note_roots_and_rules_file(self):
        workspaces.create_workspace(
            self.vault,
            'gamma',
            note_roots=['custom-notes/gamma'],
            rules_file='custom-rules/gamma/Kurallar.md',
        )

        res_note = self.engine.note_create('custom-notes/gamma/finding.md', 'Gamma finding.\n', {})
        self.assertEqual(res_note['status'], 'succeeded')
        self.assertIsNotNone(self.indexed_record('custom-notes/gamma/finding.md'))

        res_rules = self.engine.note_create('custom-rules/gamma/Kurallar.md', 'Gamma rules.\n', {})
        self.assertEqual(res_rules['status'], 'succeeded')
        self.assertIsNotNone(self.indexed_record('custom-rules/gamma/Kurallar.md'))

        # Arbitrary sibling beside rules_file is still not opened.
        with self.assertRaisesRegex(ValueError, 'note-edit'):
            self.engine.note_create('custom-rules/gamma/other.md', 'Not rules file.\n', {})
        self.assertFalse((self.vault / 'custom-rules/gamma/other.md').exists())

    def test_note_create_rejects_managed_and_reserved_paths(self):
        managed = (
            'tasks/t.md',
            'TASKS/t.md',
            'receipts/r.md',
            'RECEIPTS/r.md',
            'daily/v3/2026-11-28.md',
            'knowledge/v3/outcomes.md',
        )
        for source in managed:
            with self.subTest(source=source):
                with self.assertRaises(ValueError) as ctx:
                    self.engine.note_create(source, 'Reserved path.\n', {})
                msg = str(ctx.exception)
                self.assertIn('new semantic notes belong under', msg)
                self.assertNotIn('note-edit', msg)
                self.assertFalse((self.vault / source).exists())
        self.assert_no_journal()

    def test_note_create_rejects_non_semantic_human_paths_with_note_edit_hint(self):
        human_paths = (
            'daily/2026-11-28.md',
            'raw/clip.md',
            'readme.md',
            'projects/bare.md',
            '🏰 300-Projects/bare.md',
            '300-Projects/bare.md',
            'other/note.md',
            '🔮 850-Companion/Last-Session.md',
        )
        for source in human_paths:
            with self.subTest(source=source):
                with self.assertRaises(ValueError) as ctx:
                    self.engine.note_create(source, 'Human path note.\n', {})
                msg = str(ctx.exception)
                self.assertIn('new semantic notes belong under', msg)
                self.assertIn('note-edit', msg)
                self.assertIn('expected_sha256: null', msg)
                self.assertFalse((self.vault / source).exists())
        self.assert_no_journal()

    def test_note_create_rejects_non_markdown_extensions(self):
        for source in ('notes/plain.txt', 'projects/kitchen/Kurallar.txt'):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    self.engine.note_create(source, 'Plain text.\n', {})
                self.assertFalse((self.vault / source).exists())
        self.assert_no_journal()

    def test_note_create_rejects_dot_folders_and_path_traversal(self):
        bad = (
            '.agents/skills/foo.md',
            '.claude/note.md',
            'projects/.hidden/Kurallar.md',
            'projects/kitchen/.secret.md',
            '../escape.md',
            'projects/../escape.md',
            'projects/kitchen/../../escape.md',
        )
        for source in bad:
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    self.engine.note_create(source, 'Unsafe path.\n', {})
        self.assertEqual(list(self.vault.iterdir()), [])
        self.assert_no_journal()

    def test_note_create_rejects_sensitive_folders_even_under_projects_or_workspaces(self):
        (self.vault / '🔐 Kasa').mkdir()
        (self.vault / projects.PROJECT_SCOPES_FILE).write_text(
            json.dumps({'folders': {'🔐 Kasa': 'kasa'}, 'shared_unscoped': False}),
            encoding='utf-8',
        )
        sensitive = (
            '🔐 Kasa/secret.md',
            'projects/🔐 Kasa/Kurallar.md',
            'projects/kitchen/🔐 Kasa/secret.md',
            'projects/kitchen/Private/secret.md',
            'projects/kitchen/MÜŞTERİLER/client.md',
        )
        for source in sensitive:
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, 'sensitive directory'):
                    self.engine.note_create(source, 'Sensitive body.\n', {})
                self.assertFalse((self.vault / source).exists())
        self.assert_no_journal()

    def test_note_create_rejects_excluded_dirs_and_config_files_under_projects(self):
        index_settings.save(self.state, self.vault, add=['Arşiv'])
        with self.assertRaisesRegex(ValueError, 'excluded from the index'):
            self.engine.note_create('projects/kitchen/arşiv/old.md', 'Excluded note.\n', {})
        self.assertFalse((self.vault / 'projects').exists())

        index_settings.save(self.state, self.vault, add=['projects'])
        with self.assertRaisesRegex(ValueError, 'excluded from the index'):
            self.engine.note_create('projects/kitchen/Kurallar.md', 'Excluded parent.\n', {})
        self.assertFalse((self.vault / 'projects').exists())
        index_settings.save(self.state, self.vault, remove=['projects'])

        config_and_builtin_excluded = (
            'projects/kitchen/AGENTS.md',
            'projects/kitchen/CLAUDE.md',
            'projects/kitchen/setup-custom.md',
            'projects/kitchen/node_modules/pkg.md',
            'projects/kitchen/__pycache__/cache.md',
            'projects/kitchen/receipts/item.md',
        )
        for source in config_and_builtin_excluded:
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, 'excluded'):
                    self.engine.note_create(source, 'Excluded source.\n', {})
                self.assertFalse((self.vault / source).exists())
        self.assert_no_journal()

    def test_note_create_workspace_config_cannot_override_safety_or_break_standard_roots(self):
        # Malicious workspace registry entries must never bypass reserved/root/traversal gates.
        (self.vault / workspaces.WORKSPACES_FILE).write_text(
            json.dumps({
                'schema_version': 1,
                'workspaces': {
                    'evil': {
                        'id': 'evil',
                        'name': 'Evil',
                        'kind': 'general',
                        'lifecycle': 'active',
                        'rules_file': 'Kurallar.md',
                        'note_roots': ['tasks', 'receipts', 'daily', 'raw', '../outside', '.hidden'],
                    }
                },
            }),
            encoding='utf-8',
        )
        for source in ('Kurallar.md', 'tasks/x.md', 'receipts/x.md', 'daily/x.md', 'raw/x.md', '.hidden/x.md'):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    self.engine.note_create(source, 'Bypass attempt.\n', {})
                self.assertFalse((self.vault / source).exists())

        # Duplicate/conflicting or missing scope folders fail closed for note-create.
        (self.vault / 'areas/dup').mkdir(parents=True)
        (self.vault / workspaces.WORKSPACES_FILE).unlink()
        (self.vault / projects.PROJECT_SCOPES_FILE).write_text(
            json.dumps({
                'folders': {
                    'areas/dup': 'one',
                    'AREAS/DUP': 'two',
                    'areas/missing': 'ghost',
                },
                'shared_unscoped': False,
            }),
            encoding='utf-8',
        )
        for source in ('areas/dup/Kurallar.md', 'areas/missing/Kurallar.md'):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, 'note-edit'):
                    self.engine.note_create(source, 'Blocked scope.\n', {})
                self.assertFalse((self.vault / source).exists())

        # Corrupted workspace/scope files fail closed for custom folders without breaking notes/ or projects/.
        (self.vault / 'projects').mkdir()
        (self.vault / workspaces.WORKSPACES_FILE).write_text('{broken json', encoding='utf-8')
        (self.vault / projects.PROJECT_SCOPES_FILE).write_text('{broken json', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'note-edit'):
            self.engine.note_create('areas/kitchen/Kurallar.md', 'Should fail closed.\n', {})
        self.assertEqual(
            self.engine.note_create('notes/healthy.md', 'Still works.\n', {})['status'],
            'succeeded',
        )
        self.assertEqual(
            self.engine.note_create('projects/kitchen/Kurallar.md', 'Still works.\n', {})['status'],
            'succeeded',
        )

    def test_note_create_rejects_symlink_under_projects(self):
        outside = self.root / 'outside'
        outside.mkdir()
        inside = self.vault / 'real-inside'
        inside.mkdir()
        (self.vault / 'projects').mkdir()
        try:
            (self.vault / 'projects/linked').symlink_to(outside, target_is_directory=True)
            (self.vault / 'projects/linked-inside').symlink_to(inside, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, 'winerror', None) != 1314:  # ERROR_PRIVILEGE_NOT_HELD
                raise
            self.skipTest('symlink not permitted here: ' + str(exc))

        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.engine.note_create('projects/linked/Kurallar.md', 'Escape attempt.\n', {})
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.engine.note_create('projects/linked-inside/Kurallar.md', 'In-vault symlink.\n', {})
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(inside.iterdir()), [])
        self.assert_no_journal()

    @unittest.skipUnless(os.name == 'nt', 'Windows junction test')
    def test_note_create_rejects_windows_junction_under_projects(self):
        real = self.vault / 'real'
        real.mkdir()
        (self.vault / 'projects').mkdir()
        junction = self.vault / 'projects/junction'
        done = subprocess.run(
            ['cmd', '/c', 'mklink', '/J', str(junction), str(real)],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.addCleanup(lambda: os.rmdir(junction))

        with self.assertRaisesRegex(ValueError, 'symlink|junction'):
            self.engine.note_create('projects/junction/Kurallar.md', 'Junction escape.\n', {})
        self.assertEqual(list(real.iterdir()), [])
        self.assert_no_journal()

        # Even a Windows junction named like a discovered '-Projects' root folder must be rejected.
        root_junction = self.vault / 'Evil-Projects'
        done_root = subprocess.run(
            ['cmd', '/c', 'mklink', '/J', str(root_junction), str(real)],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(done_root.returncode, 0, done_root.stderr)
        self.addCleanup(lambda: os.rmdir(root_junction))
        with self.assertRaisesRegex(ValueError, 'symlink|junction'):
            self.engine.note_create('Evil-Projects/kitchen/Kurallar.md', 'Root junction escape.\n', {})
        self.assertEqual(list(real.iterdir()), [])
        self.assert_no_journal()

    def test_note_create_cli_project_kurallar_and_helpful_rejection(self):
        (self.vault / 'projects').mkdir()
        env = inherited_env(BEYIN_UPDATES_OFF='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        cmd = [
            sys.executable,
            str(ROOT / 'scripts/beyin_v3.py'),
            '--vault', str(self.vault),
            '--state', str(self.state),
            '--json',
            'note-create',
            '--file', '-',
        ]
        ok = subprocess.run(
            cmd,
            input=json.dumps({
                'source': 'projects/kitchen/Kurallar.md',
                'text': '# Kitchen Rules\n- Label dates.\n',
                'metadata': {},
            }),
            text=True,
            encoding='utf-8',
            capture_output=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        out = json.loads(ok.stdout)
        self.assertEqual(out['status'], 'succeeded')
        self.assertEqual(out['source'], 'projects/kitchen/Kurallar.md')

        rejected = subprocess.run(
            cmd,
            input=json.dumps({
                'source': 'daily/2026-11-28.md',
                'text': 'Daily entry.\n',
                'metadata': {},
            }),
            text=True,
            encoding='utf-8',
            capture_output=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(rejected.returncode, 1, rejected.stdout)
        err = json.loads(rejected.stderr)
        self.assertEqual(err['error'], 'ValueError')
        self.assertIn('note-edit', err['message'])
        self.assertIn('expected_sha256: null', err['message'])

    def test_note_create_needs_an_existing_project_parent(self):
        # One project, one folder: note-create opens no second project root (managed AGENTS block).
        with self.assertRaises(ValueError) as caught:
            self.engine.note_create('projects/kitchen/Kurallar.md', 'Fresh vault.\n', {})
        self.assertIn("project parent folder 'projects' does not exist", str(caught.exception))
        self.assertIn('note-edit (expected_sha256: null and content)', str(caught.exception))
        self.assertEqual(list(self.vault.iterdir()), [])

        (self.vault / '🏰 300-Projects').mkdir()
        with self.assertRaises(ValueError) as caught:
            self.engine.note_create('projects/kitchen/Kurallar.md', 'Second root.\n', {})
        self.assertIn("use the existing project parent (🏰 300-Projects)", str(caught.exception))
        self.assertFalse((self.vault / 'projects').exists())
        self.assertEqual(
            self.engine.note_create('🏰 300-Projects/kitchen/Kurallar.md', 'Rules.\n', {})['status'],
            'succeeded')
        self.assert_no_journal()

    def test_note_create_refuses_another_spelling_of_an_existing_folder(self):
        # Project and workspace rules match case-insensitively; the write must not. On macOS and
        # Windows the alias used to land in the existing folder and then fail its readback.
        (self.vault / 'Projects' / 'Kitchen').mkdir(parents=True)
        (self.vault / 'Work').mkdir()
        workspaces.create_workspace(self.vault, 'work', note_roots=['Work/Notes'],
                                    rules_file='Work/Kurallar.md')
        (self.vault / 'Work' / 'Kurallar.md').write_text('# Work rules\n', encoding='utf-8', newline='\n')
        cases = (
            ('projects/kitchen/Kurallar.md', "'Projects' already exists"),
            ('Projects/kitchen/Kurallar.md', "'Projects/Kitchen' already exists"),
            ('work/Notes/a.md', "'Work' already exists"),
            ('Work/kurallar.md', "'Work/Kurallar.md' already exists"),
        )
        before = sorted(path.relative_to(self.vault).as_posix() for path in self.vault.rglob('*'))
        for source, message in cases:
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, message + ' with another spelling'):
                    self.engine.note_create(source, 'Alias body.\n', {})
        self.assertEqual(sorted(path.relative_to(self.vault).as_posix() for path in self.vault.rglob('*')),
                         before)
        self.assert_no_journal()
        self.assertEqual(
            self.engine.note_create('Projects/Kitchen/Kurallar.md', 'Rules.\n', {})['status'], 'succeeded')
        self.assertEqual(self.engine.note_create('Work/Notes/a.md', 'Note.\n', {})['status'], 'succeeded')

    def test_note_create_refuses_symlinked_target_file(self):
        (self.vault / 'projects' / 'kitchen').mkdir(parents=True)
        outside = self.root / 'outside.md'
        try:
            (self.vault / 'projects/kitchen/Kurallar.md').symlink_to(outside)
        except OSError as exc:
            if getattr(exc, 'winerror', None) != 1314:  # ERROR_PRIVILEGE_NOT_HELD
                raise
            self.skipTest('symlink not permitted here: ' + str(exc))
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.engine.note_create('projects/kitchen/Kurallar.md', 'Through a dangling link.\n', {})
        self.assertFalse(outside.exists())
        self.assert_no_journal()

    def test_spelling_check_never_lists_a_linked_folder(self):
        # The spelling check lists folders along the path; a link is refused before any listing,
        # so an entry name from outside the vault never reaches the error message.
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'Private-Plan.md').write_text('outside\n', encoding='utf-8', newline='\n')
        (self.vault / 'projects').mkdir()
        try:
            (self.vault / 'projects/linked').symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, 'winerror', None) != 1314:  # ERROR_PRIVILEGE_NOT_HELD
                raise
            self.skipTest('symlink not permitted here: ' + str(exc))
        for source in ('projects/linked/private-plan.md', 'projects/linked/sub/Kurallar.md'):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, 'symlink') as caught:
                    self.engine.note_create(source, 'Body.\n', {})
                self.assertNotIn('Private-Plan', str(caught.exception))
        self.assertEqual([path.name for path in outside.iterdir()], ['Private-Plan.md'])
        self.assert_no_journal()

    def test_existing_notes_and_knowledge_roots_remain_allowed(self):
        for source in ('notes/plain.md', 'notes/sub/item.md', 'knowledge/concept.md', 'knowledge/sub/item.md'):
            with self.subTest(source=source):
                res = self.engine.note_create(source, 'Semantic body.\n', {})
                self.assertEqual(res['status'], 'succeeded')
                self.assertIsNotNone(self.indexed_record(source))


if __name__ == '__main__':
    unittest.main()
