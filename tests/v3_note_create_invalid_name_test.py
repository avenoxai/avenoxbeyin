"""Portable note-create and task-create names must be rejected before durable journal writes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unicodedata
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

    def refused(self, source, message, command='note-create', alias_of=None):
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
        # macOS opens an existing file under its other Unicode form, and Windows can answer for a
        # device name; the listings below prove nothing was created there either.
        if alias_of is None and not (os.name == 'nt' and 'device' in message):
            self.assertFalse(os.path.lexists(self.vault / source))  # an over-long name makes Path.exists raise on 3.11
        self.assertEqual(self.source_files('knowledge'), knowledge_before)
        self.assertEqual(self.source_files('tasks'), tasks_before)

    def test_alternate_data_stream_name_is_rejected_before_journaling(self):
        self.refused('knowledge/a.md:akis.md', 'Windows cannot store (:)')

    def test_device_names_are_rejected_before_journaling(self):
        for name in ('CON.md', 'nul.md', 'COM1.md', 'LPT9.md', 'AUX/note.md', 'COM0.md', 'lpt0.md',
                     'Con.tar.md', 'prn'):
            with self.subTest(name=name):
                self.refused('knowledge/' + name, 'reserved Windows device name')

    def test_trailing_dots_and_spaces_are_rejected_before_journaling(self):
        for name in ('nokta.', 'nokta./note.md', 'bosluk /note.md', 'not.md '):
            with self.subTest(name=name):
                self.refused('knowledge/' + name, 'ends with a dot or space')

    def test_task_create_rejects_invalid_names_before_journaling(self):
        for name, message in (
                ('a.md:akis.md', 'Windows cannot store (:)'),
                ('CON.md', 'device paths are not sources'),
                ('NUL.md', 'device paths are not sources'),
                ('nokta./task.md', 'ends with a dot or space'),
                ('bosluk /task.md', 'ends with a dot or space'),
                ('a?b.md', 'Windows cannot store (?)'),
                ('b' * 300 + '.md', 'at most 255')):
            with self.subTest(name=name):
                self.refused('tasks/' + name, message, command='task-create')

    def test_reserved_punctuation_controls_and_extended_device_names_are_rejected(self):
        for name in ('a<b.md', 'a>b.md', 'a"b.md', 'a|b.md', 'a?b.md', 'a*b.md', 'no\x01te.md', 'sek\tme.md',
                     'alt/sat\nir.md'):
            with self.subTest(name=name):
                self.refused('knowledge/' + name, 'characters Windows cannot store')
        self.refused('knowledge/geri\\bolu.md', 'use a vault-relative Markdown path with / separators')
        for name in ('CONIN$.md', 'CONOUT$.md', 'COM\u00b9.md', 'LPT\u00b2.md', 'con .md'):
            with self.subTest(name=name):
                self.refused('knowledge/' + name, 'device paths are not sources')

    @unittest.skipIf(os.name == 'nt', 'These names cannot be created on Windows.')
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
        self.assertIn('Windows cannot store (?)', error['message'])
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

    def test_over_long_names_are_rejected_before_journaling(self):
        # 128 two-byte letters: 259 UTF-8 bytes with the extension, over ext4's 255-byte name limit.
        self.refused('knowledge/' + '\u015f' * 128 + '.md', 'at most 255')
        self.refused('knowledge/' + 'a' * 300 + '.md', 'at most 255')
        self.refused('knowledge/' + '\u00e7' * 130 + '/not.md', 'at most 255')
        deep = 'knowledge/' + '/'.join(['\u00e7' * 100] * 3) + '/not.md'
        self.refused(deep, 'a new source may be at most 512')
        # Before the gate, task-create journaled the intent, failed the write with ENAMETOOLONG
        # and left every later sync in conflict.
        self.refused('tasks/' + 'b' * 300 + '.md', 'at most 255', command='task-create')
        self.assertEqual(self.succeeded('sync')['status'], 'succeeded')

    def test_name_at_the_byte_limit_is_still_created(self):
        source = 'knowledge/' + '\u015f' * 126 + '.md'  # exactly 255 UTF-8 bytes
        self.assertEqual(len(source.split('/')[-1].encode('utf-8')), 255)
        created = self.succeeded('note-create', payload={'source': source, 'text': 'Sinirdaki ad.',
                                                        'metadata': {'id': 'synthetic-limit'}})
        self.assertEqual((created['status'], created['source']), ('succeeded', source))
        self.assertTrue((self.vault / source).is_file())

    def test_portable_names_near_the_rules_are_still_created(self):
        for index, name in enumerate(('CONSOLE.md', 'COM10.md', 'lpt.md', 'auxiliary.md', 'nokta..md',
                                      '\U0001F3AF Hedef notu.md', 'Arşiv/Müşteri Şifre notu.md', 'İŞ-ığüşöç.md')):
            with self.subTest(name=name):
                created = self.succeeded('note-create', payload={
                    'source': 'knowledge/' + name, 'text': 'Gecerli ad.', 'metadata': {'id': 'synthetic-near-%d' % index}})
                self.assertEqual((created['status'], created['source']), ('succeeded', 'knowledge/' + name))
                self.assertTrue((self.vault / 'knowledge' / name).is_file())
        task = self.succeeded('task-create', payload={'source': 'tasks/Görev-çığ.md', 'text': 'Gecerli gorev.',
                                                      'metadata': {'id': 'synthetic-near-task', 'owner': 'Synthetic Reviewer',
                                                                   'status': 'active'}})
        self.assertEqual(task['source'], 'tasks/Görev-çığ.md')

    def unicode_fixture(self):
        folder, note = 'knowledge/\u00c7al\u0131\u015fma', 'knowledge/\u00d6zet.md'  # NFC on disk
        (self.vault / folder).mkdir(parents=True)
        (self.vault / folder / 'var.md').write_bytes('Var olan not.\n'.encode('utf-8'))
        (self.vault / note).write_bytes('Var olan ozet.\n'.encode('utf-8'))
        (self.vault / 'tasks/\u00c7al\u0131\u015fma').mkdir(parents=True)
        self.assertEqual(self.succeeded('sync')['status'], 'succeeded')
        return folder, note

    def nfc_names(self, folder):
        return sorted(unicodedata.normalize('NFC', path.name) for path in (self.vault / folder).iterdir())

    def test_another_unicode_form_of_an_existing_name_is_rejected_before_journaling(self):
        folder, note = self.unicode_fixture()
        nfd = lambda text: unicodedata.normalize('NFD', text)
        self.assertNotEqual(nfd(folder), folder)
        for source, command, existing in ((nfd(folder) + '/yeni.md', 'note-create', folder),
                                          (nfd(note), 'note-create', note),
                                          (nfd('tasks/\u00c7al\u0131\u015fma/g\u00f6rev.md'), 'task-create',
                                           'tasks/\u00c7al\u0131\u015fma')):
            with self.subTest(source=source):
                self.refused(source, repr(existing) + ' already exists in another Unicode form',
                             command=command, alias_of=existing)
        # One folder and one note per name remain, whatever the volume does with Unicode forms.
        self.assertEqual(self.nfc_names('knowledge'), sorted([unicodedata.normalize('NFC', '\u00c7al\u0131\u015fma'),
                                                              '\u00d6zet.md']))
        self.assertEqual(self.nfc_names(folder), ['var.md'])
        self.assertEqual((self.vault / note).read_bytes(), 'Var olan ozet.\n'.encode('utf-8'))
        self.assertEqual(self.succeeded('sync')['status'], 'succeeded')

    def test_note_edit_create_never_adds_a_second_unicode_form(self):
        folder, _ = self.unicode_fixture()
        source = unicodedata.normalize('NFD', folder) + '/yeni.md'
        done = self.command('note-edit', '--file', '-', payload={'source': source, 'expected_sha256': None,
                                                                 'content': 'Yeni not.\n'})
        if os.path.lexists(self.vault / unicodedata.normalize('NFD', folder)):
            # macOS opens the existing folder for either form; note-edit writes under its stored spelling.
            self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
            self.assertEqual(json.loads(done.stdout.decode('utf-8'))['source'], folder + '/yeni.md')
        else:
            self.assertNotEqual(done.returncode, 0)
            error = json.loads((done.stdout or done.stderr).decode('utf-8'))
            self.assertEqual(error['error'], 'ValueError')
            self.assertIn('already exists in another Unicode form', error['message'])
        self.assertEqual(len(self.nfc_names('knowledge')), 2)
        self.assertEqual(list((self.state / 'markdown-journal').glob('*')), [])


if __name__ == '__main__':
    unittest.main()
