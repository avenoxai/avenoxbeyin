"""Receipt field flags and JSON modes through the installed public entry point."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from v3_package_helpers import install, isolated_env, run_python, snapshot


class ReceiptCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-receipt-cli-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'Beyin Ölçüm'
        self.vault.mkdir()
        self.state = self.root / 'state'
        self.env = isolated_env(self.root / 'home')
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr.decode('utf-8'))
        (self.vault / 'notes').mkdir()
        for name in ('ilk.md', 'ikinci.md'):
            (self.vault / 'notes' / name).write_text('# Kaynak\n', encoding='utf-8')

    def cli(self, *args, payload=None):
        return run_python(self.vault / 'beyin.py', ['receipt', *args], self.vault, self.env, payload)

    def saved(self, event_id):
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            row = db.execute('SELECT payload FROM receipts WHERE id=?', (event_id,)).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row[0])

    def submit(self, *args, payload=None):
        result = self.cli(*args, payload=payload)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
        return json.loads(result.stdout.decode('utf-8'))

    def assert_parse_error(self, args, message):
        before = snapshot(self.vault), snapshot(self.state)
        result = self.cli(*args)
        self.assertEqual(result.returncode, 2, result.stderr.decode('utf-8'))
        self.assertIn(message, result.stderr.decode('utf-8'))
        self.assertEqual(result.stdout, b'')
        self.assertEqual((snapshot(self.vault), snapshot(self.state)), before)

    def test_flags_match_json_file_payload_and_repeated_refs(self):
        payload = {'event_id': 'same-payload', 'summary': 'Doğrulandı.\nÖğrenilen: yok',
                   'refs': ['notes/ikinci.md', 'notes/ilk.md'], 'session': 'claude-session'}
        flags = self.submit('--harness', 'claude', '--session', payload['session'],
                            '--event-id', payload['event_id'], '--summary', payload['summary'],
                            '--ref', payload['refs'][0], '--ref', payload['refs'][1])
        saved = self.saved(payload['event_id'])
        self.assertEqual({key: value for key, value in saved.items() if key != 'created_at'},
                         dict(payload, harness='claude'))
        source = self.vault / flags['source']
        original = source.read_bytes()
        receipt_json = self.root / 'receipt.json'
        receipt_json.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        json_mode = self.submit('--harness', 'claude', '--file', receipt_json)
        self.assertEqual(flags, json_mode)
        self.assertEqual(self.saved(payload['event_id']), saved)
        self.assertEqual(source.read_bytes(), original)

    def test_multiline_turkish_summary_is_preserved(self):
        summary = 'İş tamamlandı; çığ ve şüphe kontrol edildi.\nDoğrulama: geçti.\nÖğrenilen: yok'
        result = self.submit('--event-id', 'turkish', '--summary', summary, '--ref', 'notes/ilk.md')
        self.assertEqual(self.saved('turkish')['summary'], summary)
        self.assertIn((summary + '\n').encode('utf-8'), (self.vault / result['source']).read_bytes())

    def test_literal_backslash_n_is_not_expanded(self):
        summary = r'Doğrulandı.\nÖğrenilen: yok'
        self.submit('--event-id', 'literal', '--summary', summary, '--ref', 'notes/ilk.md')
        self.assertEqual(self.saved('literal')['summary'], summary)

    def test_summary_file_preserves_utf8_and_line_endings(self):
        summary = 'İlk satır.\r\nİkinci satır.\nÖğrenilen: yok'
        summary_file = self.root / 'özet.txt'
        summary_file.write_bytes(summary.encode('utf-8'))
        self.submit('--event-id', 'summary-file', '--summary-file', summary_file, '--ref', 'notes/ilk.md')
        self.assertEqual(self.saved('summary-file')['summary'], summary)
        self.submit('--file', '-', payload={'event_id': 'summary-file', 'summary': summary,
                                           'refs': ['notes/ilk.md']})

    def test_json_stdin_default_and_explicit_file_are_unchanged(self):
        for args in ([], ['--file', '-']):
            with self.subTest(args=args):
                event_id = 'stdin-' + str(len(args))
                payload = {'event_id': event_id, 'summary': 'Öğrenilen: yok', 'refs': ['notes/ilk.md']}
                self.submit(*args, '--harness', 'claude', payload=payload)
                saved = self.saved(event_id)
                self.assertEqual({key: value for key, value in saved.items() if key != 'created_at'},
                                 dict(payload, harness='claude'))

    def test_session_is_optional_and_harness_defaults_to_codex(self):
        self.submit('--event-id', 'optional-session', '--summary', 'Öğrenilen: yok', '--ref', 'notes/ilk.md')
        saved = self.saved('optional-session')
        self.assertNotIn('session', saved)
        self.assertEqual(saved['harness'], 'codex')

    def test_missing_event_id_exits_two(self):
        self.assert_parse_error(['--summary', 'Summary', '--ref', 'notes/ilk.md'],
                                '--event-id is required in flag mode')

    def test_missing_summary_exits_two(self):
        self.assert_parse_error(['--event-id', 'missing-summary', '--ref', 'notes/ilk.md'],
                                '--summary or --summary-file is required in flag mode')

    def test_both_summary_options_exit_two(self):
        self.assert_parse_error(['--event-id', 'both', '--summary', 'Summary', '--summary-file', 'unused.txt',
                                 '--ref', 'notes/ilk.md'], 'use exactly one of --summary and --summary-file')

    def test_missing_ref_exits_two(self):
        self.assert_parse_error(['--event-id', 'missing-ref', '--summary', 'Summary'],
                                '--ref is required in flag mode (at least one)')

    def test_file_cannot_be_combined_with_any_receipt_field_flag(self):
        for filename in ('-', 'receipt.json'):
            for flag, value in (('--event-id', 'mixed'), ('--summary', 'Summary'),
                                ('--summary-file', 'unused.txt'), ('--ref', 'notes/ilk.md'),
                                ('--session', 'session')):
                with self.subTest(filename=filename, flag=flag):
                    self.assert_parse_error(['--file', filename, flag, value],
                                            '--file cannot be combined with receipt flags')

    def test_missing_flag_values_exit_two(self):
        for flag in ('--event-id', '--summary', '--summary-file', '--ref', '--session'):
            with self.subTest(flag=flag):
                self.assert_parse_error([flag], 'expected one argument')

    def test_help_lists_json_and_flag_modes(self):
        result = self.cli('--help')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
        for flag in ('--file', '--harness', '--event-id', '--summary', '--summary-file', '--ref', '--session'):
            self.assertIn(flag, result.stdout.decode('utf-8'))


if __name__ == '__main__':
    unittest.main()
