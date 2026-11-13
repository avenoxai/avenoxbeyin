"""Fail closed before rollback can expose copied facts to an older reader."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from v3_update_test import load_updater


class HomepageRollbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Match updater journal paths, including macOS /var aliases and Windows short paths.
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'vault'
        self.state = self.root / 'state'
        self.vault.mkdir()
        self.state.mkdir()
        self.updater = load_updater()
        self.module = self.vault / '.claude/scripts/beyin_v3_homepages.py'
        self.module.parent.mkdir(parents=True)
        self.module.write_bytes(b'HOMEPAGE_ROLLBACK_POLICY = 1\n')
        self.note = self.vault / 'notes/Page.md'
        self.note.parent.mkdir()
        self.backup = dict(schema=1, vault=str(self.vault), direction='update', operations=[
            dict(scope='vault', name='.claude/scripts/beyin_v3_homepages.py',
                 old=None, new=self.updater.encode(self.module.read_bytes()))])
        (self.state / 'last-update.json').write_text(json.dumps(self.backup))

    def test_bound_or_malformed_projection_blocks_before_any_restore(self):
        for marker in ('<!-- beyin-field:{broken', '<!-- beyin-homepage-create:broken',
                       '---\n{"workspace_homepage": true}\n---\nOld copied value'):
            with self.subTest(marker=marker):
                self.note.write_text(marker)
                before = self.module.read_bytes()
                with self.assertRaisesRegex(ValueError, 'rollback blocked'):
                    self.updater.rollback(self.vault, self.state)
                self.assertEqual(self.module.read_bytes(), before)
                self.assertEqual(self.note.read_text(), marker)
                self.assertFalse((self.state / 'update-journal.json').exists())
                self.assertTrue((self.state / 'last-update.json').exists())

    def test_ordinary_notes_do_not_block_rollback(self):
        self.note.write_text('Human note, no derived copy.')
        result = self.updater.rollback(self.vault, self.state)
        self.assertEqual(result['status'], 'uninstalled')
        self.assertFalse(self.module.exists())
        self.assertEqual(self.note.read_text(), 'Human note, no derived copy.')

    def test_supported_reader_keeps_rollback_available(self):
        self.note.write_text('<!-- beyin-field:{bound}')
        self.backup['operations'][0]['old'] = self.updater.encode(b'HOMEPAGE_ROLLBACK_POLICY = 1\n# older safe reader\n')
        (self.state / 'last-update.json').write_text(json.dumps(self.backup))
        self.updater.rollback(self.vault, self.state)
        self.assertIn(b'older safe reader', self.module.read_bytes())

    def test_interrupted_rollback_rechecks_before_resuming(self):
        self.note.write_text('<!-- beyin-field:{bound}')
        operation = dict(self.backup['operations'][0])
        operation['old'], operation['new'] = operation['new'], operation['old']
        journal = dict(schema=1, vault=str(self.vault), direction='rollback', operations=[operation])
        (self.state / 'update-journal.json').write_text(json.dumps(journal))
        with self.assertRaisesRegex(ValueError, 'rollback blocked'):
            self.updater.rollback(self.vault, self.state)
        self.assertTrue(self.module.exists())

    def test_recover_rechecks_pending_rollback(self):
        self.note.write_text('<!-- beyin-field:{bound}')
        operation = dict(self.backup['operations'][0])
        operation['old'], operation['new'] = operation['new'], operation['old']
        journal = dict(schema=1, vault=str(self.vault), direction='rollback', operations=[operation])
        (self.state / 'update-journal.json').write_text(json.dumps(journal))
        with self.assertRaisesRegex(ValueError, 'rollback blocked'):
            self.updater.recover(self.vault, self.state)
        self.assertTrue(self.module.exists())

    def test_marker_split_across_read_boundary_blocks(self):
        self.note.write_text('x' * 65530 + '<!-- beyin-field:{bound}')
        with self.assertRaisesRegex(ValueError, 'rollback blocked'):
            self.updater.rollback(self.vault, self.state)
        self.assertTrue(self.module.exists())

    def test_unreadable_markdown_blocks_preflight(self):
        self.note.write_text('human note')
        original = Path.open
        def fail(path, *args, **kwargs):
            if path == self.note:
                raise PermissionError('synthetic unreadable note')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', fail):
            with self.assertRaisesRegex(ValueError, 'cannot verify homepage safety'):
                self.updater.rollback(self.vault, self.state)
        self.assertTrue(self.module.exists())
        self.assertFalse((self.state / 'update-journal.json').exists())


if __name__ == '__main__':
    unittest.main()
