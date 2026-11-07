"""Opt-in parallel-session notice (#170), exercised through a real install and the installed hook."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
NOTICE = '[Paralel oturum]'
MARKER_KEYS = {'schema', 'harness', 'session', 'first_at', 'last_at', 'announced'}
PROMPT_CANARY = 'PROMPT_TEXT_CANARY_NEVER_IN_A_MARKER'


def receipt_short(session_id):
    return hashlib.sha256(session_id.encode()).hexdigest()[:8]


def marker_name(harness, session_id):
    return hashlib.sha256((harness + '\0' + session_id).encode()).hexdigest() + '.json'


class ParallelSessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-parallel-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault, self.state = self.root / 'Örnek Beyin', self.root / 'state'
        self.vault.mkdir()
        self.markers = self.state / 'session-markers'
        self.count = 0
        self.env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        self.env.pop('BEYIN_V3_FILTER_HARNESS_TURNS', None)
        # A real install: the hook under test is the installed copy, not the template.
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state)], capture_output=True, text=True, encoding='utf-8',
                                env=self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_cli.py'), '--vault',
                                 str(self.vault), '--state', str(self.state), *args], capture_output=True, text=True,
                                encoding='utf-8', env=self.env, cwd=self.vault, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def hook(self, event, session, harness='claude', prompt='bugun ne yapalim', **fields):
        self.count += 1
        payload = dict({'hook_event_name': event, 'session_id': session, 'event_id': f'{session}-{self.count}',
                        'cwd': str(self.vault)}, **fields)
        if prompt is not None:
            payload['prompt'] = prompt
        result = subprocess.run([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'), '--vault',
                                 str(self.vault), '--state', str(self.state), '--harness', harness],
                                input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8',
                                env=self.env, cwd=self.vault, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout)
        return json.loads(lines[0])

    @staticmethod
    def text(output):
        specific = output.get('hookSpecificOutput') or {}
        steps = output.get('injectSteps') or [{}]
        return specific.get('additionalContext') or steps[0].get('ephemeralMessage') or ''

    def notice(self, output):
        return [line for line in self.text(output).splitlines() if line.startswith(NOTICE)]

    def enable(self):
        self.assertEqual(self.cli('preferences', '--parallel-sessions', 'on')['parallel_sessions'], 'on')

    def age(self, harness, session, seconds):
        path = self.markers / marker_name(harness, session)
        value = json.loads(path.read_text(encoding='utf-8'))
        then = time.time() - seconds
        value.update(first_at=then, last_at=then)
        path.write_text(json.dumps(value), encoding='utf-8')
        os.utime(path, (then, then))

    def test_two_sessions_see_each_other_once_with_receipt_session_ids(self):
        self.enable()
        self.assertEqual(self.notice(self.hook('UserPromptSubmit', 'session-a', prompt=PROMPT_CANARY)), [])
        lines = self.notice(self.hook('UserPromptSubmit', 'session-b'))
        self.assertEqual(lines, ["[Paralel oturum] Bu vault'ta 1 oturum daha acik: #" + receipt_short('session-a') +
                                 ' (az once). Ayni dosyaya dokunmadan once diskten yeniden oku; commit oncesi git status.'])
        self.assertTrue(lines[0].isascii())
        # The second prompt repeats nothing; A learns of B once, on its own next prompt.
        self.assertEqual(self.notice(self.hook('UserPromptSubmit', 'session-b')), [])
        self.assertIn('#' + receipt_short('session-b'), self.notice(self.hook('UserPromptSubmit', 'session-a'))[0])
        self.assertEqual(self.notice(self.hook('UserPromptSubmit', 'session-a')), [])
        # A session opened later is announced once, alone.
        self.hook('UserPromptSubmit', 'session-c', harness='codex')
        later = self.notice(self.hook('UserPromptSubmit', 'session-b'))
        self.assertEqual(len(later), 1)
        self.assertIn("1 oturum daha acik: #" + receipt_short('session-c'), later[0])
        self.assertNotIn(receipt_short('session-a'), later[0])
        # Markers carry the six fields only: no prompt, no cwd, no raw session id.
        for path in self.markers.iterdir():
            raw = path.read_text(encoding='utf-8')
            self.assertEqual(set(json.loads(raw)), MARKER_KEYS)
            for secret in (PROMPT_CANARY, str(self.vault), 'Örnek', 'session-a'):
                self.assertNotIn(secret, raw)
        self.assertTrue((self.markers / marker_name('claude', 'session-a')).is_file())
        self.assertTrue((self.markers / marker_name('codex', 'session-c')).is_file())

    def test_at_most_two_named_and_the_rest_counted(self):
        self.enable()
        for session in ('s-1', 's-2', 's-3'):
            self.hook('UserPromptSubmit', session)
        lines = self.notice(self.hook('UserPromptSubmit', 's-4'))
        self.assertEqual(len(lines), 1)
        self.assertIn('3 oturum daha acik', lines[0])
        self.assertEqual(lines[0].count('#'), 2)
        self.assertIn('ve 1 tane daha', lines[0])
        self.assertEqual(self.notice(self.hook('UserPromptSubmit', 's-4')), [])

    def test_stale_marker_is_ignored_and_a_day_old_one_pruned(self):
        self.enable()
        self.hook('UserPromptSubmit', 'old-session')
        self.hook('UserPromptSubmit', 'ancient-session')
        self.age('claude', 'old-session', 46 * 60)
        self.age('claude', 'ancient-session', 25 * 3600)
        self.assertEqual(self.notice(self.hook('UserPromptSubmit', 'new-session')), [])
        self.assertTrue((self.markers / marker_name('claude', 'old-session')).is_file())
        self.assertFalse((self.markers / marker_name('claude', 'ancient-session')).exists())

    def test_session_end_removes_only_its_own_marker(self):
        self.enable()
        self.hook('UserPromptSubmit', 'ending')
        self.hook('UserPromptSubmit', 'staying')
        self.assertEqual(self.hook('SessionEnd', 'ending', prompt=None), {})
        self.assertFalse((self.markers / marker_name('claude', 'ending')).exists())
        self.assertTrue((self.markers / marker_name('claude', 'staying')).is_file())
        fresh = self.notice(self.hook('UserPromptSubmit', 'fresh'))
        self.assertIn('1 oturum daha acik: #' + receipt_short('staying'), fresh[0])
        # A ghost after /clear is gone even when the notice was switched off in between.
        self.hook('UserPromptSubmit', 'cleared')
        self.cli('preferences', '--parallel-sessions', 'off')
        self.hook('SessionEnd', 'cleared', prompt=None)
        self.assertFalse((self.markers / marker_name('claude', 'cleared')).exists())

    def test_synthetic_turn_neither_writes_nor_announces(self):
        self.enable()
        self.hook('UserPromptSubmit', 'human')
        output = self.hook('UserPromptSubmit', 'notified', prompt='<task-notification>subagent done</task-notification>')
        self.assertEqual(self.notice(output), [])
        self.assertFalse((self.markers / marker_name('claude', 'notified')).exists())
        output = self.hook('UserPromptSubmit', 'human', prompt='<agent-message from="peer">hi</agent-message>')
        self.assertEqual(self.notice(output), [])
        self.assertEqual(json.loads((self.markers / marker_name('claude', 'human')).read_text())['announced'], [])

    def test_off_by_default_and_off_output_matches_no_feature(self):
        def scenario():
            outputs = [self.hook('UserPromptSubmit', 'off-a'), self.hook('UserPromptSubmit', 'off-b'),
                       self.hook('SessionStart', 'off-h', harness='hermes', prompt='merhaba'),
                       self.hook('SessionEnd', 'off-a', prompt=None)]
            return [json.dumps(output, sort_keys=True) for output in outputs]
        self.assertEqual(self.cli('preferences')['parallel_sessions'], 'off')
        default = scenario()
        self.assertFalse(self.markers.exists(), 'default install writes no marker')
        self.assertFalse((self.vault / '.presence').exists(), 'default off never writes synced presence')
        self.assertFalse((self.state / 'parallel-presence-device.json').exists())
        self.cli('preferences', '--parallel-sessions', 'off')
        self.assertEqual(json.loads((self.state / 'parallel-sessions.json').read_text()), {'schema': 1, 'enabled': False})
        self.assertEqual(scenario(), default)
        self.assertFalse(self.markers.exists())
        self.assertFalse(any(NOTICE in line for line in default))
        self.assertFalse((self.vault / '.beyin-preferences.json').exists(), 'never a vault preference key')

    def test_first_prompt_at_session_start_harnesses(self):
        self.enable()
        self.hook('UserPromptSubmit', 'claude-session')
        # Claude and Codex SessionStart carry no prompt yet: nothing written or announced.
        self.assertEqual(self.notice(self.hook('SessionStart', 'claude-start', prompt=None, source='startup')), [])
        self.assertFalse((self.markers / marker_name('claude', 'claude-start')).exists())
        hermes = self.notice(self.hook('SessionStart', None, harness='hermes', prompt='merhaba', conversationId='hermes-1'))
        self.assertEqual(len(hermes), 1)
        self.assertIn('#' + receipt_short('claude-session'), hermes[0])
        self.assertTrue((self.markers / marker_name('hermes', 'hermes-1')).is_file())
        opencode = self.notice(self.hook('SessionStart', 'opencode-1', harness='opencode', prompt='selam'))
        self.assertIn('2 oturum daha acik', opencode[0])
        antigravity = self.hook('PreInvocation', None, harness='antigravity', prompt=None, conversationId='ag-1',
                                invocationNum=0)
        self.assertIn('3 oturum daha acik', self.notice(antigravity)[0])
        self.assertTrue((self.markers / marker_name('antigravity', 'ag-1')).is_file())

    def test_prune_keeps_at_most_128_markers(self):
        self.enable()
        self.markers.mkdir(parents=True, exist_ok=True)
        then = time.time() - 3600
        for index in range(140):
            path = self.markers / (hashlib.sha256(str(index).encode()).hexdigest() + '.json')
            path.write_text('{}', encoding='utf-8')
            os.utime(path, (then - index, then - index))
        self.hook('UserPromptSubmit', 'pruner')
        names = {path.name for path in self.markers.iterdir()}
        self.assertEqual(len(names), 128)
        self.assertIn(marker_name('claude', 'pruner'), names)
        self.assertIn(hashlib.sha256(b'0').hexdigest() + '.json', names, 'the newest markers stay')
        self.assertNotIn(hashlib.sha256(b'139').hexdigest() + '.json', names, 'the oldest go first')

    def test_damaged_markers_and_settings_fail_open(self):
        self.enable()
        self.hook('UserPromptSubmit', 'valid')
        self.markers.joinpath('0' * 64 + '.json').write_text('{not json', encoding='utf-8')
        self.markers.joinpath('1' * 64 + '.json').write_bytes(b'\xff\xfe\x00broken')
        self.markers.joinpath('2' * 64 + '.json').write_text(json.dumps({'schema': 1, 'session': 'x'}), encoding='utf-8')
        own = self.markers / marker_name('claude', 'damaged-own')
        own.write_text('[]', encoding='utf-8')
        lines = self.notice(self.hook('UserPromptSubmit', 'damaged-own'))
        self.assertEqual(len(lines), 1)
        self.assertIn('1 oturum daha acik: #' + receipt_short('valid'), lines[0])
        self.assertEqual(set(json.loads(own.read_text(encoding='utf-8'))), MARKER_KEYS, 'a damaged own marker is replaced')
        # A damaged switch reads as off: no line, no crash, reported by preferences and doctor.
        (self.state / 'parallel-sessions.json').write_text('{"enabled": "yes"}', encoding='utf-8')
        self.assertEqual(self.notice(self.hook('UserPromptSubmit', 'after-damage')), [])
        self.assertFalse((self.markers / marker_name('claude', 'after-damage')).exists())
        prefs = self.cli('preferences')
        self.assertEqual(prefs['parallel_sessions'], 'off')
        self.assertIn('gecersiz', prefs['parallel_sessions_notice'])
        self.assertFalse(self.cli('doctor')['parallel_sessions']['valid'])
        # A marker folder that is a symlink is never followed.
        if hasattr(os, 'symlink'):
            self.enable()
            elsewhere = self.root / 'elsewhere'
            elsewhere.mkdir()
            moved = self.root / 'moved-markers'
            self.markers.rename(moved)
            try:
                os.symlink(elsewhere, self.markers, target_is_directory=True)
            except OSError:
                return
            self.assertEqual(self.notice(self.hook('UserPromptSubmit', 'linked')), [])
            self.assertEqual(list(elsewhere.iterdir()), [])

    def test_claude_ceiling_holds_with_the_notice(self):
        companion = next(path for path in self.vault.iterdir() if path.name.endswith('Companion'))
        rules = '# Kurallar\n' + ''.join(f'- kural {i}: ayrintili bir calisma kuralinin tam metni burada durur.\n'
                                         for i in range(400))
        (companion / 'Kurallar.md').write_text(rules, encoding='utf-8')
        self.cli('preferences', '--context-chars', '12000', '--companion-context-chars', '24000')
        self.enable()
        self.cli('sync')
        self.hook('UserPromptSubmit', 'other')
        output = self.hook('UserPromptSubmit', 'asking', prompt='nerede kalmistik?')
        text = self.text(output)
        self.assertTrue(text.startswith(NOTICE), text[:200])
        self.assertGreater(len(text), 5000, 'the opening context still arrives')
        self.assertLessEqual(len(text), 9500)  # client_budget: 10000 minus headroom
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 10000)  # Claude Code's own measure (#175)

    def test_preferences_and_doctor_are_machine_local_and_read_only(self):
        released = {'auto_sync', 'interval_minutes', 'context_mode', 'context_chars', 'secret_filter'}
        self.assertEqual(self.cli('preferences')['parallel_sessions'], 'off')
        doctor = self.cli('doctor')['parallel_sessions']
        self.assertEqual(doctor, {'enabled': False, 'valid': True, 'markers': 0, 'active': 0})
        self.enable()
        self.cli('preferences', '--profile', 'economical')
        self.assertEqual(set(json.loads((self.vault / '.beyin-preferences.json').read_text(encoding='utf-8'))), released)
        self.hook('UserPromptSubmit', 'doctor-a')
        before = {path.name: path.read_bytes() for path in self.markers.iterdir()}
        doctor = self.cli('doctor')['parallel_sessions']
        self.assertEqual(doctor, {'enabled': True, 'valid': True, 'markers': 1, 'active': 1})
        self.assertEqual({path.name: path.read_bytes() for path in self.markers.iterdir()}, before)
        human = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'preferences', '--human'],
                               capture_output=True, text=True, encoding='utf-8', env=self.env, cwd=self.vault,
                               timeout=60)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn('Paralel oturum bildirimi: acik', human.stdout)

    def test_prune_cleans_dead_temp_files(self):
        self.enable()
        self.markers.mkdir(parents=True, exist_ok=True)
        then = time.time()
        for index in range(130):
            path = self.markers / f".marker-dead{index}.tmp"
            path.write_text('{}', encoding='utf-8')
            os.utime(path, (then, then))

        self.hook('UserPromptSubmit', 'pruner')

        names = {path.name for path in self.markers.iterdir()}
        self.assertLessEqual(len(names), 128)

    def test_cross_machine_presence_file_detected_and_cleared_on_session_end(self):
        self.enable()
        state_b = self.root / 'state-machine-b'
        from beyin_v3_parallel import save_settings, receipt_session
        save_settings(state_b, True)

        # Machine A starts a session
        self.hook('UserPromptSubmit', 'session-on-mac')
        presence_file = self.vault / '.presence' / (receipt_session('session-on-mac') + '.json')
        self.assertTrue(presence_file.is_file(), "Presence file must be created in vault/.presence")
        pdata = json.loads(presence_file.read_text(encoding='utf-8'))
        self.assertEqual(pdata['schema'], 1)
        self.assertEqual(pdata['session'], receipt_session('session-on-mac'))
        self.assertEqual(pdata['harness'], 'claude')

        # Machine B (distinct state directory) submits a prompt on the same vault
        payload_b = {
            'hook_event_name': 'UserPromptSubmit', 'session_id': 'session-on-win',
            'event_id': 'win-prompt-1', 'cwd': str(self.vault), 'prompt': 'test prompt'
        }
        res_b = subprocess.run(
            [sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'),
             '--vault', str(self.vault), '--state', str(state_b), '--harness', 'codex'],
            input=json.dumps(payload_b), capture_output=True, text=True, encoding='utf-8',
            env=self.env, cwd=self.vault, timeout=60
        )
        self.assertEqual(res_b.returncode, 0, res_b.stderr)
        lines_b = self.notice(json.loads(res_b.stdout.strip()))
        self.assertEqual(len(lines_b), 1)
        self.assertIn("1 oturum daha acik: #" + receipt_short('session-on-mac'), lines_b[0])

        # Machine A ends its session
        self.hook('SessionEnd', 'session-on-mac', prompt=None)
        self.assertFalse(presence_file.exists(), "SessionEnd must cleanly remove presence file")

        # Machine B submits another prompt; no parallel session notice
        payload_b2 = {
            'hook_event_name': 'UserPromptSubmit', 'session_id': 'session-on-win',
            'event_id': 'win-prompt-2', 'cwd': str(self.vault), 'prompt': 'test prompt 2'
        }
        res_b2 = subprocess.run(
            [sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'),
             '--vault', str(self.vault), '--state', str(state_b), '--harness', 'codex'],
            input=json.dumps(payload_b2), capture_output=True, text=True, encoding='utf-8',
            env=self.env, cwd=self.vault, timeout=60
        )
        self.assertEqual(res_b2.returncode, 0, res_b2.stderr)
        lines_b2 = self.notice(json.loads(res_b2.stdout.strip()))
        self.assertEqual(lines_b2, [])

    def test_cross_machine_stale_presence_file_ignored(self):
        self.enable()
        from beyin_v3_parallel import receipt_session
        pfolder = self.vault / '.presence'
        pfolder.mkdir(parents=True, exist_ok=True)
        # Simulate a remote machine's presence file that is 46 minutes old (crashed session)
        remote_session = receipt_session('remote-crashed')
        remote_file = pfolder / f"{remote_session}.json"
        then = time.time() - 46 * 60
        remote_file.write_text(json.dumps({
            'schema': 1, 'session': remote_session, 'harness': 'claude',
            'last_at': then, 'state': 'foreign-state-key-xyz'
        }), encoding='utf-8')
        os.utime(remote_file, (then, then))

        # Local prompt should NOT announce the stale remote session
        lines = self.notice(self.hook('UserPromptSubmit', 'local-user'))
        self.assertEqual(lines, [])


class ParallelModuleTest(unittest.TestCase):
    """In-process checks for the failure paths a subprocess cannot reach."""

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(sys.path.remove, str(SCRIPTS))
        import beyin_v3_parallel
        self.module = beyin_v3_parallel
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-parallel-module-')
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def test_windows_sharing_violation_is_skipped_silently_and_never_announced_twice(self):
        self.module.touch(self.state, 'claude', 'first')
        with patch.object(self.module.os, 'replace', side_effect=PermissionError(32, 'sharing violation')):
            self.assertEqual(self.module.touch(self.state, 'codex', 'second'), '')
        self.assertEqual(sorted(p.suffix for p in (self.state / 'session-markers').iterdir()), ['.json'],
                         'no temporary file is left behind')
        self.assertIn('#' + receipt_short('first'), self.module.touch(self.state, 'codex', 'second'))
        self.assertEqual(self.module.touch(self.state, 'codex', 'second'), '')

    def test_failed_refresh_of_an_existing_marker_announces_nothing_and_once_later(self):
        # The session already has a marker; a later sharing violation must not announce what
        # it could not record, or every prompt during the lock would repeat the line.
        self.module.touch(self.state, 'codex', 'second')
        self.module.touch(self.state, 'claude', 'first')
        with patch.object(self.module.os, 'replace', side_effect=PermissionError(32, 'sharing violation')):
            self.assertEqual(self.module.touch(self.state, 'codex', 'second'), '')
            self.assertEqual(self.module.touch(self.state, 'codex', 'second'), '')
        self.assertIn('#' + receipt_short('first'), self.module.touch(self.state, 'codex', 'second'))
        self.assertEqual(self.module.touch(self.state, 'codex', 'second'), '')

    def test_unknown_or_missing_session_writes_nothing(self):
        for session in (None, '', 'unknown', 'x' * 513):
            self.assertEqual(self.module.touch(self.state, 'claude', session), '')
        self.assertFalse((self.state / 'session-markers').exists())

    def test_devices_with_identical_state_paths_keep_remote_session(self):
        vault = self.state / 'vault'
        vault.mkdir()
        state = self.state / 'device-state'
        state.mkdir()
        self.module.touch(state, 'claude', 'device-a-session', vault=vault)
        device_a = self.module._state_key(state)
        own_a = vault / '.presence' / (self.module.receipt_session('device-a-session') + '.json')
        before = own_a.read_bytes()
        # Two devices can share the same username, mount and vault pathname. Their
        # independent runtime roots have different identity files, even at this spelling.
        state.rename(self.state / 'saved-device-a')
        state.mkdir()
        notice = self.module.touch(state, 'codex', 'device-b-session', vault=vault)
        self.assertNotEqual(device_a, self.module._state_key(state))
        self.assertIn('#' + receipt_short('device-a-session'), notice)
        self.assertEqual(own_a.read_bytes(), before)
        self.assertEqual(self.module.doctor(state, vault=vault)['active'], 2)

    def test_doctor_does_not_create_presence_identity(self):
        self.module.doctor(self.state, vault=self.state)
        self.assertFalse((self.state / 'parallel-presence-device.json').exists())
        self.assertFalse((self.state / '.presence').exists())

    def test_continuity_malformed_id_type_error(self):
        from beyin_v3 import MemoryStore
        from beyin_v3_continuity import resolve

        temporary = tempfile.TemporaryDirectory(prefix='v3-continuity-invalid-id-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        vault = root / 'vault'
        vault.mkdir()
        store = MemoryStore(root / 'state', vault)
        self.addCleanup(store.close)
        source = vault / 'topic.md'
        source.write_text('Quartz deployment rollback procedure.', encoding='utf-8')
        store.ingest(dict(id='topic', source='topic.md', text=source.read_text(encoding='utf-8')))
        session, harness = 'test-crash', 'claude'
        folder = store.state_dir / 'topic-refs'
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256((harness + '\0' + session).encode()).hexdigest()
        path = folder / (digest + '.json')
        for ident in (['list', 'unhashable'], {'id': 'topic'}, None, 1, True, ''):
            with self.subTest(ident=ident):
                path.write_text(json.dumps(dict(version=1, at=time.time(), project=None,
                                                refs=[{'id': ident}])), encoding='utf-8')
                before = path.read_bytes()
                self.assertEqual(resolve(store, harness, session, 'bunu devam ettir',
                                         'local', budget_chars=1000), ('local', False))
                self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
