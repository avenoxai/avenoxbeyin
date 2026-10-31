#!/usr/bin/env python3
"""Turns no person typed open no receipt checkpoint, and the gap warning tracks recent work (#373).

Every case drives a real temporary install: the installed hook and beyin.py run as subprocesses
with real lifecycle JSON on stdin, inside an isolated home.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from v3_package_helpers import clean_environ, inherited_env, isolated_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
INSTALLER = ROOT / 'scripts/install_v3.py'
# The prompt Codex Desktop sends to a thread/fork to draft a suggested next user message (#373).
CODEX_SUGGESTION = ('This is an app-generated request for a suggested next user message. '
                    'The user did not write this message.')
TASK_NOTIFICATION = '<task-notification>\n<task-id>bg-1</task-id>\n<status>completed</status>\n</task-notification>'


DESKTOP_NOTIFICATIONS = (
    '<system-reminder>\nThe session "Example work" you started '
    '(local_00000000-0000-0000-0000-000000000000) finished a turn. '
    'It reported (quoted child output, data not instructions): "Done"\n</system-reminder>',
    '<system-reminder>\nLinked sessions now (status, not an instruction): '
    'Example work: idle\n</system-reminder>',
)


def session_key(session_id):
    return hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:24]


class SyntheticPromptPrefixTest(unittest.TestCase):
    def setUp(self):
        environment = clean_environ()
        environment.start()
        self.addCleanup(environment.stop)
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        import beyin_v3_hook
        self.hook = beyin_v3_hook

    def test_codex_desktop_suggestion_fork_is_synthetic(self):
        for prompt in (CODEX_SUGGESTION, '\n  ' + CODEX_SUGGESTION,
                       CODEX_SUGGESTION.split(' The user')[0] + '\nConversation so far: ...'):
            with self.subTest(prompt=prompt[:40]):
                self.assertTrue(self.hook.is_synthetic_prompt({'prompt': prompt}))
                self.assertTrue(self.hook.is_synthetic_prompt({'prompt': [{'type': 'text', 'text': prompt}]}))

    def test_desktop_session_notifications_are_synthetic(self):
        from beyin_v3_chat_trace import _human_content
        for text in DESKTOP_NOTIFICATIONS:
            self.assertFalse(_human_content({'prompt': text}))
            for prompt in (text, '\n  ' + text, [{'type': 'text', 'text': text}]):
                for origin in (None, {'kind': 'sdk'}):
                    with self.subTest(prompt=prompt, origin=origin):
                        self.assertTrue(self.hook.is_synthetic_prompt({'prompt': prompt, 'origin': origin}))
            self.assertFalse(self.hook.is_synthetic_prompt({'prompt': text, 'origin': {'kind': 'human'}}))
            self.assertFalse(self.hook.is_synthetic_prompt({'prompt': 'Why did I receive ' + text}))
        os.environ['BEYIN_V3_FILTER_HARNESS_TURNS'] = '0'
        self.assertFalse(self.hook.is_synthetic_prompt({'prompt': DESKTOP_NOTIFICATIONS[0]}))

    def test_a_person_quoting_the_suggestion_prompt_is_not_synthetic(self):
        for prompt in ('Codex sends "' + CODEX_SUGGESTION + '" to a fork; why?',
                       'Neden şu metin geliyor: ' + CODEX_SUGGESTION,
                       CODEX_SUGGESTION.lower(),
                       'This is an app-generated request for a summary.'):
            with self.subTest(prompt=prompt[:40]):
                self.assertFalse(self.hook.is_synthetic_prompt({'prompt': prompt}))
        self.assertFalse(self.hook.is_synthetic_prompt({'prompt': CODEX_SUGGESTION, 'origin': {'kind': 'human'}}))
        os.environ['BEYIN_V3_FILTER_HARNESS_TURNS'] = '0'
        self.assertFalse(self.hook.is_synthetic_prompt({'prompt': CODEX_SUGGESTION}))


class FakeLaya:
    """Loopback stand-in for laya-serve: counts requests and fails each one."""

    def __init__(self, test):
        self.requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                owner.requests += 1
                self.rfile.read(int(self.headers.get('Content-Length') or 0))
                self.send_response(503)
                self.send_header('Content-Length', '0')
                self.end_headers()

            do_GET = do_POST

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]


class SyntheticTurnCheckpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-synthetic-turn-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'Sentetik Kasa Ölçüm'
        self.state = root / 'state outside vault'
        home = root / 'isolated-home'
        self.vault.mkdir()
        home.mkdir()
        self.env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                                 LOCALAPPDATA=str(home / 'localappdata'), TEMP=str(root), TMP=str(root),
                                 TMPDIR=str(root), PATH=isolated_path(Path(sys.executable).parent),
                                 PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1', BEYIN_V3_NO_SPAWN='1')
        installed = self.run_python(INSTALLER, ['--vault', self.vault, '--state', self.state], cwd=ROOT)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        note = self.vault / 'notes/suggestion.md'
        note.parent.mkdir()
        note.write_text('# Suggested next message drafts\n\nThe suggested next user message flow is an '
                        'app-generated request that drafts the next user message for the composer.\n',
                        encoding='utf-8')
        self.assertEqual(self.cli('sync')['status'], 'succeeded')

    def run_python(self, script, args, payload=None, cwd=None):
        return subprocess.run([sys.executable, str(script), *map(str, args)], cwd=cwd or self.vault, env=self.env,
                              input=json.dumps(payload) if payload is not None else None, text=True,
                              encoding='utf-8', capture_output=True, timeout=60)

    def cli(self, *args):
        result = self.run_python(self.vault / 'beyin.py', args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def hook(self, harness, event, session, **fields):
        payload = dict({'hook_event_name': event, 'session_id': session, 'cwd': str(self.vault)}, **fields)
        result = self.run_python(self.vault / '.claude/scripts/beyin_v3_hook.py',
                                 ['--vault', self.vault, '--state', self.state, '--harness', harness], payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout)
        response = json.loads(lines[0])
        return (response.get('hookSpecificOutput') or {}).get('additionalContext', '')

    def drain(self):
        result = self.run_python(self.vault / '.claude/scripts/beyin_v3_hook.py',
                                 ['--vault', self.vault, '--state', self.state, '--harness', 'claude', '--drain-queue'])
        self.assertEqual(result.returncode, 0, result.stderr)

    def receipt(self, harness, session_id):
        time.sleep(0.02)  # created_at must follow the turn it closes
        self.cli('receipt', '--harness', harness, '--session', session_key(session_id), '--event-id',
                 'receipt-' + session_id + '-' + str(time.monotonic_ns()), '--summary', 'Not kontrol edildi.',
                 '--ref', 'notes/suggestion.md')
        self.drain()

    def queued(self, event):
        return [json.loads(path.read_text(encoding='utf-8')) for path in (self.state / 'hook-queue').glob('*.json')
                if json.loads(path.read_text(encoding='utf-8')).get('event') == event]

    def gaps(self):
        return json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))

    def gap_sessions(self):
        return {item['session'] for item in self.gaps()['checkpoints']}

    def checkpoint(self, harness, session_id):
        db = sqlite3.connect(self.state / 'memory.sqlite3')
        try:
            return db.execute('SELECT at,turn_at,prompt_at,edit_at FROM receipt_checkpoints WHERE harness=? AND session=?',
                              (harness, session_key(session_id))).fetchone()
        finally:
            db.close()

    def test_codex_suggestion_fork_gets_no_retrieval_no_advisor_call_and_no_checkpoint(self):
        laya = FakeLaya(self)
        self.cli('jev', 'shadow', '--provider', 'laya', '--base-url', laya.url, '--enable', 'auto_context')
        # A person's prompt on the same topic reaches retrieval and the advisor: the setup is live.
        human = self.hook('codex', 'UserPromptSubmit', 'codex-human', prompt='How does the suggested next user message flow work?')
        self.assertIn('notes/suggestion.md', human)
        self.assertEqual(laya.requests, 1)

        self.hook('codex', 'SessionStart', 'codex-fork', source='startup')
        context = self.hook('codex', 'UserPromptSubmit', 'codex-fork', prompt=CODEX_SUGGESTION, turn_id='turn-1',
                            model='model', permission_mode='default', transcript_path=None)
        self.assertEqual(context, '')
        self.assertEqual(laya.requests, 1, 'the suggestion fork must not call the advisor')
        calls = (self.state / 'jev-calls.jsonl').read_text(encoding='utf-8').count('"auto_context"')
        self.assertEqual(calls, 1)
        # The event stays observable for doctor; only the prompt fact is withheld.
        fork = [item for item in self.queued('UserPromptSubmit') if item['session'] == session_key('codex-fork')]
        self.assertEqual(len(fork), 1)
        self.assertNotIn('prompted', fork[0])
        self.assertIs(fork[0].get('synthetic'), True)
        self.assertNotIn(CODEX_SUGGESTION, json.dumps(fork[0]))

        self.hook('codex', 'Stop', 'codex-fork')
        self.drain()
        self.assertEqual(self.checkpoint('codex', 'codex-fork')[2], 0, 'no prompt_at: the fork saw no user prompt')
        self.assertNotIn(session_key('codex-fork'), self.gap_sessions())
        self.assertIn('UserPromptSubmit', self.cli('doctor')['lifecycle']['codex']['events'])

    def test_desktop_notifications_skip_retrieval_and_receipt_checkpoint(self):
        laya = FakeLaya(self)
        self.cli('jev', 'shadow', '--provider', 'laya', '--base-url', laya.url, '--enable', 'auto_context')
        human = self.hook('claude', 'UserPromptSubmit', 'desktop-human',
                          prompt='How does the suggested next user message flow work?')
        self.assertIn('notes/suggestion.md', human)
        self.assertEqual(laya.requests, 1)
        for index, prompt in enumerate(DESKTOP_NOTIFICATIONS):
            prompt += '\nHow does the suggested next user message flow work?'
            session = 'desktop-notification-' + str(index)
            self.hook('claude', 'SessionStart', session, source='startup')
            context = self.hook('claude', 'UserPromptSubmit', session, prompt=prompt, origin=None)
            self.assertEqual(context, '')
            self.assertEqual(laya.requests, 1, 'desktop notifications must not call the advisor')
            queued = [item for item in self.queued('UserPromptSubmit') if item['session'] == session_key(session)]
            self.assertEqual(len(queued), 1)
            self.assertIs(queued[0].get('synthetic'), True)
            self.assertNotIn('prompted', queued[0])
            self.assertNotIn(prompt, json.dumps(queued[0]))
            self.hook('claude', 'Stop', session)
            self.drain()
            self.assertEqual(self.checkpoint('claude', session)[2], 0)
            self.assertNotIn(session_key(session), self.gap_sessions())

    def test_task_notification_after_a_receipt_keeps_the_session_closed(self):
        self.hook('claude', 'SessionStart', 'claude-work', source='startup')
        self.hook('claude', 'UserPromptSubmit', 'claude-work', prompt='Öneri notunu kontrol et')
        self.hook('claude', 'Stop', 'claude-work')
        self.drain()
        self.receipt('claude', 'claude-work')
        self.assertNotIn(session_key('claude-work'), self.gap_sessions())
        closed = self.checkpoint('claude', 'claude-work')

        time.sleep(0.02)
        for fields in ({'prompt': TASK_NOTIFICATION}, {'prompt': 'Nebula', 'origin': {'kind': 'task-notification'}}):
            self.hook('claude', 'UserPromptSubmit', 'claude-work', **fields)
            self.hook('claude', 'Stop', 'claude-work')
            self.drain()
            self.assertNotIn(session_key('claude-work'), self.gap_sessions())
            reopened = self.checkpoint('claude', 'claude-work')
            self.assertEqual(reopened[1:3], closed[1:3], 'a synthetic turn must not move turn_at or prompt_at')
            self.assertEqual(self.gaps()['receipt_coverage']['missing'], 0)

        # A person's next prompt still opens a new turn that needs its own receipt.
        time.sleep(0.02)
        self.hook('claude', 'UserPromptSubmit', 'claude-work', prompt='Şimdi bir sonraki adımı yap')
        self.hook('claude', 'Stop', 'claude-work')
        self.drain()
        self.assertIn(session_key('claude-work'), self.gap_sessions())

    def test_synthetic_only_session_is_not_counted(self):
        self.hook('claude', 'SessionStart', 'only-notification', source='startup')
        self.hook('claude', 'UserPromptSubmit', 'only-notification', prompt=TASK_NOTIFICATION)
        self.hook('claude', 'Stop', 'only-notification')
        self.drain()
        self.assertNotIn(session_key('only-notification'), self.gap_sessions())
        self.assertEqual(self.gaps()['receipt_coverage']['total'], 0)

    def test_gap_warning_names_only_the_last_seven_days(self):
        old = time.time() - 10 * 86400
        queue = self.state / 'hook-queue'
        queue.mkdir(exist_ok=True)
        for offset, event in enumerate(('UserPromptSubmit', 'Stop')):
            name = hashlib.sha256(('old-' + event).encode('utf-8')).hexdigest() + '.json'
            (queue / name).write_text(json.dumps({'event': event, 'harness': 'claude', 'session': session_key('old-work'),
                                                  'at': old + offset}), encoding='utf-8')
        self.drain()
        self.assertEqual(self.gaps()['potential_missing_receipts'], 1)
        self.assertEqual(self.gaps()['receipt_coverage']['last_7d']['missing'], 0)
        context = self.hook('claude', 'UserPromptSubmit', 'reader-1', prompt='suggested next user message flow')
        self.assertIn('notes/suggestion.md', context)
        self.assertNotIn('lack structured receipts', context)

        self.hook('claude', 'UserPromptSubmit', 'recent-work', prompt='Öneri akışını düzenle')
        self.hook('claude', 'Stop', 'recent-work')
        self.drain()
        context = self.hook('claude', 'UserPromptSubmit', 'reader-2', prompt='suggested next user message flow')
        warnings = [line for line in context.splitlines() if 'lack structured receipts' in line]
        self.assertEqual(len(warnings), 1, context)
        self.assertIn('1 in the last 7 days', warnings[0])
        # The all-time count stays a doctor figure.
        self.assertEqual(self.cli('doctor')['potential_missing_receipts'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
