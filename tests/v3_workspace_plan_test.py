"""Independent planner foundations while the G0 profile contract is pending."""
import copy
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
from beyin_v3_workspace_plan import (FileDelta, canonical_bytes, content_hash,
                                     file_deltas, preview_files, write_set_hash)


class PurePlanTest(unittest.TestCase):
    def test_deterministic_no_io_and_preserves_unmentioned_identity(self):
        old = {('vault', 'Core.md'): b'user identity',
               ('vault', 'notes/Ders.md'): 'El ile yazıldı.\r\n'.encode()}
        new = {('vault', 'notes/Ders.md'): 'El ile yazıldı.\r\nYeni kaynak.\r\n'.encode(),
               ('state', 'example.json'): b'{}\n'}
        before = copy.deepcopy((old, new))
        with patch('builtins.open', side_effect=AssertionError('planner performs IO')):
            changes = file_deltas(old, new)
            result = preview_files(changes)
        self.assertEqual((old, new), before)
        self.assertEqual(len(changes), 2)
        self.assertNotIn('Core.md', result['text'])
        self.assertEqual(changes, file_deltas(dict(reversed(list(old.items()))), dict(reversed(list(new.items())))))
        self.assertEqual(write_set_hash(changes), write_set_hash(tuple(reversed(changes))))
        self.assertIn('Yeni kaynak.', result['text'])

    def test_actual_bytes_are_bound_and_missing_is_not_empty(self):
        original = FileDelta('vault', 'a.md', None, b'')
        changed = FileDelta('vault', 'a.md', None, b'\n')
        other_target = FileDelta('vault', 'b.md', None, b'')
        self.assertIsNone(content_hash(None))
        self.assertEqual(content_hash(b''), hashlib.sha256(b'').hexdigest())
        self.assertEqual(len({write_set_hash([delta]) for delta in (original, changed, other_target)}), 3)
        self.assertNotEqual(content_hash(b'x\r\n'), content_hash(b'x\n'))
        with self.assertRaises(ValueError):
            write_set_hash([original, original])

    def test_unchanged_not_rewritten_and_explicit_deletion(self):
        self.assertEqual(file_deltas({('vault', 'a.md'): b'a'}, {('vault', 'a.md'): b'a'}), ())
        deletion = file_deltas({('vault', 'a.md'): b'a'}, {('vault', 'a.md'): None})
        self.assertEqual(deletion[0].summary()['action'], 'delete')
        with self.assertRaises(ValueError):
            FileDelta('vault', 'a.md', b'a', b'a')

    def test_rejects_path_escape_and_cross_platform_aliases(self):
        for name in ('/etc/a', '../a', 'a/../b', 'a//b', './a', 'C:/a', 'a\\b',
                     'a\x00b', 'a\nb', 'NUL.md', 'a/COM1', 'a.', 'a ', 'a:b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                file_deltas({}, {('vault', name): b'x'})
        for names in [('A.md', 'a.md'), ('é.md', 'e\u0301.md'), ('a', 'A/b')]:
            with self.subTest(names=names), self.assertRaises(ValueError):
                file_deltas({}, {('vault', name): b'x' for name in names})
        with self.assertRaises(ValueError):
            file_deltas({}, {('global', 'settings.json'): b'{}'})

    def test_canonical_hash_rejects_lossy_coercion(self):
        self.assertEqual(canonical_bytes({'b': ['ş', True], 'a': 1}),
                         canonical_bytes({'a': 1, 'b': ['ş', True]}))
        self.assertEqual(canonical_bytes({'score': 1.1}), b'{"score":1.1}')
        for value in (float('nan'), float('inf'), {1: 'silently coerced key'}, b'bytes', (1, 2)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_bytes(value)

    def test_preview_does_not_hide_truncation_or_binary(self):
        delta = FileDelta('vault', 'a.md', b'a\n', b'a' * 2000)
        result = preview_files([delta], max_chars=256)
        self.assertTrue(result['truncated'])
        self.assertIn('Preview truncated', result['text'])
        self.assertEqual(result['write_set_hash'], write_set_hash([delta]))
        binary = preview_files([FileDelta('state', 'b.bin', None, b'\xff')])
        self.assertIn('Binary', binary['text'])
        self.assertIn('Final newline changed', preview_files([FileDelta('vault', 'x', b'a', b'a\n')])['text'])


if __name__ == '__main__':
    unittest.main()
