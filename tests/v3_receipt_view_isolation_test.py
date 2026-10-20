"""Receipt projections preserve local bytes without blocking healthy sources."""
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env
from v3_sync_test import load_module

ROOT = Path(__file__).resolve().parents[1]
subject = load_module()


class ReceiptViewIsolationTest(unittest.TestCase):
    def setUp(self):
        # load_module() imports SyncEngine by path, then removes its temporary
        # path. Installed clients retain this directory for lazy policy imports.
        scripts = str(ROOT / 'template/.claude/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-view-isolation-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'vault'
        self.state = self.root / 'state'
        self.write('notes/task.md', subject.render(
            {'id': 'calibration-task', 'kind': 'task', 'revision': 1, 'status': 'active'},
            'Nebula calibration SYNTHETIC_HEALTHY_CANARY.\n'))
        self.engine = subject.SyncEngine(self.vault, self.state)
        self.engine.sync()
        self.view = self.vault / 'knowledge/v3/outcomes.md'

    def write(self, source, text):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
        return path

    def seed_view(self):
        return self.engine.receipt('initial', 'Initial synthetic outcome.', ['notes/task.md'], 'codex')

    def damage_view(self):
        self.seed_view()
        self.view.write_bytes(b'Preserved local view edit.\r\n')
        return self.view.read_bytes()

    def incoming(self, event_id):
        source = 'receipts/' + hashlib.sha256(event_id.encode()).hexdigest() + '.md'
        self.write(source, subject.render(
            {'kind': 'receipt', 'event_id': event_id, 'harness': 'codex',
             'refs': ['notes/task.md'], 'visibility': 'internal',
             'created_at': '2026-09-28T12:00:00+00:00'}, 'Incoming synthetic outcome.\n'))
        return source

    def records(self):
        with self.engine.store._connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM records')]

    def assert_view_warning(self, result):
        self.assertEqual(result['status'], 'degraded', result)
        self.assertEqual(result.get('conflicts', []), [])
        warnings = [w for w in result['warnings'] if w.get('kind') == 'receipt_view']
        self.assertTrue(warnings, result)
        return warnings

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
             '--state', str(self.engine.state), *args], capture_output=True, text=True,
            encoding='utf-8', env=inherited_env(), timeout=20)

    def test_edited_view_is_preserved_with_typed_warning_and_healthy_context(self):
        original = self.damage_view()
        result = self.engine.sync()
        self.assert_view_warning(result)
        self.assertEqual(self.view.read_bytes(), original)
        context = self.engine.store.context_for('codex', 'Nebula calibration', strict=True)
        self.assertEqual([record['id'] for record in context['records']], ['calibration-task'])
        self.assertEqual([record['source'] for record in self.records()], ['notes/task.md'])

    def test_fresh_state_with_incoming_receipt_preserves_untracked_view(self):
        self.seed_view()
        original = self.view.read_bytes()
        self.incoming('remote')
        self.engine = subject.SyncEngine(self.vault, self.root / 'fresh-state')
        warnings = self.assert_view_warning(self.engine.sync())
        warning = next(w for w in warnings if w['source'] == 'knowledge/v3/outcomes.md')
        self.assertIn('untracked', warning['reason'])
        self.assertNotIn('manual', warning['reason'])
        self.assertEqual(self.view.read_bytes(), original)
        self.assertEqual([r['source'] for r in self.records()], ['notes/task.md'])

    def test_stripped_private_and_untrusted_views_never_become_context(self):
        for label, metadata in (('private', {'visibility': 'private'}),
                                ('untrusted', {'trust': 'untrusted'})):
            ref = 'notes/' + label + '.md'
            self.write(ref, subject.render(dict(id=label, **metadata), label + ' reference.\n'))
            self.engine.sync()
            self.engine.receipt(label, 'Nebula calibration HIDDEN_' + label, [ref], 'codex')
        views = list((self.vault / 'daily/v3').glob('*.md')) + [self.view]
        preserved = {}
        for path in views:
            path.write_text(subject.parse(path.read_text(encoding='utf-8'))[1], encoding='utf-8')
            preserved[path] = path.read_bytes()
        for fresh in (False, True):
            with self.subTest(fresh=fresh):
                if fresh:
                    self.engine = subject.SyncEngine(self.vault, self.root / 'fresh-state')
                report = self.engine.sync()
                self.assertFalse(any(r['source'].startswith(('knowledge/v3/', 'daily/v3/'))
                                     for r in self.records()))
                self.assert_view_warning(report)
                context = self.engine.store.context_for('codex', 'Nebula calibration', strict=True)
                self.assertEqual([r['id'] for r in context['records']], ['calibration-task'])
                self.assertNotIn('HIDDEN_', json.dumps(context))
                cli = self.cli('context', 'Nebula calibration')
                self.assertEqual(cli.returncode, 0, cli.stderr)
                self.assertIn('SYNTHETIC_HEALTHY_CANARY', cli.stdout)
                self.assertNotIn('HIDDEN_', cli.stdout)
                hook = subprocess.run([sys.executable, str(ROOT / 'template/.claude/scripts/beyin_v3_hook.py'),
                    '--vault', str(self.vault), '--state', str(self.engine.state), '--harness', 'codex'],
                    input=json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': 'stripped-' + str(fresh),
                                      'cwd': str(self.vault), 'prompt': 'Nebula calibration'}),
                    text=True, capture_output=True, env=inherited_env(), timeout=20)
                self.assertEqual(hook.returncode, 0, hook.stderr)
                self.assertIn('SYNTHETIC_HEALTHY_CANARY', hook.stdout)
                self.assertNotIn('HIDDEN_', hook.stdout)
                for path, original in preserved.items():
                    self.assertEqual(path.read_bytes(), original)

    def test_plain_user_note_at_view_path_is_indexed_until_receipts_claim_it(self):
        path = self.write('knowledge/v3/outcomes.md', 'Nebula calibration ORDINARY_CANARY.\n')
        old_ns = time.time_ns() - 10_000_000_000
        os.utime(path, ns=(old_ns, old_ns))
        self.engine.sync()
        with patch.object(subject.time, 'time_ns', return_value=time.time_ns() + 10_000_000_000):
            self.engine.sync()
        self.assertIn('knowledge/v3/outcomes.md', [r['source'] for r in self.records()])
        original = path.read_bytes()
        with self.engine.store._connect() as db:
            signature = db.execute("SELECT payload_sha256 FROM source_signatures WHERE source=?",
                                   ('knowledge/v3/outcomes.md',)).fetchone()
            self.assertTrue(signature[0])
            db.execute('DROP TABLE receipt_views')
        self.incoming('new-owner')
        report = self.engine.sync()
        self.assertNotIn('knowledge/v3/outcomes.md', [r['source'] for r in self.records()])
        self.assert_view_warning(report)
        self.assertEqual(path.read_bytes(), original)
        with self.engine.store._connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM source_signatures WHERE source=?',
                                        ('knowledge/v3/outcomes.md',)).fetchone())

    def test_another_spelling_of_the_same_view_is_not_a_source(self):
        original = self.damage_view()
        alias = self.vault / 'knowledge/v3/OUTCOMES.md'
        # A hard link exercises the same file identity on a case-sensitive CI volume.
        if not alias.exists():
            try:
                os.link(self.view, alias)
            except OSError as exc:
                self.skipTest('No hard links or case-insensitive aliases: ' + str(exc))
        self.assertTrue(alias.samefile(self.view))
        self.engine.sync()
        self.assertNotIn('knowledge/v3/OUTCOMES.md', [r['source'] for r in self.records()])
        self.assertEqual(alias.read_bytes(), original)

    def test_different_case_is_still_a_distinct_user_note_on_sensitive_volumes(self):
        self.damage_view()
        alias = self.vault / 'knowledge/v3/OUTCOMES.md'
        if alias.exists():
            self.skipTest('This volume treats the two spellings as one file')
        self.write('knowledge/v3/OUTCOMES.md', 'Nebula calibration DISTINCT_USER_NOTE.\n')
        self.engine.sync()
        self.assertIn('knowledge/v3/OUTCOMES.md', [r['source'] for r in self.records()])

    def test_case_renamed_view_on_insensitive_volume_stays_excluded(self):
        original = self.damage_view()
        alias = self.vault / 'knowledge/v3/OUTCOMES.md'
        if not alias.exists() or not alias.samefile(self.view):
            self.skipTest('Requires a case-insensitive volume')
        intermediate = self.view.with_name('rename-in-progress.md')
        self.view.rename(intermediate)
        intermediate.rename(alias)
        self.engine.sync()
        self.assertFalse(any(r['source'].lower() == 'knowledge/v3/outcomes.md' for r in self.records()))
        self.assertEqual(alias.read_bytes(), original)

    def test_view_warning_invalidates_receipt_cache_even_when_directory_mtime_stays(self):
        self.seed_view()
        directory = self.vault / 'receipts'
        old_ns = time.time_ns() - 10_000_000_000
        os.utime(directory, ns=(old_ns, old_ns))
        self.engine.sync()
        with self.engine.store._connect() as db:
            self.assertIsNotNone(db.execute(
                "SELECT value FROM metadata WHERE key='receipt_scan_signature'").fetchone())
        before = directory.stat()
        self.incoming('mtime-hidden')
        os.utime(directory, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.view.write_bytes(b'Preserved cache invalidation trigger.\n')
        report = self.engine.sync()
        self.assert_view_warning(report)
        with self.engine.store._connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM receipts WHERE id='mtime-hidden'").fetchone())
            self.assertIsNone(db.execute(
                "SELECT value FROM metadata WHERE key='receipt_scan_signature'").fetchone())
        self.engine.sync()
        with self.engine.store._connect() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM receipts WHERE id='mtime-hidden'").fetchone())

    def test_receipt_success_readback_and_idempotence_keep_view_diagnostics(self):
        original = self.damage_view()
        try:
            result = self.engine.receipt('new', 'Verified new outcome.', ['notes/task.md'], 'codex')
        except subject.ReceiptConflict as exc:
            self.fail('A preserved view must not block a valid receipt: ' + str(exc))
        self.assertEqual(result['status'], 'succeeded')
        self.assert_view_warning(result['source_sync'])
        source = self.vault / result['source']
        before = source.read_bytes()
        self.assertIn(b'Verified new outcome.', before)
        with self.engine.store._connect() as db:
            event = json.loads(db.execute("SELECT payload FROM receipts WHERE id='new'").fetchone()[0])
        self.assertEqual(event['summary'], 'Verified new outcome.')
        again = self.engine.receipt('new', 'Verified new outcome.', ['notes/task.md'], 'codex')
        self.assertEqual(again, result)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.view.read_bytes(), original)

    def test_task_success_readback_and_revision_keep_view_diagnostics(self):
        original = self.damage_view()
        try:
            result = self.engine.update_task('calibration-task', 1, {'status': 'waiting'})
        except subject.RevisionConflict as exc:
            self.fail('A preserved view must not block a valid task update: ' + str(exc))
        self.assertEqual((result['revision'], result['status']), (2, 'waiting'))
        self.assert_view_warning(result['source_sync'])
        metadata, _ = subject.parse((self.vault / 'notes/task.md').read_text(encoding='utf-8'))
        self.assertEqual((metadata['revision'], metadata['status']), (2, 'waiting'))
        self.assertEqual(self.view.read_bytes(), original)
        with self.assertRaises(subject.RevisionConflict):
            self.engine.update_task('calibration-task', 1, {'status': 'active'})

    def test_view_warning_does_not_hide_duplicate_source_conflict(self):
        original = self.damage_view()
        self.write('notes/duplicate.md', (self.vault / 'notes/task.md').read_text(encoding='utf-8'))
        result = self.engine.sync()
        self.assertEqual(result['status'], 'conflict')
        self.assertTrue(any('duplicate source id' in c['reason'] for c in result['conflicts']))
        self.assertFalse(any(c.get('kind') == 'receipt_view' for c in result['conflicts']))
        self.assertTrue(any(w.get('kind') == 'receipt_view' for w in result['warnings']))
        with self.assertRaises(subject.ReceiptConflict):
            self.engine.receipt('blocked', 'Blocked write.', ['notes/task.md'], 'codex')
        self.assertEqual(self.view.read_bytes(), original)

    def test_cli_context_recap_and_list_history_keep_warning(self):
        self.damage_view()
        context = self.cli('context', 'Nebula calibration')
        self.assertEqual(context.returncode, 0, context.stderr)
        result = json.loads(context.stdout)
        self.assertIn('SYNTHETIC_HEALTHY_CANARY', context.stdout)
        self.assertEqual(result['source_sync']['status'], 'degraded')
        self.assertTrue(any(w.get('kind') == 'receipt_view' for w in result['source_sync']['warnings']))
        recap = self.cli('recap')
        self.assertEqual(recap.returncode, 0, recap.stderr)
        self.assertEqual(json.loads(recap.stdout)['source_sync']['status'], 'degraded')
        history = self.cli('history', 'calibration-task')
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertIsInstance(json.loads(history.stdout), list)
        self.assertTrue(json.loads(history.stdout))
        self.assertIn('receipt_view', history.stderr)
        self.assertIn('knowledge/v3/outcomes.md', history.stderr)

    def test_explicit_empty_projection_retires_tracked_views_without_rerendering(self):
        self.seed_view()
        from beyin_v3_projections import project_receipts
        with self.engine.store._connect() as db:
            try:
                report = project_receipts(self.engine, db, desired={})
            except TypeError as exc:
                self.fail('Projection must accept an explicit empty desired mapping: ' + str(exc))
            self.assertEqual(report, [])
            self.assertEqual(db.execute('SELECT path FROM receipt_views').fetchall(), [])
        self.assertFalse(self.view.exists())

    def test_rogue_tracking_does_not_hide_an_ordinary_source_or_downgrade_conflict(self):
        with self.engine.store._connect() as db:
            db.execute("INSERT INTO receipt_views VALUES (?,?)", ('notes/task.md', 'rogue'))
        result = self.engine.sync()
        self.assertEqual(result['status'], 'conflict')
        self.assertIn('notes/task.md', [r['source'] for r in self.records()])
        self.assertTrue(any(c['reason'] == 'unexpected receipt view path; preserved' for c in result['conflicts']))
        self.assertFalse(any(w.get('source') == 'notes/task.md' for w in result['warnings']))

    def test_claimed_private_ref_and_alias_never_export_receipt_summary(self):
        from beyin_v3_projections import latest_receipts
        sys.path.insert(0, str(ROOT / "template/.claude/scripts"))
        self.addCleanup(sys.path.remove, str(ROOT / "template/.claude/scripts"))
        from beyin_v3_hook import receipt_context
        self.write('knowledge/v3/outcomes.md', subject.render(
            {'id': 'private-view', 'visibility': 'private'}, 'PRIVATE_SOURCE_CANARY'))
        alias = self.vault / 'notes/alias.md'
        try:
            os.link(self.view, alias)
        except OSError as exc:
            self.skipTest('Hard links unavailable: ' + str(exc))
        self.engine.sync()
        original = self.view.read_bytes()
        self.engine.receipt('private-ref', 'PRIVATE_CLAIM_CANARY', ['knowledge/v3/outcomes.md'], 'codex')
        self.engine.receipt('private-alias', 'PRIVATE_ALIAS_CANARY', ['notes/alias.md'], 'codex')
        for fresh in (False, True):
            if fresh:
                self.engine = subject.SyncEngine(self.vault, self.root / 'fresh-state')
            self.engine.sync()
            self.assertNotIn('knowledge/v3/outcomes.md', [r['source'] for r in self.records()])
            self.assertNotIn('notes/alias.md', [r['source'] for r in self.records()])
            with self.engine.store._connect() as db:
                self.assertEqual(list(latest_receipts(db, self.vault)), [])
            self.assertNotIn('CANARY', receipt_context(self.vault, self.engine.state))
            self.assertEqual(self.view.read_bytes(), original)

    @unittest.skipUnless(os.name == 'posix', 'Directory fsync barriers require POSIX')
    def test_archive_directory_barriers_precede_replacement_and_retirement(self):
        import stat
        for retire in (False, True):
            with self.subTest(retire=retire):
                for archive in self.vault.glob('*/v3/.archive'):
                    shutil.rmtree(archive)
                self.damage_view()
                original = self.view.read_bytes()
                if retire:
                    (self.state / 'v2-migration.json').write_text(json.dumps({'historical_receipts': [
                        'receipts/' + hashlib.sha256(b'initial').hexdigest() + '.md']}))
                flushed = set()
                events = []
                fsync, replace, unlink = os.fsync, os.replace, Path.unlink
                def flush(fd):
                    if stat.S_ISDIR(os.fstat(fd).st_mode):
                        flushed.add(os.fstat(fd).st_ino)
                        events.append(('directory', os.fstat(fd).st_ino))
                    else:
                        events.append(('file', os.fstat(fd).st_ino))
                    return fsync(fd)
                def before_destroy():
                    events.append(('destroy', None))
                    self.assertIn((self.view.parent / '.archive').stat().st_ino, flushed)
                    self.assertIn(self.view.parent.stat().st_ino, flushed)
                    archive = next(p for p in (self.view.parent / '.archive').iterdir() if p.read_bytes() == original)
                    self.assertLess(events.index(('file', archive.stat().st_ino)),
                                    events.index(('directory', archive.parent.stat().st_ino)))
                    self.assertLess(events.index(('directory', archive.parent.stat().st_ino)),
                                    events.index(('directory', self.view.parent.stat().st_ino)))
                def replacing(src, dst):
                    if Path(dst).resolve() == self.view.resolve():
                        before_destroy()
                    return replace(src, dst)
                def removing(path, *args, **kwargs):
                    if path.resolve() == self.view.resolve():
                        before_destroy()
                    return unlink(path, *args, **kwargs)
                with patch('os.fsync', flush), patch('os.replace', replacing), patch.object(Path, 'unlink', removing):
                    result = self.engine.sync(recover_views=True)
                self.assertEqual(result['status'], 'succeeded', result)
                self.assertIn(('destroy', None), events)

    @unittest.skipUnless(os.name == 'posix', 'Directory fsync barriers require POSIX')
    def test_failed_archive_directory_barrier_preserves_original_and_tracking(self):
        import stat
        for fail_parent in (False, True):
            for retire in (False, True):
                with self.subTest(fail_parent=fail_parent, retire=retire):
                    for archive in self.vault.glob('*/v3/.archive'):
                        shutil.rmtree(archive)
                    self.damage_view()
                    original = self.view.read_bytes()
                    if retire:
                        (self.state / 'v2-migration.json').write_text(json.dumps({'historical_receipts': [
                            'receipts/' + hashlib.sha256(b'initial').hexdigest() + '.md']}))
                    with self.engine.store._connect() as db:
                        tracked = db.execute('SELECT hash FROM receipt_views WHERE path=?', ('knowledge/v3/outcomes.md',)).fetchone()
                    fsync = os.fsync
                    def fail(fd):
                        target = self.view.parent if fail_parent else self.view.parent / '.archive'
                        if stat.S_ISDIR(os.fstat(fd).st_mode) and os.fstat(fd).st_ino == target.stat().st_ino:
                            raise OSError('injected directory durability failure')
                        return fsync(fd)
                    with patch('os.fsync', fail):
                        result = self.engine.sync(recover_views=True)
                    self.assertEqual(self.view.read_bytes(), original)
                    self.assertTrue(any(w['reason'] == 'receipt view recovery failed; preserved' for w in result['warnings']))
                    with self.engine.store._connect() as db:
                        self.assertEqual(db.execute('SELECT hash FROM receipt_views WHERE path=?', ('knowledge/v3/outcomes.md',)).fetchone(), tracked)
                    (self.state / 'v2-migration.json').unlink(missing_ok=True)

    def test_write_diagnostics_are_bounded_with_malformed_sources_and_view_warning(self):
        self.damage_view()
        for n in range(24):
            self.write('notes/broken%d.md' % n, '---\nrevision: invalid\n---\nInvalid note')
        result = self.engine.update_task('calibration-task', 1, {'status': 'waiting'})
        diag = result['source_sync']
        self.assertEqual(diag['status'], 'degraded')
        self.assertGreater(diag['warning_count'], 20)
        self.assertEqual(len(diag['warnings']), 20)
        self.assertTrue(diag['truncated'])
        report = self.engine.sync()
        self.assertTrue(any(w.get('kind') == 'receipt_view' for w in report['warnings']))
        self.assertTrue(any(w['source'] == 'notes/broken0.md' for w in report['warnings']))
        result = self.engine.receipt('bounded', 'Healthy receipt', ['notes/task.md'], 'codex')
        self.assertEqual(len(result['source_sync']['warnings']), 20)
        self.assertTrue(result['source_sync']['truncated'])

    def test_no_match_hook_keeps_operational_view_warning_without_receipt(self):
        self.damage_view()
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'no-match',
                   'cwd': str(self.vault), 'prompt': 'zxqvnonmatchingword'}
        command = [sys.executable, str(ROOT / 'template/.claude/scripts/beyin_v3_hook.py'),
                   '--vault', str(self.vault), '--state', str(self.state), '--harness', 'codex']
        env = inherited_env(BEYIN_V3_NO_SPAWN='1')
        # This exercises warning delivery, not worker latency. Finish a real sync first:
        # a detached worker may exceed SYNC_WAIT on CI and keep SQLite open during cleanup.
        worker = subprocess.run(command + ['--worker'], text=True, capture_output=True,
                                env=env, timeout=20)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        result = subprocess.run(command, input=json.dumps(payload), text=True,
                                capture_output=True, env=env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('receipt view', result.stdout)
        self.assertNotIn('Initial synthetic outcome', result.stdout)
        self.assertNotIn('SYNTHETIC_HEALTHY_CANARY', result.stdout)

    def test_readable_recap_reports_view_warning(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('followup_entry', ROOT / 'scripts/beyin_entry.py')
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        self.damage_view()
        result = json.loads(self.cli('recap').stdout)
        text = entry.human_result(result, 'recap')
        self.assertIn('Uyari:', text)
        self.assertIn('receipt view', text)

    def test_cli_invalid_source_and_view_report_both_warnings(self):
        self.damage_view()
        self.write('notes/invalid.md', '---\nrevision: invalid\n---\nHIDDEN_INVALID_BODY')
        for args in (('context', 'Nebula calibration'), ('recap',)):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertTrue(data['partial'])
            self.assert_view_warning(data['source_sync'])
            self.assertTrue(any(w['source'] == 'notes/invalid.md' for w in data['source_sync']['warnings']))
            self.assertNotIn('HIDDEN_INVALID_BODY', result.stdout)

    def test_rendering_has_no_file_or_database_side_effects(self):
        from beyin_v3_projections import _receipt_view_contents
        self.seed_view()
        before = {p: p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}
        with self.engine.store._connect() as db:
            db.execute('DROP TABLE receipt_views')
            changes = db.total_changes
            desired = _receipt_view_contents(self.engine, db)
            self.assertTrue(desired)
            self.assertEqual(changes, db.total_changes)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='receipt_views'").fetchone())
        self.assertEqual(before, {p: p.read_bytes() for p in self.vault.rglob('*') if p.is_file()})

    def test_unrelated_receipt_divergence_writes_keep_conflict_diagnostics(self):
        self.damage_view()
        source = self.vault / ('receipts/' + hashlib.sha256(b'initial').hexdigest() + '.md')
        source.write_bytes(source.read_bytes().replace(b'Initial synthetic outcome.', b'Diverged synthetic outcome.'))
        receipt = self.engine.receipt('independent', 'Independent outcome.', ['notes/task.md'], 'codex')
        task = self.engine.update_task('calibration-task', 1, {'status': 'waiting'})
        for result in (receipt, task):
            self.assertEqual(result['source_sync']['status'], 'conflict')
            self.assertEqual([c['kind'] for c in result['source_sync']['conflicts']], ['receipt_divergence'])
            self.assertTrue(any(w.get('kind') == 'receipt_view' for w in result['source_sync']['warnings']))
        self.assertNotEqual(self.cli('context', 'Nebula calibration').returncode, 0)
        self.assertNotEqual(self.cli('history', 'calibration-task').returncode, 0)

    def test_retired_view_receipt_stays_hidden_after_clean_path_reuse(self):
        from beyin_v3_projections import latest_receipts, recent_receipts
        self.write('knowledge/v3/outcomes.md', subject.render({'visibility': 'private'}, 'Private source'))
        self.engine.sync()
        result = self.engine.receipt('private-ref', 'HIDDEN_RETIRED_RECEIPT', ['knowledge/v3/outcomes.md'], 'codex')
        # Adopt regenerated views, then retire them through the historical migration boundary.
        self.engine.sync(recover_views=True)
        (self.state / 'v2-migration.json').write_text(json.dumps({'historical_receipts': [result['source']]}))
        self.engine.sync()
        self.write('knowledge/v3/outcomes.md', 'Nebula calibration CLEAN_REUSED_NOTE')
        self.engine.sync()
        self.assertIn('knowledge/v3/outcomes.md', [r['source'] for r in self.records()])
        with self.engine.store._connect() as db:
            self.assertEqual(list(latest_receipts(db, self.vault)), [])
            self.assertEqual(recent_receipts(db, vault=self.vault)['items'], [])
        self.assertNotIn('HIDDEN_RETIRED_RECEIPT', self.cli('recap').stdout)

    def test_retired_private_view_receipt_stays_hidden_after_database_reset(self):
        from beyin_v3_projections import latest_receipts, recent_receipts
        self.write('knowledge/v3/outcomes.md', subject.render({'visibility': 'private'}, 'Private source'))
        self.engine.sync()
        result = self.engine.receipt('private-ref', 'HIDDEN_RETIRED_RECEIPT',
                                     ['knowledge/v3/outcomes.md'], 'codex')
        self.engine.sync(recover_views=True)
        migration = {'historical_receipts': [result['source']]}
        (self.state / 'v2-migration.json').write_text(json.dumps(migration))
        self.engine.sync()
        self.write('knowledge/v3/outcomes.md', 'Nebula calibration CLEAN_REUSED_NOTE')
        self.engine.sync()
        # A state transfer may retain migration metadata but rebuild its SQLite
        # index. The immutable receipt must not regain its private summary.
        self.state = self.root / 'rebuilt-state'
        self.state.mkdir()
        (self.state / 'v2-migration.json').write_text(json.dumps(migration))
        self.engine = subject.SyncEngine(self.vault, self.state)
        self.engine.sync()
        self.assertIn('knowledge/v3/outcomes.md', [r['source'] for r in self.records()])
        with self.engine.store._connect() as db:
            self.assertEqual(list(latest_receipts(db, self.vault)), [])
            self.assertEqual(recent_receipts(db, vault=self.vault)['items'], [])
        self.assertNotIn('HIDDEN_RETIRED_RECEIPT', self.cli('recap').stdout)

    def test_recovered_view_hardlink_alias_remains_excluded(self):
        private = self.write('notes/private.md', subject.render(
            {'id': 'private-source', 'visibility': 'private'}, 'Private source'))
        self.engine.sync()
        self.engine.receipt('private-alias-recovery', 'HIDDEN_ALIAS_AFTER_RECOVERY',
                            ['notes/private.md'], 'codex')
        # Removing the generated header turns the preserved view into an
        # apparent ordinary note. A hard link retains its old inode after the
        # canonical path is replaced during explicit recovery.
        self.view.write_text(subject.parse(self.view.read_text(encoding='utf-8'))[1], encoding='utf-8')
        alias = self.vault / 'notes/view-alias.md'
        removed_alias = self.vault / 'notes/a-missing-alias.md'
        try:
            os.link(self.view, alias)
            os.link(self.view, removed_alias)
        except OSError as exc:
            self.skipTest('Hard links unavailable: ' + str(exc))
        original = alias.read_bytes()
        self.engine.sync()
        self.assertEqual(self.engine.sync(recover_views=True)['status'], 'succeeded')
        self.assertNotEqual(self.view.stat().st_ino, alias.stat().st_ino)
        self.engine.sync()
        self.assertEqual(alias.read_bytes(), original)
        self.assertNotIn('notes/view-alias.md', [r['source'] for r in self.records()])
        context = self.cli('context', 'HIDDEN_ALIAS_AFTER_RECOVERY')
        self.assertEqual(context.returncode, 0, context.stderr)
        self.assertNotIn('HIDDEN_ALIAS_AFTER_RECOVERY', context.stdout)
        removed_alias.unlink()
        self.state = self.root / 'alias-rebuilt-state'
        self.engine = subject.SyncEngine(self.vault, self.state)
        self.engine.sync()
        self.assertNotIn('notes/view-alias.md', [r['source'] for r in self.records()])
        self.assertNotIn('HIDDEN_ALIAS_AFTER_RECOVERY',
                         self.cli('context', 'HIDDEN_ALIAS_AFTER_RECOVERY').stdout)
        removed_alias.write_bytes(original)
        old_read_bytes = Path.read_bytes
        def unreadable_first(path):
            if path == removed_alias:
                raise OSError('injected unreadable first alias')
            return old_read_bytes(path)
        self.state = self.root / 'alias-unreadable-state'
        self.engine = subject.SyncEngine(self.vault, self.state)
        with patch.object(Path, 'read_bytes', unreadable_first):
            self.engine.sync()
        self.assertNotIn('notes/view-alias.md', [r['source'] for r in self.records()])
        removed_alias.unlink()
        alias.unlink()
        alias.write_text('Nebula calibration CLEAN_REUSED_ALIAS', encoding='utf-8')
        self.engine.sync()
        self.assertIn('notes/view-alias.md', [r['source'] for r in self.records()])
        self.assertIn('CLEAN_REUSED_ALIAS', self.cli('context', 'CLEAN_REUSED_ALIAS').stdout)

    def test_recycled_alias_inode_does_not_hide_new_source(self):
        self.damage_view()
        alias = self.vault / 'notes/recycled-alias.md'
        try:
            os.link(self.view, alias)
        except OSError as exc:
            self.skipTest('Hard links unavailable: ' + str(exc))
        old_identity = self.engine._source_identity(alias)
        self.engine.sync()
        self.assertEqual(self.engine.sync(recover_views=True)['status'], 'succeeded')
        alias.unlink()
        alias.write_text('Nebula calibration CLEAN_RECYCLED_INODE', encoding='utf-8')
        identity = subject.SyncEngine._source_identity
        def recycled(path):
            return old_identity if path.resolve() == alias.resolve() else identity(path)
        # Model the inode reuse observed on CI without depending on the local allocator.
        with patch.object(subject.SyncEngine, '_source_identity', staticmethod(recycled)):
            self.engine.sync()
        self.assertIn('notes/recycled-alias.md', [r['source'] for r in self.records()])
        self.assertIn('CLEAN_RECYCLED_INODE', self.cli('context', 'CLEAN_RECYCLED_INODE').stdout)

    def test_legacy_alias_identity_without_digest_does_not_hide_source(self):
        source = self.write('notes/legacy-alias.md', 'Nebula calibration CLEAN_LEGACY_ALIAS')
        with self.engine.store._connect() as db:
            db.execute('INSERT INTO receipt_view_aliases(source,identity) VALUES (?,?)',
                       ('notes/legacy-alias.md', subject._json(self.engine._source_identity(source))))
        self.engine.sync()
        self.assertIn('notes/legacy-alias.md', [r['source'] for r in self.records()])

    def test_archive_barriers_with_alternate_vault_spelling(self):
        alias = self.root / 'vault-spelling'
        try:
            alias.symlink_to(self.vault, target_is_directory=True)
        except OSError as exc:
            self.skipTest('Directory symlinks unavailable: ' + str(exc))
        view = self.view
        self.view = alias / view.relative_to(self.vault)
        try:
            self.test_archive_directory_barriers_precede_replacement_and_retirement()
        finally:
            self.view = view

    def test_alias_provenance_fsync_failure_keeps_original_view(self):
        if not Path('/proc/self/fd').is_dir():
            self.skipTest('Requires Linux fd path inspection')
        self.write('notes/private.md', subject.render(
            {'visibility': 'private'}, 'Private source'))
        self.engine.sync()
        self.engine.receipt('alias-fsync', 'HIDDEN_ALIAS_FSYNC', ['notes/private.md'], 'codex')
        self.view.write_text(subject.parse(self.view.read_text(encoding='utf-8'))[1], encoding='utf-8')
        alias = self.vault / 'notes/view-alias.md'
        try:
            os.link(self.view, alias)
        except OSError as exc:
            self.skipTest('Hard links unavailable: ' + str(exc))
        self.engine.sync()
        original = self.view.read_bytes()
        with self.engine.store._connect() as db:
            tracked = db.execute('SELECT hash FROM receipt_views WHERE path=?',
                                 ('knowledge/v3/outcomes.md',)).fetchone()
        fsync = os.fsync
        def fail_sidecar(fd):
            if os.readlink('/proc/self/fd/%d' % fd).endswith('.provenance.json'):
                raise OSError('injected provenance durability failure')
            return fsync(fd)
        with patch('os.fsync', fail_sidecar):
            result = self.engine.sync(recover_views=True)
        self.assertEqual(result['status'], 'degraded')
        self.assertTrue(any(w['reason'] == 'receipt view recovery failed; preserved'
                            for w in result['warnings']))
        self.assertEqual(self.view.read_bytes(), original)
        self.assertEqual(alias.read_bytes(), original)
        with self.engine.store._connect() as db:
            self.assertEqual(db.execute('SELECT hash FROM receipt_views WHERE path=?',
                                        ('knowledge/v3/outcomes.md',)).fetchone(), tracked)


if __name__ == '__main__':
    unittest.main()
