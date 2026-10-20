"""Cooperative note writes: real processes, exact bytes and CLI/session contracts."""
import base64
import hashlib
import importlib.util
import io
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
from beyin_v3_sync import SyncEngine
from beyin_v3_notes import LOCAL_DIRECTORY, NoteConflict, locked

SPEC = importlib.util.spec_from_file_location('note_cli', ROOT / 'scripts/beyin_v3.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class NoteEditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / 'vault'
        self.vault.mkdir()
        self.state = Path(self.temp.name) / 'state'
        self.engine = SyncEngine(self.vault, self.state)
        self.addCleanup(self.engine.store.close)
        # Never inherit the real agent's session into tests or child processes.
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop('CLAUDE_CODE_SESSION_ID', None)
        os.environ.pop('BEYIN_V3_NO_RECEIPT_REMINDER', None)

    def write(self, source='Threads.md', raw=b'alpha\nbeta\n'):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def request(self, old='alpha', new='ALPHA', source='Threads.md'):
        return {'source': source, 'expected_sha256': self.engine.note_read(source)['sha256'],
                'edits': [{'old': old, 'new': new}]}

    def command(self, command, *args, state=None):
        return [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                '--state', str(state or self.state), command, *args]

    def run_cli(self, command, request=None, *args):
        return subprocess.run(self.command(command, *args),
                              input=json.dumps(request) if request is not None else None,
                              text=True, encoding='utf-8', capture_output=True, timeout=30,
                              env=os.environ.copy())

    def test_missing_read_and_create_do_not_overwrite(self):
        missing = {'source': 'project/Journal.md', 'sha256': None, 'content': None}
        self.assertEqual(self.engine.note_read(missing['source']), missing)
        request = {'source': missing['source'], 'expected_sha256': None, 'content': '# Journal\r\n'}
        result = self.engine.note_edit(request)
        self.assertEqual(result['status'], 'written')
        self.assertIsNone(result['backup'])
        self.assertEqual((self.vault / missing['source']).read_bytes(), b'# Journal\r\n')
        with self.assertRaises(NoteConflict):
            self.engine.note_edit(request)
        self.assertFalse((self.state / 'receipt-reminders').exists())

    def test_bom_crlf_and_backup_are_exact_and_index_is_current(self):
        original = b'\xef\xbb\xbf# Journal\r\nalpha\r\nbeta\r\n'
        path = self.write(raw=original)
        snapshot = self.engine.note_read('Threads.md')
        self.assertEqual(snapshot['content'].encode('utf-8'), original)
        self.assertEqual(snapshot['sha256'], hashlib.sha256(original).hexdigest())
        result = self.engine.note_edit(self.request())
        self.assertEqual(result['status'], 'written')
        self.assertEqual(path.read_bytes(), original.replace(b'alpha', b'ALPHA'))
        backup = json.loads((self.vault / result['backup']).read_text(encoding='utf-8'))
        self.assertEqual(base64.b64decode(backup['bytes_base64']), original)
        self.assertEqual(backup['sha256'], snapshot['sha256'])
        self.assertEqual(backup['source'], 'Threads.md')
        with self.engine.store._connect() as db:
            record = json.loads(db.execute('SELECT payload FROM records').fetchone()[0])
        self.assertEqual(record['source_sha256'], result['sha256'])

    def test_stale_hash_and_unique_anchor_leave_bytes_untouched(self):
        path = self.write(raw=b'alpha alpha\nbeta\n')
        request = self.request()
        for value in (request, dict(request, expected_sha256='0' * 64),
                      dict(request, edits=[{'old': 'absent', 'new': 'new'}])):
            with self.subTest(value=value):
                with self.assertRaises(NoteConflict):
                    self.engine.note_edit(value)
        self.assertEqual(path.read_bytes(), b'alpha alpha\nbeta\n')
        self.assertFalse((self.vault / LOCAL_DIRECTORY / 'backups').exists())

    def test_sequential_edits_and_unchanged_have_no_backup(self):
        self.write()
        request = self.request(new='alpha')
        self.assertEqual(self.engine.note_edit(request)['status'], 'unchanged')
        self.assertFalse((self.vault / LOCAL_DIRECTORY / 'backups').exists())
        request['edits'] = [{'old': 'alpha', 'new': 'gamma'}, {'old': 'gamma', 'new': 'delta'}]
        self.assertEqual(self.engine.note_edit(request)['status'], 'written')
        self.assertEqual((self.vault / 'Threads.md').read_bytes(), b'delta\nbeta\n')

    def test_overlapping_anchors_are_not_unique(self):
        path = self.write(raw=b'aaa')
        with self.assertRaises(NoteConflict):
            self.engine.note_edit(self.request('aa', 'x'))
        self.assertEqual(path.read_bytes(), b'aaa')

    def test_invalid_requests_are_errors_without_source_writes(self):
        path = self.write()
        good = self.request()
        cases = [[], {}, {'source': 'Threads.md'}, dict(good, expected_sha256=7),
                 dict(good, content='replacement'), dict(good, edits=[]),
                 dict(good, edits=[{}]), dict(good, edits=['bad']),
                 dict(good, edits=[{'old': '', 'new': 'x'}]),
                 dict(good, edits=[{'old': 'alpha', 'new': 1}]),
                 {'source': 'new.md', 'expected_sha256': None, 'edits': []}]
        for request in cases:
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.engine.note_edit(request)
        self.assertEqual(path.read_bytes(), b'alpha\nbeta\n')

    def test_managed_configuration_hidden_outside_and_device_paths_rejected(self):
        cases = ['tasks/a.md', 'TASKS/a.md', 'receipts/a.md', 'daily/v3/a.md',
                 'knowledge/v3/outcomes.md', 'AGENTS.md', 'nested/CLAUDE.md', 'SKILL.md',
                 'config.md', 'GEMINI.md', 'setup-custom.md', '.hidden/a.md', 'notes/.a.md',
                 '../outside.md', '/outside.md', 'C:/outside.md', 'notes\\a.md',
                 'a.md:stream.md', 'notes//a.md', 'notes/../a.md', 'notes./a.md',
                 'notes /a.md', 'NUL.md', 'notes/COM1/a.md', 'notes/a.txt']
        for source in cases:
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    self.engine.note_read(source)
                with self.assertRaises(ValueError):
                    self.engine.note_edit({'source': source, 'expected_sha256': None, 'content': 'x'})
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_managed_frontmatter_rejected_even_with_bom_crlf_or_new_content(self):
        headers = ['kind: task', 'kind: receipt', 'kind: receipt-index', 'generated: true',
                   '{"kind":"task"}', '{"generated":true}']
        for header in headers:
            raw = ('\ufeff---\r\n' + header + '\r\n---\r\nalpha').encode('utf-8')
            path = self.write(raw=raw)
            with self.subTest(header=header):
                with self.assertRaises(ValueError):
                    self.engine.note_read('Threads.md')
                with self.assertRaises(ValueError):
                    self.engine.note_edit({'source': 'Threads.md',
                                          'expected_sha256': hashlib.sha256(raw).hexdigest(),
                                          'edits': [{'old': 'alpha', 'new': 'changed'}]})
                with self.assertRaises(ValueError):
                    self.engine.note_edit({'source': 'new.md', 'expected_sha256': None,
                                          'content': raw.decode('utf-8')})
            self.assertEqual(path.read_bytes(), raw)
        self.assertFalse((self.vault / 'new.md').exists())

    def test_symlink_and_linked_local_storage_rejected(self):
        target = Path(self.temp.name) / 'outside'
        target.mkdir()
        (target / 'note.md').write_bytes(b'alpha')
        try:
            (self.vault / 'link').symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest('symlink creation unavailable: ' + str(exc))
        for source in ('link/note.md', 'link/new.md'):
            with self.assertRaises(ValueError):
                self.engine.note_read(source)
            with self.assertRaises(ValueError):
                self.engine.note_edit({'source': source, 'expected_sha256': None, 'content': 'x'})
        safe = self.write()
        (self.vault / 'alias.md').symlink_to(safe)
        with self.assertRaises(ValueError):
            self.engine.note_read('alias.md')
        (self.vault / LOCAL_DIRECTORY).symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.engine.note_read('new.md')
        self.assertEqual((target / 'note.md').read_bytes(), b'alpha')
        self.assertFalse((target / 'new.md').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction test')
    def test_windows_junction_rejected_on_python_311_too(self):
        # An inside-vault target still has to be rejected as a junction.
        target = self.vault / 'real'
        target.mkdir()
        junction = self.vault / 'junction'
        done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)],
                              capture_output=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.addCleanup(lambda: os.rmdir(junction))
        with self.assertRaises(ValueError):
            self.engine.note_read('junction/new.md')
        with self.assertRaises(ValueError):
            self.engine.note_edit({'source': 'junction/new.md', 'expected_sha256': None, 'content': 'x'})

    def test_linked_lock_or_backups_are_rejected(self):
        self.write()
        request = self.request()
        local = self.vault / LOCAL_DIRECTORY
        lock = local / 'writer.lock'
        lock.unlink()
        outside = Path(self.temp.name) / 'outside.lock'
        outside.write_bytes(b'\0')
        try:
            lock.symlink_to(outside)
        except OSError as exc:
            self.skipTest('symlink creation unavailable: ' + str(exc))
        with self.assertRaises(ValueError):
            self.engine.note_edit(request)
        lock.unlink()
        target = Path(self.temp.name) / 'outside-backups'
        target.mkdir()
        (local / 'backups').symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.engine.note_edit(request)
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual((self.vault / 'Threads.md').read_bytes(), b'alpha\nbeta\n')

    def test_revalidate_path_after_lock_wait(self):
        self.write()
        import beyin_v3_notes as notes
        real = notes.source_path
        calls = []
        def validate(*args):
            calls.append(True)
            if len(calls) == 2:
                raise ValueError('path changed while waiting')
            return real(*args)
        with patch.object(notes, 'source_path', side_effect=validate):
            with self.assertRaisesRegex(ValueError, 'while waiting'):
                self.engine.note_read('Threads.md')

    def test_external_write_before_and_after_replace_is_reported(self):
        import beyin_v3_sync as sync
        import beyin_v3_notes as notes
        path = self.write()
        request = self.request()
        original = notes.tempfile.mkstemp
        def change_before(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_bytes(b'external before')
            return result
        with patch.object(notes.tempfile, 'mkstemp', side_effect=change_before):
            with self.assertRaisesRegex(NoteConflict, 'before replace'):
                self.engine.note_edit(request)
        self.assertEqual(path.read_bytes(), b'external before')
        self.assertEqual(list(self.vault.glob('.beyin-note-*.tmp')), [])
        self.write()
        original_replace = sync._safe_replace
        def change_after(src, dst):
            original_replace(src, dst)
            path.write_bytes(b'external after')
        with patch.object(sync, '_safe_replace', side_effect=change_after):
            with self.assertRaisesRegex(NoteConflict, 'after replace'):
                self.engine.note_edit(request)
        self.assertEqual(path.read_bytes(), b'external after')

    def test_cli_json_file_stdin_and_exit_codes(self):
        self.write()
        read = self.run_cli('note-read', None, 'Threads.md')
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(json.loads(read.stdout)['content'], 'alpha\nbeta\n')
        request = self.request()
        file = Path(self.temp.name) / 'request.json'
        file.write_text(json.dumps(request), encoding='utf-8-sig')
        done = self.run_cli('note-edit', None, '--file', str(file))
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertEqual(json.loads(done.stdout)['status'], 'written')
        stale = self.run_cli('note-edit', request, '--file', '-')
        self.assertEqual(stale.returncode, 2, stale.stderr + stale.stdout)
        self.assertEqual(json.loads(stale.stdout)['status'], 'conflict')
        error = self.run_cli('note-edit', {'source': '../bad.md'})
        self.assertEqual(error.returncode, 1)
        self.assertEqual(json.loads(error.stdout)['status'], 'error')

    def test_installed_entry_ships_helper_and_keeps_json_in_human_mode(self):
        installed = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault', str(self.vault),
             '--state', str(self.state)], capture_output=True, text=True, encoding='utf-8',
            env=dict(os.environ, BEYIN_V3_NO_SPAWN='1'), timeout=40)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertTrue((self.vault / '.claude/scripts/beyin_v3_notes.py').is_file())
        source = 'proje/Journal.md'
        request = {'source': source, 'expected_sha256': None, 'content': 'new journal\r\n'}
        def run(*args, payload=None):
            return subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *args],
                                  input=json.dumps(payload) if payload else None,
                                  capture_output=True, text=True, encoding='utf-8',
                                  env=os.environ.copy(), timeout=30)
        result = run('note-edit', '--human', '--file', '-', payload=request)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'written')
        read = run('note-read', source, '--human')
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(json.loads(read.stdout)['content'], request['content'])
        stale = run('note-edit', '--file', '-', payload=request)
        self.assertEqual(stale.returncode, 2, stale.stdout + stale.stderr)
        self.assertEqual(json.loads(stale.stdout)['status'], 'conflict')

    def test_local_backup_and_lock_are_git_ignored_in_existing_vault(self):
        done = subprocess.run(['git', 'init', str(self.vault)], capture_output=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.write()
        result = self.engine.note_edit(self.request())
        for source in (LOCAL_DIRECTORY + '/writer.lock', result['backup'],
                       LOCAL_DIRECTORY + '/request.json'):
            ignored = subprocess.run(['git', '-C', str(self.vault), 'check-ignore', source],
                                     capture_output=True, timeout=10)
            self.assertEqual(ignored.returncode, 0, ignored.stderr)

    def test_os_lock_timeout_is_a_conflict_and_process_death_releases_lock(self):
        code = '''import sys
from beyin_v3_sync import SyncEngine
from beyin_v3_notes import locked
engine = SyncEngine(sys.argv[1], sys.argv[2])
with locked(engine):
    print('locked', flush=True)
    sys.stdin.read()
'''
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'template/.claude/scripts'))
        child = subprocess.Popen([sys.executable, '-c', code, str(self.vault), str(self.state)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=env)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            with self.assertRaisesRegex(NoteConflict, 'writer busy'):
                with locked(self.engine, timeout=0.1):
                    self.fail('OS lock was not held by the child')
        finally:
            child.kill()
            child.communicate(timeout=10)
        self.assertIsNone(self.engine.note_read('new.md')['sha256'])

    def test_only_successful_write_with_session_calls_marker(self):
        self.write()
        args = self.command('note-edit')[2:]
        def invoke(request):
            with patch.object(sys, 'stdin', io.StringIO(json.dumps(request))):
                return cli.main(args, return_result=True)
        with patch.object(cli, 'mark_session_edit') as mark:
            self.assertEqual(invoke(self.request())[1], 0)
            mark.assert_not_called()
            os.environ['CLAUDE_CODE_SESSION_ID'] = 'synthetic-note-session'
            self.assertEqual(invoke(self.request('ALPHA', 'ALPHA'))[0]['status'], 'unchanged')
            mark.assert_not_called()
            self.assertEqual(invoke(self.request('missing', 'x'))[1], 2)
            self.assertEqual(invoke({'source': '../bad.md'})[1], 1)
            mark.assert_not_called()
            with patch.object(SyncEngine, 'sync', return_value={'status': 'succeeded'}):
                # No matching index row: the source was written but readback failed.
                self.assertEqual(invoke(self.request('ALPHA', 'unindexed'))[1], 2)
            mark.assert_not_called()
            self.engine.sync()
            self.assertEqual(invoke(self.request('unindexed', 'merged'))[0]['status'], 'written')
            mark.assert_called_once()
            # The CLI resolves the state root; macOS /var and Windows 8.3 temp names differ.
            self.assertEqual(Path(mark.call_args.args[0]).resolve(), self.state.resolve())
        marker = self.run_cli('note-edit', self.request('merged', 'final'))
        self.assertEqual(marker.returncode, 0, marker.stdout + marker.stderr)
        key = hashlib.sha256(b'synthetic-note-session').hexdigest()
        self.assertTrue((self.state / 'receipt-reminders' / (key + '.edited')).is_file())

    def test_two_real_processes_one_wins_other_conflicts_then_merges(self):
        self.write()
        requests = [self.request('alpha', 'ALPHA'), self.request('beta', 'BETA')]
        # Both children have the same snapshot and are started while the OS lock
        # is held. Different state roots must still share this vault's lock.
        with locked(self.engine):
            children = [subprocess.Popen(self.command('note-edit', '--file', '-',
                                                      state=Path(self.temp.name) / ('state-' + str(i))),
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                         env=os.environ.copy()) for i in range(2)]
            for child, request in zip(children, requests):
                child.stdin.write(json.dumps(request))
                child.stdin.close()
                child.stdin = None
        outputs = [child.communicate(timeout=30) for child in children]
        self.assertEqual(sorted(child.returncode for child in children), [0, 2], outputs)
        loser = next(i for i, child in enumerate(children) if child.returncode == 2)
        self.assertEqual(json.loads(outputs[loser][0])['status'], 'conflict')
        request = requests[loser]
        request['expected_sha256'] = self.engine.note_read('Threads.md')['sha256']
        self.assertEqual(self.engine.note_edit(request)['status'], 'written')
        self.assertEqual((self.vault / 'Threads.md').read_bytes(), b'ALPHA\nBETA\n')
        self.assertEqual(len(list((self.vault / LOCAL_DIRECTORY / 'backups').glob('*.json'))), 2)

    def test_note_read_and_edit_under_index_excluded_directory(self):
        """note-read and note-edit allow reading and modifying notes in index-excluded directories (#468)."""
        archive_dir = self.vault / '📦 900-Archive'
        archive_dir.mkdir(parents=True, exist_ok=True)
        note = archive_dir / 'Arşiv.md'
        note.write_text('# Arşiv\nEski proje durumu: aktif.\n', encoding='utf-8')

        # Configure 📦 900-Archive as excluded in state/index.json
        (self.state / 'index.json').write_text(
            json.dumps({'schema': 1, 'excluded_dirs': ['📦 900-Archive']}),
            encoding='utf-8'
        )

        # 1. note-read must succeed and return content and sha256
        read_res = self.engine.note_read('📦 900-Archive/Arşiv.md')
        self.assertEqual(read_res['source'], '📦 900-Archive/Arşiv.md')
        self.assertIn('Eski proje durumu: aktif.', read_res['content'])

        # 2. note-edit must succeed and update the note on disk safely
        edit_req = {
            'source': '📦 900-Archive/Arşiv.md',
            'expected_sha256': read_res['sha256'],
            'edits': [{'old': 'aktif', 'new': 'bırakıldı'}]
        }
        edit_res = self.engine.note_edit(edit_req)
        self.assertEqual(edit_res['status'], 'written')
        self.assertIn('bırakıldı', note.read_text(encoding='utf-8'))

        # 3. Sensitive directories (🔐 Kasa) must still be strictly refused
        kasa_dir = self.vault / '🔐 Kasa'
        kasa_dir.mkdir(parents=True, exist_ok=True)
        (kasa_dir / 'Gizli.md').write_text('# Gizli\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'sensitive directory'):
            self.engine.note_read('🔐 Kasa/Gizli.md')
        with self.assertRaisesRegex(ValueError, 'sensitive directory'):
            self.engine.note_edit({'source': '🔐 Kasa/Gizli.md', 'expected_sha256': None, 'content': '# X'})


if __name__ == '__main__':
    unittest.main()
