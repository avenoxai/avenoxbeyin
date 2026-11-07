"""#486: the Windows popup channel over real OS pipes: delays are waited for, EOF ends it."""
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import time
import unittest

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as yakala  # noqa: E402


# A child on the far side of the Windows popup protocol: the real _JsonPipe on its own stdio.
PIPE_CHILD = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, sys.argv[1])
    import beyin_v3_yakala as yakala
    pipe = yakala._JsonPipe(sys.stdin.buffer, sys.stdout.buffer)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not pipe.failed.is_set():
        message = pipe.receive()
        if message is not None:
            pipe.send({'type': 'ECHO', 'got': message})
        time.sleep(0.01)
    pipe.finish()
    sys.exit(0 if pipe.failed.is_set() else 5)
''')

SLOW_WRITER = textwrap.dedent('''
    import sys, time
    out = sys.stdout.buffer
    time.sleep(0.4)  # a slow start, as a popup that is still building its window
    out.write(b'{"type":"REA'); out.flush()
    time.sleep(0.4)  # half a packet waits in the pipe
    out.write(b'DY","n":1}\\n{"type":"ACTIVE","n":2}\\n'); out.flush()
    line = sys.stdin.buffer.readline()
    out.write(line); out.flush()
''')


class RealPipeTest(unittest.TestCase):
    """#486: delays and split packets on a real OS pipe (anonymous pipes from the job-assigned spawn
    on Windows) are waited for; only the end of the stream ends the channel."""

    def spawn(self, code, *args):
        owned = yakala._OwnedProcess([sys.executable, '-c', code, *args], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=inherited_env())
        self.addCleanup(owned.close)
        for stream in (owned.process.stdin, owned.process.stdout):
            self.addCleanup(stream.close)
        return owned.process

    def test_slow_and_split_packets_do_not_drop_the_pipe(self):
        child = self.spawn(SLOW_WRITER)
        pipe = yakala._JsonPipe(child.stdout, child.stdin)
        self.addCleanup(pipe.finish, 0)
        received, deadline = [], time.monotonic() + 10
        while len(received) < 2 and time.monotonic() < deadline:
            message = pipe.receive()
            if message is None:
                self.assertFalse(pipe.failed.is_set(), received)  # waiting is not a disconnect
                time.sleep(0.01)
            else:
                received.append(message)
        self.assertEqual(received, [{'type': 'READY', 'n': 1}, {'type': 'ACTIVE', 'n': 2}])
        self.assertFalse(pipe.failed.is_set())
        self.assertTrue(pipe.send({'type': 'ACTIVATE', 'n': 3}))
        deadline = time.monotonic() + 10
        echoed = None
        while echoed is None and time.monotonic() < deadline:
            echoed = pipe.receive()
            time.sleep(0.01)
        self.assertEqual(echoed, {'type': 'ACTIVATE', 'n': 3})
        child.wait(timeout=10)
        self.assertTrue(pipe.failed.wait(5))  # the real end of the stream is terminal

    def test_popup_side_pipe_waits_for_split_parent_packets(self):
        child = self.spawn(PIPE_CHILD, str(SCRIPTS))
        writer = child.stdin
        for part in (b'{"type":"ACT', b'IVATE","capture_id":"', b'ab"}\n'):
            writer.write(part)
            writer.flush()
            time.sleep(0.3)
        lines = []
        reader = threading.Thread(target=lambda: lines.append(child.stdout.readline()), daemon=True)
        reader.start()
        reader.join(10)
        self.assertEqual([json.loads(line) for line in lines],
                         [{'type': 'ECHO', 'got': {'type': 'ACTIVATE', 'capture_id': 'ab'}}])
        self.assertIsNone(child.poll())  # still connected after the slow packet
        writer.close()  # parent gone: the child's reader sees EOF and the popup ends
        self.assertEqual(child.wait(timeout=10), 0)


if __name__ == '__main__':
    unittest.main()
