"""UTF-8 boundaries are independent of the platform's default text encoding."""
import json
import os
from pathlib import Path
import sys
import unittest

from v3_workspace_test_io import load_utf8_json, run_json_cli


class WorkspaceTestIOTest(unittest.TestCase):
    def test_fixture_preserves_unicode(self):
        path = Path(__file__).parent / 'fixtures/v3/workspaces/v0.1.0/academic/input.json'
        fixture = load_utf8_json(path)
        self.assertIn('ödevlerimi', fixture['natural_setup_request'])
        self.assertIn('öğrendiklerimi', fixture['natural_setup_request'])

    def test_json_pipe_survives_cp1252_without_changing_child_environment(self):
        original = {'text':'Ödev — Türkçe, 日本語, 😀'}
        script = ('import sys;'
                  'raw=sys.stdin.buffer.read();'
                  'sys.stdout.buffer.write(raw)')
        env = dict(os.environ, PYTHONIOENCODING='cp1252:strict')
        completed = run_json_cli([sys.executable, '-c', script],
                                 input=json.dumps(original, ensure_ascii=False),
                                 env=env, capture_output=True, check=True)
        self.assertEqual(json.loads(completed.stdout), original)

    def test_json_helper_refuses_human_output(self):
        with self.assertRaisesRegex(ValueError, 'human CLI output'):
            run_json_cli([sys.executable, '--human'])

    def test_non_utf8_cli_response_is_not_silently_replaced(self):
        script = 'import sys;sys.stdout.buffer.write(bytes([255]))'
        with self.assertRaises(UnicodeDecodeError):
            run_json_cli([sys.executable, '-c', script], capture_output=True,
                         check=True)


if __name__ == '__main__':
    unittest.main()
