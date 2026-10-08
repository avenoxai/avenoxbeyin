#!/usr/bin/env python3
"""Offline lifecycle, queue and installer tests using isolated synthetic homes."""
import importlib.util
import base64
import hashlib
import re
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from v3_package_helpers import clean_environ

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
                    'PATH': os.defpath, 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1',
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
        self.assertEqual(contexts[0], contexts[1])

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

    def test_stop_knowledge_reminder_passes_when_learning_is_declared_none(self):
        engine = self.seed()
        def session(name):
            return hashlib.sha256(name.encode()).hexdigest()[:24]
        sess_name = 'no-learn-session'
        self.lifecycle('PostToolUse', sess_name, 'claude')
        summary = "Routine bugfix completed.\nÖğrenilen: Yok"
        engine.receipt('no-learn-1', summary, ['notes/task.md'], 'claude', session=session(sess_name))
        response = self.lifecycle('Stop', sess_name, 'claude')
        self.assertEqual(response, {})

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
                    self.lifecycle('PostToolUse', name, 'claude')
                    note = self.vault / root / 'Ajanlar' / f'wal-{name}.md'
                    note.parent.mkdir(parents=True, exist_ok=True)
                    note.write_text('# WAL\n', encoding='utf-8')
                    refs = ['notes/task.md']
                    if via_ref:
                        refs.append(note.relative_to(self.vault).as_posix())
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

    def test_stop_receipt_reminder_quiet_paths_keep_the_one_reminder(self):
        self.lifecycle('PostToolUse', 'quiet', 'claude')
        self.assertEqual(self.lifecycle('Stop', 'quiet', 'claude', stop_hook_active=True), {})
        self.assertEqual(self.lifecycle('Stop', 'quiet', 'claude', extra=['--metadata-only']), {})
        preferences = self.vault / '.beyin-preferences.json'
        preferences.write_text(json.dumps({'auto_sync': False}), encoding='utf-8')
        self.assertEqual(self.lifecycle('Stop', 'quiet', 'claude'), {})
        preferences.unlink()
        self.assertEqual(self.lifecycle('Stop', 'quiet', 'claude')['decision'], 'block')

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
        claude = json.loads(paths[0].read_text())
        self.assertEqual(claude['permissions'], originals['.claude/settings.local.json']['permissions'])
        self.assertIn(originals['.claude/settings.local.json']['hooks']['SessionStart'][0], claude['hooks']['SessionStart'])
        self.assertEqual(json.loads(paths[1].read_text())['trust'], {'synthetic-trust': 'unchanged'})
        self.assertIn('synthetic-other-agent', json.loads(paths[2].read_text()))
        self.assertIn('Keep this user content.', (self.vault / 'AGENTS.md').read_text())
        agent_config = json.loads(paths[2].read_text())
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
            # The whole encoded command, not only its launcher, must survive Git Bash.
            through_bash = subprocess.run([bash, '-lc', str(command)], input=payload, text=True, encoding='utf-8',
                                          capture_output=True, cwd=self.vault, env=self.env, timeout=20)
            self.assertEqual(through_bash.returncode, 0, through_bash.stderr)
            self.assertIn('Synthetic Reviewer', json.loads(through_bash.stdout)['hookSpecificOutput']['additionalContext'])
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
        antigravity = self.invoke(dict(payload, session_id='hygiene-cap-antigravity'), 'antigravity')
        self.assertEqual(antigravity, {'decision': 'stop'})

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


if __name__ == '__main__':
    unittest.main()
