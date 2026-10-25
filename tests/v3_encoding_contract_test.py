"""Synthetic UTF-8 boundary regressions; no real vault, network or model calls."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_workspaces import canonical_bytes, parse_document, content_hash, WorkspaceError, validate_document
import v3_workspace_sources_test as source_tests

spec = importlib.util.spec_from_file_location('encoding_audit', ROOT / 'scripts/audit_text_encoding.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
UNICODE = 'İ/I/ı/i ğ/Ğ ş/Ş ö/Ö ü/Ü ç/Ç 日本語 العربية 🧠 e\u0301 é'


class EncodingContractTest(unittest.TestCase):
    def test_canonical_json_unicode_path_and_exact_bytes(self):
        value = {'z': UNICODE, 'a': ['e\u0301', 'é']}
        expected = ('{"a":["e\u0301","é"],"z":' + json.dumps(UNICODE, ensure_ascii=False) + '}').encode('utf-8')
        raw = canonical_bytes(value)
        self.assertEqual(raw, expected)
        self.assertEqual(canonical_bytes(parse_document(raw)), raw)
        self.assertNotEqual(value['a'][0].encode('utf-8'), value['a'][1].encode('utf-8'))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ('İ-学-🧠.json')
            path.write_bytes(raw)
            self.assertEqual(parse_document(path.read_bytes()), value)
            self.assertEqual(content_hash(path.read_bytes()), content_hash(expected))
            self.assertEqual(path.read_bytes(), raw)

    def test_invalid_json_never_falls_back_to_locale_or_bom(self):
        for raw, error in [(b'{"text":"\xff"}', 'invalid_json'),
                           (b'\xef\xbb\xbf{}', 'invalid_json'),
                           (b'{"a":1,"a":2}', 'duplicate_json_key'),
                           (b'{"a":NaN}', 'invalid_number')]:
            with self.subTest(raw=raw), self.assertRaisesRegex(WorkspaceError, error):
                parse_document(raw)
        with self.assertRaises(WorkspaceError):
            validate_document({'type': 'invalid', 'data': {'text': UNICODE}})

    def source_harness(self):
        harness = source_tests.WorkspaceSourcesTest('test_text_content_search_ref_and_original_source_preserved')
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        return harness

    def test_source_newlines_unicode_and_hash_remain_exact(self):
        harness = self.source_harness()
        for index, ending in enumerate(('\n', '\r\n', '')):
            with self.subTest(ending=ending):
                name = f'İ-学-🧠-{index}.txt'
                path = harness.material / name
                raw = (UNICODE + ending + 'son' + ending).encode('utf-8')
                path.write_bytes(raw)
                result = harness.resolver.extract('academic', 'materials', name, 'user')
                self.assertEqual(result['state'], 'ready')
                reference = result['chunks'][0]['source_ref']
                self.assertEqual(reference['content_sha256'], content_hash(raw))
                self.assertEqual(harness.resolver.read(reference, 'academic', 'user'), raw)
                self.assertEqual(path.read_bytes(), raw)
                path.write_bytes(raw + b'x')
                with self.assertRaisesRegex(WorkspaceError, 'stale_source'):
                    harness.resolver.read(reference, 'academic', 'user')

    def test_invalid_source_decode_is_failed_and_source_is_untouched(self):
        harness = self.source_harness()
        path = harness.material / 'İ-geçersiz.txt'
        raw = b'valid prefix\r\n\xff\xfeinvalid'
        path.write_bytes(raw)
        result = harness.resolver.extract('academic', 'materials', path.name, 'user')
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['chunks'], [])
        self.assertEqual(path.read_bytes(), raw)
        harness.auth.revoke('user')
        with self.assertRaisesRegex(WorkspaceError, 'scope_denied'):
            harness.resolver.extract('academic', 'materials', path.name, 'user')
        self.assertEqual(path.read_bytes(), raw)

    def test_static_gate_finds_variable_path_and_subprocess_without_locale(self):
        source = "p.read_text(); p.write_text('x'); open('x'); p.open(); subprocess.run([], text=True)"
        self.assertEqual(sum(row['implicit'] for row in audit.inspect_source(source, 'synthetic.py')), 5)
        allowed = "p.read_bytes(); p.write_text('x', encoding='utf-8'); open('x', 'rb'); p.open('rb'); os.open('x', os.O_RDONLY); subprocess.run([], text=True, encoding='locale'); archive.open(entry)"
        self.assertFalse(any(row['implicit'] for row in audit.inspect_source(allowed, 'synthetic.py')))

    def test_scoped_product_gate_has_no_implicit_locale_boundaries(self):
        failures = [row for row in audit.inventory() if row['implicit'] and audit.gated(row['path'])]
        self.assertEqual(failures, [])


if __name__ == '__main__':
    unittest.main()
