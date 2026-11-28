#!/usr/bin/env python3
"""Offline lifecycle, queue and installer tests using isolated synthetic homes."""
import importlib.util
import base64
import errno
import hashlib
import re
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from v3_package_helpers import clean_environ, inherited_env, isolated_path, without_hook_clock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
HOOK = SCRIPTS / 'beyin_v3_hook.py'
INSTALLER = ROOT / 'scripts/install_v3.py'


def decoded_command(command):
    match = re.search(r'-EncodedCommand\s+([A-Za-z0-9+/=]+)', command, re.IGNORECASE)
    return base64.b64decode(match.group(1)).decode('utf-16le') if match else command


def load_hook():
    if not HOOK.is_file():
        raise AssertionError('Lifecycle adapter not implemented')
    spec = importlib.util.spec_from_file_location('v3_hook_test_subject', HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class HookInstallerTest(unittest.TestCase):
    def test_install_matches_direct_editor_tools_without_matching_shell_commands(self):
        self.install()
        for relative in ('.claude/settings.local.json', '.codex/hooks.json'):
            config = json.loads((self.vault / relative).read_text(encoding='utf-8'))
            groups = config['hooks']['PostToolUse']
            owned = [group for group in groups if any('beyin_v3_hook.py' in decoded_command(h['command'])
                                                     for h in group.get('hooks', []))]
            self.assertEqual(len(owned), 1)
            matcher = re.compile('^(?:' + owned[0]['matcher'] + ')$')
            for tool in ('Edit', 'Write', 'apply_patch', 'MultiEdit', 'NotebookEdit'):
                self.assertIsNotNone(matcher.fullmatch(tool))
            for tool in ('Bash', 'exec_command', 'Read'):
                self.assertIsNone(matcher.fullmatch(tool))

    def test_install_antigravity_events_follow_its_hook_contract(self):
        # Antigravity CLI 1.5.18 hooks.json: SessionStart/PreInvocation/Stop are flat handler lists,
        # PostToolUse groups handlers under a tool-name regex (#903, #905).
        self.install()
        managed = json.loads((self.vault / '.agents/hooks.json').read_text(encoding='utf-8'))['beyin-v3']
        self.assertEqual(list(managed), ['SessionStart', 'PreInvocation', 'PostToolUse', 'Stop'])
        for event in ('SessionStart', 'PreInvocation', 'Stop'):
            self.assertEqual(len(managed[event]), 1)
            self.assertIn('command', managed[event][0])
            self.assertNotIn('matcher', managed[event][0])
        group, = managed['PostToolUse']
        self.assertEqual(set(group), {'matcher', 'hooks'})
        self.assertEqual(group['matcher'], self.hook.ANTIGRAVITY_EDIT_MATCHER)
        spec = importlib.util.spec_from_file_location('v3_hook_test_bridge', SCRIPTS / 'beyin_v3_bridge.py')
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        self.assertEqual(bridge.ANTIGRAVITY_EDIT_MATCHER, group['matcher'])
        matcher = re.compile(group['matcher'])
        self.assertLessEqual({'write_to_file', 'replace_file_content', 'multi_replace_file_content'},
                             set(self.hook.ANTIGRAVITY_WRITE_TOOLS))  # the IDE agent's file tools
        for tool in self.hook.ANTIGRAVITY_WRITE_TOOLS:
            self.assertIsNotNone(matcher.search(tool))
        for tool in ('read_file', 'view_file', 'list_dir', 'grep_search', 'run_command', 'multi_edit_file_preview',
                     'write_file2', 'replace_file_content_preview'):
            self.assertIsNone(matcher.search(tool))
        self.assertIn('--event PostToolUse', decoded_command(group['hooks'][0]['command']).replace("'", '').replace('"', ''))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-hook-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'Synthetic Beyin Ölçüm Space'
        self.vault.mkdir()
        self.state = self.root / 'state outside vault'
        home = self.root / 'isolated-home'
        home.mkdir()
        self.env = {'HOME': str(home), 'USERPROFILE': str(home), 'APPDATA': str(home / 'appdata'),
                    'LOCALAPPDATA': str(home / 'localappdata'), 'TEMP': str(self.root), 'TMP': str(self.root),
                    'PATH': isolated_path(), 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1',
                    'BEYIN_V3_NO_SPAWN': '1'}
        for key in ('SYSTEMROOT', 'WINDIR'):
            if key in os.environ:
                self.env[key] = os.environ[key]
        # In-process calls (is_synthetic_prompt, receipt_reminder) read os.environ directly.
        environment = clean_environ()
        environment.start()
        self.addCleanup(environment.stop)
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        self.hook = load_hook()
        self.payload = {'hook_event_name': 'SessionEnd', 'session_id': 'synthetic-session',
                        'event_id': 'synthetic-event', 'cwd': str(self.vault),
                        'prompt': 'SYNTHETIC_TRANSCRIPT_CANARY_NEVER_PERSIST'}

    def seed(self):
        from beyin_v3_sync import SyncEngine
        source = self.vault / 'notes/task.md'
        source.parent.mkdir()
        source.write_text('---\n' + json.dumps({'id': 'nebula-task', 'kind': 'task', 'revision': 1,
                           'project': 'nebula', 'status': 'active', 'visibility': 'internal'}) +
                          '\n---\nNebula calibration owner is Synthetic Reviewer.\n', encoding='utf-8')
        engine = SyncEngine(self.vault, self.state)
        engine.sync()
        return engine

    def invoke(self, payload, harness='codex', script=None, extra=None):
        result = subprocess.run([sys.executable, str(script or HOOK), '--vault', str(self.vault),
                                 '--state', str(self.state), '--harness', harness] + (extra or []),
                                input=json.dumps(payload), text=True, encoding='utf-8', capture_output=True,
                                cwd=self.vault, env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, 'Hook stdout must contain exactly one JSON response')
        return json.loads(lines[0])

    def test_hook_uses_local_state_when_synced_pin_is_from_another_user(self):
        from beyin_v3 import default_state
        with patch.dict(os.environ, self.env, clear=True):
            expected = default_state(self.vault).resolve()
            foreign_base = ('C:/Users/foreign-user/AppData/Local' if sys.platform == 'win32'
                            else '/Users/foreign-user/Library/Application Support' if sys.platform == 'darwin'
                            else '/home/foreign-user/.local/state')
            foreign = Path(foreign_base) / 'beyin-v3' / expected.name
            result = self.invoke(self.payload, extra=['--metadata-only', '--state', str(foreign)])
        self.assertEqual(result, {})
        self.assertTrue((expected / 'hook-queue').is_dir())
        self.assertFalse(foreign.exists())

    def test_hook_uses_local_state_when_synced_pin_is_from_another_os(self):
        from beyin_v3 import default_state
        with patch.dict(os.environ, self.env, clear=True):
            expected = default_state(self.vault).resolve()
            foreign = f"/home/other-user/.local/state/beyin-v3/{expected.name}"
            result = self.invoke(self.payload, extra=['--metadata-only', '--state', foreign])
        self.assertEqual(result, {})
        self.assertTrue((expected / 'hook-queue').is_dir())

    def lifecycle(self, event, session, harness='claude', extra=None, **fields):
        self.lifecycle_count = getattr(self, 'lifecycle_count', 0) + 1
        payload = dict({'hook_event_name': event, 'session_id': session,
                        'event_id': session + '-' + str(self.lifecycle_count)}, **fields)
        return self.invoke(payload, harness, extra=extra)

    def install(self, uninstall=False):
        self.assertTrue(INSTALLER.is_file(), 'Installer not implemented')
        result = subprocess.run([sys.executable, str(INSTALLER), '--vault', str(self.vault), '--state', str(self.state)] +
                                (['--uninstall'] if uninstall else []), capture_output=True, text=True,
                                encoding='utf-8', cwd=self.vault, env=self.env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_queue_duplicate_and_metadata_only(self):
        first = self.hook.enqueue_event(self.vault, self.state, self.payload, 'codex')
        second = self.hook.enqueue_event(self.vault, self.state, self.payload, 'codex')
        self.assertEqual(first, second)
        pending = list((self.state / 'hook-queue').glob('*.json'))
        self.assertEqual(len(pending), 1)
        self.assertNotIn('SYNTHETIC_TRANSCRIPT_CANARY_NEVER_PERSIST', pending[0].read_text())

    def test_queue_failure_retry_and_no_duplicate_ack(self):
        self.seed()
        from beyin_v3_sync import SyncEngine
        self.hook.enqueue_event(self.vault, self.state, self.payload, 'codex')
        with patch.object(SyncEngine, 'sync', side_effect=RuntimeError('Synthetic worker crash')):
            failed = self.hook.drain_queue(self.vault, self.state)
        self.assertEqual(failed['processed'], 0)
        self.assertEqual(failed['failed'], 1)
        self.assertEqual(failed['pending'], 1)
        recovered = self.hook.drain_queue(self.vault, self.state)
        self.assertEqual(recovered['processed'], 1)
        self.assertEqual(recovered['pending'], 0)
        self.hook.enqueue_event(self.vault, self.state, self.payload, 'codex')
        repeated = self.hook.drain_queue(self.vault, self.state)
        self.assertEqual(repeated['processed'], 0)
        self.assertEqual(repeated['pending'], 0)

    def test_degraded_sync_acknowledges_event_and_keeps_explicit_health_warning(self):
        self.seed()
        broken = self.vault / 'notes/broken.md'
        broken.write_text('---\nunsupported:\n  nested:\n    deeper: metadata\n---\nExcluded synthetic note.\n', encoding='utf-8')
        self.hook.enqueue_event(self.vault, self.state, self.payload, 'codex')
        result = self.hook.drain_queue(self.vault, self.state)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['pending'], 0)
        health = json.loads((self.state / 'hook-health.json').read_text(encoding='utf-8'))
        self.assertIsInstance(health['worker_pid'], int)
        self.assertGreater(health['worker_pid'], 0)
        self.assertEqual(health['sync']['status'], 'degraded')
        self.assertEqual(
            Path(health['sync']['warnings'][0]['source']).parts,
            ('notes', 'broken.md'),
        )

    def test_non_skill_entries_beside_skills_do_not_block_the_queue(self):
        self.seed()
        skills = self.vault / '.claude/skills'
        skills.mkdir(parents=True)
        (skills / 'LICENSE-upstream.txt').write_text('upstream license', encoding='utf-8')
        shared = self.vault / '.agents/skills/shared_utils'
        shared.mkdir(parents=True)
        (shared / 'helper.py').write_text('pass', encoding='utf-8')
        self.hook.enqueue_event(self.vault, self.state, self.payload, 'codex')
        result = self.hook.drain_queue(self.vault, self.state)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['pending'], 0)
        health = json.loads((self.state / 'hook-health.json').read_text(encoding='utf-8'))
        self.assertEqual(health['sync']['status'], 'succeeded')
        self.assertNotIn('skill_conflicts', health['sync'])
        self.assertEqual(sorted(health['sync']['skill_unmanaged']), ['LICENSE-upstream.txt', 'shared_utils'])

    def test_skill_conflict_is_reported_without_blocking_the_queue(self):
        self.seed()
        for side, body in (('.agents', 'codex edit'), ('.claude', 'claude edit')):
            path = self.vault / side / 'skills/sample/SKILL.md'
            path.parent.mkdir(parents=True)
            path.write_text(body, encoding='utf-8')
        self.hook.enqueue_event(self.vault, self.state, self.payload, 'codex')
        result = self.hook.drain_queue(self.vault, self.state)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['pending'], 0)
        health = json.loads((self.state / 'hook-health.json').read_text(encoding='utf-8'))
        self.assertEqual(health['sync']['skill_conflicts'], ['sample'])
        self.assertEqual((self.vault / '.agents/skills/sample/SKILL.md').read_text(encoding='utf-8'), 'codex edit')
        self.assertEqual((self.vault / '.claude/skills/sample/SKILL.md').read_text(encoding='utf-8'), 'claude edit')
        doctor = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'doctor'], capture_output=True, text=True,
                                encoding='utf-8', cwd=self.vault, env=self.env, timeout=20)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        report = json.loads(doctor.stdout)
        self.assertEqual(report['skill_conflicts'], ['sample'])
        self.assertEqual(report['status'], 'needs_attention')

    def test_queue_crash_after_sync_before_ack_retries_without_extra_revision(self):
        engine = self.seed()
        source = self.vault / 'notes/task.md'
        source.write_text(source.read_text(encoding='utf-8').replace('"revision": 1', '"revision": 2').replace('owner is', 'new owner is'), encoding='utf-8')
        self.hook.enqueue_event(self.vault, self.state, self.payload, 'codex')
        real_replace = os.replace
        def fail_ack(src, dst, *args, **kwargs):
            if Path(dst).parent.name == 'hook-done':
                raise OSError('Synthetic crash before queue acknowledgement')
            return real_replace(src, dst, *args, **kwargs)
        with patch.object(self.hook.os, 'replace', side_effect=fail_ack):
            try:
                self.hook.drain_queue(self.vault, self.state)
            except OSError:
                pass
        self.assertEqual(len(list((self.state / 'hook-queue').glob('*.json'))), 1)
        history_before = engine.store.history('nebula-task')
        self.assertEqual(history_before[-1]['revision'], 2)
        result = self.hook.drain_queue(self.vault, self.state)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['pending'], 0)
        self.assertEqual(engine.store.history('nebula-task'), history_before)

    def test_concurrent_queue_drains_acknowledge_event_once(self):
        self.seed()
        self.hook.enqueue_event(self.vault, self.state, self.payload, 'codex')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.hook.drain_queue(self.vault, self.state), range(2)))
        self.assertEqual(sum(r['processed'] for r in results), 1)
        self.assertEqual(list((self.state / 'hook-queue').glob('*.json')), [])

    def test_codex_claude_stdin_contexts_equal(self):
        self.seed()
        payload = dict(self.payload, hook_event_name='SessionStart', prompt='Nebula calibration')
        contexts = []
        for harness in ('codex', 'claude'):
            response = self.invoke(payload, harness)
            contexts.append(response['hookSpecificOutput']['additionalContext'])
            self.assertIn('Synthetic Reviewer', contexts[-1])
            self.assertIn('notes/task.md', contexts[-1])
        self.assertEqual(without_hook_clock(contexts[0]), without_hook_clock(contexts[1]))

    def test_cross_client_context_comparison_survives_minute_rollover(self):
        # Keep real subprocess hooks/source delivery; advance only the observed clock
        # field to reproduce a CI boundary without waiting for wall time.
        invoke = self.invoke
        moments = iter(('2026-11-12 15:18 +0000', '2026-11-12 15:19 +0000'))
        def across_minute(*args, **kwargs):
            response = invoke(*args, **kwargs)
            output = response['hookSpecificOutput']
            output['additionalContext'], changed = re.subn(
                r'(?m)^(Receipt session=[0-9a-f]{24}; now=)\d{4}-\d{2}-\d{2} '
                r'\d{2}:\d{2} [+-]\d{4}(; )',
                lambda match: match[1] + next(moments) + match[2],
                output['additionalContext'], count=1)
            self.assertEqual(changed, 1)
            return response
        with patch.object(self, 'invoke', side_effect=across_minute):
            self.test_codex_claude_stdin_contexts_equal()

    def test_restart_context_includes_receipt_as_historical_evidence(self):
        engine = self.seed()
        summary = 'SYNTHETIC_RECEIPT_CONTEXT Latest agreed verification step.'
        engine.receipt('restart-receipt', summary, ['notes/task.md'], 'codex')
        response = self.invoke(dict(self.payload, hook_event_name='SessionStart', prompt='Nebula calibration'))
        context = response['hookSpecificOutput']['additionalContext']
        self.assertIn(summary, context)
        self.assertTrue(any(marker in context.casefold() for marker in ['historical', 'history', 'geçmiş', 'gecmis']))

    def test_receipt_context_truncation_marker_when_exceeding_budget(self):
        engine = self.seed()
        engine.receipt('short', 'Short summary.', ['notes/task.md'], 'codex')
        short = 'receipts/' + hashlib.sha256(b'short').hexdigest() + '.md'
        context = self.hook.receipt_context(str(self.vault), str(self.state))
        self.assertIn((self.vault / short).read_text(encoding='utf-8'), context)
        self.assertNotIn('[truncated:', context)
        self.assertIn(f'Latest receipt ({short}; ', context)

        refs = []
        for i in range(60):
            (self.vault / f'notes/ref_{i:03d}.md').write_text(f'Ref {i}.\n', encoding='utf-8')
            refs.append(f'notes/ref_{i:03d}.md')
        engine.receipt('long', 'Long summary.', refs, 'codex')
        long = 'receipts/' + hashlib.sha256(b'long').hexdigest() + '.md'
        self.assertGreater(len((self.vault / long).read_text(encoding='utf-8')), 1200)
        context = self.hook.receipt_context(str(self.vault), str(self.state))
        self.assertTrue(context.endswith('\n[truncated: read source]\n'))
        header = f'\nLatest receipt ({long}; historical agent claim, not independently verified):\n'
        self.assertTrue(context.startswith(header))
        body = context[len(header):]
        self.assertEqual(len(body), 1200)
        # SessionStart clips this block from the end; the source must survive that (#147).
        import beyin_v3_companion as companion
        clipped = companion.clip(context, 400)
        self.assertIn(long, clipped)
        self.assertTrue(clipped.endswith('\n[truncated: read source]\n'))

    def test_session_start_receipt_skips_private_and_untrusted_refs(self):
        # The global bridge never showed such a receipt; the vault hook now shares its gate.
        engine = self.seed()
        notes = self.vault / 'notes'
        (notes / 'secret.md').write_text('---\nvisibility: private\n---\n# Müşteri Şifre\nArşiv notu.\n', encoding='utf-8')
        (notes / 'pasted.md').write_text('---\n' + json.dumps({'id': 'pasted', 'kind': 'note', 'trust': 'untrusted'}) +
                                         '\n---\nPasted text.\n', encoding='utf-8')
        engine.sync()
        engine.receipt('visible', 'VISIBLE_RECEIPT_CANARY', ['notes/task.md'], 'codex')
        time.sleep(0.01)
        engine.receipt('private', 'PRIVATE_RECEIPT_CANARY Müşteri sözleşmesi', ['notes/task.md', 'notes/secret.md'], 'codex')
        time.sleep(0.01)
        engine.receipt('untrusted', 'UNTRUSTED_RECEIPT_CANARY', ['notes/pasted.md'], 'claude')
        context = self.hook.receipt_context(str(self.vault), str(self.state))
        self.assertIn('VISIBLE_RECEIPT_CANARY', context)
        for canary in ('PRIVATE_RECEIPT_CANARY', 'UNTRUSTED_RECEIPT_CANARY', 'notes/secret.md'):
            self.assertNotIn(canary, context)
        response = self.invoke(dict(self.payload, hook_event_name='SessionStart'))
        opening = response['hookSpecificOutput']['additionalContext']
        self.assertIn('VISIBLE_RECEIPT_CANARY', opening)
        self.assertNotIn('PRIVATE_RECEIPT_CANARY', opening)
        self.assertNotIn('UNTRUSTED_RECEIPT_CANARY', opening)
        # Only private receipts: no receipt block at all rather than the private one.
        (self.vault / 'receipts' / (hashlib.sha256(b'visible').hexdigest() + '.md')).unlink()
        self.assertEqual(self.hook.receipt_context(str(self.vault), str(self.state)), '')

    def test_latest_receipt_follows_created_at_not_file_mtime(self):
        # #112: git pull and sync clients rewrite mtimes; the receipt's own stamp decides.
        engine = self.seed()
        engine.receipt('older', 'OLDER_RECEIPT_CANARY', ['notes/task.md'], 'codex')
        time.sleep(0.01)
        engine.receipt('newer', 'NEWER_RECEIPT_CANARY', ['notes/task.md'], 'codex')
        older = self.vault / 'receipts' / (hashlib.sha256(b'older').hexdigest() + '.md')
        future = time.time() + 3600
        os.utime(older, (future, future))
        context = self.hook.receipt_context(str(self.vault), str(self.state))
        self.assertIn('NEWER_RECEIPT_CANARY', context)
        self.assertNotIn('OLDER_RECEIPT_CANARY', context)
        # A receipt whose source file is gone, or a hand-made file the index never adopted, is not offered.
        (self.vault / 'receipts' / (hashlib.sha256(b'newer').hexdigest() + '.md')).unlink()
        (self.vault / 'receipts/hand-made.md').write_text('HAND_MADE_CANARY\n', encoding='utf-8')
        context = self.hook.receipt_context(str(self.vault), str(self.state))
        self.assertIn('OLDER_RECEIPT_CANARY', context)
        self.assertNotIn('HAND_MADE_CANARY', context)
        # No runtime index yet: no receipt block, never an error.
        self.assertEqual(self.hook.receipt_context(str(self.vault), str(self.root / 'empty-state')), '')

    def test_stop_stdin_queues_and_explicit_worker_drains(self):
        self.seed()
        response = self.invoke(dict(self.payload, hook_event_name='Stop'), 'claude')
        self.assertEqual(response, {})
        self.assertEqual(len(list((self.state / 'hook-queue').glob('*.json'))), 1)
        drained = self.invoke({}, 'claude', extra=['--drain-queue'])
        self.assertEqual(drained['processed'], 1)
        self.assertEqual(drained['pending'], 0)

    def test_stop_receipt_reminder_blocks_once_for_claude_and_codex(self):
        for harness in ('claude', 'codex'):
            session = 'reminder-' + harness
            self.assertEqual(self.lifecycle('PostToolUse', session, harness), {})
            queued = len(list((self.state / 'hook-queue').glob('*.json')))
            first = self.lifecycle('Stop', session, harness)
            self.assertEqual(first['decision'], 'block')
            self.assertIn('receipt --harness ' + harness + ' --session ' + hashlib.sha256(session.encode()).hexdigest()[:24], first['reason'])
            self.assertIn('--event-id EVENT_ID --summary "Work result" --ref PATH', first['reason'])
            self.assertIn('python3 beyin.py receipt --file RECEIPT_JSON --harness ' + harness, first['reason'])
            self.assertIn('Receipt session=' + hashlib.sha256(session.encode()).hexdigest()[:24] + ';', first['reason'])
            # The Stop checkpoint is queued before any reminder work.
            self.assertEqual(len(list((self.state / 'hook-queue').glob('*.json'))), queued + 1)
            self.lifecycle('PostToolUse', session, harness)
            self.assertEqual(self.lifecycle('Stop', session, harness), {})
        # Other harnesses have no Stop block contract here.
        for harness in ('hermes', 'opencode', 'omp'):
            self.lifecycle('PostToolUse', 'other-' + harness, harness)
            self.assertEqual(self.lifecycle('Stop', 'other-' + harness, harness), {})

    def test_claude_context_mode_defers_receipt_once_and_keeps_retrieval(self):
        self.seed()
        self.env['BEYIN_V3_RECEIPT_REMINDER'] = 'context'
        session = 'context-mode-private-session-id'
        self.lifecycle('PostToolUse', session, 'claude')
        self.assertEqual(self.lifecycle('Stop', session, 'claude'), {})
        self.assertEqual(self.lifecycle('Stop', session, 'claude'), {})
        pending = list((self.state / 'receipt-reminders').glob('*.pending-receipt'))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].read_bytes(), b'')
        self.assertNotIn(session, pending[0].name)
        for other_session, prompt in (('unrelated', 'Nebula calibration'), (session, '<task-notification>done</task-notification>')):
            result = self.lifecycle('UserPromptSubmit', other_session, 'claude', prompt=prompt)
            self.assertNotIn('Reminder from the previous turn', json.dumps(result))
        result = self.lifecycle('UserPromptSubmit', session, 'claude', prompt='Nebula calibration owner')
        self.assertNotIn('decision', result)
        context = result['hookSpecificOutput']['additionalContext']
        self.assertIn('Reminder from the previous turn', context)
        self.assertIn('Nebula calibration owner is Synthetic Reviewer', context)
        self.assertNotIn(session, context)
        self.assertNotIn('Reminder from the previous turn',
                         json.dumps(self.lifecycle('UserPromptSubmit', session, 'claude', prompt='Next')))
        self.lifecycle('PostToolUse', 'other-client', 'codex')
        self.assertEqual(self.lifecycle('Stop', 'other-client', 'codex')['decision'], 'block')

    def test_claude_context_mode_checks_receipt_and_opt_out_before_delivery(self):
        engine = self.seed()
        self.env['BEYIN_V3_RECEIPT_REMINDER'] = 'context'
        for session, prompt in (('received', 'Next'), ('opted-out', '[KAYDETME] Next'), ('disabled', 'Next')):
            self.lifecycle('PostToolUse', session, 'claude')
            self.assertEqual(self.lifecycle('Stop', session, 'claude'), {})
            if session == 'received':
                digest = hashlib.sha256(session.encode()).hexdigest()[:24]
                engine.receipt('received-event', 'Edited the task note.', ['notes/task.md'], 'claude', session=digest)
            if session == 'disabled':
                self.env['BEYIN_V3_NO_RECEIPT_REMINDER'] = '1'
            result = self.lifecycle('UserPromptSubmit', session, 'claude', prompt=prompt)
            self.assertNotIn('Reminder from the previous turn', json.dumps(result))
            if session != 'disabled':
                result = self.lifecycle('UserPromptSubmit', session, 'claude', prompt='Next again')
                self.assertNotIn('Reminder from the previous turn', json.dumps(result))

    def test_claude_context_mode_defers_learning_reminder_once(self):
        engine = self.seed()
        self.env['BEYIN_V3_RECEIPT_REMINDER'] = 'context'
        session = 'deferred-learning'
        digest = hashlib.sha256(session.encode()).hexdigest()[:24]
        self.lifecycle('PostToolUse', session, 'claude')
        engine.receipt('learning-event', 'Refactor bitti.\nÖğrenilen: WAL timeout en az 5 sn.', ['notes/task.md'],
                       'claude', session=digest)
        self.assertEqual(self.lifecycle('Stop', session, 'claude'), {})
        first = self.lifecycle('UserPromptSubmit', session, 'claude', prompt='Next')
        self.assertIn('Learnings were reported', first['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('Learnings were reported',
                         json.dumps(self.lifecycle('UserPromptSubmit', session, 'claude', prompt='Next again')))

    def test_stop_receipt_reminder_passes_when_receipt_is_present(self):
        engine = self.seed()
        def session(name):
            return hashlib.sha256(name.encode()).hexdigest()[:24]
        self.lifecycle('PostToolUse', 'receipted', 'claude')
        engine.receipt('receipted-1', 'Edited the task note.', ['notes/task.md'], 'claude', session=session('receipted'))
        self.assertEqual(self.lifecycle('Stop', 'receipted', 'claude'), {})
        # Edits after that receipt open a new window.
        self.lifecycle('PostToolUse', 'receipted', 'claude')
        self.assertEqual(self.lifecycle('Stop', 'receipted', 'claude')['decision'], 'block')
        # Same match as the receipt gap projection: harness and session must both agree.
        self.lifecycle('PostToolUse', 'other-harness', 'codex')
        engine.receipt('other-harness-1', 'Edited the task note.', ['notes/task.md'], 'claude', session=session('other-harness'))
        self.assertEqual(self.lifecycle('Stop', 'other-harness', 'codex')['decision'], 'block')
        self.lifecycle('PostToolUse', 'no-session', 'claude')
        engine.receipt('no-session-1', 'Edited the task note.', ['notes/task.md'], 'claude')
        self.assertEqual(self.lifecycle('Stop', 'no-session', 'claude')['decision'], 'block')

    def test_user_prompt_submit_accepts_null_and_block_list_prompts(self):
        self.seed()
        for index, prompt in enumerate((None, [{'type': 'text', 'text': 'Nebula calibration owner'}], 42)):
            output = self.lifecycle('UserPromptSubmit', 'non-string-' + str(index), 'claude', prompt=prompt)
            self.assertFalse((self.state / 'hook-error.json').exists(), prompt)
            self.assertNotIn('V3 source sync failed', json.dumps(output), prompt)
        self.assertEqual(self.hook.prompt_text({'prompt': ['a', {'type': 'text', 'text': 'b'}, {'type': 'image'}]}), 'a\nb')
        self.assertEqual(self.hook.prompt_text({'prompt': None}), '')
        self.assertEqual(self.hook.prompt_text(None), '')

    def test_undecodable_prompt_byte_keeps_the_turn_and_a_cut_payload_is_recorded(self):
        self.seed()

        def raw(data):
            result = subprocess.run([sys.executable, str(HOOK), '--vault', str(self.vault), '--state', str(self.state),
                                     '--harness', 'claude'], input=data, capture_output=True, cwd=self.vault,
                                    env=self.env, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'raw-bytes', 'event_id': 'raw-bytes-1',
                   'prompt': 'Nebula calibration owner BYTE'}
        # A stray byte in the prompt is read as U+FFFD; the turn keeps its context.
        output = raw(json.dumps(payload).encode('utf-8').replace(b'BYTE', b'\xff'))
        self.assertIn('Nebula calibration owner', output['hookSpecificOutput']['additionalContext'])
        self.assertFalse((self.state / 'hook-error.json').exists())
        # A payload cut at the 1 MB read is not JSON: '{}' for the host, and doctor still sees why.
        cut = json.dumps(dict(payload, event_id='raw-bytes-2', prompt='x' * 1_100_000)).encode('utf-8')
        self.assertEqual(raw(cut), {})
        error = json.loads((self.state / 'hook-error.json').read_text(encoding='utf-8'))
        self.assertEqual(error['error'], 'JSONDecodeError')

    def test_utf8_bom_stdin_payload_injects_context_without_error(self):
        """PowerShell wrapper on Windows can prepend a UTF-8 BOM to stdin; the hook must decode cleanly."""
        self.seed()
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'bom-session', 'event_id': 'bom-1',
                   'prompt': 'Nebula calibration owner'}
        bom_data = b'\xef\xbb\xbf' + json.dumps(payload).encode('utf-8')
        result = subprocess.run([sys.executable, str(HOOK), '--vault', str(self.vault), '--state', str(self.state),
                                 '--harness', 'claude'], input=bom_data, capture_output=True, cwd=self.vault,
                                env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertIn('Nebula calibration owner', output['hookSpecificOutput']['additionalContext'])
        self.assertFalse((self.state / 'hook-error.json').exists())

    def test_stop_receipt_reminder_session_closes_the_gap(self):
        engine = self.seed()
        self.lifecycle('UserPromptSubmit', 'gap-session', 'claude', prompt='update the calibration note')
        self.lifecycle('PostToolUse', 'gap-session', 'claude')
        blocked = self.lifecycle('Stop', 'gap-session', 'claude')
        session = re.search(r'Receipt session=([0-9a-f]{24});', blocked['reason']).group(1)
        engine.receipt('gap-receipt', 'Edited the task note.', ['notes/task.md'], 'claude', session=session)
        self.assertEqual(self.lifecycle('Stop', 'gap-session', 'claude', stop_hook_active=True), {})
        self.assertEqual(self.hook.drain_queue(self.vault, self.state)['failed'], 0)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))
        self.assertEqual(gaps['potential_missing_receipts'], 0)

    def test_stop_knowledge_reminder_blocks_when_receipt_has_learning_without_knowledge_note(self):
        engine = self.seed()
        def session(name):
            return hashlib.sha256(name.encode()).hexdigest()[:24]
        sess_name = 'learn-session'
        self.lifecycle('PostToolUse', sess_name, 'claude')
        summary = "Completed SQLite refactor.\nÖğrenilen: SQLite WAL modunda timeout süresi en az 5 saniye olmalı."
        engine.receipt('learn-receipt-1', summary, ['notes/task.md'], 'claude', session=session(sess_name))
        first = self.lifecycle('Stop', sess_name, 'claude')
        self.assertEqual(first.get('decision'), 'block')
        self.assertIn('knowledge/concepts/', first.get('reason', ''))
        second = self.lifecycle('Stop', sess_name, 'claude')
        self.assertEqual(second, {})

    def test_stop_knowledge_reminder_passes_when_knowledge_ref_is_present(self):
        engine = self.seed()
        def session(name):
            return hashlib.sha256(name.encode()).hexdigest()[:24]
        sess_name = 'know-ref-session'
        self.lifecycle('PostToolUse', sess_name, 'claude')
        summary = "Completed SQLite refactor.\nÖğrenilen: SQLite WAL modunda timeout süresi en az 5 saniye olmalı."
        engine.note_create('knowledge/concepts/sqlite-wal.md', 'WAL mode details', {'id': 'sw-1'})
        engine.receipt('know-receipt-1', summary, ['knowledge/concepts/sqlite-wal.md'], 'claude', session=session(sess_name))
        response = self.lifecycle('Stop', sess_name, 'claude')
        self.assertEqual(response, {})

    def test_stop_knowledge_reminder_does_not_accept_untouched_refs(self):
        engine = self.seed()
        for harness in ('claude', 'codex'):
            for index, root in enumerate(('knowledge/concepts', '500-Knowledge', '🧠 500-Knowledge')):
                with self.subTest(harness=harness, root=root):
                    name = f'old-knowledge-{harness}-{index}'
                    relative = f'{root}/{name}.md'
                    note = self.vault / relative
                    note.parent.mkdir(parents=True, exist_ok=True)
                    note.write_text('# Existing knowledge\n', encoding='utf-8')
                    os.utime(note, (0, 0))
                    self.lifecycle('PostToolUse', name, harness)
                    session = hashlib.sha256(name.encode()).hexdigest()[:24]
                    engine.receipt(name, 'İş tamamlandı.\nÖğrenilen: Yeni bir yöntem.', [relative],
                                   harness, session=session)
                    response = self.lifecycle('Stop', name, harness)
                    self.assertEqual(response.get('decision'), 'block')
                    self.assertIn('Learnings were reported', response.get('reason', ''))
                    self.assertEqual(self.lifecycle('Stop', name, harness), {})

    def test_stop_knowledge_reminder_accepts_note_written_before_the_window_opened(self):
        # Maintainer: the editor write of a note precedes the PostToolUse that opens the window,
        # so a window whose first edit is the knowledge note itself fails the mtime test. The
        # editor tool's own path is the evidence (Claude file_path, Codex apply_patch header).
        engine = self.seed()
        for harness in ('claude', 'codex'):
            with self.subTest(harness=harness):
                name = f'first-edit-knowledge-{harness}'
                relative = f'🧠 500-Knowledge/Öğrenim-{harness}.md'
                note = self.vault / relative
                note.parent.mkdir(parents=True, exist_ok=True)
                note.write_text('# Şükrü onayı\n\nMüşteri arşivi onaysız kapanmaz.\n', encoding='utf-8')
                before = time.time() - 5
                os.utime(note, (before, before))
                tool_input = ({'file_path': str(note)} if harness == 'claude' else
                              {'command': f'*** Begin Patch\n*** Add File: {relative}\n+# Şükrü\n*** End Patch\n'})
                self.lifecycle('PostToolUse', name, harness, tool_input=tool_input, cwd=str(self.vault))
                session = hashlib.sha256(name.encode()).hexdigest()[:24]
                engine.receipt(name, 'İş tamamlandı.\nÖğrenilen: Onay kuralı.', [relative], harness, session=session)
                self.assertEqual(self.lifecycle('Stop', name, harness), {})

    def test_stop_knowledge_reminder_accepts_knowledge_edit_beyond_five_paths_in_patch(self):
        # Issue #509: apply_patch editing 6+ files with distilled note as 6th or later path.
        engine = self.seed()
        name = 'multi-patch-knowledge'
        relative = 'knowledge/concepts/sqlite-wal.md'
        note = self.vault / relative
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text('# SQLite WAL\nTimeout 5s.\n', encoding='utf-8')
        before = time.time() - 10
        os.utime(note, (before, before))

        patch_lines = ['*** Begin Patch\n']
        for i in range(1, 6):
            f_rel = f'notes/file{i}.md'
            f_path = self.vault / f_rel
            f_path.parent.mkdir(parents=True, exist_ok=True)
            f_path.write_text(f'# File {i}\n', encoding='utf-8')
            patch_lines.append(f'*** Update File: {f_rel}\n+change {i}\n')
        patch_lines.append(f'*** Update File: {relative}\n+WAL timeout rule\n*** End Patch\n')
        command = ''.join(patch_lines)

        tool_input = {'command': command}
        self.lifecycle('PostToolUse', name, 'codex', tool_input=tool_input, cwd=str(self.vault))
        session = hashlib.sha256(name.encode()).hexdigest()[:24]
        engine.receipt(name, 'İş tamamlandı.\nÖğrenilen: WAL timeout en az 5 sn.', [relative], 'codex', session=session)
        # Must pass without block: knowledge was edited in the 6th path
        self.assertEqual(self.lifecycle('Stop', name, 'codex'), {})

    def test_knowledge_update_requires_a_recent_file_not_a_ref_alone(self):
        receipt = {'refs': ['knowledge/concepts/existing.md']}
        note = self.vault / receipt['refs'][0]
        note.parent.mkdir(parents=True)
        self.assertFalse(self.hook._has_knowledge_update(None, receipt, 20))
        self.assertFalse(self.hook._has_knowledge_update(self.vault, receipt, 20))
        note.write_text('# Existing\n', encoding='utf-8')
        os.utime(note, (10, 10))
        self.assertFalse(self.hook._has_knowledge_update(self.vault, receipt, 20))
        note.write_text('# Updated\nNew knowledge.\n', encoding='utf-8')
        os.utime(note, (30, 30))
        self.assertTrue(self.hook._has_knowledge_update(self.vault, receipt, 20))
        # A parallel session's recent note does not count when this receipt does not cite it (#509).
        self.assertFalse(self.hook._has_knowledge_update(self.vault, {'refs': ['notes/task.md']}, 20))
        note.unlink()
        self.assertFalse(self.hook._has_knowledge_update(self.vault, receipt, 20))

    def test_knowledge_update_rejects_symlink_and_nonknowledge_target(self):
        self.seed()
        folder = self.vault / 'knowledge/concepts'
        folder.mkdir(parents=True)
        alias = folder / 'alias.md'
        try:
            alias.symlink_to(self.vault / 'notes/task.md')
        except OSError:
            self.skipTest('symlinks unavailable')
        receipt = {'refs': ['knowledge/concepts/alias.md']}
        self.assertFalse(self.hook._has_knowledge_update(self.vault, receipt, 0))
        alias.unlink()
        folder.rmdir()
        folder.symlink_to(self.vault / 'notes', target_is_directory=True)
        receipt = {'refs': ['knowledge/concepts/task.md']}
        self.assertFalse(self.hook._has_knowledge_update(self.vault, receipt, 0))

    def test_stop_knowledge_reminder_passes_when_learning_is_declared_none(self):
        engine = self.seed()
        def session(name):
            return hashlib.sha256(name.encode()).hexdigest()[:24]
        sess_name = 'no-learn-session'
        self.lifecycle('PostToolUse', sess_name, 'claude')
        summary = "Routine bugfix completed.\nÖğrenilen: Yok (rutin hata düzeltmesi)"
        engine.receipt('no-learn-1', summary, ['notes/task.md'], 'claude', session=session(sess_name))
        response = self.lifecycle('Stop', sess_name, 'claude')
        self.assertEqual(response, {})

    def test_stop_knowledge_reminder_challenges_bare_no_learning_without_explanation(self):
        engine = self.seed()
        def session(name):
            return hashlib.sha256(name.encode()).hexdigest()[:24]
        sess_name = 'bare-no-learn-session'
        self.lifecycle('PostToolUse', sess_name, 'claude')
        summary = "Routine bugfix completed.\nÖğrenilen: Yok"
        engine.receipt('bare-1', summary, ['notes/task.md'], 'claude', session=session(sess_name))
        response = self.lifecycle('Stop', sess_name, 'claude')
        self.assertEqual(response.get('decision'), 'block')
        self.assertIn("Öğrenilen: yok", response.get('reason', ''))
        # A second stop passes open
        self.assertEqual(self.lifecycle('Stop', sess_name, 'claude'), {})

    def test_learning_declaration_reads_only_the_label_line(self):
        declared = self.hook._has_declared_learning
        cases = {
            # Declared learnings the first regex missed.
            'Learned: WAL needs a 5 second busy timeout': True,
            '- Öğrenilen — WAL timeout en az 5 sn': True,
            'Öğrenilen: yoklama akışı artık idempotent': True,
            'ÖĞRENİLEN: WAL timeout': True,
            'KALICI ÖĞRENİM: WAL timeout': True,
            # Explicit "no learning" answers and non-labels the first regex flagged.
            'Öğrenilen: Hiçbiri': False,
            'ÖĞRENİLEN: HİÇBİRİ': False,
            'Öğrenilen: kalıcı öğrenim bulunmadı': False,
            'Yapılan: fix.\nÖğrenilen:\n\nAçık kalan: testler': False,
            'Fixed machine learning: pipeline config': False,
            'Tuned learning-rate schedule in the trainer': False,
            'Ders-plan sayfası düzeltildi': False,
            'Öğrenilen: yok.': False,
            # A parenthetical only explains a "none" answer; text after it is still a learning.
            'Öğrenilen: yok (rutin kontrol)': False,
            'Öğrenilen: yok (ayrıntı araştırma notunda).': False,
            'Learned: none (routine check)': False,
            'Öğrenilen: yok (rutin) ama WAL timeout en az 5 sn': True,
            'Öğrenilen: yoklama (idempotent) akışı': True,
        }
        for summary, expected in cases.items():
            with self.subTest(summary=summary):
                self.assertIs(declared(summary), expected)

    def test_stop_knowledge_reminder_accepts_human_knowledge_root(self):
        # The official template keeps human-curated knowledge in 🧠 500-Knowledge/ (or 500-Knowledge/).
        engine = self.seed()
        summary = 'Araştırma bitti.\nÖğrenilen: WAL timeout en az 5 sn.'
        for index, root in enumerate(('🧠 500-Knowledge', '500-Knowledge')):
            for via_ref in (True, False):
                with self.subTest(root=root, via_ref=via_ref):
                    name = f'human-root-{index}-{via_ref}'
                    session = hashlib.sha256(name.encode()).hexdigest()[:24]
                    note = self.vault / root / 'Ajanlar' / f'wal-{name}.md'
                    note.parent.mkdir(parents=True, exist_ok=True)
                    if via_ref:
                        self.lifecycle('PostToolUse', name, 'claude')
                        note.write_text('# WAL\n', encoding='utf-8')
                        refs = ['notes/task.md', note.relative_to(self.vault).as_posix()]
                    else:
                        note.write_text('# WAL\n', encoding='utf-8')
                        self.lifecycle('PostToolUse', name, 'claude', tool_input={'file_path': str(note)})
                        refs = ['notes/task.md']
                    engine.receipt(f'{name}-1', summary, refs, 'claude', session=session)
                    self.assertEqual(self.lifecycle('Stop', name, 'claude'), {})
                    os.utime(note, (0, 0))  # keep the folder scan of the next case honest

    def test_stop_knowledge_reminder_ignores_other_roots(self):
        engine = self.seed()
        session = hashlib.sha256('elsewhere'.encode()).hexdigest()[:24]
        self.lifecycle('PostToolUse', 'elsewhere', 'claude')
        note = self.vault / 'notes' / '500-Knowledge-plan.md'
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text('# plan\n', encoding='utf-8')
        engine.receipt('elsewhere-1', 'Refactor bitti.\nÖğrenilen: WAL timeout en az 5 sn.',
                       ['notes/500-Knowledge-plan.md'], 'claude', session=session)
        self.assertEqual(self.lifecycle('Stop', 'elsewhere', 'claude').get('decision'), 'block')

    def test_stop_knowledge_reminder_follows_a_receipt_written_for_the_receipt_reminder(self):
        engine = self.seed()
        session = hashlib.sha256('chain'.encode()).hexdigest()[:24]
        self.lifecycle('PostToolUse', 'chain', 'claude')
        self.assertIn('knowledge/concepts/', self.lifecycle('Stop', 'chain', 'claude')['reason'])
        # The agent answers with a receipt that declares a learning; no further file edits.
        engine.receipt('chain-1', 'Refactor bitti.\nÖğrenilen: WAL timeout en az 5 sn.', ['notes/task.md'],
                       'claude', session=session)
        self.assertEqual(self.lifecycle('Stop', 'chain', 'claude', stop_hook_active=True), {})
        nxt = self.lifecycle('Stop', 'chain', 'claude')
        self.assertEqual(nxt.get('decision'), 'block')
        self.assertIn('Learnings were reported', nxt['reason'])
        self.assertEqual(self.lifecycle('Stop', 'chain', 'claude'), {})

    def test_stop_knowledge_reminder_ignores_generated_knowledge_files(self):
        engine = self.seed()
        session = hashlib.sha256('seeds'.encode()).hexdigest()[:24]
        self.lifecycle('PostToolUse', 'seeds', 'claude')
        # A V2 compiler run, git pull or iCloud sync touches the seeds; nothing was distilled.
        (self.vault / 'knowledge').mkdir(exist_ok=True)
        for name in ('index.md', 'log.md'):
            (self.vault / 'knowledge' / name).write_text('# seed\n', encoding='utf-8')
        engine.receipt('seeds-1', 'Refactor bitti.\nÖğrenilen: WAL timeout en az 5 sn.',
                       ['notes/task.md', 'knowledge/index.md'], 'claude', session=session)
        self.assertEqual(self.lifecycle('Stop', 'seeds', 'claude').get('decision'), 'block')

    def test_stop_receipt_reminder_passes_without_edits(self):
        self.assertEqual(self.lifecycle('Stop', 'no-edits', 'claude'), {})
        # The global bridge (--metadata-only) keeps no reminder state.
        self.lifecycle('PostToolUse', 'bridged', 'claude', extra=['--metadata-only'])
        self.assertEqual(self.lifecycle('Stop', 'bridged', 'claude'), {})
        self.assertFalse((self.state / 'receipt-reminders').exists())

    def test_stop_receipt_reminder_fails_open_on_broken_runtime_state(self):
        self.lifecycle('PostToolUse', 'broken', 'claude')
        database = self.state / 'memory.sqlite3'
        database.write_bytes(b'not a sqlite database')
        self.assertEqual(self.lifecycle('Stop', 'broken', 'claude'), {})
        database.unlink()
        self.assertEqual(self.lifecycle('Stop', 'broken', 'claude')['decision'], 'block')

    def test_stop_receipt_reminder_honours_user_opt_out(self):
        self.lifecycle('UserPromptSubmit', 'opt-out', 'codex', prompt='bunu [kaydetme]')
        self.lifecycle('PostToolUse', 'opt-out', 'codex')
        self.assertEqual(self.lifecycle('Stop', 'opt-out', 'codex'), {})
        self.env['BEYIN_V3_NO_RECEIPT_REMINDER'] = '1'
        self.lifecycle('PostToolUse', 'env-opt-out', 'claude')
        self.assertEqual(self.lifecycle('Stop', 'env-opt-out', 'claude'), {})
        del self.env['BEYIN_V3_NO_RECEIPT_REMINDER']
        self.assertEqual(self.lifecycle('Stop', 'env-opt-out', 'claude'), {})

    def test_stop_receipt_reminder_honours_casefold_opt_out(self):
        """[KAYDETME] uppercase or mixed case should opt out of receipt reminders (#754)."""
        self.lifecycle('UserPromptSubmit', 'case-opt-out', 'claude', prompt='Bunu lütfen [KAYDETME]')
        self.lifecycle('PostToolUse', 'case-opt-out', 'claude')
        self.assertEqual(self.lifecycle('Stop', 'case-opt-out', 'claude'), {})
        folder = self.state / 'receipt-reminders'
        digest = hashlib.sha256(b'case-opt-out').hexdigest()
        self.assertTrue((folder / f"{digest}.optout").exists())

    def test_stop_receipt_reminder_quiet_paths_keep_the_one_reminder(self):
        self.lifecycle('PostToolUse', 'quiet', 'claude')
        self.assertEqual(self.lifecycle('Stop', 'quiet', 'claude', stop_hook_active=True), {})
        self.assertEqual(self.lifecycle('Stop', 'quiet', 'claude', extra=['--metadata-only']), {})
        preferences = self.vault / '.beyin-preferences.json'
        preferences.write_text(json.dumps({'auto_sync': False}), encoding='utf-8')
        self.assertEqual(self.lifecycle('Stop', 'quiet', 'claude'), {})
        preferences.unlink()
        self.assertEqual(self.lifecycle('Stop', 'quiet', 'claude')['decision'], 'block')

    def test_stop_receipt_reminder_sees_cli_writes_from_the_claude_bash_tool(self):
        # #212: Bash is not a PostToolUse matcher. A source write through beyin.py names its Claude
        # session through CLAUDE_CODE_SESSION_ID, so Stop sees it; other shells mark nothing.
        def write(name, session=None):
            env = dict(self.env, **({'CLAUDE_CODE_SESSION_ID': session} if session else {}))
            return subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                   '--state', str(self.state), 'note-create', '--file', '-'],
                                  input=json.dumps({'source': 'notes/' + name + '.md', 'text': '# Synthetic ' + name + '\n'}),
                                  text=True, encoding='utf-8', capture_output=True, cwd=self.vault, env=env, timeout=60)
        self.lifecycle('UserPromptSubmit', 'bash-write', 'claude', prompt='not yaz')
        self.assertEqual(write('bash-write', 'bash-write').returncode, 0)
        self.assertEqual(self.lifecycle('Stop', 'bash-write', 'claude')['decision'], 'block')
        # A refused write and a write from the user's own terminal leave no edit window.
        self.lifecycle('UserPromptSubmit', 'refused', 'claude', prompt='tekrar yaz')
        self.assertNotEqual(write('bash-write', 'refused').returncode, 0)
        self.assertEqual(write('terminal').returncode, 0)
        self.assertEqual(self.lifecycle('Stop', 'refused', 'claude'), {})
        self.assertEqual(sorted(path.name for path in (self.state / 'receipt-reminders').glob('*.edited')),
                         [hashlib.sha256(b'bash-write').hexdigest() + '.edited'])
        # The user opt-out covers this path too.
        self.env['BEYIN_V3_NO_RECEIPT_REMINDER'] = '1'
        self.assertEqual(write('opted-out', 'opted-out').returncode, 0)
        self.assertFalse((self.state / 'receipt-reminders' / (hashlib.sha256(b'opted-out').hexdigest() + '.edited')).exists())

    def test_antigravity_start_only_first_invocation_and_final_idle_stop(self):
        self.seed()
        payload = {'hook_event_name': 'PreInvocation', 'conversationId': 'synthetic-agy',
                   'invocationNum': 0, 'event_id': 'ag-start', 'prompt': 'Nebula calibration'}
        response = self.invoke(payload, 'antigravity')
        self.assertIn('Synthetic Reviewer', response['injectSteps'][0]['ephemeralMessage'])
        self.assertEqual(self.invoke(dict(payload, invocationNum=1), 'antigravity'), {})
        before = len(list((self.state / 'hook-queue').glob('*.json')))
        stop = {'hook_event_name': 'Stop', 'conversationId': 'synthetic-agy', 'fullyIdle': False, 'event_id': 'ag-stop'}
        self.assertEqual(self.invoke(stop, 'antigravity'), {'decision': 'stop'})
        self.assertEqual(len(list((self.state / 'hook-queue').glob('*.json'))), before)
        self.assertEqual(self.invoke(dict(stop, fullyIdle=True), 'antigravity'), {'decision': 'stop'})
        self.assertEqual(len(list((self.state / 'hook-queue').glob('*.json'))), before + 1)

    def agy_tool(self, session, name, args, **fields):
        # Antigravity CLI 1.5.18 PostToolUse shape: toolCall name/args, error "" on success, camelCase.
        payload = dict({'hookEventName': 'PostToolUse', 'conversationId': session, 'cwd': str(self.vault),
                        'error': '', 'stepIdx': 3, 'toolCall': {'id': 'call-1', 'name': name, 'args': args}}, **fields)
        return self.invoke(payload, 'antigravity', extra=['--event', 'PostToolUse'])

    def test_antigravity_stop_receipt_reminder_continues_model(self):
        self.seed()
        session = 'ag-reminder-session'
        reminders = self.state / 'receipt-reminders'
        edited = reminders / (hashlib.sha256(session.encode()).hexdigest() + '.edited')
        note = str((self.vault / 'notes/task.md').resolve())
        outside = str((self.root / 'outside project/app.py').resolve())
        # Reads, shell steps, failed writes and writes outside the vault open no edit window;
        # PostToolUse answers {} as Antigravity's contract expects.
        self.assertEqual(self.agy_tool(session, 'read_file', {'Path': note}), {})
        self.assertEqual(self.agy_tool(session, 'run_command', {'CommandLine': 'ls'}), {})
        self.assertEqual(self.agy_tool(session, 'write_file', {'Path': note, 'Content': 'x'}, error='denied'), {})
        self.assertEqual(self.agy_tool(session, 'write_file', {'Path': outside, 'Content': 'x'}), {})
        self.assertFalse(edited.exists())
        stop = {'hookEventName': 'Stop', 'conversationId': session, 'fullyIdle': True,
                'continuationActive': False, 'lastUserInput': 'edit the note'}
        self.assertEqual(self.invoke(stop, 'antigravity', extra=['--event', 'Stop']), {'decision': 'stop'})
        # A vault write (edit_file with Path) does.
        self.assertEqual(self.agy_tool(session, 'edit_file', {'Path': note, 'Target': 'a', 'Replacement': 'b'}), {})
        self.assertTrue(edited.exists())
        # A Stop that ends work a Stop hook asked for (continuationActive) is not asked again.
        self.assertEqual(self.invoke(dict(stop, continuationActive=True), 'antigravity', extra=['--event', 'Stop']),
                         {'decision': 'stop'})
        res = self.invoke(stop, 'antigravity', extra=['--event', 'Stop'])
        self.assertEqual(res.get('decision'), 'continue')
        self.assertIn('Receipt session=', res.get('reason', ''))
        self.assertIn('--harness antigravity', res['reason'])
        # One reminder per session.
        self.assertEqual(self.invoke(stop, 'antigravity', extra=['--event', 'Stop']), {'decision': 'stop'})

    def test_antigravity_continued_stop_by_execution_num_asks_nothing_more_that_turn(self):
        # Recorded on Antigravity CLI 1.5.18: the Stop right after {"decision":"continue"} came with
        # continuationActive false and executionNum 1; the next user message's first Stop has 0 again.
        engine = self.seed()
        session = 'agy-execution-num'
        note = str((self.vault / 'notes/task.md').resolve())
        self.assertEqual(self.agy_tool(session, 'write_file', {'Path': note, 'Content': 'x'}), {})
        stop = {'hookEventName': 'Stop', 'conversationId': session, 'fullyIdle': True, 'continuationActive': False,
                'executionNum': 0, 'terminationReason': 'NO_TOOL_CALL', 'error': '', 'lastUserInput': 'edit the note'}
        first = self.invoke(stop, 'antigravity', extra=['--event', 'Stop'])
        self.assertEqual(first['decision'], 'continue')
        self.assertIn('Receipt session=', first['reason'])
        # The model answers with a receipt that declares a learning but writes no knowledge note.
        engine.receipt('agy-execution-num-receipt', 'Not güncellendi.\nÖğrenilen: Nebula kalibrasyonu haftalık.',
                       ['notes/task.md'], 'antigravity', session=hashlib.sha256(session.encode()).hexdigest()[:24])
        # The continued Stop of the same turn asks nothing more (no second continue in one turn).
        self.assertEqual(self.invoke(dict(stop, executionNum=1), 'antigravity', extra=['--event', 'Stop']),
                         {'decision': 'stop'})
        # The learning reminder comes at the next message's Stop, once.
        later = self.invoke(stop, 'antigravity', extra=['--event', 'Stop'])
        self.assertEqual(later['decision'], 'continue')
        self.assertIn('knowledge/concepts/', later['reason'])
        self.assertEqual(self.invoke(stop, 'antigravity', extra=['--event', 'Stop']), {'decision': 'stop'})
        continued = self.hook.antigravity_continued_stop
        for value in (True, '1', 1.0, -1, 0, None):  # only a positive integer counts
            with self.subTest(executionNum=value):
                self.assertFalse(continued({'executionNum': value, 'continuationActive': False}))
        self.assertTrue(continued({'executionNum': 2}))
        self.assertTrue(continued({'executionNum': 0, 'continuationActive': True}))

    def test_antigravity_abnormal_stop_respects_termination_reason_and_error(self):
        # Antigravity Stop payloads carry terminationReason and error; abnormal/terminal stops
        # must return {"decision":"stop"} without spawning a worker or clearing .edited.
        abnormal_reasons = (
            'USER_CANCELED',
            'ERROR',
            'MAX_INVOCATIONS',
            'MAX_FORCED_INVOCATIONS',
            'MAX_TOKEN_BUDGET_EXCEEDED',
            'TERMINAL_STEP_TYPE',
            'TERMINAL_CUSTOM_HOOK',
            'INJECTED_RESPONSE',
        )
        self.seed()
        session = 'agy-abnormal-stop'
        reminders = self.state / 'receipt-reminders'
        digest = hashlib.sha256(session.encode()).hexdigest()
        edited = reminders / (digest + '.edited')
        done = reminders / (digest + '.done')
        note = str((self.vault / 'notes/task.md').resolve())
        self.assertEqual(self.agy_tool(session, 'write_file', {'Path': note, 'Content': 'x'}), {})
        self.assertTrue(edited.exists())
        queued_before = len(list((self.state / 'hook-queue').glob('*.json')))
        base_stop = {'hookEventName': 'Stop', 'conversationId': session, 'fullyIdle': True,
                     'continuationActive': False, 'executionNum': 0, 'lastUserInput': 'edit the note'}

        for reason in abnormal_reasons:
            with self.subTest(terminationReason=reason):
                res = self.invoke(dict(base_stop, terminationReason=reason, error=''),
                                  'antigravity', extra=['--event', 'Stop'])
                self.assertEqual(res, {'decision': 'stop'})
                self.assertTrue(edited.exists())
                self.assertFalse(done.exists())
                self.assertEqual(len(list((self.state / 'hook-queue').glob('*.json'))), queued_before)

        for fields in ({'terminationReason': 'NO_TOOL_CALL', 'error': 'model error'}, {'error': 'tool error'}):
            with self.subTest(error_case=fields):
                res = self.invoke(dict(base_stop, **fields), 'antigravity', extra=['--event', 'Stop'])
                self.assertEqual(res, {'decision': 'stop'})
                self.assertTrue(edited.exists())
                self.assertFalse(done.exists())
                self.assertEqual(len(list((self.state / 'hook-queue').glob('*.json'))), queued_before)

        # Verify no background sync worker is spawned on an abnormal Stop even when spawning is enabled.
        spawn_flag = self.root / 'spawned.flag'
        guard = self.root / 'guard_no_spawn_hook.py'
        guard.write_text(
            f'import pathlib, runpy, subprocess, sys\n'
            f'sys.path.insert(0, {str(SCRIPTS)!r})\n'
            f'def record_spawn(*args, **kwargs):\n'
            f'    pathlib.Path({str(spawn_flag)!r}).write_text("spawned", encoding="utf-8")\n'
            f'    raise RuntimeError("unexpected worker spawn")\n'
            f'subprocess.Popen = record_spawn\n'
            f'sys.argv[0] = {str(HOOK)!r}\n'
            f'runpy.run_path({str(HOOK)!r}, run_name="__main__")\n',
            encoding='utf-8',
        )
        saved_no_spawn = self.env.pop('BEYIN_V3_NO_SPAWN', None)
        try:
            res = self.invoke(dict(base_stop, terminationReason='USER_CANCELED', error=''),
                              'antigravity', script=guard, extra=['--event', 'Stop'])
            self.assertEqual(res, {'decision': 'stop'})
            self.assertFalse(spawn_flag.exists())
        finally:
            if saved_no_spawn is not None:
                self.env['BEYIN_V3_NO_SPAWN'] = saved_no_spawn

        # Next normal NO_TOOL_CALL turn still sees preserved .edited and asks for a receipt.
        normal = self.invoke(dict(base_stop, terminationReason='NO_TOOL_CALL', error=''),
                             'antigravity', extra=['--event', 'Stop'])
        self.assertEqual(normal.get('decision'), 'continue')
        self.assertIn('Receipt session=', normal.get('reason', ''))

        # Legacy clients that omit terminationReason and error keep normal reminder behaviour.
        legacy_session = 'agy-legacy-stop'
        legacy_edited = reminders / (hashlib.sha256(legacy_session.encode()).hexdigest() + '.edited')
        self.assertEqual(self.agy_tool(legacy_session, 'write_file', {'Path': note, 'Content': 'y'}), {})
        self.assertTrue(legacy_edited.exists())
        legacy_stop = {'hookEventName': 'Stop', 'conversationId': legacy_session, 'fullyIdle': True,
                       'continuationActive': False, 'lastUserInput': 'edit the note'}
        legacy_res = self.invoke(legacy_stop, 'antigravity', extra=['--event', 'Stop'])
        self.assertEqual(legacy_res.get('decision'), 'continue')
        self.assertIn('Receipt session=', legacy_res.get('reason', ''))

        # Direct helper coverage across reasons, errors, legacy omissions and non-string values.
        abnormal = self.hook.antigravity_abnormal_stop
        for reason in abnormal_reasons + ('UNKNOWN_TERMINAL_REASON',):
            with self.subTest(helper_reason=reason):
                self.assertTrue(abnormal({'terminationReason': reason, 'error': ''}))
        for payload in (
            {'terminationReason': 'NO_TOOL_CALL', 'error': 'rate limit exceeded'},
            {'error': 'upstream failure'},
            {'terminationReason': None, 'error': 'timeout'},
            {'terminationReason': '', 'error': 'cancelled'},
        ):
            with self.subTest(helper_error=payload):
                self.assertTrue(abnormal(payload))
        for payload in (
            {'terminationReason': 'NO_TOOL_CALL', 'error': ''},
            {'terminationReason': ' NO_TOOL_CALL ', 'error': '   '},
            {'terminationReason': 'NO_TOOL_CALL', 'error': None},
            {'terminationReason': 'NO_TOOL_CALL'},
            {},
            {'terminationReason': None, 'error': None},
            {'terminationReason': '', 'error': ''},
            {'terminationReason': '   ', 'error': '   '},
            {'terminationReason': 123, 'error': True},
            {'terminationReason': False, 'error': ['err']},
            # The enum's zero value records no reason; like an absent field it keeps normal Stop.
            {'terminationReason': 'UNSPECIFIED', 'error': ''},
        ):
            with self.subTest(helper_normal=payload):
                self.assertFalse(abnormal(payload))
        # Names the 1.5.18 binary defines but its hook document does not list are not a normal end.
        for reason in ('CHAINED', 'EARLY_CONTINUE', 'HALTED_STEP'):
            with self.subTest(undocumented=reason):
                self.assertTrue(abnormal({'terminationReason': reason, 'error': ''}))
        self.assertTrue(abnormal({'terminationReason': 'UNSPECIFIED', 'error': 'model failure'}))

    def test_antigravity_stop_contract_docs_name_every_input_the_stop_decision_reads(self):
        # docs/v3/PLATFORM-TESTS.md and GLOBAL-BRIDGE.md are the Antigravity Stop contract. #937 made
        # Stop read terminationReason and error without a word in them, so the docs said "continue"
        # where the code said stop. Invariant: every payload field the Stop decision reads (recorded
        # from the helpers it calls, so a new field cannot slip past), every terminationReason value
        # it treats as a normal end and the bounded transcript read's sizes are named in the
        # paragraph that holds the Stop contract.
        class Recorder(dict):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.read = set()

            def get(self, key, default=None):
                self.read.add(key)
                return super().get(key, default)

            def __getitem__(self, key):
                self.read.add(key)
                return super().__getitem__(key)

            def __contains__(self, key):
                self.read.add(key)
                return super().__contains__(key)

        # A fully idle Stop without user input reaches every check, the transcript read included.
        payload = Recorder(fullyIdle=True, lastUserInput='', initialNumSteps=1,
                           transcriptPath=str(self.root / 'missing.jsonl'))
        self.hook.antigravity_abnormal_stop(payload)
        self.hook.antigravity_continued_stop(payload)
        self.hook._antigravity_nonhuman_turn(payload, 'Stop')
        fields = payload.read | {'fullyIdle'}  # main() reads fullyIdle inline
        self.assertLessEqual({'error', 'terminationReason', 'executionNum', 'continuationActive',
                              'initialNumSteps', 'lastUserInput', 'transcriptPath'}, fields)
        # A field may be written `name` or `name: value`.
        names = [r'`%s[`:]' % re.escape(field) for field in sorted(fields)]
        names += [r'`%s`' % re.escape(value) for value in self.hook.ANTIGRAVITY_NORMAL_TERMINATIONS]
        names += [r'\b%d MB\b' % (self.hook.ANTIGRAVITY_TRANSCRIPT_LIMIT // (1024 * 1024)),
                  r'\b%d KB\b' % (self.hook.ANTIGRAVITY_TRANSCRIPT_HEAD // 1024),
                  re.escape('{"decision":"continue"}'), re.escape('{"decision":"stop"}')]
        for doc in ('PLATFORM-TESTS.md', 'GLOBAL-BRIDGE.md'):
            text = (ROOT / 'docs' / 'v3' / doc).read_text(encoding='utf-8')
            blocks = [block for block in re.split(r'\n\s*\n|\n(?=- )', text) if '`fullyIdle' in block]
            self.assertEqual(len(blocks), 1, doc)
            for name in names:
                with self.subTest(doc=doc, name=name):
                    self.assertRegex(blocks[0], name)

    def test_antigravity_tool_input_maps_only_successful_file_writes(self):
        for call, error, expected in (
                ({'name': 'write_file', 'args': {'Path': '/v/a.md', 'Content': 'x'}}, '', {'file_path': '/v/a.md'}),
                ({'name': 'edit_file', 'args': {'Path': '/v/a.md'}}, None, {'file_path': '/v/a.md'}),
                ({'name': 'code_action', 'args': {'TargetFile': '/v/b.md'}}, '', {'file_path': '/v/b.md'}),
                ({'name': 'code_action', 'args': {'AbsolutePath': '/v/c.md'}}, '', {'file_path': '/v/c.md'}),
                # The IDE agent's file tools, named in the Antigravity 1.5.18 binary with TargetFile.
                ({'name': 'write_to_file', 'args': {'TargetFile': '/v/d.md', 'CodeContent': 'x'}}, '', {'file_path': '/v/d.md'}),
                ({'name': 'replace_file_content', 'args': {'TargetFile': '/v/e.md'}}, '', {'file_path': '/v/e.md'}),
                ({'name': 'multi_replace_file_content', 'args': {'TargetFile': '/v/f.md'}}, '', {'file_path': '/v/f.md'}),
                ({'name': 'replace_file_content', 'args': {'TargetFile': '/v/e.md'}}, 'target content not found', None),
                ({'name': 'view_file', 'args': {'AbsolutePath': '/v/a.md'}}, '', None),
                ({'name': 'list_dir', 'args': {'DirectoryPath': '/v'}}, '', None),
                ({'name': 'write_file', 'args': {}}, '', {}),
                ({'name': 'write_file', 'args': None}, '', {}),
                ({'name': 'write_file', 'args': {'Path': '/v/a.md'}}, 'exit status 1', None),
                ({'name': 'read_file', 'args': {'Path': '/v/a.md'}}, '', None),
                ({'name': 'run_command', 'args': {'CommandLine': 'ls'}}, '', None),
                ('write_file', '', None), (None, '', None)):
            with self.subTest(call=call, error=error):
                payload = {'toolCall': call} if error is None else {'toolCall': call, 'error': error}
                self.assertEqual(self.hook.antigravity_tool_input(payload), expected)

    def test_antigravity_filters_nested_turns_before_checkpointing(self):
        self.seed()
        transcript = (self.vault / 'antigravity.jsonl').resolve()
        def transcript_steps(*types):
            transcript.write_text(''.join(json.dumps({'step_index': index, 'type': kind}) + '\n'
                                          for index, kind in enumerate(types)), encoding='utf-8')
        payload = {'hook_event_name': 'PreInvocation', 'conversationId': 'antigravity-report',
                   'invocationNum': 0, 'lastUserInput': '', 'initialNumSteps': 0,
                   'transcriptPath': str(transcript)}
        queue = self.state / 'hook-queue'
        self.assertEqual(self.invoke(payload, 'antigravity'), {})
        self.assertEqual(list(queue.glob('*.json')), [])
        transcript_steps('PLANNER_RESPONSE')
        self.assertEqual(self.invoke(dict(payload, initialNumSteps=1), 'antigravity'), {})
        stop = dict(payload, hook_event_name='Stop', fullyIdle=True, initialNumSteps=1)
        self.assertEqual(self.invoke(stop, 'antigravity'), {'decision': 'stop'})
        self.assertEqual(list(queue.glob('*.json')), [])

        transcript_steps('USER_INPUT', 'PLANNER_RESPONSE')
        self.assertEqual(self.invoke(dict(payload, initialNumSteps=2, lastUserInput='old prompt'), 'antigravity'), {})
        self.assertEqual(list(queue.glob('*.json')), [])
        self.assertIn('injectSteps', self.invoke(dict(payload, initialNumSteps=1, lastUserInput='prompt'), 'antigravity'))
        self.assertEqual(self.invoke(dict(stop, lastUserInput='prompt'), 'antigravity'), {'decision': 'stop'})
        self.assertEqual(len(list(queue.glob('*.json'))), 2)

    def test_antigravity_unknown_transcript_and_filter_opt_out_preserve_turns(self):
        self.seed()
        transcript = (self.vault / 'antigravity.jsonl').resolve()
        transcript.write_text('{"step_index":0,"type":"NEW_EVENT"}\n', encoding='utf-8')
        payload = {'hook_event_name': 'PreInvocation', 'conversationId': 'antigravity-unknown',
                   'invocationNum': 0, 'lastUserInput': 'prompt', 'initialNumSteps': 1,
                   'transcriptPath': str(transcript)}
        self.assertIn('injectSteps', self.invoke(payload, 'antigravity'))
        transcript.write_text('{"step_index":0,"type":"PLANNER_RESPONSE"}\n', encoding='utf-8')
        self.env['BEYIN_V3_FILTER_HARNESS_TURNS'] = '0'
        # A new conversation: the first one already had its opening (#903).
        self.assertIn('injectSteps', self.invoke(dict(payload, conversationId='antigravity-unknown-2'), 'antigravity'))

    def _write_large_antigravity_transcript(self, path, first_type, last_type, *, extra_bytes=256 * 1024, gap_at_end=False):
        target = self.hook.ANTIGRAVITY_TRANSCRIPT_LIMIT + extra_bytes
        pad = 'x' * 8192
        lines = [json.dumps({'step_index': 0, 'type': first_type, 'detail': 'synthetic-start'}) + '\n']
        size = len(lines[0].encode('utf-8'))
        step = 1
        while size < target:
            line = json.dumps({'step_index': step, 'type': 'TOOL_OUTPUT', 'detail': pad}) + '\n'
            lines.append(line)
            size += len(line.encode('utf-8'))
            step += 1
        final_index = step + 2 if gap_at_end else step
        lines.append(json.dumps({'step_index': final_index, 'type': last_type, 'detail': 'synthetic-end'}) + '\n')
        path.write_text(''.join(lines), encoding='utf-8', newline='\n')
        self.assertGreater(path.stat().st_size, self.hook.ANTIGRAVITY_TRANSCRIPT_LIMIT)
        return final_index + 1

    def test_antigravity_large_transcript_preinvocation_tail_filters_nonhuman_and_preserves_human(self):
        self.seed()
        transcript = (self.vault / 'antigravity-large.jsonl').resolve()
        queue = self.state / 'hook-queue'
        count = self._write_large_antigravity_transcript(transcript, 'USER_INPUT', 'PLANNER_RESPONSE')
        nonhuman = {'hook_event_name': 'PreInvocation', 'conversationId': 'agy-large-nonhuman',
                    'invocationNum': 0, 'lastUserInput': 'old prompt', 'initialNumSteps': count,
                    'transcriptPath': str(transcript)}
        self.assertTrue(self.hook._antigravity_nonhuman_turn(nonhuman, 'PreInvocation'))
        self.assertEqual(self.invoke(nonhuman, 'antigravity'), {})
        self.assertEqual(list(queue.glob('*.json')), [])
        # Out-of-bounds or unread middle-gap step indices fail open (preserve the turn).
        self.assertFalse(self.hook._antigravity_nonhuman_turn(dict(nonhuman, initialNumSteps=count + 1), 'PreInvocation'))
        self.assertFalse(self.hook._antigravity_nonhuman_turn(dict(nonhuman, initialNumSteps=15), 'PreInvocation'))

        # When initial - 1 is USER_INPUT in a >2 MB transcript, the human turn proceeds normally.
        count_human = self._write_large_antigravity_transcript(transcript, 'USER_INPUT', 'USER_INPUT')
        human = dict(nonhuman, conversationId='agy-large-human', lastUserInput='new prompt',
                     initialNumSteps=count_human)
        self.assertFalse(self.hook._antigravity_nonhuman_turn(human, 'PreInvocation'))
        self.assertIn('injectSteps', self.invoke(human, 'antigravity'))

        # A non-contiguous step_index in the tail fails open.
        count_corrupt = self._write_large_antigravity_transcript(
            transcript, 'USER_INPUT', 'PLANNER_RESPONSE', gap_at_end=True)
        self.assertFalse(self.hook._antigravity_nonhuman_turn(
            dict(nonhuman, initialNumSteps=count_corrupt), 'PreInvocation'))

    def test_antigravity_large_transcript_stop_filters_subagent_and_preserves_human(self):
        self.seed()
        transcript = (self.vault / 'antigravity-large-stop.jsonl').resolve()
        queue = self.state / 'hook-queue'
        count = self._write_large_antigravity_transcript(transcript, 'PLANNER_RESPONSE', 'PLANNER_RESPONSE')
        stop = {'hook_event_name': 'Stop', 'conversationId': 'agy-large-subagent',
                'fullyIdle': True, 'lastUserInput': '', 'initialNumSteps': count,
                'transcriptPath': str(transcript)}
        # Stop asks whether any step was user input; the unread middle of a long transcript could
        # hold one, so a head and tail with a gap between them fail open, as 3.18.0 did (queued).
        self.assertFalse(self.hook._antigravity_nonhuman_turn(stop, 'Stop'))
        self.assertEqual(self.invoke(stop, 'antigravity'), {'decision': 'stop'})
        self.assertEqual(len(list(queue.glob('*.json'))), 1)

        # A >2 MB transcript with USER_INPUT at step 0 (only in the head chunk) is NOT a subagent.
        count_human = self._write_large_antigravity_transcript(transcript, 'USER_INPUT', 'PLANNER_RESPONSE')
        human_stop = dict(stop, conversationId='agy-large-human-stop', initialNumSteps=count_human)
        self.assertFalse(self.hook._antigravity_nonhuman_turn(human_stop, 'Stop'))
        self.assertEqual(self.invoke(human_stop, 'antigravity'), {'decision': 'stop'})
        self.assertEqual(len(list(queue.glob('*.json'))), 2)

        # Overlapping head+tail (just over 2 MB) with CRLF endings still classifies accurately.
        count_overlap = self._write_large_antigravity_transcript(
            transcript, 'PLANNER_RESPONSE', 'PLANNER_RESPONSE', extra_bytes=1024)
        transcript.write_bytes(transcript.read_bytes().replace(b'\n', b'\r\n'))
        self.assertTrue(self.hook._antigravity_nonhuman_turn(
            dict(stop, initialNumSteps=count_overlap), 'Stop'))
        self.assertTrue(self.hook._antigravity_nonhuman_turn(
            dict(stop, initialNumSteps=count_overlap), 'PreInvocation'))

        # If step 0 itself exceeds the 64 KB head chunk, Stop fails open while PreInvocation still reads tail.
        giant_step0 = json.dumps({'step_index': 0, 'type': 'USER_INPUT', 'detail': 'u' * (70 * 1024)}) + '\n'
        pad = 'x' * 8192
        lines = [giant_step0]
        size = len(giant_step0.encode('utf-8'))
        step = 1
        while size < self.hook.ANTIGRAVITY_TRANSCRIPT_LIMIT + 128 * 1024:
            line = json.dumps({'step_index': step, 'type': 'TOOL_OUTPUT', 'detail': pad}) + '\n'
            lines.append(line)
            size += len(line.encode('utf-8'))
            step += 1
        lines.append(json.dumps({'step_index': step, 'type': 'PLANNER_RESPONSE'}) + '\n')
        transcript.write_text(''.join(lines), encoding='utf-8', newline='\n')
        self.assertFalse(self.hook._antigravity_nonhuman_turn(dict(stop, initialNumSteps=step + 1), 'Stop'))
        self.assertTrue(self.hook._antigravity_nonhuman_turn(dict(stop, initialNumSteps=step + 1), 'PreInvocation'))

    def test_antigravity_bounded_transcript_read_never_filters_more_than_a_full_read(self):
        # Invariant (#940): over the limit, the head+tail read may decide less than a full read
        # (fail open) but never more. PreInvocation reads step initialNumSteps-1 verbatim, so it
        # matches the full read exactly; Stop decides only without an unread gap. The small limits
        # sweep the tail cut over every byte of LF and CRLF lines with 2-, 3- and 4-byte UTF-8.
        hook = self.hook
        layouts = {
            'human': ['USER_INPUT'] + ['GENERIC', 'PLANNER_RESPONSE'] * 30,
            'subagent': ['SYSTEM_MESSAGE'] + ['GENERIC', 'PLANNER_RESPONSE'] * 30,
            'human-input-mid-only': (['SYSTEM_MESSAGE'] + ['GENERIC', 'PLANNER_RESPONSE'] * 12 + ['USER_INPUT']
                                     + ['GENERIC', 'PLANNER_RESPONSE'] * 17),
            'ends-with-user-input': ['USER_INPUT'] + ['GENERIC', 'PLANNER_RESPONSE'] * 30 + ['USER_INPUT'],
        }
        transcript = (self.root / 'bounded-sweep.jsonl').resolve()
        checked = decided = 0
        with patch.object(hook, 'ANTIGRAVITY_TRANSCRIPT_LIMIT', 2048), patch.object(hook, 'ANTIGRAVITY_TRANSCRIPT_HEAD', 256):
            for name, kinds in layouts.items():
                for newline in ('\n', '\r\n'):
                    for pad in range(48):
                        rows = [json.dumps({'step_index': i, 'type': kind, 'content': 'ş€😀' * (3 + i % 4)},
                                           ensure_ascii=False) for i, kind in enumerate(kinds)]
                        rows[-1] = rows[-1][:-1] + ', "pad": "' + 'ğ' * pad + '"}'
                        transcript.write_text(newline.join(rows) + newline, encoding='utf-8', newline='')
                        raw = transcript.read_bytes()
                        self.assertGreater(len(raw), 2048)
                        full = hook._parse_antigravity_steps(raw, start_at_zero=True)
                        types = [full[i] for i in range(len(full))]
                        count = len(types)
                        oracle = {'PreInvocation': types[count - 1] == 'PLANNER_RESPONSE',
                                  'Stop': 'USER_INPUT' not in types and 'PLANNER_RESPONSE' in types}
                        for event, last_input in (('PreInvocation', 'old prompt'), ('Stop', '')):
                            with self.subTest(layout=name, newline=repr(newline), pad=pad, event=event):
                                got = hook._antigravity_nonhuman_turn(
                                    {'initialNumSteps': count, 'lastUserInput': last_input,
                                     'transcriptPath': str(transcript)}, event)
                                checked += 1
                                decided += got
                                if event == 'PreInvocation':
                                    self.assertEqual(got, oracle[event])
                                elif got:
                                    self.assertTrue(oracle[event])
        self.assertGreater(decided, 0)
        self.assertEqual(checked, len(layouts) * 2 * 48 * 2)
        # The unread middle is where the one user input of 'human-input-mid-only' sits: Stop must
        # fail open there (3.19.0 review: the first bounded read called that conversation a subagent).
        with patch.object(hook, 'ANTIGRAVITY_TRANSCRIPT_LIMIT', 2048), patch.object(hook, 'ANTIGRAVITY_TRANSCRIPT_HEAD', 256):
            kinds = layouts['human-input-mid-only']
            rows = [json.dumps({'step_index': i, 'type': kind, 'content': 'x' * 40}) for i, kind in enumerate(kinds)]
            transcript.write_text('\n'.join(rows) + '\n', encoding='utf-8', newline='')
            head, tail = hook._antigravity_transcript_steps(str(transcript))
            middle = kinds.index('USER_INPUT')
            self.assertNotIn(middle, head)
            self.assertNotIn(middle, tail)
            self.assertFalse(hook._antigravity_nonhuman_turn(
                {'initialNumSteps': len(kinds), 'lastUserInput': '', 'transcriptPath': str(transcript)}, 'Stop'))

    def test_antigravity_large_transcript_security_rejects_symlinks_and_reparse_points(self):
        import stat
        real_dir = (self.root / 'real-transcripts').resolve()
        real_dir.mkdir()
        transcript = real_dir / 'subagent.jsonl'
        count = self._write_large_antigravity_transcript(transcript, 'PLANNER_RESPONSE', 'PLANNER_RESPONSE')
        payload = {'lastUserInput': '', 'initialNumSteps': count, 'transcriptPath': str(transcript)}
        # Control: the direct regular >2 MB transcript is accepted.
        self.assertTrue(self.hook._antigravity_nonhuman_turn(payload, 'PreInvocation'))

        # Direct file symlink to >2 MB transcript must fail open when OS allows symlinks.
        link = self.root / 'linked-large.jsonl'
        try:
            link.symlink_to(transcript)
        except OSError:
            pass
        else:
            self.assertFalse(self.hook._antigravity_nonhuman_turn(
                dict(payload, transcriptPath=str(link)), 'PreInvocation'))
            self.assertIsNone(self.hook._antigravity_transcript_steps(str(link)))

        # On Windows, directory junctions work without admin and must also be rejected.
        if os.name == 'nt':
            junction = self.root / 'junction-transcripts'
            created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(real_dir)],
                                     capture_output=True, text=True, timeout=15)
            if created.returncode == 0:
                self.addCleanup(lambda: os.rmdir(junction) if junction.exists() else None)
                via_junction = str(junction / transcript.name)
                self.assertFalse(self.hook._antigravity_nonhuman_turn(
                    dict(payload, transcriptPath=via_junction), 'PreInvocation'))
                self.assertIsNone(self.hook._antigravity_transcript_steps(via_junction))

        # Simulated Windows reparse point attribute on >2 MB regular file must be rejected before open.
        real_lstat = Path.lstat
        class FakeStat:
            def __init__(self, inner):
                self._inner = inner
                self.st_mode = inner.st_mode
                self.st_size = inner.st_size
                self.st_file_attributes = getattr(inner, 'st_file_attributes', 0) | stat.FILE_ATTRIBUTE_REPARSE_POINT
        with patch.object(Path, 'lstat', autospec=True,
                          side_effect=lambda p: FakeStat(real_lstat(p)) if p == transcript else real_lstat(p)), \
                patch.object(os, 'open', side_effect=AssertionError('reparse point must not be opened')):
            self.assertFalse(self.hook._antigravity_nonhuman_turn(payload, 'PreInvocation'))

        # Non-regular directory, relative path, and NUL byte fail open.
        folder = (self.root / 'folder.jsonl').resolve()
        folder.mkdir()
        for bad in (str(folder), 'relative.jsonl', str(transcript) + '\0.jsonl'):
            with self.subTest(bad=bad):
                self.assertFalse(self.hook._antigravity_nonhuman_turn(
                    dict(payload, transcriptPath=bad), 'PreInvocation'))

    def test_antigravity_fast_path_skips_heavy_imports_on_later_preinvocation_and_non_idle_stop(self):
        probe = (
            'import contextlib, io, json, sys\n'
            'sys.path.insert(0, sys.argv[1])\n'
            'import beyin_v3_hook\n'
            'raw_input = sys.stdin.read()\n'
            'sys.argv = ["beyin_v3_hook.py"] + sys.argv[2:]\n'
            'sys.stdin = io.StringIO(raw_input)\n'
            'out = io.StringIO()\n'
            'with contextlib.redirect_stdout(out):\n'
            '    beyin_v3_hook.main()\n'
            'loaded = sorted(m for m in ("beyin_v3", "beyin_v3_preferences", "beyin_v3_sync") if m in sys.modules)\n'
            'print(json.dumps({"output": out.getvalue().strip(), "loaded": loaded}))\n'
        )
        cases = [
            (['--event', 'PreInvocation'],
             {'hookEventName': 'PreInvocation', 'conversationId': 'fast-1', 'invocationNum': 3}, '{}'),
            ([],
             {'hook_event_name': 'PreInvocation', 'conversationId': 'fast-2', 'invocationNum': 2}, '{}'),
            (['--event', 'Stop'],
             {'hookEventName': 'Stop', 'conversationId': 'fast-3', 'fullyIdle': False}, '{"decision":"stop"}'),
            ([],
             {'hook_event_name': 'Stop', 'conversationId': 'fast-4', 'fullyIdle': None}, '{"decision":"stop"}'),
            (['--event', 'PreInvocation'],
             '\ufeff' + json.dumps({'hookEventName': 'PreInvocation', 'conversationId': 'fast-bom', 'invocationNum': 1}),
             '{}'),
        ]
        for extra, payload, expected in cases:
            with self.subTest(extra=extra, expected=expected):
                raw = payload if isinstance(payload, str) else json.dumps(payload)
                result = subprocess.run(
                    [sys.executable, '-c', probe, str(SCRIPTS),
                     '--vault', str(self.vault), '--state', str(self.state),
                     '--harness', 'antigravity', *extra],
                    input=raw, text=True, encoding='utf-8', capture_output=True,
                    cwd=self.vault, env=self.env, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report['output'], expected)
                self.assertEqual(report['loaded'], [])

    def test_antigravity_fast_path_preserves_malformed_json_error_and_in_vault_state_refusal(self):
        # Malformed JSON and non-dict JSON still fail open and write hook-error.json; Stop keeps
        # its quiet decision on the error path (#928).
        for event, raw_bytes, expected_error, expected_out in (
                ('PreInvocation', b'{invalid json', 'JSONDecodeError', b'{}'),
                ('Stop', b'{invalid json', 'JSONDecodeError', b'{"decision":"stop"}'),
                ('PreInvocation', b'[]', 'AttributeError', b'{}')):
            with self.subTest(event=event, expected_error=expected_error):
                error_path = self.state / 'hook-error.json'
                if error_path.exists():
                    error_path.unlink()
                result = subprocess.run(
                    [sys.executable, str(HOOK), '--vault', str(self.vault), '--state', str(self.state),
                     '--harness', 'antigravity', '--event', event],
                    input=raw_bytes, capture_output=True, cwd=self.vault, env=self.env, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(result.stdout.strip(), expected_out)
                self.assertTrue(error_path.exists())
                self.assertEqual(json.loads(error_path.read_text(encoding='utf-8'))['error'], expected_error)

        # In-vault --state still raises ValueError even on fast-path payloads without writing inside vault.
        inside = self.vault / '.state'
        for event, payload in (
                ('PreInvocation', {'hookEventName': 'PreInvocation', 'invocationNum': 3}),
                ('Stop', {'hookEventName': 'Stop', 'fullyIdle': False})):
            with self.subTest(in_vault_event=event):
                result = subprocess.run(
                    [sys.executable, str(HOOK), '--vault', str(self.vault), '--state', str(inside),
                     '--harness', 'antigravity', '--event', event],
                    input=json.dumps(payload).encode('utf-8'), capture_output=True,
                    cwd=self.vault, env=self.env, timeout=20)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Runtime state must be outside vault', result.stderr.decode())
                self.assertFalse(inside.exists())

    def test_antigravity_opening_once_per_conversation_then_per_message_retrieval(self):
        # Antigravity CLI 1.5.18: SessionStart runs once per conversation (not on resume), and
        # PreInvocation's invocationNum restarts at 0 for every user message (#903).
        self.seed()
        conversation = 'agy-903'
        queue = self.state / 'hook-queue'
        base = {'conversationId': conversation, 'cwd': str(self.vault), 'workspacePaths': [str(self.vault)]}
        first = 'Nebula calibration status?'
        start = self.invoke(dict(base, hookEventName='SessionStart', lastUserInput=first), 'antigravity',
                            extra=['--event', 'SessionStart'])
        opening = start['injectSteps'][0]['ephemeralMessage']
        self.assertIn('Synthetic Reviewer', opening)
        turn = dict(base, hookEventName='PreInvocation', invocationNum=0, initialNumSteps=1, lastUserInput=first)
        # The first message's PreInvocation is already answered by the start.
        self.assertEqual(self.invoke(turn, 'antigravity', extra=['--event', 'PreInvocation']), {})
        self.assertEqual(self.invoke(dict(turn, invocationNum=1, initialNumSteps=4), 'antigravity',
                                     extra=['--event', 'PreInvocation']), {})
        queued = len(list(queue.glob('*.json')))
        # A later user message gets source-backed retrieval for its own text, not the opening again.
        second = dict(turn, initialNumSteps=9, lastUserInput='Who owns Nebula calibration?')
        result = self.invoke(second, 'antigravity', extra=['--event', 'PreInvocation'])
        text = result['injectSteps'][0]['ephemeralMessage']
        self.assertIn('companion protocol', opening)
        self.assertIn('"records"', text)
        self.assertIn('Synthetic Reviewer', text)
        self.assertNotIn('companion protocol', text)
        self.assertEqual(len(list(queue.glob('*.json'))), queued + 1)
        events = [json.loads(path.read_text(encoding='utf-8'))['event'] for path in queue.glob('*.json')]
        self.assertEqual(sorted(events), ['SessionStart', 'UserPromptSubmit'])
        # An unrelated message injects nothing at all.
        self.assertEqual(self.invoke(dict(second, lastUserInput='zzqx'), 'antigravity',
                                     extra=['--event', 'PreInvocation']), {})

    def test_antigravity_opt_out_in_the_first_message_still_counts(self):
        # The opening answers the first message, so its PreInvocation is quiet; [kaydetme] still applies.
        self.seed()
        conversation = 'agy-optout'
        first = 'Bunu [kaydetme], sadece bak'
        self.invoke({'hookEventName': 'SessionStart', 'conversationId': conversation, 'lastUserInput': first},
                    'antigravity', extra=['--event', 'SessionStart'])
        turn = {'hookEventName': 'PreInvocation', 'conversationId': conversation, 'invocationNum': 0,
                'initialNumSteps': 1, 'lastUserInput': first}
        self.assertEqual(self.invoke(turn, 'antigravity', extra=['--event', 'PreInvocation']), {})
        digest = hashlib.sha256(conversation.encode()).hexdigest()
        self.assertTrue((self.state / 'receipt-reminders' / (digest + '.optout')).exists())

    def test_antigravity_without_session_start_opens_once_then_retrieves(self):
        # A client without SessionStart, or a conversation begun before the update: the first
        # human PreInvocation still opens the conversation, once.
        self.seed()
        turn = {'hookEventName': 'PreInvocation', 'conversationId': 'agy-legacy', 'invocationNum': 0,
                'initialNumSteps': 1, 'lastUserInput': 'Nebula calibration status?', 'cwd': str(self.vault)}
        opening = self.invoke(turn, 'antigravity', extra=['--event', 'PreInvocation'])
        self.assertIn('companion protocol', opening['injectSteps'][0]['ephemeralMessage'])
        again = self.invoke(dict(turn, initialNumSteps=5), 'antigravity', extra=['--event', 'PreInvocation'])
        self.assertIn('"records"', again['injectSteps'][0]['ephemeralMessage'])
        self.assertNotIn('companion protocol', again['injectSteps'][0]['ephemeralMessage'])

    def test_antigravity_nested_session_start_is_silent(self):
        # #885: a nested agent's conversation starts without user input; it gets no opening or checkpoint.
        self.seed()
        start = {'hookEventName': 'SessionStart', 'conversationId': 'agy-nested', 'lastUserInput': ''}
        self.assertEqual(self.invoke(start, 'antigravity', extra=['--event', 'SessionStart']), {})
        self.assertEqual(list((self.state / 'hook-queue').glob('*.json')), [])
        self.assertFalse((self.state / 'antigravity-sessions').exists())
        self.env['BEYIN_V3_FILTER_HARNESS_TURNS'] = '0'
        self.assertIn('injectSteps', self.invoke(start, 'antigravity', extra=['--event', 'SessionStart']))

    def test_antigravity_marker_only_for_a_conversation_the_hook_serves(self):
        # A skipped, internal or no-memory run answers nothing, so it leaves no conversation marker either.
        self.seed()
        markers = self.state / 'antigravity-sessions'
        start = {'hookEventName': 'SessionStart', 'conversationId': 'agy-skip', 'lastUserInput': 'Nebula calibration?'}
        turn = {'hookEventName': 'PreInvocation', 'conversationId': 'agy-skip', 'invocationNum': 0,
                'initialNumSteps': 1, 'lastUserInput': 'Nebula calibration?'}
        for variable in ('BEYIN_V3_SKIP', 'BEYIN_V3_INTERNAL'):
            with self.subTest(variable=variable):
                self.env[variable] = '1'
                self.assertEqual(self.invoke(start, 'antigravity', extra=['--event', 'SessionStart']), {})
                self.assertEqual(self.invoke(turn, 'antigravity', extra=['--event', 'PreInvocation']), {})
                del self.env[variable]
        self.assertEqual(self.invoke(dict(start, no_memory=True), 'antigravity', extra=['--event', 'SessionStart']), {})
        self.assertEqual(self.invoke(dict(turn, no_memory=True), 'antigravity', extra=['--event', 'PreInvocation']), {})
        self.assertFalse(markers.exists())
        self.assertIn('injectSteps', self.invoke(start, 'antigravity', extra=['--event', 'SessionStart']))
        self.assertEqual(len(list(markers.glob('[0-9a-f]*.json'))), 1)

    def test_antigravity_stop_keeps_stop_decision_on_skip_no_memory_and_errors(self):
        # Antigravity's Stop contract expects {"decision":"stop"} on every quiet exit,
        # matching quiet() and the global bridge (#903).
        self.seed()
        stop = {'hookEventName': 'Stop', 'conversationId': 'agy-stop-quiet', 'fullyIdle': True}
        for variable in ('BEYIN_V3_SKIP', 'BEYIN_V3_INTERNAL'):
            with self.subTest(variable=variable):
                self.env[variable] = '1'
                self.assertEqual(self.invoke(stop, 'antigravity', extra=['--event', 'Stop']), {'decision': 'stop'})
                del self.env[variable]
        self.assertEqual(self.invoke(dict(stop, no_memory=True), 'antigravity', extra=['--event', 'Stop']),
                         {'decision': 'stop'})
        # Invalid JSON on stdin (unexpected error path) still returns {"decision":"stop"} for Stop.
        bad = subprocess.run([sys.executable, str(HOOK), '--vault', str(self.vault), '--state', str(self.state),
                              '--harness', 'antigravity', '--event', 'Stop'],
                             input=b'{invalid json', capture_output=True, env=self.env, timeout=60)
        self.assertEqual(bad.returncode, 0, bad.stderr.decode(errors='replace'))
        self.assertEqual(json.loads(bad.stdout), {'decision': 'stop'})

    def test_antigravity_marker_keeps_a_keyed_digest_never_the_plain_prompt_hash(self):
        self.seed()
        prompt = 'evet'
        markers = self.state / 'antigravity-sessions'
        self.invoke({'hookEventName': 'SessionStart', 'conversationId': 'agy-hmac', 'lastUserInput': prompt},
                    'antigravity', extra=['--event', 'SessionStart'])
        marker = markers / (hashlib.sha256(b'agy-hmac').hexdigest() + '.json')
        pending = json.loads(marker.read_text(encoding='utf-8'))['pending']
        self.assertNotEqual(pending, hashlib.sha256(prompt.encode()).hexdigest())
        key = bytes.fromhex(json.loads((markers / 'key.json').read_text(encoding='utf-8'))['key'])
        self.assertEqual(len(key), 32)
        import hmac
        self.assertEqual(pending, hmac.new(key, prompt.encode(), hashlib.sha256).hexdigest())
        if os.name == 'posix':
            self.assertEqual((markers / 'key.json').stat().st_mode & 0o077, 0)
        # The keyed digest still recognises the prompt the start answered.
        turn = {'hookEventName': 'PreInvocation', 'conversationId': 'agy-hmac', 'invocationNum': 0,
                'initialNumSteps': 1, 'lastUserInput': prompt}
        self.assertEqual(self.invoke(turn, 'antigravity', extra=['--event', 'PreInvocation']), {})
        self.assertNotIn('pending', json.loads(marker.read_text(encoding='utf-8')))

    def test_antigravity_unreadable_marker_key_fails_open_to_retrieval(self):
        # Without a usable key no digest is kept; the first message then gets retrieval, never silence.
        self.seed()
        markers = self.state / 'antigravity-sessions'
        markers.mkdir(parents=True)
        (markers / 'key.json').write_text('not json', encoding='utf-8', newline='\n')
        first = 'Who owns Nebula calibration?'
        self.invoke({'hookEventName': 'SessionStart', 'conversationId': 'agy-nokey', 'lastUserInput': first},
                    'antigravity', extra=['--event', 'SessionStart'])
        marker = markers / (hashlib.sha256(b'agy-nokey').hexdigest() + '.json')
        self.assertNotIn('pending', json.loads(marker.read_text(encoding='utf-8')))
        turn = {'hookEventName': 'PreInvocation', 'conversationId': 'agy-nokey', 'invocationNum': 0,
                'initialNumSteps': 1, 'lastUserInput': first}
        result = self.invoke(turn, 'antigravity', extra=['--event', 'PreInvocation'])
        self.assertIn('"records"', result['injectSteps'][0]['ephemeralMessage'])
        self.assertEqual((markers / 'key.json').read_text(encoding='utf-8'), 'not json')

    def test_internal_recursion_guard_skips_queue(self):
        self.env['BEYIN_V3_INTERNAL'] = '1'
        self.assertEqual(self.invoke(self.payload), {})
        self.assertEqual(list((self.state / 'hook-queue').glob('*.json')), [])

    def test_public_skip_guard_skips_queue(self):
        self.env['BEYIN_V3_SKIP'] = '1'
        self.assertEqual(self.invoke(self.payload), {})
        self.assertEqual(list((self.state / 'hook-queue').glob('*.json')), [])

    def test_public_skip_guard_honours_only_exact_one(self):
        # Same convention as BEYIN_V3_NO_SPAWN: 0, false or empty must not silently disable memory.
        for count, value in enumerate(('0', 'false', ''), start=1):
            with self.subTest(value=value):
                self.env['BEYIN_V3_SKIP'] = value
                self.assertEqual(self.invoke(dict(self.payload, event_id='synthetic-skip-%d' % count)), {})
                self.assertEqual(len(list((self.state / 'hook-queue').glob('*.json'))), count)

    def test_install_repeat_preserves_unrelated_configuration_and_trust(self):
        originals = {}
        for relative, payload in [('.claude/settings.local.json', {'hooks': {'SessionStart': [{'matcher': 'synthetic-other', 'hooks': [{'type': 'command', 'command': 'synthetic-unrelated'}]}]}, 'permissions': {'allow': ['Read']}}),
                                  ('.codex/hooks.json', {'hooks': {}, 'trust': {'synthetic-trust': 'unchanged'}}),
                                  ('.agents/hooks.json', {'synthetic-other-agent': {'PreInvocation': []}})]:
            path = self.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding='utf-8')
            originals[relative] = payload
        (self.vault / 'AGENTS.md').write_text('# Synthetic instructions\nKeep this user content.\n', encoding='utf-8')
        (self.vault / 'CLAUDE.md').write_text('# Claude original\n', encoding='utf-8')
        self.install()
        paths = [self.vault / name for name in originals] + [self.vault / 'AGENTS.md', self.vault / 'CLAUDE.md']
        first = [p.read_bytes() for p in paths]
        self.install()
        self.assertEqual([p.read_bytes() for p in paths], first)
        claude = json.loads(paths[0].read_text(encoding='utf-8'))
        self.assertEqual(claude['permissions'], originals['.claude/settings.local.json']['permissions'])
        self.assertIn(originals['.claude/settings.local.json']['hooks']['SessionStart'][0], claude['hooks']['SessionStart'])
        self.assertEqual(json.loads(paths[1].read_text(encoding='utf-8'))['trust'], {'synthetic-trust': 'unchanged'})
        self.assertIn('synthetic-other-agent', json.loads(paths[2].read_text(encoding='utf-8')))
        self.assertIn('Keep this user content.', (self.vault / 'AGENTS.md').read_text(encoding='utf-8'))
        agent_config = json.loads(paths[2].read_text(encoding='utf-8'))
        def commands(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == 'command' and isinstance(item, str):
                        yield decoded_command(item)
                    else:
                        yield from commands(item)
            elif isinstance(value, list):
                for item in value:
                    yield from commands(item)
        self.assertTrue(any('beyin_v3_hook.py' in command for command in commands(agent_config)))

    def test_repeat_install_preserves_literal_backslashes_in_instruction_paths(self):
        self.vault = self.root / r'Vault\hooks'
        self.vault.mkdir(parents=True)
        self.install()
        first = (self.vault / 'AGENTS.md').read_bytes()
        self.install()
        self.assertEqual((self.vault / 'AGENTS.md').read_bytes(), first)

    def stale_hook_commands(self):
        spec = importlib.util.spec_from_file_location('v3_hook_test_installer', INSTALLER)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        old = self.root / 'old account' / 'Beyin'
        return installer.commands([str(old / 'python.exe'), str(old / '.claude/scripts/beyin_v3_hook.py'),
                                   '--vault', str(old), '--state', str(self.root / 'old account' / 'state'),
                                   '--harness', 'claude'])

    def doctor_report(self):
        doctor = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'doctor'], capture_output=True, text=True,
                                encoding='utf-8', cwd=self.vault, env=self.env, timeout=30)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        return json.loads(doctor.stdout)

    def test_install_replaces_synced_encoded_hooks_from_another_install(self):
        # #204: hook files synced from another account carry its Windows EncodedCommand entries.
        # This state has no record of them and the encoded text never names beyin_v3_hook.py.
        _, encoded = self.stale_hook_commands()
        (self.vault / '.claude').mkdir()
        user_hook = {'type': 'command', 'command': 'echo user-owned'}
        (self.vault / '.claude/settings.local.json').write_text(json.dumps({'hooks': {'SessionStart': [
            {'hooks': [{'type': 'command', 'command': encoded, 'timeout': 20}]}, {'hooks': [user_hook]}]}}),
            encoding='utf-8')
        self.install()
        groups = json.loads((self.vault / '.claude/settings.local.json').read_text(encoding='utf-8'))['hooks']['SessionStart']
        commands = [hook['command'] for group in groups for hook in group['hooks']]
        self.assertNotIn(encoded, commands)
        self.assertIn(user_hook['command'], commands)
        self.assertEqual(len([c for c in commands if 'beyin_v3_hook.py' in decoded_command(c)]), 1)

    def test_reinstall_over_synced_encoded_hook_applies_and_drops_it(self):
        # The same file arriving after this machine's install: reinstall and update must not stop
        # with a managed-file conflict over it (they did not before #204 either).
        self.install()
        _, encoded = self.stale_hook_commands()
        name = self.vault / '.claude/settings.local.json'
        data = json.loads(name.read_text(encoding='utf-8'))
        data['hooks']['SessionStart'].append({'hooks': [{'type': 'command', 'command': encoded, 'timeout': 20}]})
        name.write_text(json.dumps(data), encoding='utf-8')
        self.install()
        groups = json.loads(name.read_text(encoding='utf-8'))['hooks']['SessionStart']
        commands = [hook['command'] for group in groups for hook in group['hooks']]
        self.assertNotIn(encoded, commands)
        self.assertEqual(len([c for c in commands if 'beyin_v3_hook.py' in decoded_command(c)]), 1)

    def test_doctor_reports_hook_commands_from_another_install(self):
        self.install()
        report = self.doctor_report()
        self.assertEqual(report['hook_paths']['status'], 'ok')
        self.assertGreater(report['hook_paths']['checked'], 0)
        name = self.vault / '.claude/settings.local.json'
        data = json.loads(name.read_text(encoding='utf-8'))
        for command in self.stale_hook_commands():
            data['hooks']['SessionStart'].append({'hooks': [{'type': 'command', 'command': command, 'timeout': 20}]})
        name.write_text(json.dumps(data), encoding='utf-8')
        report = self.doctor_report()
        self.assertEqual(report['status'], 'needs_attention')
        paths = report['hook_paths']
        self.assertEqual(paths['status'], 'stale')
        old = str(self.root / 'old account' / 'Beyin')
        reasons = {(item['reason'], item['path']) for item in paths['stale']}
        self.assertIn(('other_vault', old), reasons)
        self.assertIn(('hook_script_missing', str(Path(old) / '.claude/scripts/beyin_v3_hook.py')), reasons)
        self.assertIn('--state', paths['hint'])

    def test_installed_command_runs_with_spaces_and_unicode(self):
        self.install()
        self.seed()
        config = json.loads((self.vault / '.codex/hooks.json').read_text())
        candidates = [hook for matcher in config['hooks']['SessionStart'] for hook in matcher.get('hooks', [])
                      if 'beyin_v3_hook.py' in (decoded_command(hook.get('command', '')) + ' '.join(hook.get('args', [])))]
        self.assertEqual(len(candidates), 1)
        hook = candidates[0]
        command = [hook['command']] + hook['args'] if hook.get('args') else hook['command']
        payload = json.dumps(dict(self.payload, hook_event_name='SessionStart', prompt='Nebula calibration'))
        if os.name == 'nt':
            # System32\bash.exe is the WSL launcher; Claude Code uses Git for Windows' bash (#204).
            git_bash = [p for p in (r'C:\Program Files\Git\bin\bash.exe', r'C:\Program Files\Git\usr\bin\bash.exe')
                        if os.path.exists(p)]
            bash = git_bash[0] if git_bash else shutil.which('bash', path=os.environ.get('PATH'))
            self.assertIsNotNone(bash, 'Native Claude Code on Windows requires Git Bash')
            launcher = str(command).split(' -NoProfile ')[0]
            probe = subprocess.run([bash, '-lc', launcher + " -NoProfile -NonInteractive -Command 'exit 0'"],
                                   text=True, encoding='utf-8', capture_output=True,
                                   cwd=self.vault, env=self.env, timeout=20)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            # The whole encoded command, not only its launcher, must survive Git Bash (#204), as a plain
            # and as a login shell. Without PATHEXT Windows PowerShell does not run python.exe as a
            # console program: the command returns exit 0 with no output and leaves a process behind
            # (measured on the runners). cmd.exe supplies PATHEXT when it is missing, Git Bash passes the
            # environment on as it is, so this cleared environment has to carry it like a real session does.
            bash_env = dict(self.env, PATHEXT=os.environ.get('PATHEXT') or '.COM;.EXE;.BAT;.CMD')
            for flag in ('-c', '-lc'):
                through_bash = subprocess.run([bash, flag, str(command)], input=payload, text=True, encoding='utf-8',
                                              capture_output=True, cwd=self.vault, env=bash_env, timeout=30)
                self.assertEqual(through_bash.returncode, 0, (flag, through_bash.stderr))
                self.assertIn('Synthetic Reviewer',
                              json.loads(through_bash.stdout)['hookSpecificOutput']['additionalContext'], flag)
            result = subprocess.run(command, shell=True, input=payload, text=True, encoding='utf-8',
                                    capture_output=True, cwd=self.vault, env=self.env, timeout=20)
        else:
            result = subprocess.run(command, shell=isinstance(command, str), input=payload,
                                    text=True, encoding='utf-8', capture_output=True, cwd=self.vault, env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Synthetic Reviewer', json.loads(result.stdout)['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('pwsh', str(command).lower())
        if os.name == 'nt':
            launcher = str(command).lower().split(' -noprofile ')[0].strip(chr(34))
            self.assertTrue(launcher.endswith('/windowspowershell/v1.0/powershell.exe'))
            self.assertNotIn('\\', launcher)
            self.assertIn('beyin_v3_hook.py', decoded_command(command))
            self.assertEqual(hook['commandWindows'], command)
        else:
            self.assertNotIn('powershell', str(command).lower())

    def test_session_start_pins_companion_continuity_sources(self):
        companion = self.vault / '🔮 850-Companion'
        companion.mkdir()
        for name, body in {
            'Last-Session.md': 'Previous verified outcome.',
            'Threads.md': 'Active owner Synthetic Reviewer.',
            'Kurallar.md': 'Always verify current source.',
            'Journal.md': ('older\n' * 600) + 'LATEST_JOURNAL_SENTINEL',
        }.items():
            (companion / name).write_text(body, encoding='utf-8')
        from beyin_v3_sync import SyncEngine
        SyncEngine(self.vault, self.state).sync()
        payload = {'hook_event_name': 'SessionStart', 'session_id': 'synthetic-start', 'event_id': 'start-pins'}
        text = self.invoke(payload)['hookSpecificOutput']['additionalContext']
        for name in ('Last-Session.md', 'Threads.md', 'Kurallar.md', 'Journal.md'):
            self.assertIn(name, text)
        self.assertIn('LATEST_JOURNAL_SENTINEL', text)

    def test_cli_unicode_json_with_non_utf8_redirected_environment(self):
        unicode_state = self.root / 'runtime-ölçüm-🔭'
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(unicode_state), 'init'], capture_output=True,
                                cwd=self.vault, env=dict(self.env, PYTHONIOENCODING='cp1252'), timeout=20)
        self.assertEqual(result.returncode, 0, 'CLI must emit lossless JSON even with non-UTF8 redirected environment')
        payload = json.loads(result.stdout.decode('utf-8'))
        self.assertTrue(payload['initialized'])
        self.assertEqual(payload['state'], str(unicode_state.resolve()))

    def test_installer_preserves_unicode_json_under_non_utf8_default(self):
        path = self.vault / '.codex/hooks.json'
        path.parent.mkdir(parents=True)
        expected = 'Synthetic ölçüm 🔭 setting'
        path.write_text(json.dumps({'hooks': {}, 'unrelated': expected}, ensure_ascii=False), encoding='utf-8')
        spec = importlib.util.spec_from_file_location('installer_encoding_test_subject', INSTALLER)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        original_read = Path.read_text
        def legacy_default(file, encoding=None, errors=None, **kwargs):
            return original_read(file, encoding=encoding or 'cp1252', errors=errors, **kwargs)
        with patch.object(Path, 'read_text', legacy_default):
            installer.install(self.vault, self.state)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['unrelated'], expected)

    def test_cli_reads_utf8_json_stdin_under_non_utf8_environment(self):
        self.seed()
        summary = 'Synthetic ölçüm 🔭 receipt'
        payload = {'event_id': 'unicode-stdin', 'summary': summary, 'refs': ['notes/task.md']}
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'receipt'], input=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                capture_output=True, cwd=self.vault, env=dict(self.env, PYTHONIOENCODING='cp1252'), timeout=20)
        self.assertEqual(result.returncode, 0)
        receipt = json.loads(result.stdout.decode('utf-8'))
        self.assertIn(summary, (self.vault / receipt['source']).read_text(encoding='utf-8'))

    def test_cli_reads_utf8_bom_json_stdin(self):
        self.seed()
        summary = 'BOM stdin receipt'
        payload = {'event_id': 'bom-stdin', 'summary': summary, 'refs': ['notes/task.md']}
        bom_data = b'\xef\xbb\xbf' + json.dumps(payload, ensure_ascii=False).encode('utf-8')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'receipt'], input=bom_data,
                                capture_output=True, cwd=self.vault, env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout.decode('utf-8'))
        self.assertIn(summary, (self.vault / receipt['source']).read_text(encoding='utf-8'))

    def test_uninstall_restores_original_files(self):
        for name in ('AGENTS.md', 'CLAUDE.md'):
            (self.vault / name).write_text('Original synthetic ' + name + '\n', encoding='utf-8')
        originals = {name: (self.vault / name).read_bytes() for name in ('AGENTS.md', 'CLAUDE.md')}
        self.install()
        self.install(uninstall=True)
        for name, raw in originals.items():
            self.assertEqual((self.vault / name).read_bytes(), raw)

    # --- Opt-in hygiene (#130): defaults silent, preferences gate, harness gate ---

    def enable(self, **hygiene):
        # Machine-local opt-in (maintainer change): state/hygiene.json, never .beyin-preferences.json.
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'hygiene.json').write_text(json.dumps(dict(schema=1, **hygiene)), encoding='utf-8')

    def test_hygiene_defaults_are_completely_silent(self):
        self.seed()
        big = self.vault / 'notes/uzun.md'
        big.write_text('kelime ' * 600, encoding='utf-8')
        payload = {'hook_event_name': 'PostToolUse', 'session_id': 'hygiene-default',
                   'tool_input': {'file_path': str(big)}}
        self.assertEqual(self.invoke(payload, 'claude'), {})
        start = self.invoke(dict(self.payload, hook_event_name='SessionStart'), 'claude')
        self.assertNotIn('Soru sirasi', json.dumps(start))
        self.assertFalse((self.state / 'soruldu').exists())
        self.assertFalse((self.state / 'touch-log.tsv').exists())

    def test_word_cap_opt_in_warns_claude_and_codex_not_antigravity(self):
        self.seed()
        big = self.vault / 'notes/uzun.md'
        big.write_text('kelime ' * 600, encoding='utf-8')
        self.enable(word_cap_warning=True)
        payload = {'hook_event_name': 'PostToolUse', 'session_id': 'hygiene-cap',
                   'tool_input': {'file_path': str(big)}}
        for harness in ('claude', 'codex'):
            response = self.invoke(dict(payload, session_id='hygiene-cap-' + harness), harness)
            self.assertIn('Bolum SINYALI', response['hookSpecificOutput']['additionalContext'])
        antigravity = self.invoke(dict(payload, session_id='hygiene-cap-antigravity',
                                       toolCall={'name': 'write_file', 'args': {'Path': str(big)}}), 'antigravity')
        self.assertEqual(antigravity, {})  # PostToolUse takes {} in Antigravity's contract

    def test_custom_max_words_is_honoured(self):
        self.seed()
        note = self.vault / 'notes/kirpik.md'
        note.write_text('kelime ' * 60, encoding='utf-8')
        self.enable(word_cap_warning=True, max_words=50)
        payload = {'hook_event_name': 'PostToolUse', 'session_id': 'hygiene-cap-custom',
                   'tool_input': {'file_path': str(note)}}
        response = self.invoke(payload, 'claude')
        self.assertIn('10 kelime tavan uzerinde', response['hookSpecificOutput']['additionalContext'])

    def test_folder_questions_opt_in_asks_once_per_cooldown(self):
        self.seed()
        (self.vault / 'Beden').mkdir()
        then = time.time() - 60 * 86400
        os.utime(self.vault / 'Beden', (then, then))
        self.enable(folder_questions=True)
        first = self.invoke(dict(self.payload, hook_event_name='SessionStart'), 'claude')
        context = first['hookSpecificOutput']['additionalContext']
        self.assertIn('Soru sirasi', context)
        self.assertIn('Beden', context)
        stamps = list((self.state / 'soruldu').glob('*.stamp'))
        self.assertEqual([stamp.stem for stamp in stamps], ['Beden'])
        second = self.invoke(dict(self.payload, hook_event_name='SessionStart'), 'claude')
        self.assertNotIn('Soru sirasi', json.dumps(second))

    def test_promotion_opt_in_touch_log_records_once(self):
        self.seed()
        self.enable(promotion=True)
        payload = {'hook_event_name': 'PostToolUse', 'session_id': 'hygiene-promo',
                   'tool_input': {'file_path': str(self.vault / 'notes/task.md')}}
        self.invoke(payload, 'claude')
        self.assertTrue((self.state / 'touch-log.tsv').exists())
        self.assertIn('notes/task.md', (self.state / 'touch-log.tsv').read_text(encoding='utf-8'))

    def test_word_cap_follows_the_opt_in_under_every_profile(self):
        # Maintainer regression: the warning lived after the context_mode early return,
        # so the economical and manual profiles never delivered it.
        import beyin_v3_preferences as preferences
        self.seed()
        big = self.vault / 'notes/uzun.md'
        big.write_text('kelime ' * 600, encoding='utf-8')
        self.enable(word_cap_warning=True)
        for profile in ('economical', 'manual'):
            preferences.save(self.vault, {}, profile)
            payload = {'hook_event_name': 'PostToolUse', 'session_id': 'hygiene-' + profile,
                       'tool_input': {'file_path': str(big)}}
            response = self.invoke(payload, 'claude')
            self.assertIn('Bolum SINYALI', response['hookSpecificOutput']['additionalContext'], profile)
        self.assertEqual(self.invoke(dict(payload, session_id='bridge'), 'claude', extra=['--metadata-only']), {})

    def test_codex_apply_patch_payload_is_measured_and_logged(self):
        # Codex reports apply_patch as tool_input.command; there is no file_path in the payload.
        self.seed()
        (self.vault / 'Makaleler').mkdir()
        big = self.vault / 'Makaleler/uzun.md'
        big.write_text('kelime ' * 600, encoding='utf-8')
        self.enable(word_cap_warning=True, promotion=True)
        patch = '*** Begin Patch\n*** Update File: Makaleler/uzun.md\n@@\n-kelime\n+kelime\n*** End Patch\n'
        payload = {'hook_event_name': 'PostToolUse', 'session_id': 'codex-patch', 'cwd': str(self.vault),
                   'tool_name': 'apply_patch', 'tool_input': {'command': patch}}
        response = self.invoke(payload, 'codex')
        self.assertIn('Makaleler/uzun.md', response['hookSpecificOutput']['additionalContext'])
        self.assertIn('Makaleler/uzun.md', (self.state / 'touch-log.tsv').read_text(encoding='utf-8'))

    def test_hook_resolves_active_project_from_json_and_junctions(self):
        self.seed()
        # Tests basic json mapping and ensures fallback doesn't crash on deleted dirs
        (self.vault / '.beyin-projects.json').write_text(json.dumps({
            "folders": {"custom/path": "mapped-project"}
        }))
        cwd = self.vault / 'custom' / 'path' / 'inside'
        cwd.mkdir(parents=True)
        # Should resolve and respond correctly
        self.invoke(dict(self.payload, cwd=str(cwd)))
        queued = list((self.state / 'hook-queue').glob('*.json'))
        self.assertTrue(queued)
        event_data = json.loads(queued[-1].read_text(encoding='utf-8'))
        self.assertEqual(event_data.get('project'), 'mapped-project')

        deleted_cwd = self.vault / 'projects' / 'deleted-proj'
        deleted_cwd.mkdir(parents=True)
        cwd_path = str(deleted_cwd)
        import shutil
        shutil.rmtree(cwd_path)
        # Should fallback gracefully without crashing
        self.invoke(dict(self.payload, cwd=cwd_path))

    def test_is_synthetic_prompt_detection(self):
        # Prefix heuristics
        for prefix in (
            '<task-notification> subagent finished',
            '   <task-notification> with leading whitespace',
            'Another Claude session sent a message: ping',
            '<agent-message id="1">result</agent-message>',
            '<local-command-caveat> warning',
            '<command-name>git status</command-name>',
            '<local-command-stdout> command output',
        ):
            with self.subTest(prefix=prefix):
                self.assertTrue(self.hook.is_synthetic_prompt({'prompt': prefix}))

        # Origin metadata checks
        self.assertTrue(self.hook.is_synthetic_prompt({'prompt': 'normal work', 'origin': {'kind': 'task-notification'}}))
        self.assertTrue(self.hook.is_synthetic_prompt({'prompt': 'normal work', 'origin': {'kind': 'peer'}}))
        self.assertTrue(self.hook.is_synthetic_prompt({'prompt': 'normal work', 'origin': 'task-notification'}))
        self.assertFalse(self.hook.is_synthetic_prompt({'prompt': 'normal work', 'origin': {'kind': 'human'}}))
        self.assertFalse(self.hook.is_synthetic_prompt({'prompt': 'normal work', 'origin': 'human'}))

        # Human & edge case inputs
        self.assertFalse(self.hook.is_synthetic_prompt({'prompt': 'Lütfen kodları incele'}))
        self.assertFalse(self.hook.is_synthetic_prompt({'prompt': ''}))
        self.assertFalse(self.hook.is_synthetic_prompt({}))
        self.assertFalse(self.hook.is_synthetic_prompt(None))

        # Opt-out environment variable
        with patch.dict(os.environ, {'BEYIN_V3_FILTER_HARNESS_TURNS': '0'}):
            self.assertFalse(self.hook.is_synthetic_prompt({'prompt': '<task-notification> test'}))

    def test_user_prompt_submit_skips_retrieval_on_synthetic_prompts(self):
        self.seed()
        before = len(list((self.state / 'hook-queue').glob('*.json')))

        # 1. Synthetic prefix returns {} immediately, but queues metadata
        resp_prefix = self.lifecycle('UserPromptSubmit', 'synth-sess-1', 'claude',
                                     prompt='<task-notification> subagent done')
        self.assertEqual(resp_prefix, {})
        queue_files = list((self.state / 'hook-queue').glob('*.json'))
        self.assertEqual(len(queue_files), before + 1)

        # 2. Synthetic origin returns {} even with keywords matching seeded note
        resp_origin = self.lifecycle('UserPromptSubmit', 'synth-sess-2', 'claude',
                                     prompt='Nebula calibration',
                                     origin={'kind': 'task-notification'})
        self.assertEqual(resp_origin, {})
        self.assertEqual(len(list((self.state / 'hook-queue').glob('*.json'))), before + 2)

        # 3. Genuine human query delivers search context
        resp_human = self.lifecycle('UserPromptSubmit', 'human-sess', 'claude',
                                    prompt='Nebula calibration')
        self.assertIn('hookSpecificOutput', resp_human)
        self.assertIn('Synthetic Reviewer', resp_human['hookSpecificOutput']['additionalContext'])
        self.assertEqual(len(list((self.state / 'hook-queue').glob('*.json'))), before + 3)

    def slow_sync_hook(self, exclusive=False):
        """The real hook with a source sync that outlasts SYNC_WAIT and a due check.

        exclusive: the sync outgrows the page cache during the wait and holds the index's
        exclusive lock until the hook process exits, as a large rewrite does before commit.
        """
        wrapper = self.root / 'slow_sync_hook.py'
        wrapper.write_text(f'''import runpy, sqlite3, subprocess, sys
sys.path.insert(0, {str(SCRIPTS)!r})
HELD = []
class SlowWorker:
    returncode = None
    def __init__(self, *args, **kwargs):
        pass
    def wait(self, timeout=None):
        if {exclusive!r}:
            db = sqlite3.connect({str(self.state / 'memory.sqlite3')!r}, isolation_level=None)
            db.execute('BEGIN EXCLUSIVE')
            HELD.append(db)
        raise subprocess.TimeoutExpired('worker', timeout)
subprocess.Popen = SlowWorker
import beyin_v3_preferences
beyin_v3_preferences.claim_check = lambda *args, **kwargs: True
sys.argv[0] = {str(HOOK)!r}
runpy.run_path({str(HOOK)!r}, run_name='__main__')
''', encoding='utf-8')
        self.env.pop('BEYIN_V3_NO_SPAWN')
        return wrapper

    def test_user_prompt_submit_reads_previous_index_while_sync_is_pending(self):
        # A sync slower than SYNC_WAIT used to inject only a pending notice, so a large vault
        # never received prompt retrieval at all. The last committed index still answers.
        self.seed()
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'slow-sync', 'event_id': 'slow-sync-1',
                   'cwd': str(self.vault), 'prompt': 'Nebula calibration owner'}
        context = self.invoke(payload, 'claude', script=self.slow_sync_hook())['hookSpecificOutput']['additionalContext']
        self.assertIn('Synthetic Reviewer', context)
        self.assertIn('previous index', context)

    def test_user_prompt_submit_pending_sync_lock_stays_inside_hook_budget(self):
        # Maintainer: the read-only store waited SQLite's default 10 s for a sync holding the
        # exclusive lock, past the client's 5 s hook limit. The bounded wait keeps the pending
        # notice and records no hook error, because the worker itself is not failing.
        self.seed()
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'locked-sync', 'event_id': 'locked-sync-1',
                   'cwd': str(self.vault), 'prompt': 'Nebula calibration owner'}
        started = time.monotonic()
        context = self.invoke(payload, 'claude', script=self.slow_sync_hook(exclusive=True))['hookSpecificOutput']['additionalContext']
        # The hook bounds its own wait to HOOK_BUDGET; the margin covers a slow runner's interpreter start.
        self.assertLess(time.monotonic() - started, 8)
        self.assertIn('V3 source sync is pending', context)
        self.assertNotIn('failed or conflicted', context)
        self.assertFalse((self.state / 'hook-error.json').exists())

    def test_user_prompt_submit_without_index_keeps_pending_notice(self):
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'first-sync', 'event_id': 'first-sync-1',
                   'cwd': str(self.vault), 'prompt': 'Nebula calibration owner'}
        context = self.invoke(payload, 'claude', script=self.slow_sync_hook())['hookSpecificOutput']['additionalContext']
        self.assertIn('V3 source sync is pending', context)

    def deferred_sync_hook(self):
        """The real hook where interval preference defers source synchronization."""
        wrapper = self.root / 'deferred_sync_hook.py'
        wrapper.write_text(f'''import runpy, sys
sys.path.insert(0, {str(SCRIPTS)!r})
import beyin_v3_preferences
beyin_v3_preferences.claim_check = lambda *args, **kwargs: False
sys.argv[0] = {str(HOOK)!r}
runpy.run_path({str(HOOK)!r}, run_name='__main__')
''', encoding='utf-8')
        self.env.pop('BEYIN_V3_NO_SPAWN', None)
        return wrapper

    def test_locked_cadence_record_still_answers_the_turn_and_names_the_skip(self):
        # Another client holds BEGIN IMMEDIATE on the cadence record. The turn keeps its context
        # and records no hook error, and the notice names the unwritable record instead of
        # reporting an interval preference the user never chose.
        self.seed()
        import beyin_v3_preferences as preferences
        preferences.save(self.vault, {'interval_minutes': 15})
        self.assertTrue(preferences.claim_check(self.state, preferences.read(self.vault), 'SessionStart'))
        self.env.pop('BEYIN_V3_NO_SPAWN', None)
        with closing(sqlite3.connect(self.state / 'check-cadence.sqlite3', timeout=0)) as holder, holder:
            holder.execute('BEGIN IMMEDIATE')
            payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'locked-cadence',
                       'event_id': 'locked-cadence-1', 'cwd': str(self.vault),
                       'prompt': 'Nebula calibration owner'}
            context = self.invoke(payload, 'claude')['hookSpecificOutput']['additionalContext']
        self.assertIn('Synthetic Reviewer', context)
        self.assertIn('cadence record could not be written', context)
        self.assertIn('doctor', context)
        self.assertNotIn('deferred by your interval preference', context)
        self.assertFalse((self.state / 'hook-error.json').exists())

    def test_user_prompt_submit_reads_previous_index_when_check_is_deferred(self):
        # Issue #398: context_mode: turn with interval_minutes > 0 used to drop context entirely.
        # When check is deferred, prompt retrieval reads the previous committed index.
        self.seed()
        import beyin_v3_preferences as preferences
        preferences.save(self.vault, {'context_mode': 'turn', 'interval_minutes': 15})
        # Exercise the real interval claim instead of forcing its result in a wrapper.
        self.assertTrue(preferences.claim_check(self.state, preferences.read(self.vault), 'UserPromptSubmit'))
        self.env.pop('BEYIN_V3_NO_SPAWN', None)
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'deferred-turn', 'event_id': 'deferred-1',
                   'cwd': str(self.vault), 'prompt': 'Nebula calibration owner'}
        context = self.invoke(payload, 'claude')['hookSpecificOutput']['additionalContext']
        self.assertIn('Synthetic Reviewer', context)
        self.assertIn('deferred by your interval preference', context)
        self.assertIn('previous index', context)
        self.assertLessEqual(len(context.encode('utf-16-le')) // 2, 10000)
        self.assertFalse((self.state / 'hook-error.json').exists())

    def test_user_prompt_submit_without_index_when_check_is_deferred_keeps_notice(self):
        import beyin_v3_preferences as preferences
        preferences.save(self.vault, {'context_mode': 'turn', 'interval_minutes': 15})
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'first-deferred', 'event_id': 'first-deferred-1',
                   'cwd': str(self.vault), 'prompt': 'Nebula calibration owner'}
        context = self.invoke(payload, 'claude', script=self.deferred_sync_hook())['hookSpecificOutput']['additionalContext']
        self.assertIn('deferred by your interval preference', context)
        self.assertNotIn('previous index', context)
        self.assertFalse((self.state / 'hook-error.json').exists())

    def test_user_prompt_submit_synthetic_filter_honours_opt_out_env(self):
        self.seed()
        self.env['BEYIN_V3_FILTER_HARNESS_TURNS'] = '0'
        resp = self.lifecycle('UserPromptSubmit', 'optout-sess', 'claude',
                              prompt='<task-notification> Nebula calibration')
        self.assertIn('hookSpecificOutput', resp)
        self.assertIn('Synthetic Reviewer', resp['hookSpecificOutput']['additionalContext'])

    def test_unknown_origin_kinds_fall_back_to_the_prompt_prefix(self):
        # Claude Code also tags turns a person typed elsewhere (channel, bridge, remote); only
        # the known non-human kinds skip retrieval, anything else is judged by the prompt text.
        for kind in ('channel', 'bridge', 'remote', 'cli', ''):
            with self.subTest(kind=kind):
                self.assertFalse(self.hook.is_synthetic_prompt({'prompt': 'Nebula calibration', 'origin': {'kind': kind}}))
                self.assertFalse(self.hook.is_synthetic_prompt({'prompt': 'Nebula calibration', 'origin': kind}))
                self.assertTrue(self.hook.is_synthetic_prompt({'prompt': '<task-notification>\n<task-id>x</task-id>',
                                                               'origin': {'kind': kind}}))
        self.assertTrue(self.hook.is_synthetic_prompt({'prompt': 'plain text', 'origin': {'kind': 'coordinator'}}))
        self.assertFalse(self.hook.is_synthetic_prompt({'prompt': '<task-notification>', 'origin': {'kind': 'human'}}))
        self.assertFalse(self.hook.is_synthetic_prompt({'prompt': 'Nebula', 'origin': {'kind': ['peer']}}))
        # Content-block prompts (#156) are judged by their text like a plain string.
        self.assertTrue(self.hook.is_synthetic_prompt({'prompt': [{'type': 'text', 'text': '<task-notification>\n<task-id>x</task-id>'}]}))
        self.assertFalse(self.hook.is_synthetic_prompt({'prompt': [{'type': 'text', 'text': 'Nebula calibration'}]}))
        self.assertFalse(self.hook.is_synthetic_prompt({'prompt': None}))
        self.seed()
        resp = self.lifecycle('UserPromptSubmit', 'channel-sess', 'claude', prompt='Nebula calibration',
                              origin={'kind': 'channel', 'server': 'telegram'})
        self.assertIn('Synthetic Reviewer', resp['hookSpecificOutput']['additionalContext'])

    def test_hook_resolves_project_from_vault_cwd_without_explicit_payload(self):
        """Hook resolves active project from cwd within vault and injects its Kurallar.md (#288)."""
        engine = self.seed()
        proj_dir = self.vault / 'projects/alpha'
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / 'Kurallar.md').write_text('# Alpha Kurallari\n- **kural:** ALPHA_HOOK_RULE\n', encoding='utf-8')
        (self.vault / 'notes/alpha_note.md').write_text('---\n{"id": "alpha-note", "kind": "note", "project": "alpha", "visibility": "internal"}\n---\nAlpha Note\n', encoding='utf-8')
        engine.sync()

        # SessionStart inside projects/alpha without payload['project']
        payload = dict(self.payload, hook_event_name='SessionStart', cwd=str(proj_dir), prompt='Alpha query')
        payload.pop('project', None)
        response = self.invoke(payload, 'claude')
        context = response.get('hookSpecificOutput', {}).get('additionalContext', '')
        self.assertIn('ALPHA_HOOK_RULE', context)

        # Negative control: SessionStart at vault root does not inject project rules
        root_payload = dict(self.payload, hook_event_name='SessionStart', cwd=str(self.vault), prompt='Root query')
        root_payload.pop('project', None)
        root_response = self.invoke(root_payload, 'claude')
        root_context = root_response.get('hookSpecificOutput', {}).get('additionalContext', '')
        self.assertNotIn('ALPHA_HOOK_RULE', root_context)

    def test_cwd_project_selects_rules_without_narrowing_retrieval(self):
        """A project found from cwd picks the project rules only; retrieval keeps notes without that field (#288)."""
        from beyin_v3_sync import SyncEngine
        general = self.vault / 'notes/genel.md'
        general.parent.mkdir()
        general.write_text('---\n' + json.dumps({'id': 'genel', 'kind': 'task', 'revision': 1, 'status': 'active',
                                                 'visibility': 'internal'}) +
                           '\n---\nKuzeyrüzgarı kalibrasyonunu Synthetic Reviewer yürütüyor.\n', encoding='utf-8')
        alpha = self.vault / 'projects/alpha'
        alpha.mkdir(parents=True)
        (alpha / 'Kurallar.md').write_text('# Alpha\n- **kural:** ALPHA_ONLY_RULE\n', encoding='utf-8')
        SyncEngine(self.vault, self.state).sync()
        turn = self.lifecycle('UserPromptSubmit', 'alpha-turn', 'claude', prompt='Kuzeyrüzgarı kalibrasyonunu kim yürütüyor',
                              cwd=str(alpha))
        self.assertIn('Synthetic Reviewer', turn.get('hookSpecificOutput', {}).get('additionalContext', ''))
        opening = self.lifecycle('SessionStart', 'alpha-open', 'claude', cwd=str(alpha))
        context = opening.get('hookSpecificOutput', {}).get('additionalContext', '')
        self.assertIn('ALPHA_ONLY_RULE', context)
        self.assertIn('Synthetic Reviewer', context)

    def test_query_alias_loads_canonical_scoped_rules(self):
        engine = self.seed()
        folder = self.vault / 'projects/client'
        folder.mkdir(parents=True)
        (folder / 'Kurallar.md').write_text('# Rules\n- [kapsam: alpha] SCOPED_ALPHA_ALIAS_CANARY\n', encoding='utf-8')
        (self.vault / '.beyin-projects.json').write_text(json.dumps({'folders': {'projects/client': 'alpha'}}), encoding='utf-8')
        engine.sync()
        payload = dict(self.payload, hook_event_name='SessionStart', cwd=str(self.vault), prompt='client projesinde kurallar')
        payload.pop('project', None)
        context = self.invoke(payload, 'claude').get('hookSpecificOutput', {}).get('additionalContext', '')
        self.assertIn('SCOPED_ALPHA_ALIAS_CANARY', context)

    def test_hook_resolves_project_from_query_and_sibling_cwd(self):
        """Hook resolves active project from query at vault root and from sibling directory (#288, #403)."""
        engine = self.seed()
        proj_dir = self.vault / 'projects/alpha'
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / 'Kurallar.md').write_text('# Alpha Kurallari\n- **kural:** ALPHA_HOOK_QUERY_RULE\n', encoding='utf-8')
        engine.sync()

        # 1. At vault root, prompt explicitly mentions 'alpha'
        payload = dict(self.payload, hook_event_name='SessionStart', cwd=str(self.vault), prompt='alpha projesinde kural ve testler')
        payload.pop('project', None)
        response = self.invoke(payload, 'claude')
        context = response.get('hookSpecificOutput', {}).get('additionalContext', '')
        self.assertIn('ALPHA_HOOK_QUERY_RULE', context)

        # 2. Sibling directory outside vault named 'alpha'
        sibling = Path(self.tmp.name) / 'alpha' / 'sub'
        sibling.mkdir(parents=True)
        sibling_payload = dict(self.payload, hook_event_name='SessionStart', cwd=str(sibling), prompt='unscoped prompt')
        sibling_payload.pop('project', None)
        sibling_response = self.invoke(sibling_payload, 'claude')
        sibling_context = sibling_response.get('hookSpecificOutput', {}).get('additionalContext', '')
        self.assertIn('ALPHA_HOOK_QUERY_RULE', sibling_context)


    def test_session_start_reports_due_review_count_and_oldest_date_without_sources(self):
        from beyin_v3_sync import SyncEngine
        source = self.vault / 'private-review.md'
        source.write_text('---\n' + json.dumps({'id': 'review-item', 'review_at': '2020-01-02',
                           'visibility': 'internal'}) + '\n---\nPrivate review text.\n', encoding='utf-8')
        SyncEngine(self.vault, self.state).sync()
        response = self.invoke(dict(self.payload, hook_event_name='SessionStart'))
        context = response['hookSpecificOutput']['additionalContext']
        self.assertIn('Due reviews: 1; oldest 2020-01-02.', context)
        self.assertNotIn('private-review.md', context)
        self.assertNotIn(str(self.vault), context)


class HookFailOpenTest(unittest.TestCase):
    """#417: the foreground hook answers the client even when its own diagnostics cannot be written."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-fail-open-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.state = self.root / 'state'
        self.vault.mkdir()
        self.state.mkdir()
        self.env = inherited_env()  # an agent session's BEYIN_V3_SKIP would answer {} before any code under test (#169)

    def run_hook(self, payload, *extra, state=None):
        return subprocess.run([sys.executable, str(HOOK), '--vault', str(self.vault), '--state', str(state or self.state),
                               '--harness', 'claude', *extra], input=payload, capture_output=True, env=self.env, timeout=60)

    def test_invalid_json_stdin_fails_open_without_crash(self):
        result = self.run_hook(b'{invalid json')
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout.strip(), b'{}')

    def test_error_diagnostic_writes_only_numeric_os_metadata(self):
        hook = load_hook()
        diagnostic_path = self.state / 'hook-error.json'
        missing = self.vault / 'synthetic-private-path.md'
        with self.assertRaises(FileNotFoundError) as caught:
            missing.read_text(encoding='utf-8')
        hook.record_error(self.state, caught.exception)
        diagnostic = json.loads(diagnostic_path.read_text(encoding='utf-8'))
        self.assertEqual(set(diagnostic), {'at', 'error', 'errno'})
        self.assertEqual(diagnostic['error'], 'FileNotFoundError')
        self.assertEqual(diagnostic['errno'], errno.ENOENT)

        invalid = OSError('synthetic private message')
        invalid.errno = 'synthetic nonnumeric value'
        for exc in (OSError('no errno'), invalid, ValueError('synthetic private message')):
            with self.subTest(error=type(exc).__name__, errno=getattr(exc, 'errno', None)):
                hook.record_error(self.state, exc)
                diagnostic = json.loads(diagnostic_path.read_text(encoding='utf-8'))
                self.assertEqual(set(diagnostic), {'at', 'error'})
                self.assertEqual(diagnostic['error'], type(exc).__name__)

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'a read-only folder needs POSIX permissions and a non-root user')
    def test_read_only_state_still_answers_the_prompt(self):
        # Enqueueing fails, and so does the hook-error.json the handler writes: the turn still gets the
        # failure notice and exit 0 instead of a PermissionError traceback.
        self.state.chmod(0o555)
        self.addCleanup(self.state.chmod, 0o755)
        payload = json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': 'ro', 'event_id': 'ro-1',
                              'cwd': str(self.vault), 'prompt': 'Nebula'}).encode()
        result = self.run_hook(payload)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        context = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('V3 source sync failed', context)

    def test_failed_drain_keeps_its_exit_code_and_single_result(self):
        # The foreground reads the worker's exit code to tell a failed sync from a finished one; a drain
        # that failed must not exit 0 or print a second JSON document.
        (self.vault / 'notes').mkdir()
        (self.vault / 'notes/canary.md').write_text('# Nebula\nNebula canary.\n', encoding='utf-8', newline='\n')
        (self.state / 'hook-queue').mkdir()
        (self.state / 'hook-queue' / ('a' * 64 + '.json')).write_text(
            json.dumps({'event': 'UserPromptSubmit', 'harness': 'claude', 'session': 's', 'at': time.time()}),
            encoding='utf-8')
        (self.state / 'skills.sqlite3').mkdir()  # skill mirroring fails after the source sync
        result = self.run_hook(b'', '--drain-queue')
        self.assertEqual(result.returncode, 1, result.stderr.decode())
        self.assertEqual(json.loads(result.stdout)['failed'], 1)
        self.assertTrue((self.state / 'hook-error.json').exists())

    def test_state_inside_vault_is_refused_without_writing_into_the_vault(self):
        # Runtime state inside the vault is an install error; the hook says so and never writes there.
        inside = self.vault / '.state'
        result = self.run_hook(b'{}', state=inside)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Runtime state must be outside vault', result.stderr.decode())
        self.assertFalse(inside.exists())


class HookReadOnlyStateTest(unittest.TestCase):
    """A client can run its hook commands without write access to the runtime state.

    Some harnesses sandbox lifecycle commands (a Seatbelt workspace-write profile, a read-only
    home): the vault stays readable, the state folder refuses the queue write and the worker
    spawn. The turn still has to answer, and the previous committed index is readable, so the
    context comes from there with a one-line note instead of the #417 sync failure notice.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-read-only-state-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.state = self.root / 'state'
        self.vault.mkdir()
        self.state.mkdir()
        self.env = inherited_env()
        self.env['BEYIN_V3_NO_SPAWN'] = '1'

    def seed(self):
        """Commit one synthetic source while the state is still writable."""
        note = self.vault / 'notes' / 'task.md'
        note.parent.mkdir(parents=True)
        note.write_text('---\n' + json.dumps({'id': 'nebula-task', 'kind': 'task', 'revision': 1,
                                              'project': 'nebula', 'status': 'active',
                                              'visibility': 'internal'}) +
                        '\n---\nNebula calibration owner is Synthetic Reviewer.\n', encoding='utf-8')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'sync'], capture_output=True, env=self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def run_hook(self, payload, *extra, script=None, env=None):
        return subprocess.run([sys.executable, str(script or HOOK), '--vault', str(self.vault),
                               '--state', str(self.state), '--harness', 'claude', *extra],
                              input=json.dumps(payload), text=True, encoding='utf-8', capture_output=True,
                              cwd=str(self.vault), env=env or self.env, timeout=60)

    def context(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['hookSpecificOutput']['additionalContext']

    def deny_state_writes(self):
        self.state.chmod(0o555)
        self.addCleanup(self.state.chmod, 0o755)

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'a read-only folder needs POSIX permissions and a non-root user')
    def test_indexed_read_only_state_answers_from_the_previous_index(self):
        self.seed()
        self.deny_state_writes()
        context = self.context(self.run_hook({'hook_event_name': 'UserPromptSubmit', 'session_id': 'ro-indexed',
                                              'event_id': 'ro-indexed-1', 'cwd': str(self.vault),
                                              'prompt': 'Nebula calibration owner'}))
        self.assertIn('Synthetic Reviewer', context)
        self.assertIn('state folder is not writable', context)
        self.assertIn('previous index', context)
        self.assertEqual(context.count('state folder is not writable'), 1)
        self.assertIn('`beyin.py sync` from a client that can write state', context)
        self.assertNotIn('failed or conflicted', context)
        self.assertFalse((self.state / 'hook-error.json').exists())

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'a read-only folder needs POSIX permissions and a non-root user')
    def test_read_only_note_survives_a_turn_without_matches(self):
        # Per-turn retrieval is strict and an empty match injects nothing, but the reason it cannot
        # be refreshed is not a search result: the one-line note still reaches the turn.
        self.seed()
        self.deny_state_writes()
        context = self.context(self.run_hook({'hook_event_name': 'UserPromptSubmit', 'session_id': 'ro-nomatch',
                                              'event_id': 'ro-nomatch-1', 'cwd': str(self.vault),
                                              'prompt': 'quasar telemetry'}))
        self.assertIn('state folder is not writable', context)
        self.assertNotIn('Synthetic Reviewer', context)

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'a read-only folder needs POSIX permissions and a non-root user')
    def test_read_only_state_without_an_index_keeps_the_sync_failure_notice(self):
        # Nothing was ever indexed, so there is no context to fall back to: #417 still names the
        # failure instead of pretending a previous index answered.
        self.deny_state_writes()
        context = self.context(self.run_hook({'hook_event_name': 'UserPromptSubmit', 'session_id': 'ro-empty',
                                              'event_id': 'ro-empty-1', 'cwd': str(self.vault),
                                              'prompt': 'Nebula calibration owner'}))
        self.assertIn('V3 source sync failed', context)
        self.assertIn('cannot run in this client', context)
        self.assertNotIn('this context comes from the previous index', context)
        self.assertFalse((self.state / 'hook-error.json').exists())

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'a read-only folder needs POSIX permissions and a non-root user')
    def test_stop_in_a_read_only_state_stays_empty(self):
        # Stop never carries context, a refused state write included: the fail-open answer is {}.
        self.seed()
        self.deny_state_writes()
        result = self.run_hook({'hook_event_name': 'Stop', 'session_id': 'ro-stop', 'event_id': 'ro-stop-1',
                                'cwd': str(self.vault)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'a read-only folder needs POSIX permissions and a non-root user')
    def test_session_start_in_a_read_only_state_opens_from_the_previous_index(self):
        # SessionStart takes a different branch from UserPromptSubmit (the companion opening), and a
        # sandboxed client must get the same one-line note there instead of the #417 failure notice.
        self.seed()
        self.deny_state_writes()
        context = self.context(self.run_hook({'hook_event_name': 'SessionStart', 'session_id': 'ro-session',
                                              'event_id': 'ro-session-1', 'cwd': str(self.vault)}))
        self.assertIn('state folder is not writable', context)
        self.assertIn('previous index', context)
        self.assertEqual(context.count('state folder is not writable'), 1)
        self.assertNotIn('V3 source sync failed', context)
        self.assertFalse((self.state / 'hook-error.json').exists())

    def seed_edit_reminder(self, session_id):
        """Leave the edited-session marker a PostToolUse writes while the state was still writable."""
        folder = self.state / 'receipt-reminders'
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(session_id.encode()).hexdigest()
        (folder / (digest + '.edited')).write_text(json.dumps({'at': time.time()}), encoding='utf-8')
        return folder, digest

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'a read-only folder needs POSIX permissions and a non-root user')
    def test_stop_ignores_a_pending_edit_reminder_when_the_state_refuses_writes(self):
        # A partly read-only state is the case that matters: the state folder refuses the queue write
        # while receipt-reminders/ still accepts one, so the reminder could turn Stop into a block.
        # Upstream answers {} for a refused state write (#417) and this path must keep that.
        self.seed()
        folder, digest = self.seed_edit_reminder('ro-stop-edited')
        self.deny_state_writes()
        result = self.run_hook({'hook_event_name': 'Stop', 'session_id': 'ro-stop-edited',
                                'event_id': 'ro-stop-edited-1', 'cwd': str(self.vault)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})
        self.assertTrue((folder / (digest + '.edited')).exists())
        self.assertFalse((folder / (digest + '.done')).exists())

    def test_stop_still_blocks_a_pending_edit_reminder_in_a_writable_state(self):
        # The reminder contract itself is untouched: the same marker that stays silent above still
        # blocks once when the state accepts writes.
        self.seed()
        self.seed_edit_reminder('rw-stop-edited')
        result = self.run_hook({'hook_event_name': 'Stop', 'session_id': 'rw-stop-edited',
                                'event_id': 'rw-stop-edited-1', 'cwd': str(self.vault)})
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['decision'], 'block')
        self.assertIn('Files were edited in this session', output['reason'])

    def refused_spawn_hook(self):
        """The real hook where the sandbox refuses to start the sync worker."""
        wrapper = self.root / 'refused_spawn_hook.py'
        wrapper.write_text(f'''import runpy, subprocess, sys
sys.path.insert(0, {str(SCRIPTS)!r})
import beyin_v3_preferences
beyin_v3_preferences.claim_check = lambda *args, **kwargs: True
def refused(*args, **kwargs):
    raise PermissionError(1, 'Operation not permitted')
subprocess.Popen = refused
sys.argv[0] = {str(HOOK)!r}
runpy.run_path({str(HOOK)!r}, run_name='__main__')
''', encoding='utf-8')
        return wrapper

    def test_refused_worker_spawn_falls_back_to_the_previous_index(self):
        self.seed()
        env = dict(self.env)
        env.pop('BEYIN_V3_NO_SPAWN', None)
        context = self.context(self.run_hook({'hook_event_name': 'UserPromptSubmit', 'session_id': 'ro-spawn',
                                              'event_id': 'ro-spawn-1', 'cwd': str(self.vault),
                                              'prompt': 'Nebula calibration owner'},
                                             script=self.refused_spawn_hook(), env=env))
        self.assertIn('Synthetic Reviewer', context)
        self.assertIn('could not start its sync worker', context)
        self.assertFalse((self.state / 'hook-error.json').exists())

    def failed_enqueue_hook(self, code, label):
        """The real hook where enqueueing fails with an error that is not a permission refusal."""
        wrapper = self.root / f'{label}_hook.py'
        wrapper.write_text(f'''import importlib.util, sys
sys.path.insert(0, {str(SCRIPTS)!r})
sys.argv = [{str(HOOK)!r}] + sys.argv[1:]
spec = importlib.util.spec_from_file_location('hook_under_test', {str(HOOK)!r})
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
def refused(*args, **kwargs):
    raise OSError({code}, {label!r})
hook.enqueue_event = refused
hook.main()
''', encoding='utf-8')
        return wrapper

    def test_non_permission_write_failure_keeps_the_417_notice(self):
        # A full disk is a real sync failure, not a read-only client: it must not claim that a
        # previous index answered, and it must not take the refusal note.
        self.seed()
        context = self.context(self.run_hook({'hook_event_name': 'UserPromptSubmit', 'session_id': 'full-disk',
                                              'event_id': 'full-disk-1', 'cwd': str(self.vault),
                                              'prompt': 'Nebula calibration owner'},
                                             script=self.failed_enqueue_hook(errno.ENOSPC, 'enospc')))
        self.assertIn('V3 source sync failed', context)
        self.assertIn('Run the local CLI doctor', context)
        self.assertNotIn('metadata remains queued', context)
        self.assertFalse(list((self.state / 'hook-queue').glob('*.json')))
        self.assertNotIn('state folder is not writable', context)
        self.assertNotIn('Synthetic Reviewer', context)

        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'doctor'], capture_output=True, env=self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        diagnostic = json.loads(result.stdout)['hook-error.json']
        self.assertEqual(set(diagnostic), {'at', 'error', 'errno'})
        self.assertEqual(diagnostic['error'], 'OSError')
        self.assertEqual(diagnostic['errno'], errno.ENOSPC)

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'a read-only folder needs POSIX permissions and a non-root user')
    def test_read_only_fallback_rechecks_revoked_source_visibility(self):
        self.seed()
        note = self.vault / 'notes' / 'task.md'
        note.write_text(note.read_text(encoding='utf-8').replace('"internal"', '"private"'),
                        encoding='utf-8')
        self.deny_state_writes()
        for event in ('SessionStart', 'UserPromptSubmit'):
            with self.subTest(event=event):
                context = self.context(self.run_hook({
                    'hook_event_name': event, 'session_id': 'ro-revoked',
                    'event_id': 'ro-revoked-' + event, 'cwd': str(self.vault),
                    'prompt': 'Nebula calibration owner'}))
                self.assertIn('state folder is not writable', context)
                self.assertNotIn('Synthetic Reviewer', context)

    def test_write_refusal_classification_keeps_other_failures(self):
        # Only the refusals that leave the existing index readable take the read-only path; a full
        # disk, a missing path or a non-OS error must still reach the #417 handler.
        hook = load_hook()
        for code in (errno.EPERM, errno.EACCES, errno.EROFS):
            with self.subTest(errno=code):
                self.assertTrue(hook.permission_refused(PermissionError(code, os.strerror(code))))
        for code in (errno.ENOSPC, errno.ENOENT, errno.EIO, errno.EBUSY):
            with self.subTest(errno=code):
                self.assertFalse(hook.permission_refused(OSError(code, os.strerror(code))))
        # Windows raises these with errno unset (or mapped); a POSIX interpreter can still build the
        # same object, so the win32 mapping is checked here and in the CI matrix.
        for winerror in (5, 19):  # ERROR_ACCESS_DENIED, ERROR_WRITE_PROTECT
            with self.subTest(winerror=winerror):
                denied = OSError(0, 'synthetic')
                denied.winerror = winerror
                self.assertTrue(hook.permission_refused(denied))
        for winerror in (32, 33):  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
            with self.subTest(winerror=winerror):
                transient = OSError(0, 'synthetic')
                transient.winerror = winerror
                self.assertFalse(hook.permission_refused(transient))
        self.assertFalse(hook.permission_refused(RuntimeError('synthetic')))


if __name__ == '__main__':
    unittest.main()
