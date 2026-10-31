"""Receipt reminders ignore known edits outside the vault (#567), with real installs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env, isolated_path

ROOT = Path(__file__).resolve().parents[1]


class ReceiptScopeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-receipt-scope-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vault = self.root / 'Synthetic Vault'
        self.state = self.root / 'state'
        self.outside = self.root / 'outside'
        self.home = self.root / 'home'
        for folder in (self.vault, self.outside, self.home):
            folder.mkdir()
        self.env = inherited_env(HOME=str(self.home), USERPROFILE=str(self.home),
                                 APPDATA=str(self.home / 'appdata'), LOCALAPPDATA=str(self.home / 'local'),
                                 PATH=isolated_path(Path(sys.executable).parent), BEYIN_V3_NO_SPAWN='1',
                                 PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault',
                                 str(self.vault), '--state', str(self.state)], env=self.env,
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.hook = self.vault / '.claude/scripts/beyin_v3_hook.py'

    def invoke(self, event, session, harness='claude', **fields):
        payload = dict(hook_event_name=event, session_id=session, **fields)
        result = subprocess.run([sys.executable, str(self.hook), '--vault', str(self.vault),
                                 '--state', str(self.state), '--harness', harness], cwd=self.vault,
                                env=self.env, input=json.dumps(payload), text=True,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def check_edit(self, session, tool_input, expected, harness='claude', **fields):
        self.assertEqual(self.invoke('PostToolUse', session, harness=harness,
                                     tool_name='apply_patch' if 'command' in tool_input else 'Write',
                                     tool_input=tool_input, **fields), {})
        marker = self.state / 'receipt-reminders' / (hashlib.sha256(session.encode()).hexdigest() + '.edited')
        self.assertEqual(marker.exists(), expected, session)
        stop = self.invoke('Stop', session, harness=harness)
        self.assertEqual(stop.get('decision') == 'block', expected, stop)

    def test_absolute_relative_and_traversal_paths(self):
        (self.outside / 'output.txt').write_text('synthetic output')
        (self.vault / 'note.md').write_text('# synthetic note')
        cases = [
            ('outside', {'file_path': str(self.outside / 'output.txt')}, False, {}),
            ('sibling-prefix', {'path': str(self.root / 'Synthetic Vault sibling' / 'out.txt')}, False, {}),
            ('outside-notebook', {'notebook_path': str(self.outside / 'notebook.ipynb')}, False, {}),
            ('relative-outside', {'file_path': 'output.txt'}, False, {'cwd': str(self.outside)}),
            ('traversal', {'file_path': '../outside/output.txt'}, False, {'cwd': str(self.vault)}),
            ('inside', {'file_path': str(self.vault / 'note.md')}, True, {}),
            ('relative-inside', {'file_path': 'note.md'}, True, {'cwd': str(self.vault)}),
            ('unknown', {}, True, {}),
            ('patch-unknown-cwd', {'command': '*** Begin Patch\n*** Delete File: ' +
                                 str(self.outside / 'old.md') + '\n*** End Patch'}, True, {}),
            ('relative-unknown-cwd', {'file_path': 'note.md'}, True, {}),
            ('mixed-unknown-relative', {'file_path': str(self.outside / 'output.txt'), 'path': 'note.md'}, True, {}),
            ('mixed-malformed', {'file_path': str(self.outside / 'output.txt'), 'path': []}, True, {}),
            ('malformed-command', {'file_path': str(self.outside / 'output.txt'), 'command': []}, True, {}),
        ]
        for session, tool_input, expected, fields in cases:
            with self.subTest(session=session):
                self.check_edit(session, tool_input, expected, **fields)

    def test_patch_deletions_moves_and_mixed_paths(self):
        cases = [
            ('delete-outside', '*** Delete File: ../outside/deleted.md', False),
            ('delete-inside', '*** Delete File: deleted.md', True),
            ('move-outside', '*** Update File: ../outside/old.md\n*** Move to: ../outside/new.md', False),
            ('move-inside-out', '*** Update File: old.md\n*** Move to: ../outside/new.md', True),
            ('move-out-inside', '*** Update File: ../outside/old.md\n*** Move to: new.md', True),
            ('mixed', '*** Add File: ../outside/out.md\n+x\n*** Add File: inside.md\n+y', True),
            ('malformed-patch-header', '*** Update File: ../outside/out.md\n*** Delete File:', True),
            ('unsupported-patch-header', '*** Update File: ../outside/out.md\n*** Unknown File: inside.md', True),
            ('outside-add-update', '*** Add File: ../outside/out.md\n+x\n*** Update File: ../outside/other.md', False),
        ]
        for session, headers, expected in cases:
            with self.subTest(session=session):
                self.check_edit(session, {'command': '*** Begin Patch\n' + headers + '\n*** End Patch'},
                                expected, harness='codex', cwd=str(self.vault))

    def test_outside_edits_do_not_close_or_prevent_a_real_edit_window(self):
        outside = {'file_path': str(self.outside / 'output.txt')}
        inside = {'file_path': str(self.vault / 'note.md')}
        self.check_edit('outside-then-inside', outside, False)
        self.check_edit('outside-then-inside', inside, True)
        self.invoke('PostToolUse', 'inside-then-outside', tool_input=inside)
        self.check_edit('inside-then-outside', outside, True)

    def test_symlink_escape_and_inside_target(self):
        (self.outside / 'file.md').write_text('# outside')
        (self.vault / 'file.md').write_text('# inside')
        try:
            (self.vault / 'escape').symlink_to(self.outside, target_is_directory=True)
            (self.outside / 'into-vault').symlink_to(self.vault, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('symlink creation unavailable: ' + str(exc))
        self.check_edit('symlink-out', {'file_path': str(self.vault / 'escape/file.md')}, False)
        self.check_edit('symlink-in', {'file_path': str(self.outside / 'into-vault/file.md')}, True)


if __name__ == '__main__':
    unittest.main()
