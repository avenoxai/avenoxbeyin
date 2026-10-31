"""Portable note-create and task-create names must be rejected before durable journal writes."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env, install


class NoteCreateInvalidNameTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='beyin-invalid-note-name-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.vault, self.state, home = root / 'vault', root / 'state', root / 'home'
        self.vault.mkdir()
        home.mkdir()
        self.env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                                 LOCALAPPDATA=str(home / 'localappdata'), BEYIN_V3_NO_SPAWN='1',
                                 PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr.decode('utf-8', errors='replace'))

    def command(self, *args, payload=None):
        return subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *args, '--json'],
                              cwd=self.vault, env=self.env, capture_output=True, timeout=60,
                              input=json.dumps(payload).encode('utf-8') if payload is not None else None)

    def succeeded(self, *args, payload=None):
        done = self.command(*args, payload=payload)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return json.loads(done.stdout.decode('utf-8'))

    def source_files(self, folder):
        root = self.vault / folder
        if not root.exists():
            return []
        return sorted(path.relative_to(self.vault).as_posix()
                      for path in root.rglob('*') if path.is_file())

    def refused(self, source, message, command='note-create'):
        knowledge_before, tasks_before = self.source_files('knowledge'), self.source_files('tasks')
        metadata = {'id': 'synthetic-invalid-' + hashlib.sha256(source.encode()).hexdigest()[:12]}
        if command == 'task-create':
            metadata.update(owner='Synthetic Reviewer', status='active')
        done = self.command(command, payload={'source': source, 'text': 'Synthetic calibration note.',
                                              'metadata': metadata})
        self.assertNotEqual(done.returncode, 0, done.stdout.decode('utf-8', errors='replace'))
        error = json.loads(done.stderr.decode('utf-8'))
        self.assertEqual(error['error'], 'ValueError')
        self.assertIn(message, error['message'])
        self.assertEqual(list((self.state / 'markdown-journal').glob('*')), [],
                         'invalid source must not leave a durable journal intent')
        self.assertFalse((self.vault / source).exists())
        self.assertEqual(self.source_files('knowledge'), knowledge_before)
        self.assertEqual(self.source_files('tasks'), tasks_before)

    def test_alternate_data_stream_name_is_rejected_before_journaling(self):
        self.refused('knowledge/a.md:akis.md', 'use a vault-relative Markdown path')

    def test_device_names_are_rejected_before_journaling(self):
        for name in ('CON.md', 'nul.md', 'COM1.md', 'LPT9.md', 'AUX/note.md'):
            with self.subTest(name=name):
                self.refused('knowledge/' + name, 'device paths are not sources')

    def test_trailing_dots_and_spaces_are_rejected_before_journaling(self):
        for name in ('nokta.', 'nokta./note.md', 'bosluk /note.md'):
            with self.subTest(name=name):
                self.refused('knowledge/' + name, 'only visible vault-relative sources are supported')

    def test_task_create_rejects_invalid_names_before_journaling(self):
        for name, message in (
                ('a.md:akis.md', 'use a vault-relative Markdown path'),
                ('CON.md', 'device paths are not sources'),
                ('NUL.md', 'device paths are not sources'),
                ('nokta./task.md', 'only visible vault-relative sources are supported'),
                ('bosluk /task.md', 'only visible vault-relative sources are supported')):
            with self.subTest(name=name):
                self.refused('tasks/' + name, message, command='task-create')

    def test_reserved_punctuation_controls_and_extended_device_names_are_rejected(self):
        for name in ('a<b.md', 'a>b.md', 'a"b.md', 'a|b.md', 'a?b.md', 'a*b.md', 'no\x01te.md'):
            with self.subTest(name=name):
                self.refused('knowledge/' + name, 'use a vault-relative Markdown path')
        for name in ('CONIN$.md', 'CONOUT$.md', 'COM\u00b9.md', 'LPT\u00b2.md', 'con .md'):
            with self.subTest(name=name):
                self.refused('knowledge/' + name, 'device paths are not sources')

    def test_existing_notes_with_portable_punctuation_still_read_and_edit(self):
        # These names are legal on macOS and Linux and sync already indexes them.
        # Create refuses them; read and edit of a file that is already there must not.
        bodies = {
            'knowledge/Neden?.md': 'Neden sorusu duruyor.\n',
            'knowledge/A|B.md': 'Boru adı duruyor.\n',
            'knowledge/"Alıntı".md': 'Tırnaklı ad duruyor.\n',
        }
        for relative, body in bodies.items():
            path = self.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body.encode('utf-8'))
        for relative, body in bodies.items():
            with self.subTest(source=relative, action='read'):
                read = self.succeeded('note-read', relative)
                self.assertEqual(read['source'], relative)
                self.assertIn(body.splitlines()[0], read['content'])
            with self.subTest(source=relative, action='edit'):
                path = self.vault / relative
                raw = path.read_bytes()
                edited = self.succeeded('note-edit', '--file', '-', payload={
                    'source': relative,
                    'expected_sha256': hashlib.sha256(raw).hexdigest(),
                    'edits': [{'old': body.splitlines()[0], 'new': body.splitlines()[0] + ' güncellendi.'}]})
                self.assertEqual(edited['status'], 'written')
                self.assertIn('güncellendi.', path.read_text(encoding='utf-8'))

    def test_note_edit_of_a_missing_note_uses_the_create_name_gate(self):
        source = 'notes/Yeni?.md'
        done = self.command('note-edit', '--file', '-', payload={
            'source': source, 'expected_sha256': None, 'content': 'Yeni not.\n'})
        self.assertNotEqual(done.returncode, 0)
        error = json.loads((done.stdout or done.stderr).decode('utf-8'))
        self.assertEqual(error['error'], 'ValueError')
        self.assertIn('use a vault-relative Markdown path', error['message'])
        self.assertFalse((self.vault / source).exists())

        turkish = 'notes/Yeni-çığ.md'
        created = self.succeeded('note-edit', '--file', '-', payload={
            'source': turkish, 'expected_sha256': None, 'content': 'Türkçe yeni not.\n'})
        self.assertEqual(created['status'], 'written')
        self.assertIn('Türkçe yeni not.', (self.vault / turkish).read_text(encoding='utf-8'))

    def test_valid_note_create_and_edit_still_work(self):
        source = 'knowledge/calibration.md'
        created = self.succeeded('note-create', payload={'source': source, 'text': 'Synthetic calibration note.',
                                                        'metadata': {'id': 'synthetic-valid'}})
        self.assertEqual(created['status'], 'succeeded')
        turkish = self.succeeded('note-create', payload={
            'source': 'knowledge/İş-çığ.md', 'text': 'Türkçe ad kalır.',
            'metadata': {'id': 'synthetic-turkish'}})
        self.assertEqual(turkish['status'], 'succeeded')
        self.assertTrue((self.vault / 'knowledge/İş-çığ.md').is_file())
        path = self.vault / source
        raw = path.read_bytes()
        edited = self.succeeded('note-edit', '--file', '-', payload={
            'source': source, 'expected_sha256': hashlib.sha256(raw).hexdigest(),
            'edits': [{'old': 'Synthetic calibration note.', 'new': 'Synthetic calibration updated.'}]})
        self.assertEqual(edited['status'], 'written')
        self.assertIn(b'Synthetic calibration updated.', path.read_bytes())
        self.assertEqual(list((self.state / 'markdown-journal').glob('*')), [])
        self.assertEqual(self.succeeded('sync')['status'], 'succeeded')
        self.succeeded('context', 'Synthetic calibration')


if __name__ == '__main__':
    unittest.main()
