"""Receipt field flags and JSON modes through the installed public entry point."""
from contextlib import closing
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_embedded_receipt_preserves_caller_output_encoding(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/beyin_v3.py'
        spec = importlib.util.spec_from_file_location('embedded_receipt_cli', script)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        for encoding in ('ascii', 'cp1252'):
            with self.subTest(encoding=encoding):
                with closing(io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors='backslashreplace')) as stdout, \
                     closing(io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors='backslashreplace')) as stderr:
                    before = (stdout.encoding, stdout.errors, stderr.encoding, stderr.errors)
                    with patch.object(sys, 'stdout', stdout), patch.object(sys, 'stderr', stderr), \
                         patch.object(sys, 'stdin', io.StringIO()):
                        result, code = cli.main(['--vault', str(self.vault), '--state', str(self.state),
                            'receipt', '--event-id', 'embedded-' + encoding,
                            '--summary', 'Ölçüm kaydedildi. Öğrenilen: yok', '--ref', 'notes/ilk.md'],
                            return_result=True)
                    self.assertEqual((code, result['status']), (0, 'succeeded'))
                    self.assertEqual((stdout.encoding, stdout.errors, stderr.encoding, stderr.errors), before)
                    self.assertEqual((stdout.buffer.getvalue(), stderr.buffer.getvalue()), (b'', b''))

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

    def test_summary_file_drops_utf8_bom(self):
        # Windows PowerShell 5.1 `Set-Content -Encoding UTF8` and `Out-File` write a BOM.
        summary = 'Öğrenilen: yok'
        summary_file = self.root / 'bom.txt'
        summary_file.write_bytes(b'\xef\xbb\xbf' + summary.encode('utf-8'))
        self.submit('--event-id', 'bom', '--summary-file', summary_file, '--ref', 'notes/ilk.md')
        self.assertEqual(self.saved('bom')['summary'], summary)

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

    def test_links_name_external_results_beside_vault_refs(self):
        pull = 'https://github.com/avenoxai/avenoxbeyin/pull/320#issuecomment-1'
        flags = self.submit('--harness', 'claude', '--event-id', 'linked', '--summary', 'PR açıldı.',
                            '--ref', 'notes/ilk.md', '--link', pull, '--link', 'http://örnek.test/a:b', '--link', pull)
        saved = self.saved('linked')
        self.assertEqual(saved['links'], [pull, 'http://örnek.test/a:b'])  # kept in order, once each
        self.assertEqual(saved['refs'], ['notes/ilk.md'])
        source = self.vault / flags['source']
        self.assertIn(pull, source.read_text(encoding='utf-8'))
        # JSON mode gives the same receipt; a different link set for the same event id is a collision.
        payload = {'event_id': 'linked', 'summary': 'PR açıldı.', 'refs': ['notes/ilk.md'],
                   'links': [pull, 'http://örnek.test/a:b']}
        self.assertEqual(self.submit('--harness', 'claude', '--file', '-', payload=payload), flags)
        changed = self.cli('--harness', 'claude', '--file', '-', payload=dict(payload, links=[pull]))
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn('collision', changed.stderr.decode('utf-8') + changed.stdout.decode('utf-8'))
        # Recap shows the links; a fresh state rebuilds them from the receipt source.
        recap = run_python(self.vault / 'beyin.py', ['recap'], self.vault, self.env)
        self.assertEqual(recap.returncode, 0, recap.stderr.decode('utf-8'))
        item = next(item for item in json.loads(recap.stdout.decode('utf-8'))['items'] if item['source'] == flags['source'])
        self.assertEqual(item['links'], saved['links'])
        (self.state / 'memory.sqlite3').unlink()
        rebuilt = run_python(self.vault / 'beyin.py', ['sync'], self.vault, self.env)
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr.decode('utf-8'))
        self.assertEqual(self.saved('linked')['links'], saved['links'])

    def test_receipt_without_links_is_unchanged(self):
        result = self.submit('--event-id', 'no-links', '--summary', 'Öğrenilen: yok', '--ref', 'notes/ilk.md')
        self.assertNotIn('links', self.saved('no-links'))
        self.assertNotIn('"links"', (self.vault / result['source']).read_text(encoding='utf-8'))

    def test_unsafe_links_are_refused_and_nothing_is_written(self):
        token = 'github_pat_' + 'A1b2C3d4E5' * 4
        for link in ('notes/ilk.md', 'ftp://example.com/a', 'javascript:alert(1)', 'https://user:pw@example.com/',
                     'https://example.com/a b', 'https://example.com/\x07', 'https:///no-host',
                     'https://example.com/?token=' + token, 'https://example.com/' + 'a' * 2048):
            with self.subTest(link=link[:40]):
                before = snapshot(self.vault)
                result = self.cli('--event-id', 'unsafe', '--summary', 'Özet', '--ref', 'notes/ilk.md', '--link', link)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(snapshot(self.vault), before)  # no receipt source; state may hold a first sync
        too_many = {'event_id': 'many', 'summary': 'Özet', 'refs': ['notes/ilk.md'],
                    'links': ['https://example.com/' + str(n) for n in range(21)]}
        self.assertNotEqual(self.cli('--file', '-', payload=too_many).returncode, 0)

    def test_help_lists_json_and_flag_modes(self):
        result = self.cli('--help')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
        for flag in ('--file', '--harness', '--event-id', '--summary', '--summary-file', '--ref', '--link', '--session'):
            self.assertIn(flag, result.stdout.decode('utf-8'))

    def test_synced_links_have_the_same_secret_and_count_gate(self):
        token = 'github_pat_' + 'A1b2C3d4E5' * 4
        cases = [
            ['https://example.com/?token=' + token],
            ['https://example.com/?token=' + token.replace('_', '%5F')],
            ['https://example.com/' + str(n) for n in range(21)],
        ]
        receipts = self.vault / 'receipts'
        receipts.mkdir(exist_ok=True)
        for index, links in enumerate(cases):
            ident = 'remote-unsafe-' + str(index)
            path = receipts / (hashlib.sha256(ident.encode()).hexdigest() + '.md')
            data = {'kind': 'receipt', 'event_id': ident, 'harness': 'codex', 'refs': ['notes/ilk.md'],
                    'links': links, 'visibility': 'internal', 'created_at': '2026-10-12T12:00:00+00:00'}
            path.write_text('---\n' + json.dumps(data) + '\n---\nRemote outcome\n', encoding='utf-8')
        before = snapshot(receipts)
        result = run_python(self.vault / 'beyin.py', ['sync'], self.vault, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'degraded')
        self.assertEqual(snapshot(receipts), before)
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 0)
        recap = run_python(self.vault / 'beyin.py', ['recap'], self.vault, self.env)
        self.assertNotIn(token.encode(), recap.stdout)
        self.assertEqual(json.loads(recap.stdout)['items'], [])


if __name__ == '__main__':
    unittest.main()
