"""Error reporting (#587): a missing note is said, a broken settings file names itself, and an
error a person reads keeps its Turkish characters as UTF-8 instead of \\u escapes.

The legacy code page cases set PYTHONIOENCODING, the same text layer a piped Windows console
(cp1254, cp1252) gives Python; the JSON must stay valid UTF-8 there and must not crash.
"""
from contextlib import closing
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env, install, isolated_env, run_python

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts/beyin_v3.py'
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_preferences as preferences  # noqa: E402


def escaped(text):
    """The \\u-escaped form ensure_ascii=True wrote before #587."""
    return json.dumps(text)[1:-1].encode('ascii')


def load_cli():
    spec = importlib.util.spec_from_file_location('error_reporting_cli', CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceCliErrorTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='v3-error-reporting-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()

    def run_cli(self, *args, encoding='utf-8'):
        env = inherited_env(PYTHONIOENCODING=encoding, PYTHONDONTWRITEBYTECODE='1',
                            BEYIN_V3_NO_SPAWN='1', BEYIN_UPDATES_OFF='1')
        return subprocess.run([sys.executable, str(CLI), '--vault', str(self.vault), '--state', str(self.state),
                               *map(str, args)], capture_output=True, stdin=subprocess.DEVNULL, env=env, timeout=60)

    def test_note_read_says_whether_the_note_exists_and_must_exist_is_an_error(self):
        note = self.vault / 'Görüşmeler' / 'Not.md'
        note.parent.mkdir()
        note.write_bytes('# Görüşme notu\n'.encode('utf-8'))
        missing = self.run_cli('note-read', 'Görüşmeler/Yok.md')
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertEqual(json.loads(missing.stdout),
                         {'source': 'Görüşmeler/Yok.md', 'exists': False, 'sha256': None, 'content': None})
        found = json.loads(self.run_cli('note-read', 'Görüşmeler/Not.md').stdout)
        self.assertEqual((found['exists'], found['content']), (True, '# Görüşme notu\n'))

        # Exit 1 and status error, the documented note error; exit 2 stays "conflict".
        refused = self.run_cli('note-read', 'Görüşmeler/Yok.md', '--must-exist')
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertEqual(refused.stderr, b'')
        error = json.loads(refused.stdout.decode('utf-8'))
        self.assertEqual((error['status'], error['error']), ('error', 'FileNotFoundError'))
        self.assertEqual(error['message'], 'note not found: Görüşmeler/Yok.md')
        self.assertFalse((self.vault / 'Görüşmeler/Yok.md').exists())
        present = self.run_cli('note-read', '--must-exist', 'Görüşmeler/Not.md')
        self.assertEqual(present.returncode, 0, present.stdout + present.stderr)
        self.assertEqual(json.loads(present.stdout), found)

    def test_error_json_keeps_real_characters_as_utf8_under_a_legacy_code_page(self):
        request = self.root / 'Şifre-yok.json'
        for encoding in ('cp1252', 'ascii'):
            with self.subTest(encoding=encoding):
                cases = (
                    # Generic command error on stderr.
                    (('--json', 'task-update', '--file', request), 1, 'stderr', 'Şifre-yok.json'),
                    # Note commands keep their JSON contract on stdout.
                    (('note-edit', '--file', self.root / 'Müşteri.json'), 1, 'stdout', 'Müşteri.json'),
                    # Usage errors: argparse in --json mode, and the note usage error (exit 64).
                    (('--json', 'recap', '--days', 'ş'), 2, 'stderr', "invalid int value: 'ş'"),
                    (('note-read', 'x.md', '--şifre'), 64, 'stdout', '--şifre'),
                )
                for args, code, channel, text in cases:
                    with self.subTest(args=args[:2]):
                        done = self.run_cli(*args, encoding=encoding)
                        self.assertEqual(done.returncode, code, done.stdout + done.stderr)
                        raw = done.stderr if channel == 'stderr' else done.stdout
                        other = done.stdout if channel == 'stderr' else done.stderr
                        self.assertEqual(other, b'')
                        self.assertIn(text.encode('utf-8'), raw)
                        self.assertNotIn(escaped(text), raw)
                        self.assertIn(text, json.loads(raw.decode('utf-8'))['message'])

    def test_hidden_and_reordering_characters_stay_escaped(self):
        # Turkish letters print as they are; a bidi override, a line separator or a lone
        # surrogate would hide or reorder text, or break UTF-8, so it stays escaped.
        value = {'message': 'Erişim \N{RIGHT-TO-LEFT OVERRIDE}engellendi\N{LINE SEPARATOR}'
                            ' \ud800 \U000e0001 \U0001f600'}
        text = load_cli().readable_json(value)
        self.assertEqual(json.loads(text), value)
        self.assertIn('Erişim', text)
        self.assertIn('\U0001f600', text)
        for hidden in ('\\u202e', '\\u2028', '\\ud800', '\\udb40\\udc01'):
            self.assertIn(hidden, text)
        text.encode('utf-8')  # never a lone surrogate on the stream
        # argparse names an unknown option without repr(), so the raw character reaches the JSON.
        option = '--a\N{RIGHT-TO-LEFT OVERRIDE}b'
        done = self.run_cli('note-read', 'x.md', option, encoding='cp1252')
        self.assertEqual(done.returncode, 64, done.stdout + done.stderr)
        self.assertNotIn(option.encode('utf-8'), done.stdout)
        self.assertIn(b'--a\\u202eb', done.stdout)
        self.assertEqual(json.loads(done.stdout)['message'], 'unrecognized arguments: ' + option)

    def test_embedded_error_returns_the_result_and_leaves_caller_streams_alone(self):
        cli = load_cli()
        with closing(io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='backslashreplace')) as stdout, \
             closing(io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='backslashreplace')) as stderr:
            with patch.object(sys, 'stdout', stdout), patch.object(sys, 'stderr', stderr), \
                 patch.object(sys, 'stdin', io.StringIO()):
                error, code = cli.main(['--vault', str(self.vault), '--state', str(self.state),
                                        'task-update', '--file', str(self.root / 'Şifre-yok.json')],
                                       return_result=True)
            self.assertEqual((code, error['error']), (1, 'FileNotFoundError'))
            self.assertIn('Şifre-yok.json', error['message'])
            self.assertEqual((stdout.encoding, stderr.encoding), ('cp1252', 'cp1252'))
            self.assertEqual((stdout.buffer.getvalue(), stderr.buffer.getvalue()), (b'', b''))

    def test_stream_that_cannot_take_utf8_still_gets_valid_escaped_json(self):
        class FixedEncoding(io.TextIOWrapper):
            def reconfigure(self, **_):
                raise io.UnsupportedOperation('fixed encoding')

        cli = load_cli()
        with closing(FixedEncoding(io.BytesIO(), encoding='ascii')) as stdout, \
             closing(FixedEncoding(io.BytesIO(), encoding='ascii')) as stderr:
            with patch.object(sys, 'stdout', stdout), patch.object(sys, 'stderr', stderr), \
                 patch.object(sys, 'stdin', io.StringIO()):
                code = cli.main(['--vault', str(self.vault), '--state', str(self.state),
                                 'task-update', '--file', str(self.root / 'Şifre-yok.json')])
                stderr.flush()
            self.assertEqual(code, 1)
            self.assertEqual(stdout.buffer.getvalue(), b'')
            error = json.loads(stderr.buffer.getvalue().decode('ascii'))
            self.assertIn('Şifre-yok.json', error['message'])


class SettingsFileErrorTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='v3-settings-error-')
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name)

    def test_unparseable_settings_name_the_file_and_keep_the_error_type(self):
        cases = (('.beyin-preferences.json', preferences.read),
                 ('.beyin-daily-layout.json', preferences.read_daily_layout))
        for name, read in cases:
            for raw, kind in ((b'{"auto_sync": }', json.JSONDecodeError), (b'{"a": "\xff"}', UnicodeDecodeError)):
                with self.subTest(name=name, raw=raw):
                    (self.vault / name).write_bytes(raw)
                    with self.assertRaises(kind) as caught:
                        read(self.vault)
                    self.assertIn(name, str(caught.exception))
                    self.assertIsInstance(caught.exception, ValueError)
        broken = b'{"auto_sync": }'
        (self.vault / '.beyin-preferences.json').write_bytes(broken)
        with self.assertRaisesRegex(json.JSONDecodeError, r'^\.beyin-preferences\.json is not valid JSON: '
                                                          r'Expecting value: line 1 column 15 \(char 14\)$'):
            preferences.save(self.vault, {'auto_sync': False})
        # A malformed user file is reported, never overwritten.
        self.assertEqual((self.vault / '.beyin-preferences.json').read_bytes(), broken)


class InstalledEntryErrorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix='v3-entry-errors-')
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name)
        cls.vault, cls.state = cls.root / 'vault', cls.root / 'state'
        cls.vault.mkdir()
        cls.env = dict(isolated_env(cls.root / 'home'), BEYIN_UPDATES_OFF='1')
        installed = install(cls.vault, cls.state, cls.env)
        if installed.returncode:
            raise AssertionError(installed.stderr.decode('utf-8', errors='replace'))

    def run_entry(self, *args, encoding='utf-8'):
        return run_python(self.vault / 'beyin.py', args, self.vault, dict(self.env, PYTHONIOENCODING=encoding))

    def broken(self, name, raw):
        path = self.vault / name
        original = path.read_bytes() if path.exists() else None
        path.write_bytes(raw)
        self.addCleanup(lambda: path.write_bytes(original) if original is not None else path.unlink())

    def test_broken_runtime_file_is_named_in_json_and_human_mode(self):
        self.broken('.beyin-runtime.json', b'{"state": }')
        for mode in ('--json', '--human'):
            with self.subTest(mode=mode):
                done = self.run_entry('doctor', mode)
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertEqual(done.stdout, b'')
                stderr = done.stderr.decode('utf-8')
                self.assertIn('.beyin-runtime.json gecerli JSON degil (Expecting value: line 1 column 11', stderr)
                if mode == '--json':
                    self.assertEqual(json.loads(stderr)['error'], 'ValueError')

    def test_broken_preferences_file_is_named_by_the_installed_cli(self):
        self.broken('.beyin-preferences.json', b'{"auto_sync": }')
        for command in ('doctor', 'preferences'):
            with self.subTest(command=command):
                done = self.run_entry('--json', command)
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertEqual(done.stdout, b'')
                error = json.loads(done.stderr.decode('utf-8'))
                self.assertEqual(error['error'], 'JSONDecodeError')
                self.assertTrue(error['message'].startswith('.beyin-preferences.json is not valid JSON: '), error)
        self.assertEqual((self.vault / '.beyin-preferences.json').read_bytes(), b'{"auto_sync": }')

    def test_entry_point_errors_keep_real_characters_under_a_legacy_code_page(self):
        package = self.root / 'Şifre-paket.zip'
        cases = ((('--json', 'update', '--package', package), 1, 'Şifre-paket.zip'),
                 (('--json', 'update', '--şifre'), 2, '--şifre'))
        for args, code, text in cases:
            with self.subTest(args=args[1:3]):
                done = self.run_entry(*args, encoding='cp1252')
                self.assertEqual(done.returncode, code, done.stderr)
                self.assertEqual(done.stdout, b'')
                self.assertIn(text.encode('utf-8'), done.stderr)
                self.assertNotIn(escaped(text), done.stderr)
                self.assertIn(text, json.loads(done.stderr.decode('utf-8'))['message'])


if __name__ == '__main__':
    unittest.main()
