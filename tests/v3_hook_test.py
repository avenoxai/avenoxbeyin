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
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

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
        broken.write_text('---\nunsupported:\n  nested: metadata\n---\nExcluded synthetic note.\n', encoding='utf-8')
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
        }
        for summary, expected in cases.items():
            with self.subTest(summary=summary):
                self.assertIs(declared(summary), expected)

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
            bash = shutil.which('bash', path=os.environ.get('PATH'))
            self.assertIsNotNone(bash, 'Native Claude Code on Windows requires Git Bash')
            launcher = str(command).split(' -NoProfile ')[0]
            probe = subprocess.run([bash, '-lc', launcher + " -NoProfile -NonInteractive -Command 'exit 0'"],
                                   text=True, encoding='utf-8', capture_output=True,
                                   cwd=self.vault, env=self.env, timeout=20)
            self.assertEqual(probe.returncode, 0, probe.stderr)
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


if __name__ == '__main__':
    unittest.main()
