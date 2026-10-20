"""Codex's reviewed feature contract and client-specific settings (no model calls)."""
import contextlib
import json
from pathlib import Path
import subprocess
import types
import unittest
from unittest.mock import patch

from tests.v3_yakala_ajan_test import AgentFiles, yakala


CONFIG_FILES = yakala._agent_codex_config_files
CODEX_HOME = yakala._agent_codex_home
MDM_PREFLIGHT = yakala._agent_codex_mdm_preflight
PREFLIGHT = yakala._agent_preflight


# Feature names from openai/codex rust-v0.155.1, codex-rs/features/src/lib.rs.
# Keep this independent of CODEX_DISABLED: a new required name needs a compatibility test.
FEATURES = ('apps', 'browser_use', 'browser_use_external', 'computer_use', 'goals',
            'image_generation', 'in_app_browser', 'multi_agent', 'multi_agent_v2',
            'plugins', 'remote_plugin', 'shell_tool', 'unified_exec', 'view_image')


def feature_table(disabled=False):
    rows = [f'{name:<48}  {"stable":<17}  {str(not disabled).lower()}' for name in FEATURES]
    # Codex includes removed features and stages containing spaces. Unrelated rows are legal.
    rows += ['code_mode              under development  true',
             'code_mode_host         stable             true',
             'apply_patch_freeform   removed            false',
             'guardianv2.thread_context  under development false']
    return '\n'.join(rows) + '\n'


class CodexFeatureReviewTest(AgentFiles):
    def preflight(self, version='codex-cli 0.155.1', plain=None, effective=None,
                  rc=0, mcp_enabled=False, fail_mcp_disable=False):
        self.calls = []

        def run(argv, timeout, cwd=None):
            self.calls.append((argv, timeout, cwd))
            if argv[-1] == '--version':
                return 0, version, ''
            if argv[-2:] == ['features', 'list']:
                restricted = '--disable' in argv
                text = effective if restricted else plain
                return rc, feature_table(restricted) if text is None else text, ''
            if argv[-3:] == ['mcp', 'list', '--json']:
                enabled = mcp_enabled and (fail_mcp_disable or
                                           'mcp_servers.local_mcp.enabled=false' not in argv)
                return 0, json.dumps([{'name': 'local_mcp', 'enabled': enabled}]), ''
            if argv[-3:] == ['plugin', 'list', '--json']:
                # The user's plugin can be enabled, but --disable plugins applies to this run.
                return 0, '{"installed": [], "available": []}', ''
            if argv[-2:] == ['exec', '--help']:
                return 0, '--ignore-rules --json --sandbox --skip-git-repo-check', ''
            if argv[-1] == '--help':
                return 0, '--disable --ask-for-approval --strict-config', ''
            self.fail('Preflight attempted an unexpected CLI invocation: ' + repr(argv))

        with patch.object(yakala, '_run', side_effect=run):
            return yakala._agent_preflight('codex', ['codex'], self.vault)

    def test_supported_versions_discover_then_disable_without_json_or_model_calls(self):
        for version in ('codex-cli 0.155.1', 'codex-cli 0.160.0\r\n', 'codex-cli 0.163.0'):
            with self.subTest(version=version):
                self.assertEqual(self.preflight(version), ())
                calls = [entry[0] for entry in self.calls]
                tables = [argv for argv in calls if 'features' in argv]
                self.assertEqual(tables[0], ['codex', 'features', 'list'])
                self.assertEqual(len(tables), 2)
                self.assertNotIn('--json', tables[1])
                self.assertNotIn('code_mode', tables[1])
                self.assertNotIn('code_mode_host', tables[1])
                self.assertIn('agents.enabled=false', tables[1])
                for feature in FEATURES:
                    self.assertIn(['--disable', feature],
                                  [tables[1][i:i + 2] for i in range(len(tables[1]))])
                self.assertTrue(all(cwd == str(self.vault) and timeout == 15
                                    for _, timeout, cwd in self.calls))
                self.assertTrue(all('--help' in argv or '--version' in argv or 'list' in argv
                                    for argv in calls))

    def test_unsupported_or_ambiguous_version_stops_before_feature_flags(self):
        for version in ('codex-cli 0.155.0', 'codex-cli 0.154.2', 'codex-cli 0.160.0-alpha.1',
                        'codex-cli 0.164.0-alpha.3',
                        'another CLI 0.160.0', '2.1.302 (Claude Code)', '', '0.160.0'):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'Codex 0.155.1'):
                self.preflight(version)
            self.assertFalse(any('--disable' in argv for argv, _, _ in self.calls))

    def test_missing_strict_config_support_stops_before_loading_a_profile(self):
        with patch.object(yakala, '_run', return_value=(0, '--disable --ask-for-approval', '')) as run:
            with self.assertRaisesRegex(ValueError, 'guvenlik bayraklarini'):
                yakala._agent_preflight('codex', ['codex'], self.vault)
        self.assertEqual(run.call_count, 1)

    def test_every_required_feature_must_be_present_even_if_all_others_are_off(self):
        for feature in FEATURES:
            table = '\n'.join(row for row in feature_table(True).splitlines()
                              if row.split()[0] != feature)
            with self.subTest(feature=feature), self.assertRaisesRegex(ValueError, feature):
                self.preflight(plain=table)
            self.assertFalse(any('--disable' in argv for argv, _, _ in self.calls))

    def test_effective_independent_capability_must_really_be_false_after_disable(self):
        for feature in FEATURES:
            if feature == 'unified_exec':  # independently tested behind the shell_tool master gate
                continue
            rows = feature_table(True).splitlines()
            table = '\n'.join(row.rsplit('false', 1)[0] + 'true'
                              if row.split()[0] == feature else row for row in rows)
            with self.subTest(feature=feature), self.assertRaisesRegex(ValueError, feature):
                self.preflight(effective=table)
            self.assertFalse(any('plugin' in argv or 'mcp' in argv for argv, _, _ in self.calls))

    def test_forced_unified_exec_selector_is_accepted_only_behind_disabled_shell_tool(self):
        def table(enabled):
            return '\n'.join(row.rsplit('false', 1)[0] + 'true' if row.split()[0] in enabled else row
                              for row in feature_table(True).splitlines())
        self.assertEqual(self.preflight(effective=table({'unified_exec'})), ())
        with self.assertRaisesRegex(ValueError, 'shell_tool'):
            self.preflight(effective=table({'unified_exec', 'shell_tool'}))

    def test_required_removed_flag_is_refused_even_when_cli_accepts_it(self):
        table = '\n'.join(line.replace('stable', 'removed') if line.startswith('view_image') else line
                          for line in feature_table(True).splitlines())
        self.assertIn('removed', next(line for line in table.splitlines() if line.startswith('view_image')))
        with self.assertRaisesRegex(ValueError, 'kaldirilmis: view_image'):
            self.preflight(plain=table)

    def test_bad_duplicate_unknown_stage_or_oversized_table_is_refused(self):
        for table in ('', '[]', 'name stage enabled', 'view_image stable yes',
                      feature_table() + 'view_image stable false\n',
                      feature_table() + 'future_feature future-stage false\n',
                      feature_table() + 'x' * (128 * 1024)):
            with self.subTest(table=table[:40]), self.assertRaises(ValueError):
                self.preflight(plain=table)
        with self.assertRaises(ValueError):
            self.preflight(rc=1)

    def test_feature_probe_timeout_is_propagated_without_cli_fallback(self):
        with patch.object(yakala, '_run', side_effect=subprocess.TimeoutExpired('codex features list', 15)):
            with self.assertRaises(subprocess.TimeoutExpired):
                yakala._agent_codex_features(['codex'], self.vault)

    def test_enabled_user_mcp_is_disabled_and_rechecked_for_this_run(self):
        self.assertEqual(self.preflight(mcp_enabled=True), ('local_mcp',))
        calls = [argv for argv, _, _ in self.calls if argv[-3:] == ['mcp', 'list', '--json']]
        self.assertEqual(len(calls), 2)
        self.assertNotIn('mcp_servers.local_mcp.enabled=false', calls[0])
        self.assertIn('mcp_servers.local_mcp.enabled=false', calls[1])
        self.assertFalse(self.config_path.exists())

    def test_mcp_that_remains_enabled_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, 'MCP sunuculari'):
            self.preflight(mcp_enabled=True, fail_mcp_disable=True)

    def test_platform_and_view_matrix_keeps_tool_limits_and_compatible_filesystem_policy(self):
        self.configure('codex')
        _, job, _ = self.queued(url='https://example.com/post')
        names = set()
        for os_name, platform, view in (
                (name, platform, view)
                for name, platform in (('nt', 'win32'), ('posix', 'linux'), ('posix', 'darwin'))
                for view in ('gizli', 'pencere')):
            system = types.SimpleNamespace(**{**vars(yakala.os), 'name': os_name})
            runtime = types.SimpleNamespace(**{**vars(yakala.sys), 'platform': platform})
            with self.subTest(platform=platform, view=view), \
                    patch.object(yakala, 'os', system), patch.object(yakala, 'sys', runtime), \
                    patch.object(yakala, '_agent_window_supported', return_value=True), \
                    patch.object(yakala, '_agent_executable', return_value=['codex']):
                job['config']['gorunum'] = view
                argv = yakala._agent_command(self.vault, self.state, job, job['config'])
                self.assertIn('--strict-config', argv)
                profile = self.assert_codex_filesystem_policy(argv)
                if profile is not None:
                    names.add(profile)
                self.assertEqual(argv[argv.index('-a') + 1], 'never')
                self.assertIn('agents.enabled=false', argv)
                for feature in FEATURES:
                    self.assertIn(['--disable', feature], [argv[i:i + 2] for i in range(len(argv))])
                self.assertIn('mcp_servers.yakala.enabled=true', argv)
                self.assertIn('mcp_servers.yakala.required=true', argv)
                self.assertIn('web_search="disabled"', argv)
                self.assertNotIn('apply_patch_freeform', argv)  # removed, not a security switch
                self.assertNotIn('code_mode', argv)
                self.assertNotIn('code_mode_host', argv)
                self.assertEqual('exec' in argv, view == 'gizli')
                if view == 'pencere':
                    self.assertEqual(argv[-2], '--')
        # A name copied from the user's config must not contribute narrower read/write grants.
        self.assertEqual(len(names), 4)


class CodexPermissionReviewTest(AgentFiles):
    @contextlib.contextmanager
    def posix(self, platform='linux'):
        # Only Codex policy probes/argv use the simulated platform. State/card
        # writes must keep the host OS (Windows cannot open directories to fsync).
        system = types.SimpleNamespace(**{**vars(yakala.os), 'name': 'posix'})
        runtime = types.SimpleNamespace(**{**vars(yakala.sys), 'platform': platform})
        with patch.object(yakala, 'os', system), patch.object(yakala, 'sys', runtime):
            yield

    def preflight(self, *args, **kwargs):
        with self.posix():
            return CodexFeatureReviewTest.preflight(self, *args, **kwargs)

    def job_preflight(self, *args, **kwargs):
        with self.posix():
            return PREFLIGHT(*args, **kwargs)

    def setUp(self):
        super().setUp()
        self.home = self.codex_home.return_value
        self.home.mkdir()
        self.sources = [(self.home / name, name != 'config.toml')
                        for name in ('config.toml', 'managed_config.toml', 'requirements.toml')]
        self.sources += [(Path(self.tmp.name) / 'etc' / 'codex' / name, name != 'config.toml')
                         for name in ('config.toml', 'managed_config.toml', 'requirements.toml')]
        self.codex_config_files.return_value = tuple(self.sources)

    def write_config(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def test_allowlists_in_every_user_and_system_layer_abort_before_feature_probes(self):
        for path, _ in self.sources:
            for text in ('allowed_permission_profiles = [":workspace"]',
                         '"allowed_permission_profiles" = []',
                         "'allowed_permission_profiles' = [':read-only']",
                         '[requirements]\nallowed_permission_profiles = [":workspace"]',
                         '[profiles.work]\nallowed_permission_profiles = [":workspace"]',
                         'allowed_sandbox_modes = ["workspace-write"]',
                         'remote_sandbox_config = {}'):
                with self.subTest(source=path.name, text=text):
                    self.write_config(path, text)
                    try:
                        with self.assertRaisesRegex(ValueError, 'ajan baslatilmadi') as caught:
                            self.preflight()
                        self.assertTrue(str(caught.exception).isascii())
                        self.assertEqual(len(self.calls), 1)  # --help only; no model/run commands
                    finally:
                        path.unlink()

    def test_managed_default_selectors_are_refused_but_user_legacy_defaults_still_work(self):
        for key, value in (('default_permissions', '":workspace"'), ('sandbox_mode', '"workspace-write"')):
            for path, managed in self.sources:
                with self.subTest(key=key, managed=managed):
                    self.write_config(path, key + ' = ' + value)
                    try:
                        if managed:
                            with self.assertRaisesRegex(ValueError, key):
                                self.preflight()
                        else:
                            self.assertEqual(self.preflight(), ())
                    finally:
                        path.unlink()

    def test_missing_and_unrestricted_configs_preserve_the_existing_command_policy(self):
        self.assertEqual(self.preflight(), ())
        for path, _ in self.sources:
            self.write_config(path, '# allowed_permission_profiles = [":workspace"]\n'
                                    'model = "gpt-test"\n'
                                    'notice = "allowed_permission_profiles"\n'
                                    '[permissions.other.filesystem]\n":root" = "write"\n')
        before = {path: path.read_bytes() for path, _ in self.sources}
        self.assertEqual(self.preflight(), ())
        self.assertEqual({path: path.read_bytes() for path, _ in self.sources}, before)
        for view in ('gizli', 'pencere'):
            with patch.object(yakala, '_agent_window_supported', return_value=True):
                self.configure('codex', gorunum=view)
                _, job, _ = self.queued()
                with self.posix(), patch.object(yakala, '_agent_executable', return_value=['codex']):
                    self.assert_codex_filesystem_policy(
                        yakala._agent_command(self.vault, self.state, job, job['config']))

    def test_bad_oversized_or_unreadable_config_is_not_treated_as_missing(self):
        path = self.sources[0][0]
        for data in (b'allowed_permission_profiles = [', b'\xff', b'#' * (1024 * 1024 + 1)):
            with self.subTest(data=data[:40]):
                path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, 'ajan baslatilmadi'):
                    self.preflight()
        path.unlink()
        path.mkdir()  # open fails on every OS, including privileged CI users
        with self.assertRaisesRegex(ValueError, 'okunamadi'):
            self.preflight()
        with patch.object(yakala.Path, 'open', side_effect=PermissionError('private path')):
            with self.assertRaisesRegex(ValueError, 'okunamadi') as caught:
                self.preflight()
        self.assertNotIn('private path', str(caught.exception))

    def test_both_views_stop_the_job_and_leave_an_ascii_explanation_on_the_card(self):
        self.write_config(self.sources[1][0], 'allowed_permission_profiles = [":workspace"]')
        for view in ('gizli', 'pencere'):
            with self.subTest(view=view), \
                    patch.object(yakala, '_agent_window_supported', return_value=True), \
                    patch.object(yakala, '_agent_executable', return_value=['codex']), \
                    patch.object(yakala, '_run', return_value=(0, '--disable --ask-for-approval --strict-config', '')), \
                    patch.object(yakala, '_agent_preflight', side_effect=self.job_preflight), \
                    patch.object(yakala, '_agent_command') as command, \
                    patch.object(yakala, '_agent_prepare_runtime') as runtime, \
                    patch.object(yakala, '_agent_owned_process') as launch, \
                    patch.object(yakala, '_agent_run_window') as window:
                self.configure('codex', gorunum=view)
                _, job, original = self.queued()
                yakala._agent_run_job(self.vault, self.state, job)
                command.assert_not_called()
                runtime.assert_not_called()
                launch.assert_not_called()
                window.assert_not_called()
                stored = yakala._agent_load_job(self.vault, self.state, job['id'])
                card = yakala.find_card(self.vault, original['id'], self.state)
                self.assertEqual(stored['state'], 'approval_required')
                self.assertEqual(card['meta']['durum'], 'onay-bekliyor')
                self.assertIn('allowed_permission_profiles', card['meta']['ajan_notu'])
                self.assertTrue(card['meta']['ajan_notu'].isascii())

    def test_windows_does_not_read_the_profile_policy_and_keeps_read_only(self):
        self.write_config(self.sources[1][0], 'allowed_permission_profiles = [":workspace"]')
        system = types.SimpleNamespace(**{**vars(yakala.os), 'name': 'nt'})
        with patch.object(yakala, 'os', system):
            self.assertEqual(CodexFeatureReviewTest.preflight(self), ())
            self.assertEqual(yakala._agent_codex_permissions(), ['-s', 'read-only'])
        self.codex_config_files.assert_not_called()

    def test_new_managed_policy_created_by_a_cli_probe_is_rechecked(self):
        inventory = yakala._agent_codex_inventory

        def list_inventory(*args, **kwargs):
            result = inventory(*args, **kwargs)
            self.write_config(self.sources[2][0], 'allowed_permission_profiles = [":workspace"]')
            return result

        with patch.object(yakala, '_agent_codex_inventory', side_effect=list_inventory):
            with self.assertRaisesRegex(ValueError, 'allowed_permission_profiles'):
                self.preflight()

    def test_managed_cloud_cache_is_refused_instead_of_guessing_its_schema(self):
        path = self.home / 'cloud-config-bundle-cache.json'
        for data in ('{"requirements_toml": "allowed_permission_profiles = []"}', '{invalid'):
            with self.subTest(data=data):
                path.write_text(data, encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'ajan baslatilmadi'):
                    self.preflight()

    def test_codex_home_and_config_layer_discovery(self):
        system = Path('/etc/codex')
        for env_home in (str(self.home), 'relative-home', '~/custom-codex'):
            with self.subTest(home=env_home), \
                    patch.dict(yakala.os.environ, CODEX_HOME=env_home), \
                    patch.object(yakala.Path, 'home', return_value=self.home.parent), \
                    patch.object(yakala, '_agent_codex_home', side_effect=CODEX_HOME), \
                    patch.object(yakala.os, 'scandir') as scan:
                scan.return_value.__enter__.return_value = ()
                expected = (self.vault / Path(env_home).expanduser()).resolve()
                self.assertEqual(CODEX_HOME(self.vault), expected)
                paths = CONFIG_FILES(self.vault)
                for name in ('config.toml', 'managed_config.toml', 'requirements.toml'):
                    self.assertIn((expected / name, name != 'config.toml'), paths)
                    self.assertIn((system / name, name != 'config.toml'), paths)
                for parent in (self.vault, *self.vault.parents):
                    self.assertIn((parent / '.codex' / 'config.toml', False), paths)
        with patch.dict(yakala.os.environ), patch.object(yakala.Path, 'home', return_value=self.home.parent):
            yakala.os.environ.pop('CODEX_HOME', None)
            self.assertEqual(CODEX_HOME(self.vault), (self.home.parent / '.codex').resolve())

    def test_profile_fragment_is_discovered_and_unreadable_directory_aborts(self):
        self.write_config(self.home / 'work.config.toml', 'allowed_permission_profiles = []')
        scandir = yakala.os.scandir

        def scan(folder):
            # Exercise real discovery only inside this fixture, never /etc or
            # the host user's config directories.
            return scandir(folder if Path(folder) == self.home else self.home / 'missing')

        self.codex_config_files.side_effect = CONFIG_FILES
        with patch.object(yakala.os, 'scandir', side_effect=scan):
            with self.assertRaisesRegex(ValueError, 'allowed_permission_profiles'):
                self.preflight()
        with patch.object(yakala.os, 'scandir', side_effect=PermissionError('private directory')):
            with self.assertRaisesRegex(ValueError, 'konumlari okunamadi.*ajan baslatilmadi') as caught:
                self.preflight()
        self.assertTrue(str(caught.exception).isascii())
        self.assertNotIn('private directory', str(caught.exception))

    def test_macos_managed_preferences_are_refused_and_absence_is_accepted(self):
        for key_index in (None, 0, 1, 2, 3):
            import ctypes
            from unittest.mock import Mock
            cf = Mock()
            cf.CFStringCreateWithCString.return_value = 1
            cf.CFPreferencesCopyAppValue.return_value = 0
            if key_index is not None:
                cf.CFPreferencesCopyAppValue.side_effect = [
                    2 if index == key_index else 0 for index in range(4)]
            with self.subTest(key_index=key_index), self.posix(platform='darwin'), \
                    patch.object(yakala, '_agent_codex_mdm_preflight', side_effect=MDM_PREFLIGHT), \
                    patch.object(ctypes, 'CDLL', return_value=cf):
                if key_index is None:
                    self.assertEqual(CodexFeatureReviewTest.preflight(self), ())
                else:
                    with self.assertRaisesRegex(ValueError, 'macOS.*ajan baslatilmadi'):
                        CodexFeatureReviewTest.preflight(self)
        with self.posix(platform='darwin'), \
                patch.object(yakala, '_agent_codex_mdm_preflight', side_effect=MDM_PREFLIGHT), \
                patch.object(ctypes, 'CDLL', side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(ValueError, 'macOS.*ajan baslatilmadi'):
                CodexFeatureReviewTest.preflight(self)


class AgentClientOptionsReviewTest(AgentFiles):
    def test_switching_claude_to_codex_resets_model_effort_and_executable(self):
        previous_binary = str(Path(self.tmp.name) / 'claude.exe')
        self.configure('claude', extra_args=['--model', 'haiku', '--effort', 'low'],
                       executable=previous_binary, timeout_seconds=180)
        yakala.configure_agent(self.vault, self.state, 'codex')
        config = yakala.agent_settings(self.vault, self.state)
        self.assertEqual(config['extra_args'], [])
        self.assertIsNone(config['executable'])
        self.assertEqual(config['timeout_seconds'], 180)
        _, job, _ = self.queued()
        with patch.object(yakala, '_agent_executable', return_value=['codex']) as executable:
            argv = yakala._agent_command(self.vault, self.state, job, job['config'])
        executable.assert_called_once_with('codex', None)
        self.assertNotIn('haiku', argv)
        self.assertNotIn('--effort', argv)
        self.assertNotIn(previous_binary, argv)

    def test_switching_back_does_not_leak_codex_model_to_claude(self):
        self.configure('codex', extra_args=['--model', 'gpt-test'])
        yakala.configure_agent(self.vault, self.state, 'claude')
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['extra_args'], [])

    def test_explicit_new_options_are_applied_after_client_defaults(self):
        self.configure('claude', extra_args=['--model', 'haiku', '--effort', 'low'])
        binary = str(Path(self.tmp.name) / 'codex.exe')
        self.configure('codex', extra_args=['--model', 'gpt-new'], executable=binary)
        config = yakala.agent_settings(self.vault, self.state)
        self.assertEqual(config['extra_args'], ['--model', 'gpt-new'])
        self.assertEqual(config['executable'], binary)

    def test_same_client_keeps_options_when_changing_shared_settings(self):
        binary = str(Path(self.tmp.name) / 'claude.exe')
        self.configure('claude', extra_args=['--model', 'haiku'], executable=binary)
        yakala.configure_agent(self.vault, self.state, 'claude', network='acik')
        config = yakala.agent_settings(self.vault, self.state)
        self.assertEqual(config['extra_args'], ['--model', 'haiku'])
        self.assertEqual(config['executable'], binary)
        self.assertEqual(config['ag'], 'acik')

    def test_off_then_enable_cannot_reintroduce_previous_cli_options(self):
        self.configure('claude', extra_args=['--model', 'haiku', '--effort', 'low'])
        yakala.configure_agent(self.vault, self.state, 'kapali')
        yakala.configure_agent(self.vault, self.state, 'codex')
        self.assertEqual(yakala.agent_settings(self.vault, self.state)['extra_args'], [])

    def test_switch_does_not_rewrite_already_queued_client_snapshot(self):
        self.configure('claude', extra_args=['--model', 'haiku'])
        _, job, _ = self.queued()
        yakala.configure_agent(self.vault, self.state, 'codex')
        previous = yakala._agent_load_job(self.vault, self.state, job['id'])
        self.assertEqual(previous['client'], 'claude')
        self.assertEqual(previous['config']['extra_args'], ['--model', 'haiku'])


if __name__ == '__main__':
    unittest.main()
