"""Global boundary adapters exercised in isolated homes, never user hook settings."""
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from v3_package_helpers import inherited_env, install

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_bridge as bridge
import beyin_v3_hook as hook
from beyin_v3_sync import SyncEngine


class BridgeTest(unittest.TestCase):
    def cleanup_tmp(self):
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def setUp(self):
        kwargs = {'ignore_cleanup_errors': True} if sys.version_info >= (3, 10) else {}
        self.tmp = tempfile.TemporaryDirectory(**kwargs)
        self.addCleanup(self.cleanup_tmp)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'Brain Space'; self.vault.mkdir()
        self.state = self.root / 'state'
        self.project = self.root / 'Projects' / 'Örnek Space'; self.project.mkdir(parents=True)
        self.home = self.root / 'home'; self.home.mkdir()
        self.env = inherited_env(HOME=str(self.home), USERPROFILE=str(self.home),
                        BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        self.payload = dict(hook_event_name='SessionStart', session_id='one-session', event_id='start',
                            cwd=str(self.project), prompt='TRANSCRIPT_CANARY')

    def invoke(self, payload=None, extra=(), harness='codex', script=None, cwd=None):
        command = [sys.executable, str(script or SCRIPTS / 'beyin_v3_bridge.py'),
                   '--vault', str(self.vault), '--state', str(self.state), '--harness', harness,
                   '--project-root', str(self.root / 'Projects'), *extra]
        result = subprocess.run(command, input=json.dumps(self.payload if payload is None else payload),
                                text=True, encoding='utf-8', capture_output=True, env=self.env,
                                cwd=cwd or self.project, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def queued(self):
        return [json.loads(p.read_text(encoding='utf-8')) for p in (self.state / 'hook-queue').glob('*.json')]

    def test_external_boundary_context_and_project_metadata_for_both_clients(self):
        for harness in ('claude', 'codex'):
            result = self.invoke(harness=harness)
            text = result['hookSpecificOutput']['additionalContext']
            self.assertIn('receipt --harness ' + harness, text)
            self.assertIn('--event-id EVENT_ID --summary "Work result" --ref PATH', text)
            self.assertIn(str(self.vault / 'beyin.py'), text)
            self.assertLessEqual(len(text), 1500)
        events = self.queued()
        self.assertEqual(len(events), 2)
        for event in events:
            self.assertEqual(event['project'], self.project.name)
            self.assertEqual(len(event['project_id']), 24)
        self.assertNotIn(str(self.project), json.dumps(events))
        self.assertNotIn('TRANSCRIPT_CANARY', json.dumps(events))

    def test_utf8_bom_stdin_payload_handled_cleanly(self):
        bom_input = b'\xef\xbb\xbf' + json.dumps(self.payload).encode('utf-8')
        command = [sys.executable, str(SCRIPTS / 'beyin_v3_bridge.py'),
                   '--vault', str(self.vault), '--state', str(self.state), '--harness', 'claude',
                   '--project-root', str(self.root / 'Projects')]
        result = subprocess.run(command, input=bom_input, capture_output=True, env=self.env,
                                cwd=self.project, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn('hookSpecificOutput', data)

    def test_sensitive_folder_task_cannot_enter_external_project_context(self):
        secret = self.vault / '🔐 Kasa/task.md'
        secret.parent.mkdir()
        secret.write_text('Task details from a private folder', encoding='utf-8')
        safe = self.vault / 'notes/task.md'
        safe.parent.mkdir()
        safe.write_text('Ordinary task', encoding='utf-8')
        record = {'source': secret.relative_to(self.vault).as_posix(),
                  'source_sha256': hashlib.sha256(secret.read_bytes()).hexdigest()}
        self.assertFalse(bridge._fresh(self.vault, record))
        record = {'source': safe.relative_to(self.vault).as_posix(),
                  'source_sha256': hashlib.sha256(safe.read_bytes()).hexdigest()}
        self.assertTrue(bridge._fresh(self.vault, record))

    def test_vault_subdirectory_and_other_installed_vault_skip_duplicates(self):
        for cwd in (self.vault, self.vault / 'nested', self.project):
            cwd.mkdir(exist_ok=True)
            if cwd == self.project: (cwd / '.beyin-runtime.json').write_text('{}')
            self.assertEqual(self.invoke(dict(self.payload, cwd=str(cwd)),
                                        extra=['--project-root', str(self.root)]), {})
        self.assertEqual(self.queued(), [])

    def test_allowlist_sibling_prefix_and_symlink_escape_are_not_authorized(self):
        sibling = self.root / 'Projects-elsewhere'; sibling.mkdir()
        self.assertEqual(self.invoke(dict(self.payload, cwd=str(sibling))), {})
        link = self.project / 'outside'
        try: link.symlink_to(sibling, target_is_directory=True)
        except OSError: pass  # Native Windows without symlink permission still tests prefix boundary.
        else: self.assertEqual(self.invoke(dict(self.payload, cwd=str(link))), {})
        self.assertEqual(self.queued(), [])

    def test_unsupported_events_no_memory_internal_and_manual_are_inert(self):
        for event in ('UserPromptSubmit', 'PostToolUse', 'Unexpected'):
            self.assertEqual(self.invoke(dict(self.payload, hook_event_name=event)), {})
        self.assertEqual(self.invoke(dict(self.payload, no_memory=True)), {})
        self.env['BEYIN_V3_INTERNAL'] = '1'
        self.assertEqual(self.invoke(), {})
        del self.env['BEYIN_V3_INTERNAL']
        (self.vault / '.beyin-preferences.json').write_text('{"auto_sync": false}')
        self.assertEqual(self.invoke(), {})
        self.assertEqual(self.queued(), [])

    def test_public_skip_guard_is_inert(self):
        self.env['BEYIN_V3_SKIP'] = '1'
        self.assertEqual(self.invoke(), {})
        self.assertEqual(self.queued(), [])

    def test_public_skip_guard_honours_only_exact_one(self):
        for count, value in enumerate(('0', 'false', ''), start=1):
            with self.subTest(value=value):
                self.env['BEYIN_V3_SKIP'] = value
                result = self.invoke(dict(self.payload, event_id='start-%d' % count))
                self.assertIn('receipt --harness codex', result['hookSpecificOutput']['additionalContext'])
                self.assertEqual(len(self.queued()), count)

    def test_independent_budget_events_and_local_context_off(self):
        for budget in ('0', '20'):
            self.assertEqual(self.invoke(extra=['--context-chars', budget]), {})
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.invoke(extra=['--event', 'SessionEnd']), {})
        (self.vault / '.beyin-preferences.json').write_text('{"context_mode": "off"}')
        self.assertEqual(self.invoke(), {})

    def test_same_event_and_session_ids_in_different_projects_do_not_collide(self):
        self.invoke(); self.invoke()
        other = self.root / 'Projects' / 'second' / self.project.name; other.mkdir(parents=True)
        self.invoke(dict(self.payload, cwd=str(other)))
        events = self.queued()
        self.assertEqual(len(events), 2)
        self.assertEqual(len({e['session'] for e in events}), 2)
        self.assertEqual(len({e['project_id'] for e in events}), 2)

    def test_global_start_never_injects_companion_or_receipts(self):
        self.state.mkdir()
        (self.state / 'jev.json').write_text('{"mode":"on","features":["auto_context"]}')
        (self.vault / 'receipts').mkdir()
        (self.vault / 'receipts/private.md').write_text('PRIVATE_VAULT_CANARY')
        result = self.invoke()
        self.assertNotIn('PRIVATE_VAULT_CANARY', json.dumps(result))
        self.assertFalse((self.state / 'jev-calls.jsonl').exists())

    def test_unknown_session_cannot_mix_unrelated_receipt_checkpoints(self):
        for session in ('', 'unknown', None, 1):
            self.assertEqual(self.invoke(dict(self.payload, session_id=session)), {})
        self.assertEqual(self.queued(), [])

    def test_origin_ignores_invalid_cwd(self):
        for cwd in (123, '', 'relative', str(self.project) + '\0bad', str(self.root / 'deleted')):
            with self.subTest(cwd=cwd):
                self.assertEqual(bridge.origin({'cwd': cwd}, 'codex', self.vault), {})

    def test_origin_ignores_deleted_process_directory(self):
        from unittest.mock import patch
        with patch.object(bridge.os, 'getcwd', side_effect=FileNotFoundError):
            self.assertEqual(bridge.origin({}, 'codex', self.vault), {})

    def test_origin_ignores_symlink_loop(self):
        left, right = self.root / 'loop-a', self.root / 'loop-b'
        try:
            left.symlink_to(right, target_is_directory=True)
            right.symlink_to(left, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable on this platform')
        self.assertEqual(bridge.origin({'cwd': str(left)}, 'codex', self.vault), {})
        self.assertEqual(self.invoke(dict(self.payload, cwd=str(left))), {})

    def test_cwd_environment_and_process_fallback(self):
        payload = {k: v for k, v in self.payload.items() if k != 'cwd'}
        self.env['CLAUDE_PROJECT_DIR'] = str(self.project)
        self.assertIn('hookSpecificOutput', self.invoke(payload, harness='claude', cwd=self.home))
        self.assertIn('hookSpecificOutput', self.invoke(payload, harness='codex'))
        self.assertEqual(self.invoke(dict(self.payload, cwd=123)), {})

    def agy(self, native, workspaces, cwd=None, **fields):
        # Antigravity sends camelCase fields, no cwd, and runs the hook in the hooks.json folder.
        payload = dict(conversationId='agy-conversation', workspacePaths=[str(p) for p in workspaces], **fields)
        return self.invoke(payload, extra=['--native-event', native], harness='antigravity', cwd=cwd or self.home)

    def test_antigravity_first_workspace_is_the_project_and_only_boundaries_count(self):
        result = self.agy('PreInvocation', [self.project], invocationNum=0)
        text = result['injectSteps'][0]['ephemeralMessage']
        self.assertIn('receipt --harness antigravity', text)
        self.assertEqual(self.agy('PreInvocation', [self.project], invocationNum=1), {})
        self.assertEqual(self.agy('Stop', [self.project], fullyIdle=False), {'decision': 'stop'})
        self.assertEqual(self.agy('Stop', [self.project], fullyIdle=True), {'decision': 'stop'})
        events = self.queued()
        self.assertEqual(sorted(e['event'] for e in events), ['SessionStart', 'Stop'])
        self.assertEqual({e['harness'] for e in events}, {'antigravity'})
        self.assertEqual({e['project'] for e in events}, {self.project.name})
        # Same conversation id in another project is a different receipt session.
        other = self.root / 'Projects' / 'other'; other.mkdir()
        self.agy('PreInvocation', [other], invocationNum=0)
        self.assertEqual(len({e['session'] for e in self.queued()}), 2)

    def test_antigravity_needs_an_allowed_workspace_and_none_owned_by_a_vault(self):
        # The hook process runs in the hooks.json folder; it never stands in for the project.
        for workspaces in ([], [self.vault], [self.project, self.vault]):
            self.assertEqual(self.agy('PreInvocation', workspaces, cwd=self.project, invocationNum=0), {})
        self.assertEqual(self.invoke(dict(conversationId='agy-conversation', invocationNum=0),
                                     extra=['--native-event', 'PreInvocation'], harness='antigravity'), {})
        self.assertEqual(self.queued(), [])

    def test_antigravity_relative_workspace_is_never_resolved_against_the_hook_folder(self):
        # A relative entry would resolve against the hooks.json folder, so its vault check is meaningless.
        other = self.root / 'Projects' / 'other'; other.mkdir()
        for workspaces in (['Projects/other'], [str(self.project), 'Projects/other']):
            self.assertEqual(self.invoke(dict(conversationId='agy-relative', invocationNum=0, workspacePaths=workspaces),
                                         extra=['--native-event', 'PreInvocation'], harness='antigravity',
                                         cwd=self.root), {})
        self.assertEqual(self.queued(), [])

    def test_antigravity_config_is_a_named_hook_whose_command_runs(self):
        config = self.invoke(extra=['--print-config'], harness='antigravity')
        self.assertEqual(list(config), ['beyin-v3-bridge'])
        self.assertEqual(set(config['beyin-v3-bridge']), {'PreInvocation', 'Stop'})
        command = config['beyin-v3-bridge']['PreInvocation'][0]['command']
        payload = dict(conversationId='agy-config', invocationNum=0, workspacePaths=[str(self.project)])
        result = subprocess.run(command, shell=True, input=json.dumps(payload), capture_output=True,
                                text=True, encoding='utf-8', env=self.env, cwd=self.home, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Receipt session=', json.loads(result.stdout)['injectSteps'][0]['ephemeralMessage'])
        narrowed = self.invoke(extra=['--print-config', '--event', 'SessionStart'], harness='antigravity')
        self.assertEqual(set(narrowed['beyin-v3-bridge']), {'PreInvocation'})
        with self.assertRaises(AssertionError):
            self.invoke(extra=['--print-config', '--event', 'SessionEnd'], harness='antigravity')

    def test_folder_below_another_installed_vault_is_skipped_for_every_client(self):
        # The owning vault is found by walking up, so a subfolder is as silent as the vault root.
        below = self.root / 'Projects' / 'second-vault' / 'notes' / 'deep'; below.mkdir(parents=True)
        (self.root / 'Projects' / 'second-vault' / '.beyin-runtime.json').write_text('{}')
        for harness in ('codex', 'claude'):
            self.assertEqual(self.invoke(dict(self.payload, cwd=str(below)), harness=harness), {})
        for workspaces in ([below], [self.project, below]):
            self.assertEqual(self.agy('PreInvocation', workspaces, invocationNum=0), {})
        self.assertEqual(self.queued(), [])

    def test_antigravity_vault_adapter_labels_the_first_workspace_never_the_hook_folder(self):
        # A real install: the vault's own .agents/hooks.json shares the bridge's project rule (origin()).
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        local = json.loads((self.vault / '.agents/hooks.json').read_text(encoding='utf-8'))['beyin-v3']
        config = self.invoke(extra=['--print-config'], harness='antigravity',
                             script=self.vault / '.claude/scripts/beyin_v3_bridge.py')['beyin-v3-bridge']
        # The global hook keeps the timeout the installer chose for the vault's Antigravity hooks.
        self.assertEqual({native: handlers[0]['timeout'] for native, handlers in config.items()},
                         {native: handlers[0]['timeout'] for native, handlers in local.items()})
        hook_folder = self.vault / '.agents'  # Antigravity runs a hook where its hooks.json lives.
        cases = {'agy-vault': (dict(workspacePaths=[str(self.vault)]), self.vault.name),
                 'agy-first-of-two': (dict(workspacePaths=[str(self.project), str(self.vault)]), self.project.name),
                 'agy-cwd-field': (dict(workspacePaths=[str(self.vault)], cwd=str(hook_folder)), self.vault.name),
                 'agy-no-workspaces': ({}, None),
                 'agy-null-workspace': (dict(workspacePaths=[None]), None),
                 'agy-relative-workspace': (dict(workspacePaths=['.']), None),
                 'agy-cwd-field-only': (dict(cwd=str(hook_folder)), None)}
        for conversation, (fields, _) in cases.items():
            result = subprocess.run(local['PreInvocation'][0]['command'], shell=True, capture_output=True, text=True,
                                    input=json.dumps(dict(fields, conversationId=conversation, invocationNum=0)),
                                    encoding='utf-8', env=self.env, cwd=hook_folder, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
        events = {event['session']: event for event in self.queued()}
        self.assertEqual(len(events), len(cases))
        for conversation, (_, label) in cases.items():
            with self.subTest(conversation=conversation):
                # The vault adapter keeps the plain conversation id as the session.
                event = events[hashlib.sha256(conversation.encode()).hexdigest()[:24]]
                self.assertEqual(event['harness'], 'antigravity')
                self.assertEqual(event.get('project'), label)
                self.assertEqual('project_id' in event, label is not None)
        # The global hook sees the same in-vault conversation and leaves it to the vault.
        self.assertEqual(self.agy('PreInvocation', [self.vault], invocationNum=0), {})
        self.assertEqual(len(self.queued()), len(cases))

    def test_gap_projection_preserves_project_and_receipt_clears_gap(self):
        self.invoke()
        self.invoke(dict(self.payload, hook_event_name='Stop', event_id='stop'))
        self.assertEqual(hook.drain_queue(self.vault, self.state)['processed'], 2)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints']
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]['project'], self.project.name)
        engine = SyncEngine(self.vault, self.state)
        engine.note_create('notes/result.md', 'Synthetic result.', {'id': 'result', 'project': 'demo'})
        engine.receipt('finished', 'Synthetic result.', ['notes/result.md'], 'codex', session=gaps[0]['session'])
        engine.sync()
        self.assertEqual(json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['potential_missing_receipts'], 0)

    def test_legacy_checkpoint_schema_migrates_without_losing_rows(self):
        engine = SyncEngine(self.vault, self.state)
        with engine.store._connect() as db:
            db.execute('DROP TABLE IF EXISTS receipt_checkpoints')
            db.execute('CREATE TABLE receipt_checkpoints(harness TEXT,session TEXT,at REAL,PRIMARY KEY(harness,session))')
            db.execute("INSERT INTO receipt_checkpoints VALUES ('codex','old',123)")
        hook.drain_queue(self.vault, self.state)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints']
        self.assertEqual(gaps, [], 'legacy rows without prompt evidence do not imply missing work')
        with engine.store._connect() as db:
            self.assertEqual(db.execute("SELECT harness,session,at FROM receipt_checkpoints WHERE session='old'").fetchone(),
                             ('codex', 'old', 123.0), 'migration preserves the source row')

    def test_installed_zip_config_executes_from_external_project_without_global_writes(self):
        package = self.root / 'candidate.zip'
        def run(*args):
            result = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True,
                                    encoding='utf-8', env=self.env, cwd=self.project, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result
        run(ROOT / 'scripts/build_v3_release.py', '--version', '3.1.1', '--output', package)
        with zipfile.ZipFile(package) as archive: archive.extractall(self.root / 'package')
        run(self.root / 'package/scripts/install_v3.py', '--vault', self.vault, '--state', self.state)
        installed = self.vault / '.claude/scripts/beyin_v3_bridge.py'
        self.assertTrue(installed.is_file())
        for harness in ('codex', 'claude'):
            config = self.invoke(extra=['--print-config'], harness=harness, script=installed)
            self.assertEqual(set(config['hooks']), set(bridge.EVENTS))
            command = config['hooks']['SessionStart'][0]['hooks'][0]['command']
            result = subprocess.run(command, shell=True, input=json.dumps(self.payload), capture_output=True,
                                    text=True, encoding='utf-8', env=self.env, cwd=self.project, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Receipt session=', json.loads(result.stdout)['hookSpecificOutput']['additionalContext'])
        self.assertFalse((self.home / '.codex/hooks.json').exists())
        self.assertFalse((self.home / '.claude/settings.json').exists())

    def test_project_context_off_by_default_leaves_output_byte_for_byte_identical(self):
        engine = SyncEngine(self.vault, self.state)
        engine.task_create('tasks/test.md', 'Task', {'id': 'task-a', 'title': 'Task A', 'status': 'active', 'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user'})
        engine.sync()
        result = self.invoke()
        text = result['hookSpecificOutput']['additionalContext']
        self.assertNotIn('Project context', text)
        self.assertNotIn('Task A', text)

    def test_project_context_on_injects_scoped_receipt_and_matching_due_tasks(self):
        bridge.save_project_context(self.state, True)
        self.invoke(dict(self.payload, event_id='init-start'))
        self.invoke(dict(self.payload, hook_event_name='Stop', event_id='init-stop'))
        hook.drain_queue(self.vault, self.state)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints']
        session = gaps[0]['session']
        engine = SyncEngine(self.vault, self.state)
        engine.note_create('notes/out.md', 'Outcome', {'id': 'out', 'project': self.project.name})
        engine.receipt('rec-1', 'Sprint 12 completed with zero bugs.', ['notes/out.md'], 'codex', session=session)
        engine.task_create('tasks/deploy.md', 'Deploy details', {'id': 'deploy', 'title': 'Deploy v2', 'next_action': 'Run ansible', 'status': 'active', 'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user'})
        engine.sync()
        result = self.invoke(dict(self.payload, event_id='second-start'))
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn('Project context (' + self.project.name + '):', text)
        self.assertIn('Son kayit: Sprint 12 completed with zero bugs.', text)
        self.assertIn('Tarihi gelen gorevler:', text)
        self.assertIn('- Deploy v2: Run ansible', text)

    def test_different_cwd_receipts_do_not_leak_across_projects(self):
        bridge.save_project_context(self.state, True)
        self.invoke(dict(self.payload, event_id='start-a'))
        self.invoke(dict(self.payload, hook_event_name='Stop', event_id='stop-a'))
        hook.drain_queue(self.vault, self.state)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints']
        session_a = gaps[0]['session']

        other = self.root / 'Projects' / 'Other Project'; other.mkdir()
        self.invoke(dict(self.payload, cwd=str(other), event_id='start-b'), cwd=other)
        self.invoke(dict(self.payload, cwd=str(other), hook_event_name='Stop', event_id='stop-b'), cwd=other)
        hook.drain_queue(self.vault, self.state)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints']
        session_b = [g['session'] for g in gaps if g['project'] == 'Other Project'][0]

        engine = SyncEngine(self.vault, self.state)
        engine.note_create('notes/out_a.md', 'Outcome A', {'id': 'out_a', 'project': self.project.name})
        engine.note_create('notes/out_b.md', 'Outcome B', {'id': 'out_b', 'project': 'Other Project'})
        engine.receipt('rec-a', 'ALPHA_PROJECT_CANARY: Completed project A work.', ['notes/out_a.md'], 'codex', session=session_a)
        engine.receipt('rec-b', 'BETA_PROJECT_CANARY: Completed project B work.', ['notes/out_b.md'], 'codex', session=session_b)
        engine.sync()

        text_a = self.invoke(dict(self.payload, event_id='read-a'))['hookSpecificOutput']['additionalContext']
        self.assertIn('ALPHA_PROJECT_CANARY', text_a)
        self.assertNotIn('BETA_PROJECT_CANARY', text_a)

        text_b = self.invoke(dict(self.payload, cwd=str(other), event_id='read-b'), cwd=other)['hookSpecificOutput']['additionalContext']
        self.assertIn('BETA_PROJECT_CANARY', text_b)
        self.assertNotIn('ALPHA_PROJECT_CANARY', text_b)

    def test_other_projects_due_tasks_show_count_only_never_titles(self):
        bridge.save_project_context(self.state, True)
        engine = SyncEngine(self.vault, self.state)
        engine.task_create('tasks/my.md', 'My task', {'id': 'my-task', 'title': 'Visible Local Task', 'next_action': 'Fix it', 'status': 'active', 'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user'})
        engine.task_create('tasks/other1.md', 'Other 1', {'id': 'other-1', 'title': 'SECRET_TITLE_ONE', 'next_action': 'Secret action 1', 'status': 'active', 'due_at': '2026-09-01', 'project': 'Unrelated Project', 'owner': 'user'})
        engine.task_create('tasks/other2.md', 'Other 2', {'id': 'other-2', 'title': 'SECRET_TITLE_TWO', 'next_action': 'Secret action 2', 'status': 'waiting', 'due_at': '2026-09-02', 'project': 'Different Project', 'owner': 'user'})
        engine.sync()
        text = self.invoke()['hookSpecificOutput']['additionalContext']
        self.assertIn('- Visible Local Task: Fix it', text)
        self.assertIn('(bu proje disinda 2 tarihi gelmis gorev)', text)
        self.assertNotIn('SECRET_TITLE_ONE', text)
        self.assertNotIn('SECRET_TITLE_TWO', text)
        self.assertNotIn('Secret action', text)

    def test_private_records_never_enter_project_context(self):
        bridge.save_project_context(self.state, True)
        self.invoke(dict(self.payload, event_id='start-priv'))
        self.invoke(dict(self.payload, hook_event_name='Stop', event_id='stop-priv'))
        hook.drain_queue(self.vault, self.state)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints']
        session = gaps[0]['session']
        engine = SyncEngine(self.vault, self.state)
        engine.note_create('notes/public.md', 'Outcome', {'id': 'public-out', 'project': self.project.name})
        engine.note_create('notes/secret.md', 'Private outcome', {'id': 'secret-out', 'project': self.project.name, 'visibility': 'private'})
        engine.receipt('rec-old', 'OLDER_PUBLIC_RECEIPT', ['notes/public.md'], 'codex', session=session)
        engine.receipt('rec-priv', 'PRIVATE_RECEIPT_CANARY', ['notes/secret.md'], 'codex', session=session)
        engine.task_create('tasks/priv_task.md', 'Private task', {'id': 'priv-task', 'title': 'PRIVATE_TASK_CANARY', 'next_action': 'Secret', 'status': 'active', 'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user', 'visibility': 'private'})
        engine.task_create('tasks/other_priv.md', 'Other private', {'id': 'other-priv', 'title': 'Other', 'status': 'active', 'due_at': '2026-09-01', 'project': 'Elsewhere', 'owner': 'user', 'visibility': 'private'})
        engine.sync()
        text = self.invoke(dict(self.payload, event_id='read-priv'))['hookSpecificOutput']['additionalContext']
        self.assertNotIn('PRIVATE_RECEIPT_CANARY', text)
        self.assertNotIn('PRIVATE_TASK_CANARY', text)
        self.assertNotIn('bu proje disinda', text)
        # A receipt that cites a private source is skipped, not the whole block.
        self.assertIn('Son kayit: OLDER_PUBLIC_RECEIPT', text)

    def test_stale_superseded_and_untrusted_tasks_stay_out(self):
        bridge.save_project_context(self.state, True)
        engine = SyncEngine(self.vault, self.state)
        base = {'status': 'active', 'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user'}
        engine.task_create('tasks/stale.md', 'Body', dict(base, id='stale', title='STALE_TASK'))
        engine.task_create('tasks/old.md', 'Body', dict(base, id='old', title='OLD_TASK'))
        engine.task_create('tasks/new.md', 'Body', dict(base, id='new', title='NEW_TASK'))
        engine.note_create('notes/decision.md', 'Old task replaced', {'id': 'decision', 'project': self.project.name, 'supersedes': ['old']})
        engine.task_create('tasks/web.md', 'Body', dict(base, id='web', title='UNTRUSTED_TASK'))
        engine.sync()
        with engine.store._connect() as db:
            row = json.loads(db.execute("SELECT payload FROM records WHERE id='web'").fetchone()[0])
            db.execute("UPDATE records SET payload=? WHERE id='web'", (json.dumps(dict(row, trust='untrusted')),))
        with (self.vault / 'tasks/stale.md').open('a', encoding='utf-8') as handle:
            handle.write('edited after indexing\n')
        text = self.invoke()['hookSpecificOutput']['additionalContext']
        self.assertIn('- NEW_TASK', text)
        for canary in ('STALE_TASK', 'OLD_TASK', 'UNTRUSTED_TASK'):
            self.assertNotIn(canary, text)

    def test_project_context_failure_keeps_the_startup_instructions(self):
        bridge.save_project_context(self.state, True)
        baseline = self.invoke(dict(self.payload, event_id='before'))['hookSpecificOutput']['additionalContext']
        (self.state / 'memory.sqlite3').write_bytes(b'not a database')
        text = self.invoke(dict(self.payload, event_id='broken'))['hookSpecificOutput']['additionalContext']
        self.assertIn('receipt --harness codex', text)
        self.assertNotIn('Project context', text)
        self.assertEqual(text, baseline)
        (self.state / 'project-context.json').write_text('[true]', encoding='utf-8')
        self.assertFalse(bridge.read_project_context(self.state))

    def test_default_budget_keeps_due_tasks_next_to_a_long_receipt(self):
        bridge.save_project_context(self.state, True)
        self.invoke(dict(self.payload, event_id='start-long'))
        self.invoke(dict(self.payload, hook_event_name='Stop', event_id='stop-long'))
        hook.drain_queue(self.vault, self.state)
        session = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints'][0]['session']
        engine = SyncEngine(self.vault, self.state)
        engine.note_create('notes/long.md', 'Outcome', {'id': 'long-out', 'project': self.project.name})
        engine.receipt('rec-long', 'LONG_SUMMARY ' + 'word ' * 200, ['notes/long.md'], 'codex', session=session)
        engine.task_create('tasks/due.md', 'Body', {'id': 'due', 'title': 'DUE_NEXT_TO_LONG', 'status': 'active', 'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user'})
        engine.sync()
        text = self.invoke(dict(self.payload, event_id='read-long'))['hookSpecificOutput']['additionalContext']
        self.assertLessEqual(len(text), 1500)
        self.assertIn('Son kayit: LONG_SUMMARY', text)
        self.assertIn('...', text)
        self.assertIn('- DUE_NEXT_TO_LONG', text)

    def test_budget_cap_drops_entries_cleanly_without_partial_cut(self):
        engine = SyncEngine(self.vault, self.state)
        for i in range(15):
            engine.task_create(f'tasks/t_{i:02d}.md', 'Body', {
                'id': f'task-{i:02d}',
                'title': f'TASK_{i:02d}_TITLE_' + ('x' * 40),
                'next_action': f'Action {i}',
                'status': 'active', 'due_at': '2026-09-01',
                'project': self.project.name, 'owner': 'user'
            })
        engine.sync()
        ctx = bridge.project_context(self.vault, self.state, 'test-id', self.project.name, budget=150)
        self.assertLessEqual(len(ctx), 150)
        lines = ctx.splitlines()
        for line in lines[2:]:
            self.assertTrue(line.startswith('- TASK_'))
            self.assertIn(': Action', line)

    def test_project_context_handles_legacy_schema_without_project_id_column(self):
        engine = SyncEngine(self.vault, self.state)
        engine.task_create('tasks/legacy_due.md', 'Body', {
            'id': 'legacy_due', 'title': 'LEGACY_DUE_TASK', 'status': 'active',
            'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user'
        })
        engine.sync()
        import sqlite3
        db = sqlite3.connect(self.state / 'memory.sqlite3')
        try:
            db.execute('DROP TABLE IF EXISTS receipt_checkpoints')
            db.execute('CREATE TABLE receipt_checkpoints(harness TEXT, session TEXT, at REAL, turn_at REAL DEFAULT 0, PRIMARY KEY(harness,session))')
        finally:
            db.close()
        ctx = bridge.project_context(self.vault, self.state, 'test-id', self.project.name, budget=1200)
        self.assertIn('Project context (' + self.project.name + '):', ctx)
        self.assertIn('- LEGACY_DUE_TASK', ctx)

    def raw(self, data):
        command = [sys.executable, str(SCRIPTS / 'beyin_v3_bridge.py'), '--vault', str(self.vault), '--state',
                   str(self.state), '--harness', 'claude', '--project-root', str(self.root / 'Projects')]
        result = subprocess.run(command, input=data, capture_output=True, env=self.env, cwd=self.project, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_undecodable_prompt_byte_keeps_the_event_and_a_cut_payload_stays_inert(self):
        # A stray byte in the prompt no longer costs the whole event: it is read as U+FFFD.
        data = json.dumps(self.payload).encode('utf-8').replace(b'TRANSCRIPT_CANARY', b'TRANSCRIPT\xffCANARY')
        self.assertIn('receipt --harness claude', self.raw(data)['hookSpecificOutput']['additionalContext'])
        self.assertEqual(len(self.queued()), 1)
        # A payload cut at the 1 MB read is not JSON: no context, no event, exit 0.
        cut = json.dumps(dict(self.payload, event_id='cut', prompt='x' * 1_100_000)).encode('utf-8')
        self.assertEqual(self.raw(cut), {})
        self.assertEqual(len(self.queued()), 1)

    def test_bridge_install_and_uninstall_antigravity(self):
        gemini_dir = self.home / '.gemini/config'
        gemini_dir.mkdir(parents=True, exist_ok=True)
        hooks_json = gemini_dir / 'hooks.json'
        hooks_json.write_text(json.dumps({'custom-hook': {'PreInvocation': [{'type': 'command', 'command': 'echo custom'}]}}), encoding='utf-8')
        gemini_md = gemini_dir / 'GEMINI.md'
        gemini_md.write_text("# Existing Instructions\nKeep this rule safe.\n", encoding='utf-8')

        # Install bridge
        res = bridge.install_bridge(self.vault, self.state, 'antigravity', [self.project], home=self.home)
        self.assertEqual(res['status'], 'installed')
        self.assertEqual(res['harness'], 'antigravity')

        # Verify hooks.json has beyin-v3-bridge AND preserved custom-hook
        data = json.loads(hooks_json.read_text(encoding='utf-8'))
        self.assertIn('beyin-v3-bridge', data)
        self.assertIn('custom-hook', data)
        self.assertTrue((gemini_dir / 'hooks.json.bak').is_file())

        # Verify GEMINI.md has pointer and preserved custom rule
        md_text = gemini_md.read_text(encoding='utf-8')
        self.assertIn('<!-- beyin-v3-bridge:start -->', md_text)
        self.assertIn('"' + str(self.vault) + '"', md_text)
        self.assertIn('Keep this rule safe.', md_text)
        self.assertTrue((gemini_dir / 'GEMINI.md.bak').is_file())

        # Idempotent install: run again, verify content is preserved and no extra backups
        res2 = bridge.install_bridge(self.vault, self.state, 'antigravity', [self.project], home=self.home)
        self.assertEqual(res2['status'], 'installed')
        self.assertFalse((gemini_dir / 'hooks.json.bak.1').exists())

        # Uninstall bridge
        un_res = bridge.uninstall_bridge('antigravity', home=self.home)
        self.assertEqual(un_res['status'], 'uninstalled')
        self.assertGreaterEqual(un_res['removed_count'], 1)

        # Verify hooks.json no longer has beyin-v3-bridge but still has custom-hook
        data_after = json.loads(hooks_json.read_text(encoding='utf-8'))
        self.assertNotIn('beyin-v3-bridge', data_after)
        self.assertIn('custom-hook', data_after)

        # Verify GEMINI.md no longer has pointer block but still has custom rule
        md_after = gemini_md.read_text(encoding='utf-8')
        self.assertNotIn('<!-- beyin-v3-bridge:start -->', md_after)
        self.assertIn('Keep this rule safe.', md_after)

    def test_install_rejects_invalid_global_settings_without_replacing_them(self):
        for harness in ('claude', 'codex', 'antigravity'):
            target = bridge.global_hook_path(harness, home=self.home)
            target.parent.mkdir(parents=True, exist_ok=True)
            for raw in (b'{invalid', b'[]', b'{"hooks": []}', b'{"hooks": {"SessionStart": {}}}'):
                if harness == 'antigravity' and b'hooks' in raw:
                    continue
                with self.subTest(harness=harness, raw=raw):
                    target.write_bytes(raw)
                    with self.assertRaises(ValueError):
                        bridge.install_bridge(self.vault, self.state, harness, [self.project], home=self.home)
                    self.assertEqual(target.read_bytes(), raw)
                    self.assertFalse(target.with_name(target.name + '.bak').exists())

    def test_install_preserves_unknown_handlers_and_replaces_windows_only_bridge(self):
        target = bridge.global_hook_path('codex', home=self.home)
        target.parent.mkdir(parents=True)
        foreign = {'matcher': 'custom', 'hooks': ['opaque-handler', {'type': 'future', 'command': None}]}
        windows = {'hooks': [{'type': 'command', 'commandWindows': 'python beyin_v3_bridge.py'}]}
        target.write_text(json.dumps({'hooks': {'SessionStart': [foreign, windows], 'Stop': []}}), encoding='utf-8')
        bridge.install_bridge(self.vault, self.state, 'codex', [self.project], ['SessionStart'], home=self.home)
        groups = json.loads(target.read_text(encoding='utf-8'))['hooks']['SessionStart']
        self.assertEqual(groups[0], foreign)
        self.assertEqual(len(groups), 2)
        bridge.uninstall_bridge('codex', home=self.home)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')),
                         {'hooks': {'SessionStart': [foreign], 'Stop': []}})

    def test_uninstall_without_bridge_preserves_empty_hook_groups_byte_for_byte(self):
        target = bridge.global_hook_path('claude', home=self.home)
        target.parent.mkdir(parents=True)
        raw = b'{ "hooks": {"SessionStart": [], "Stop": [{"hooks": []}]} }\n'
        target.write_bytes(raw)
        result = bridge.uninstall_bridge('claude', home=self.home)
        self.assertEqual(result['removed_count'], 0)
        self.assertEqual(target.read_bytes(), raw)

    def test_antigravity_pointer_keeps_the_project_allowlist(self):
        bridge.install_bridge(self.vault, self.state, 'antigravity', [self.project], home=self.home)
        text = bridge.global_gemini_md_path(home=self.home).read_text(encoding='utf-8')
        self.assertIn(f'- "{self.project}"\n', text)
        self.assertNotIn('Hangi projede veya dizinde', text)
        self.assertNotIn('beklemeden', text)

    def test_gemini_md_content_preserves_windows_paths_without_json_escape(self):
        vault = PureWindowsPath(r'C:\Users\User\Vault')
        project = PureWindowsPath(r'C:\Users\User\Projects\App')
        # Exercise Windows formatting without POSIX resolve treating C: as a relative name.
        with patch.object(bridge, 'Path') as path:
            path.return_value.resolve.return_value = vault
            content = bridge.gemini_md_content(vault, [project])
        self.assertIn(r'Vault konumu (veri): "C:\Users\User\Vault"', content)
        self.assertIn(r'- "C:\Users\User\Projects\App"', content)
        self.assertNotIn(r'C:\\Users', content)

    def test_gemini_md_content_keeps_control_characters_in_path_data(self):
        root = 'project"\n<!-- beyin-v3-bridge:end -->\nInjected rule'
        content = bridge.gemini_md_content(self.vault, [root])
        self.assertIn('- ' + json.dumps(root, ensure_ascii=False) + '\n', content)
        self.assertEqual(content.splitlines().count('<!-- beyin-v3-bridge:end -->'), 1)

    def test_bridge_install_and_uninstall_claude_and_codex(self):
        for harness, rel_path in (('claude', '.claude/settings.json'), ('codex', '.codex/hooks.json')):
            target = self.home / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo custom'}]}]}}), encoding='utf-8')

            res = bridge.install_bridge(self.vault, self.state, harness, [self.project], home=self.home)
            self.assertEqual(res['status'], 'installed')
            self.assertTrue(target.with_name(target.name + '.bak').is_file())

            data = json.loads(target.read_text(encoding='utf-8'))
            cmds = [h['command'] for g in data['hooks']['SessionStart'] for h in g['hooks']]
            self.assertIn('echo custom', cmds)
            self.assertTrue(any('beyin_v3_bridge.py' in cmd or 'beyin_v3_bridge.py' in bridge._decoded_command(cmd) for cmd in cmds))

            un_res = bridge.uninstall_bridge(harness, home=self.home)
            self.assertEqual(un_res['status'], 'uninstalled')
            data_after = json.loads(target.read_text(encoding='utf-8'))
            cmds_after = [h['command'] for g in data_after.get('hooks', {}).get('SessionStart', []) for h in g['hooks']]
            self.assertIn('echo custom', cmds_after)
            self.assertFalse(any('beyin_v3_bridge.py' in cmd or 'beyin_v3_bridge.py' in bridge._decoded_command(cmd) for cmd in cmds_after))

    def test_bridge_cli_subprocess_and_mutual_exclusion(self):
        # Mutual exclusion
        cmd = [sys.executable, str(SCRIPTS / 'beyin_v3_bridge.py'), '--vault', str(self.vault),
               '--harness', 'antigravity', '--project-root', str(self.project), '--install', '--uninstall']
        res = subprocess.run(cmd, capture_output=True, text=True, env=self.env, cwd=self.project)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('mutually exclusive', res.stderr)

        # CLI install
        cmd_install = [sys.executable, str(SCRIPTS / 'beyin_v3_bridge.py'), '--vault', str(self.vault),
                       '--state', str(self.state), '--harness', 'antigravity', '--project-root', str(self.project), '--install']
        res_inst = subprocess.run(cmd_install, capture_output=True, text=True, env=self.env, cwd=self.project)
        self.assertEqual(res_inst.returncode, 0, res_inst.stderr)
        data = json.loads(res_inst.stdout)
        self.assertEqual(data['status'], 'installed')

        # CLI uninstall
        cmd_uninst = [sys.executable, str(SCRIPTS / 'beyin_v3_bridge.py'), '--harness', 'antigravity', '--uninstall']
        res_uninst = subprocess.run(cmd_uninst, capture_output=True, text=True, env=self.env, cwd=self.project)
        self.assertEqual(res_uninst.returncode, 0, res_uninst.stderr)
        data_un = json.loads(res_uninst.stdout)
        self.assertEqual(data_un['status'], 'uninstalled')

    def test_bridge_print_config_cli_and_human_mode(self):
        import shutil
        vault_entry = self.vault / 'beyin.py'
        shutil.copy(str(ROOT / 'scripts/beyin_entry.py'), str(vault_entry))
        (self.vault / '.beyin-version').write_text('3.9.1', encoding='utf-8')
        (self.vault / '.beyin-runtime.json').write_text(json.dumps({'state': str(self.state)}), encoding='utf-8')
        vault_scripts = self.vault / '.claude/scripts'
        vault_scripts.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(ROOT / 'scripts/beyin_v3.py'), str(vault_scripts / 'beyin_v3_cli.py'))
        shutil.copy(str(SCRIPTS / 'beyin_v3_bridge.py'), str(vault_scripts / 'beyin_v3_bridge.py'))

        # Run with --human to test interactive terminal code path
        res_human = subprocess.run(
            [sys.executable, str(vault_entry), '--human', 'bridge', '--harness', 'claude',
             '--project-root', str(self.project), '--print-config'],
            capture_output=True, text=True, env=self.env, cwd=self.project, timeout=20,
        )
        self.assertEqual(res_human.returncode, 0, res_human.stderr)
        self.assertNotIn('cannot unpack non-iterable', res_human.stderr)
        config_data = json.loads(res_human.stdout)
        self.assertIn('hooks', config_data)
        self.assertIn('SessionStart', config_data['hooks'])

        # Antigravity print-config
        res_ag = subprocess.run(
            [sys.executable, str(vault_entry), 'bridge', '--harness', 'antigravity',
             '--project-root', str(self.project), '--print-config'],
            capture_output=True, text=True, env=self.env, cwd=self.project, timeout=20,
        )
        self.assertEqual(res_ag.returncode, 0, res_ag.stderr)
        ag_data = json.loads(res_ag.stdout)
        self.assertIn('beyin-v3-bridge', ag_data)
        self.assertIn('PreInvocation', ag_data['beyin-v3-bridge'])


if __name__ == '__main__':
    unittest.main()
