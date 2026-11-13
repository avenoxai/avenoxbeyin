"""Regression gates through source and freshly installed CLI, using fictional notes."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_homepages import CELL
from beyin_v3_notes import NotePartialWrite
from beyin_v3_sync import SyncEngine, parse, render
from v3_package_helpers import install, isolated_env

UNTRUSTED = ({'trust': 'untrusted'}, {'trusted': False},
             {'status': 'untrusted'}, {'kind': 'untrusted'})


class HomepageBoundariesTest(unittest.TestCase):
    installed = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()
        self.env = isolated_env(self.root / 'home')
        self.page = 'notes/home/Page.md'
        self.source = 'notes/source/Record.md'
        for folder in ('notes/home', 'notes/source'):
            (self.vault / folder).mkdir(parents=True)
        if self.installed:
            run = install(self.vault, self.state, self.env)
            self.assertEqual(run.returncode, 0, run.stderr)
        self.reset()

    def reset(self, project='chosen'):
        for source in (self.page, self.source):
            (self.vault / source).unlink(missing_ok=True)
        (self.vault / '.beyin-projects.json').unlink(missing_ok=True)
        metadata = {'id': 'record', 'kind': 'note', 'facts': {'value': 'First'}}
        if project is not None:
            metadata['project'] = project
        self.write(self.source, metadata, '# Record\n')
        self.request = dict(source=self.page, expected_sha256=None, title='My page',
                            project=project, selections=[dict(source=self.source, fields=[dict(
                                field='facts.value', label='Value', editable=True)])])

    def write(self, source, metadata, body):
        (self.vault / source).write_text(render(metadata, body), encoding='utf-8')

    def change(self, source, **changes):
        metadata, body = parse((self.vault / source).read_text(encoding='utf-8'))
        self.write(source, dict(metadata, **changes), body)

    def cli(self, *args, ok=True):
        script = self.vault / 'beyin.py' if self.installed else ROOT / 'scripts/beyin_v3.py'
        run = subprocess.run([sys.executable, str(script), '--vault', str(self.vault),
                              '--state', str(self.state), '--json', *args], env=self.env,
                             capture_output=True, text=True, encoding='utf-8', timeout=40)
        self.assertEqual(run.returncode == 0, ok, run.stdout + run.stderr)
        return json.loads(run.stdout)

    def create(self, ok=True):
        path = self.root / 'create.json'
        path.write_text(json.dumps(self.request), encoding='utf-8')
        return self.cli('homepage-create', '--file', str(path), ok=ok)

    def snapshot(self):
        return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.vault / 'notes').rglob('*.md')}

    def scopes(self, folders):
        (self.vault / '.beyin-projects.json').write_text(json.dumps(
            dict(folders=folders, shared_unscoped=True)), encoding='utf-8')

    def unscoped_existing(self):
        # Empty project fields are valid legacy metadata and defer to folders.
        # This also reaches replay on the old runtime, whose new project:null
        # page failed indexing before any binding could be exercised.
        self.reset(project='')
        self.write(self.page, {'project': ''}, '# My page\nMy personal note.\n')
        self.request.pop('project')
        self.request['expected_sha256'] = hashlib.sha256((self.vault / self.page).read_bytes()).hexdigest()

    def deny_flow(self, side, metadata=None, folders=None):
        self.unscoped_existing() if folders else self.reset()
        self.create()
        if metadata:
            self.change(side, **metadata)
        if folders:
            self.scopes(folders)
        # Both directions are pending; neither may cross an invalid boundary.
        self.change(self.source, facts={'value': 'Remote'})
        home = self.vault / self.page
        home.write_text(home.read_text(encoding='utf-8').replace('-->First<!--', '-->Local<!--'),
                        encoding='utf-8')
        before = self.snapshot()
        self.create(ok=False)
        self.assertEqual(before, self.snapshot())
        self.cli('sync')
        self.assertEqual(before, self.snapshot())
        # Also isolate each direction: a divergent value alone would be a normal conflict.
        home.write_text(home.read_text(encoding='utf-8').replace('-->Local<!--', '-->First<!--'),
                        encoding='utf-8')
        before = self.snapshot()
        self.cli('sync')
        self.assertEqual(before, self.snapshot())
        self.change(self.source, facts={'value': 'First'})
        home.write_text(home.read_text(encoding='utf-8').replace('-->First<!--', '-->Local<!--'),
                        encoding='utf-8')
        before = self.snapshot()
        self.cli('sync')
        self.assertEqual(before, self.snapshot())

    def test_all_source_trust_markers_reject_creation(self):
        for marker in UNTRUSTED:
            with self.subTest(marker=marker):
                self.reset()
                self.change(self.source, **marker)
                before = self.snapshot()
                self.create(ok=False)
                self.assertEqual(before, self.snapshot())

    def test_all_home_trust_markers_reject_attachment(self):
        for marker in UNTRUSTED:
            with self.subTest(marker=marker):
                self.reset()
                self.write(self.page, marker, '# My original heading\nMy note.\n')
                self.request['expected_sha256'] = hashlib.sha256((self.vault / self.page).read_bytes()).hexdigest()
                before = self.snapshot()
                self.create(ok=False)
                self.assertEqual(before, self.snapshot())

    def test_all_trust_markers_reject_replay_and_both_sync_directions(self):
        for side in (self.source, self.page):
            for marker in UNTRUSTED:
                with self.subTest(side=side, marker=marker):
                    self.deny_flow(side, metadata=marker)

    def test_different_folder_projects_reject_creation(self):
        self.unscoped_existing()
        self.scopes({'notes/home': 'alpha', 'notes/source': 'beta'})
        before = self.snapshot()
        self.create(ok=False)
        self.assertEqual(before, self.snapshot())

    def test_changed_folder_scope_rejects_replay_and_both_sync_directions(self):
        self.deny_flow(self.source, folders={'notes/home': 'alpha', 'notes/source': 'beta'})

    def test_blocked_home_and_source_reject_creation_and_existing_bindings(self):
        for folder in ('notes/home', 'notes/source'):
            folders = {'notes': 'alpha', folder: 'alpha', folder + '/': 'beta'}
            with self.subTest(folder=folder, phase='create'):
                self.unscoped_existing()
                self.scopes(folders)
                before = self.snapshot()
                self.create(ok=False)
                self.assertEqual(before, self.snapshot())
            with self.subTest(folder=folder, phase='existing'):
                self.deny_flow(self.source, folders=folders)

    def test_equal_effective_scope_and_explicit_project_precedence(self):
        for explicit in (None, 'chosen'):
            with self.subTest(explicit=explicit):
                self.reset(project=explicit)
                self.scopes({'notes/home': 'alpha', 'notes/source': 'alpha' if explicit is None else 'beta'})
                self.create()
                before = self.snapshot()
                self.assertEqual(self.create()['status'], 'unchanged')
                self.assertEqual(before, self.snapshot())
                self.change(self.source, facts={'value': 'Remote'})
                self.cli('sync')
                home = self.vault / self.page
                self.assertIn('-->Remote<!--', home.read_text(encoding='utf-8'))
                home.write_text(home.read_text(encoding='utf-8').replace('-->Remote<!--', '-->Local<!--'), encoding='utf-8')
                self.cli('sync')
                metadata, _ = parse((self.vault / self.source).read_text(encoding='utf-8'))
                self.assertEqual(metadata['facts']['value'], 'Local')

    def test_keyed_home_edit_replays_final_hash_without_writes_and_detects_user_change(self):
        self.create()
        home = self.vault / self.page
        raw = home.read_bytes()
        first = CELL.search(raw.decode('utf-8'))[0]
        request = dict(source=self.page, expected_sha256=hashlib.sha256(raw).hexdigest(),
                       idempotency_key='homepage-edit', edits=[dict(old=first,
                           new=first.replace('-->First<!--', '-->Next<!--'))])
        path = self.root / 'edit.json'
        path.write_text(json.dumps(request), encoding='utf-8')
        answer = self.cli('note-edit', '--file', str(path))
        final_hash = hashlib.sha256(home.read_bytes()).hexdigest()
        self.assertEqual(answer['sha256'], final_hash)
        before = self.snapshot()
        replay = self.cli('note-edit', '--file', str(path))
        self.assertTrue(replay['idempotent'])
        self.assertEqual(replay['sha256'], final_hash)
        self.assertEqual(before, self.snapshot())
        home.write_text(home.read_text(encoding='utf-8') + '\nMy newer personal note.\n', encoding='utf-8')
        before = self.snapshot()
        self.cli('note-edit', '--file', str(path), ok=False)
        self.assertEqual(before, self.snapshot())

    def test_explicit_source_matches_folder_home_and_deeper_valid_scope_overrides_block(self):
        self.reset(project=None)
        self.change(self.source, project='alpha')
        self.scopes({'notes/home': 'alpha', 'notes/source': 'beta'})
        self.create()
        self.reset(project=None)
        nested = 'notes/source/Nested/Record.md'
        (self.vault / nested).parent.mkdir()
        (self.vault / self.source).rename(self.vault / nested)
        self.source = nested
        self.request['selections'][0]['source'] = nested
        self.scopes({'notes/home': 'alpha', 'notes/source': 'alpha',
                     'notes/source/': 'beta', 'notes/source/Nested': 'alpha'})
        self.create()
        before = self.snapshot()
        self.assertEqual(self.create()['status'], 'unchanged')
        self.cli('sync')
        self.assertEqual(before, self.snapshot())


class InstalledHomepageBoundariesTest(HomepageBoundariesTest):
    installed = True


class HomepageFinalizationTest(unittest.TestCase):
    def test_keyed_retry_recovers_interruption_after_cell_refresh(self):
        fixture = HomepageBoundariesTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.create()
        engine = SyncEngine(fixture.vault, fixture.state)
        home = fixture.vault / fixture.page
        raw = home.read_bytes()
        first = CELL.search(raw.decode('utf-8'))[0]
        request = dict(source=fixture.page, expected_sha256=hashlib.sha256(raw).hexdigest(),
                       idempotency_key='interrupted-home-edit', edits=[dict(old=first,
                           new=first.replace('-->First<!--', '-->Next<!--'))])
        refresh = engine._refresh_homepages

        def interrupt_after_refresh(result, **kwargs):
            refresh(result, **kwargs)
            raise RuntimeError('interrupted after verified cell refresh')

        with patch.object(engine, '_refresh_homepages', interrupt_after_refresh):
            with self.assertRaises(NotePartialWrite):
                engine.note_edit(request)
        before = fixture.snapshot()
        answer = engine.note_edit(request)
        self.assertTrue(answer['idempotent'])
        self.assertEqual(answer['sha256'], hashlib.sha256(home.read_bytes()).hexdigest())
        self.assertEqual(before, fixture.snapshot())

    def test_external_edit_after_refresh_is_not_accepted_as_completion(self):
        fixture = HomepageBoundariesTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.create()
        engine = SyncEngine(fixture.vault, fixture.state)
        home = fixture.vault / fixture.page
        raw = home.read_bytes()
        first = CELL.search(raw.decode('utf-8'))[0]
        request = dict(source=fixture.page, expected_sha256=hashlib.sha256(raw).hexdigest(),
                       idempotency_key='concurrent-home-edit', edits=[dict(old=first,
                           new=first.replace('-->First<!--', '-->Next<!--'))])
        refresh = engine._refresh_homepages

        def user_edit_after_refresh(result, **kwargs):
            refresh(result, **kwargs)
            home.write_bytes(home.read_bytes() + b'\nMy latest personal note.\n')

        with patch.object(engine, '_refresh_homepages', user_edit_after_refresh):
            with self.assertRaises(NotePartialWrite):
                engine.note_edit(request)
        before = fixture.snapshot()
        from beyin_v3_notes import NoteConflict
        with self.assertRaises(NoteConflict):
            engine.note_edit(request)
        self.assertEqual(before, fixture.snapshot())


if __name__ == '__main__':
    unittest.main()
