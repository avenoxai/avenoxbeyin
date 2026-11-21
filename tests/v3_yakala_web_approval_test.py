"""PR #717's normal web route must carry the existing Defuddle approval state.

Real cards, process and ZIP-installed CLI; external helpers and network stay offline stubs.
First consent requires a terminal; cached consent remains usable headlessly.
"""
from contextlib import nullcontext
import io
import json
from pathlib import Path
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from v3_package_helpers import clean_environ, inherited_env
from v3_yakala_defuddle_consent_test import TerminalInput

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as yakala

ARTICLE = 'Synthetic article text with no external data. ' * 8
FALLBACK = 'Synthetic standard-library fallback text. ' * 8
TOOLS = dict(npx='synthetic-npx', yt_dlp=None, whisper=None, pdftotext=None, ffmpeg=None)


class WebApprovalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-web-approval-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault, self.state = self.root / 'Örnek Beyin', self.root / 'state'
        self.vault.mkdir()
        home = self.root / 'home'
        environ = clean_environ(HOME=str(home), USERPROFILE=str(home),
                                XDG_STATE_HOME=str(home / 'state'),
                                XDG_CONFIG_HOME=str(home / 'config'),
                                XDG_DATA_HOME=str(home / 'data'),
                                XDG_CACHE_HOME=str(home / 'cache'))
        environ.start()
        self.addCleanup(environ.stop)
        for target, options in (
                ('tools', dict(return_value=TOOLS)),
                ('_HelperNetwork', dict(side_effect=lambda: nullcontext())),
                ('_fetch_html', dict(return_value=None)),
                ('via_stdlib', dict(return_value=FALLBACK))):
            patcher = patch.object(yakala, target, **options)
            setattr(self, target + '_mock', patcher.start())
            self.addCleanup(patcher.stop)
        dns = patch.object(socket, 'getaddrinfo', return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
        dns.start()
        self.addCleanup(dns.stop)
        dial = patch.object(socket, 'create_connection', side_effect=AssertionError('offline fixture'))
        dial.start()
        self.addCleanup(dial.stop)

    def process(self, name, reply):
        card = yakala.capture(self.vault, url='https://example.com/' + name, state=self.state)
        err = io.StringIO()
        with patch('sys.stdin', TerminalInput(reply)), patch('sys.stderr', err), \
                patch.object(yakala, '_run', return_value=(0, json.dumps({'content': ARTICLE}), '')) as run:
            entry = yakala.process(self.vault, [card['id']], state=self.state)['kartlar'][0]
        return entry, run, err.getvalue()

    def test_process_web_denial_and_eof_never_run_defuddle(self):
        for name, reply in (('denied', 'hayir\n'), ('eof', '')):
            with self.subTest(reply=reply):
                entry, run, err = self.process(name, reply)
                run.assert_not_called()
                self.assertIn('Onay veriyor musunuz?', err)
                self.assertEqual((entry['durum'], entry['yontem']), ('cikarildi', 'http'))
                self.assertIn(FALLBACK.strip(), (self.vault / entry['ham']).read_text(encoding='utf-8'))
                self.assertFalse(yakala._defuddle_approval_path(self.state).exists())

    def test_process_web_approval_is_persisted_and_reused(self):
        entry, run, err = self.process('approved', 'e\n')
        self.assertEqual(entry['yontem'], 'defuddle')
        self.assertIn('Onay veriyor musunuz?', err)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-2:], ['--', 'https://example.com/approved'])
        path = yakala._defuddle_approval_path(self.state)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),
                         {'schema': 1, 'approved': True, 'version': yakala.DEFUDDLE})
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        again, run, err = self.process('reused', '')
        self.assertEqual(again['yontem'], 'defuddle')
        run.assert_called_once()
        self.assertNotIn('Onay veriyor musunuz?', err)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertFalse(list(self.vault.rglob('defuddle-approval.json')))

    def test_process_web_changed_version_requires_approval_again(self):
        path = yakala._defuddle_approval_path(self.state)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'schema': 1, 'approved': True, 'version': 'defuddle@0.0.0'}))
        before = path.read_bytes()
        entry, run, err = self.process('new-version', 'no\n')
        run.assert_not_called()
        self.assertIn('Onay veriyor musunuz?', err)
        self.assertEqual(entry['yontem'], 'http')
        self.assertEqual(path.read_bytes(), before)

    def test_library_process_without_state_keeps_http_fallback_without_npx(self):
        card = yakala.capture(self.vault, url='https://example.com/no-state', state=self.state)
        with patch('sys.stdin', TerminalInput('yes\n')), patch.object(yakala, '_run') as run:
            entry = yakala.process(self.vault, [card['id']])['kartlar'][0]
        run.assert_not_called()
        self.assertEqual(entry['yontem'], 'http')
        self.assertIn('state', entry['not'])
        self.assertFalse(yakala._defuddle_approval_path(self.state).exists())

    def test_missing_npx_and_web_clipper_do_not_prompt(self):
        with patch.object(yakala, '_prompt_defuddle_approval', side_effect=AssertionError('no helper needed')):
            self.tools_mock.return_value = dict(TOOLS, npx=None)
            entry, run, err = self.process('no-npx', '')
            run.assert_not_called()
            self.assertEqual(entry['yontem'], 'http')
            self.assertEqual(err, '')
            self.tools_mock.return_value = TOOLS
            card = dict(meta={'url': 'https://example.com/clipped', 'kaynak_turu': 'web'},
                        body='## Icerik\n' + ARTICLE)
            self.assertEqual(yakala.extract(self.vault, card, TOOLS, state=self.state),
                             (ARTICLE.strip(), 'web-clipper', None))
        self.assertFalse(yakala._defuddle_approval_path(self.state).exists())

    def test_zip_installed_cli_web_denial_then_approval(self):
        env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')

        def run(script, *args, reply='', terminal=False):
            command = [sys.executable, str(script), *map(str, args)]
            child_env = dict(env, YAKALA_FIXTURE_TERMINAL='1' if terminal is True else '0')
            if terminal == 'pty':
                import pty
                master, slave = pty.openpty()
                try:
                    child = subprocess.Popen(command, stdin=slave, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, env=child_env, cwd=self.vault,
                                             text=True, encoding='utf-8')
                    try:
                        os.write(master, reply.encode('utf-8'))
                        out, err = child.communicate(timeout=60)
                        result = subprocess.CompletedProcess(command, child.returncode, out, err)
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.communicate(timeout=10)
                finally:
                    os.close(master)
                    os.close(slave)
            else:
                result = subprocess.run(command, input=reply, env=child_env, cwd=self.vault,
                                        capture_output=True, text=True, encoding='utf-8', timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout), result.stderr

        package = self.root / 'candidate.zip'
        run(ROOT / 'scripts/build_v3_release.py', '--version', '3.1.1', '--output', package)
        with zipfile.ZipFile(package) as archive:
            archive.extractall(self.root / 'package')
        run(self.root / 'package/scripts/install_v3.py', '--vault', self.vault, '--state', self.state)
        fixtures = self.root / 'fixtures'
        fixtures.mkdir()
        calls = fixtures / 'calls.jsonl'
        # Only helper/network I/O is replaced in the child. The installed entry, its state
        # resolution, real card processing, approval prompt and persisted file are exercised.
        (fixtures / 'sitecustomize.py').write_text(
            'import json, socket, sys, os\nfrom pathlib import Path\nfrom contextlib import nullcontext\n'
            'import beyin_v3_yakala as y\n'
            'if os.environ.get("YAKALA_FIXTURE_TERMINAL") == "1": sys.stdin.isatty = lambda: True\n'
            'def helper(command, **kwargs):\n'
            f'    with Path({str(calls)!r}).open("a", encoding="utf-8") as log:\n'
            '        log.write(json.dumps(command) + "\\n")\n'
            f'    return 0, json.dumps({{"content": {ARTICLE!r}}}), ""\n'
            'def no_network(*args, **kwargs):\n    raise AssertionError("offline fixture")\n'
            'socket.create_connection = no_network\n'
            "socket.getaddrinfo = lambda host, port, *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port))]\n"
            f'y.tools = lambda: {TOOLS!r}\n'
            'y._run = helper\ny._HelperNetwork = lambda: nullcontext()\ny._fetch_html = lambda *a, **kw: None\n'
            f'y.via_stdlib = lambda url: {FALLBACK!r}\n', encoding='utf-8')
        env['PYTHONPATH'] = os.pathsep.join((str(fixtures), str(self.vault / '.claude/scripts')))
        cli = self.vault / 'beyin.py'
        for name, reply in (('cli-denied', 'no\n'), ('cli-eof', '')):
            run(cli, 'yakala', 'ekle', 'https://example.com/' + name)
            result, err = run(cli, 'yakala', 'isle', '--ses-yok', reply=reply, terminal=True)
            self.assertIn('Onay veriyor musunuz?', err)
            self.assertEqual(result['kartlar'][0]['yontem'], 'http')
            self.assertFalse(calls.exists(), 'denial/EOF must not reach the external helper')
            self.assertFalse(yakala._defuddle_approval_path(self.state).exists())
        run(cli, 'yakala', 'ekle', 'https://example.com/cli-piped-yes')
        result, err = run(cli, 'yakala', 'isle', '--ses-yok', reply='yes\n')
        self.assertEqual(result['kartlar'][0]['yontem'], 'http')
        self.assertNotIn('Onay veriyor musunuz?', err)
        self.assertFalse(calls.exists())
        self.assertFalse(yakala._defuddle_approval_path(self.state).exists())
        for name, reply in (('cli-approved', 'yes\n'), ('cli-reused', '')):
            run(cli, 'yakala', 'ekle', 'https://example.com/' + name)
            result, err = run(cli, 'yakala', 'isle', '--ses-yok', reply=reply, terminal=bool(reply))
            self.assertEqual(result['kartlar'][0]['yontem'], 'defuddle')
            self.assertEqual('Onay veriyor musunuz?' in err, bool(reply))
        logged = [json.loads(line) for line in calls.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([command[-1] for command in logged],
                         ['https://example.com/cli-approved', 'https://example.com/cli-reused'])
        self.assertTrue(yakala._defuddle_approval_granted(self.state))
        if os.name != 'nt':
            # Real POSIX terminal descriptor: no isatty adapter or approval mock.
            yakala._defuddle_approval_path(self.state).unlink()
            run(cli, 'yakala', 'ekle', 'https://example.com/cli-real-terminal')
            result, err = run(cli, 'yakala', 'isle', '--ses-yok', reply='yes\n', terminal='pty')
            self.assertEqual(result['kartlar'][0]['yontem'], 'defuddle')
            self.assertIn('Onay veriyor musunuz?', err)
            self.assertTrue(yakala._defuddle_approval_granted(self.state))
            logged = [json.loads(line) for line in calls.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(logged), 3)
            self.assertEqual(logged[-1][-1], 'https://example.com/cli-real-terminal')


if __name__ == '__main__':
    unittest.main()
