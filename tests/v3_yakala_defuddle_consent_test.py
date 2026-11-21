"""Fail-closed Defuddle consent. No real package or external network is used."""
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as y


class TerminalInput(io.StringIO):
    """Synthetic human terminal input; real persistence and gate remain in use."""
    def isatty(self):
        return True


def approved_test_state(test):
    """Explicit synthetic consent for tests whose subject is an already-approved helper."""
    tmp = tempfile.TemporaryDirectory(prefix='approved-helper-state-')
    test.addCleanup(tmp.cleanup)
    state = Path(tmp.name)
    with patch('sys.stdin', TerminalInput('yes\n')), patch('sys.stderr', io.StringIO()):
        test.assertTrue(y._prompt_defuddle_approval(state))
    return state


class DefuddleConsentTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='defuddle-consent-')
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / 'state'
        self.path = y._defuddle_approval_path(self.state)
        self.path.parent.mkdir(parents=True)
        self.record = {'schema': 1, 'approved': True, 'version': y.DEFUDDLE}
        for p in (patch.object(y, '_HelperNetwork', side_effect=lambda: nullcontext()),
                  patch.object(socket, 'getaddrinfo', return_value=[
                      (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]),
                  patch.object(socket, 'create_connection', side_effect=AssertionError('offline fixture')),
                  patch('sys.stderr', io.StringIO())):
            p.start()
            self.addCleanup(p.stop)

    def write(self, record):
        self.path.write_text(json.dumps(record), encoding='utf-8')

    def invoke(self, state):
        info = {}
        text = y.via_defuddle('https://example.com/article', {'npx': 'synthetic-npx'}, info, state=state)
        return text, info

    def test_only_explicit_boolean_and_supported_integer_schema_grant(self):
        invalid = [[], None, True, 1, 'yes', {}]
        invalid += [dict(self.record, approved=v) for v in ('false', 'true', 1, 0, [], {}, None, False)]
        invalid += [dict(self.record, schema=v) for v in (True, 1.0, '1', 0, 2, None, [], {})]
        invalid += [{k: v for k, v in self.record.items() if k != missing}
                    for missing in ('approved', 'schema', 'version')]
        invalid += [dict(self.record, version='defuddle@0.0.0')]
        for record in invalid:
            with self.subTest(record=record):
                self.write(record)
                before = self.path.read_bytes()
                with patch('sys.stdin', TerminalInput('no\n')), patch.object(y, '_run') as run:
                    self.assertFalse(y._defuddle_approval_granted(self.state))
                    self.assertIsNone(self.invoke(self.state)[0])
                run.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_unreadable_and_missing_records_deny(self):
        for raw in (b'{', b'\xff', b'', b'[' * 1100 + b']' * 1100, b' ' * 4097):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.assertFalse(y._defuddle_approval_granted(self.state))
        with patch.object(os, 'open', side_effect=PermissionError('synthetic unreadable file')):
            self.assertFalse(y._defuddle_approval_granted(self.state))
        self.path.unlink()
        self.assertFalse(y._defuddle_approval_granted(self.state))
        self.path.mkdir()
        self.assertFalse(y._defuddle_approval_granted(self.state))

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO')
    def test_fifo_and_regular_file_replaced_by_fifo_never_block(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                if replace:
                    self.write(self.record)
                else:
                    os.mkfifo(self.path)
                code = ("import sys, os; from pathlib import Path; "
                        f"sys.path.insert(0, {str(ROOT / 'template/.claude/scripts')!r}); "
                        "import beyin_v3_yakala as y\n"
                        "real_open = os.open\n"
                        "def replaced(path, flags):\n"
                        "    Path(path).unlink()\n"
                        "    os.mkfifo(path)\n"
                        "    return real_open(path, flags)\n"
                        + ("os.open = replaced\n" if replace else "")
                        + f"print(y._defuddle_approval_granted(Path({str(self.state)!r})))")
                result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                        text=True, env=inherited_env(), timeout=5)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, 'False'), result.stderr)
                self.path.unlink()

    def test_linked_approval_is_not_accepted(self):
        target = self.path.with_name('target.json')
        target.write_text(json.dumps(self.record), encoding='utf-8')
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        self.assertFalse(y._defuddle_approval_granted(self.state))
        self.assertEqual(json.loads(target.read_text()), self.record)

    def test_approved_record_is_reused_without_reading_stdin_or_rewriting(self):
        self.write(self.record)
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        with patch('sys.stdin', None), patch.object(y, '_prompt_defuddle_approval', side_effect=AssertionError('cached')), \
                patch.object(y, '_run', return_value=(0, json.dumps({'content': 'article ' * 30}), '')) as run:
            self.assertIsNotNone(self.invoke(self.state)[0])
        run.assert_called_once()
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)

    def test_missing_state_never_prompts_runs_or_selects_implicit_state(self):
        with patch('sys.stdin', TerminalInput('yes\n')), patch.object(y, '_run') as run, \
                patch.object(y, '_prompt_defuddle_approval') as prompt, \
                patch.object(y, 'resolve_state', side_effect=AssertionError('no implicit state')):
            result, info = self.invoke(None)
        self.assertIsNone(result)
        self.assertIn('state', info['uyari'])
        run.assert_not_called()
        prompt.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_noninteractive_input_is_never_read_even_if_yes_is_ready(self):
        for stream in (None, io.StringIO('yes\n'), Mock(isatty=Mock(return_value=False)),
                       Mock(isatty=Mock(side_effect=OSError('no terminal')))):
            with self.subTest(stream=type(stream).__name__), patch('sys.stdin', stream), \
                    patch.object(y, '_run') as run:
                self.assertIsNone(self.invoke(self.state)[0])
                run.assert_not_called()
                if isinstance(stream, Mock):
                    stream.readline.assert_not_called()
                elif stream is not None:
                    self.assertEqual(stream.tell(), 0)
        closed = io.StringIO('yes\n')
        closed.close()
        with patch('sys.stdin', closed):
            self.assertFalse(y._prompt_defuddle_approval(self.state))
        self.assertFalse(self.path.exists())

    def test_interactive_denial_eof_interrupt_and_read_error_never_grant(self):
        for response in ('no\n', '', 'yesterday\n', 'yes please\n', '\n', 'yes' + ' ' * 130 + '\n'):
            with self.subTest(response=response), patch('sys.stdin', TerminalInput(response)), \
                    patch.object(y, '_run') as run:
                self.assertIsNone(self.invoke(self.state)[0])
                run.assert_not_called()
        for error in (EOFError(), KeyboardInterrupt(), OSError('read failure'), ValueError('closed stream')):
            stream = Mock(isatty=Mock(return_value=True), readline=Mock(side_effect=error))
            with self.subTest(error=type(error).__name__), patch('sys.stdin', stream), patch.object(y, '_run') as run:
                self.assertIsNone(self.invoke(self.state)[0])
                run.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_interactive_explicit_consent_persists_original_schema(self):
        for reply in ('e\n', 'EVET\n', ' yes \n', 'Y\n'):
            with self.subTest(reply=reply), patch('sys.stdin', TerminalInput(reply)), \
                    patch.object(y, '_run', return_value=(0, json.dumps({'content': 'article ' * 30}), '')) as run:
                self.assertIsNotNone(self.invoke(self.state)[0])
                run.assert_called_once()
                self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), self.record)
                self.path.unlink()

    def test_cannot_save_consent_never_runs_helper(self):
        with patch('sys.stdin', TerminalInput('yes\n')), \
                patch('beyin_v3_sync.atomic', side_effect=PermissionError('read-only state')), \
                patch.object(y, '_run') as run:
            self.assertIsNone(self.invoke(self.state)[0])
        run.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_open_empty_or_partial_stdin_pipe_returns_without_waiting_for_eof(self):
        code = ('import sys; from pathlib import Path; '
                f'sys.path.insert(0, {str(ROOT / "template/.claude/scripts")!r}); '
                'import beyin_v3_yakala as y; '
                f'print(y._prompt_defuddle_approval(Path({str(self.state)!r})))')
        for pending in (b'', b'y', b'yes\n'):
            with self.subTest(pending=pending):
                child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=inherited_env())
                try:
                    if pending:
                        child.stdin.write(pending)
                        child.stdin.flush()
                    child.wait(timeout=5)  # stdin deliberately remains OPEN, unlike communicate(input=...)
                    self.assertEqual(child.returncode, 0)
                    self.assertEqual(child.stdout.read().decode().strip(), 'False')
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                    for stream in (child.stdin, child.stdout, child.stderr):
                        stream.close()
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
