import unittest
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

# Ensure the scripts directory is in sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / 'template/.claude/scripts'))


class TestHygieneAndCompactRaceEdgeCases(unittest.TestCase):

    def test_beyin_v3_hygiene_save_inbox_settings_unlink_crash(self):
        from beyin_v3_hygiene import save_inbox_settings
        with TemporaryDirectory() as d:
            state = Path(d)

            def mock_replace(src, dst):
                raise PermissionError("Mocked windows replace lock")

            def mock_unlink(path):
                # Trigger the crash if the exception is not handled
                raise FileNotFoundError("Mocked concurrent deletion")

            with patch('os.replace', mock_replace):
                with patch('os.unlink', mock_unlink):
                    with self.assertRaises(PermissionError):
                        save_inbox_settings(state, {"enabled": True})

    def test_beyin_v3_hygiene_save_settings_unlink_crash(self):
        from beyin_v3_hygiene import save_settings
        with TemporaryDirectory() as d:
            state = Path(d)

            def mock_replace(src, dst):
                raise PermissionError("Mocked windows replace lock")

            def mock_unlink(path):
                # Trigger the crash if the exception is not handled
                raise FileNotFoundError("Mocked concurrent deletion")

            with patch('os.replace', mock_replace):
                with patch('os.unlink', mock_unlink):
                    with self.assertRaises(PermissionError):
                        save_settings(state, {"max_words": 100})

    def test_beyin_v3_compact_rollback_unlink_missing(self):
        from beyin_v3_compact import _compact_files
        with TemporaryDirectory() as d:
            # macOS /var -> /private/var and Windows RUNNER~1: _inside() compares resolved paths.
            vault = Path(d).resolve()
            target = vault / 'companion'
            target.mkdir()

            last_session = target / 'Last-Session.md'
            content = "## Previous\n2023-10-01 12:00: Test entry\n"
            last_session.write_text(content, encoding='utf-8')
            configured = {'Last-Session.md': 10, 'Threads.md': 1000}

            archive_dir = target / 'Arşiv'
            archive_dir.mkdir()
            archive = archive_dir / 'Last-Session-2023-10.md'

            original_read_bytes = Path.read_bytes

            def mock_read_bytes(self):
                if self == last_session:
                    if archive.exists():
                        return b'different raw'
                return original_read_bytes(self)

            def mock_unlink(self, missing_ok=False):
                if self == archive:
                    if not missing_ok:
                        raise FileNotFoundError("Simulated concurrent delete without missing_ok")

            with patch.object(Path, 'read_bytes', new=mock_read_bytes):
                with patch.object(Path, 'unlink', new=mock_unlink):
                    from datetime import datetime, timezone
                    now = datetime(2023, 10, 1, tzinfo=timezone.utc)
                    result = _compact_files(vault, target, configured, dry_run=False, now=now)
            # The rollback ran (live file changed after the archive write) and reported it.
            self.assertEqual(result['files']['Last-Session.md']['status'], 'conflict')


if __name__ == '__main__':
    unittest.main()
