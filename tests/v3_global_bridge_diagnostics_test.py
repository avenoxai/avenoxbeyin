"""Moved global bridge paths are reported without changing user settings (#618)."""
import base64
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest

from v3_package_helpers import ROOT, install, isolated_env, run_python, snapshot


class GlobalBridgeDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-global-bridge-diagnostic-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / 'home'
        self.env = isolated_env(self.home)
        self.old, self.vault = self.root / "Old Brain's Örnek", self.root / 'Moved Brain'
        self.state, self.project = self.root / 'machine state', self.root / 'Project'
        self.old.mkdir()
        self.project.mkdir()
        done = install(self.old, self.state, self.env)
        self.assertEqual(done.returncode, 0, done.stderr)

    def cli(self, vault, *args):
        done = run_python(vault / 'beyin.py', args, self.root, self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, b'')
        return done.stdout.decode('utf-8')

    def bridge(self, vault, harness):
        return self.cli(vault, 'bridge', '--harness', harness, '--project-root', self.project,
                        '--event', 'SessionStart', '--install')

    def move(self):
        self.old.rename(self.vault)
        done = run_python(ROOT / 'scripts/install_v3.py',
                          ['--vault', self.vault, '--state', self.state, '--rebind-state'],
                          self.root, self.env)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_installed_bridge_move_and_explicit_reinstall(self):
        for harness in ('claude', 'codex', 'antigravity'):
            self.bridge(self.old, harness)
        self.move()
        before_home = snapshot(self.home)
        before_vault = snapshot(self.vault)
        report = json.loads(self.cli(self.vault, 'doctor', '--json'))
        self.assertEqual(report['hook_paths']['status'], 'ok')
        global_paths = report.get('global_bridge_paths', {})
        self.assertEqual(global_paths.get('status'), 'stale', global_paths)
        self.assertEqual({row['harness'] for row in global_paths['stale']},
                         {'claude', 'codex', 'antigravity'})
        before_state = snapshot(self.state)
        self.assertEqual(json.loads(self.cli(self.vault, 'doctor'))['global_bridge_paths'], global_paths)
        human = self.cli(self.vault, 'doctor', '--human')
        for harness in ('claude', 'codex', 'antigravity'):
            self.assertIn('bridge --harness ' + harness, human)
        self.assertIn('--install', human)
        self.assertEqual(snapshot(self.home), before_home)
        self.assertEqual(snapshot(self.vault), before_vault)
        self.assertEqual(snapshot(self.state), before_state)
        for harness in ('claude', 'codex', 'antigravity'):
            self.bridge(self.vault, harness)
        recovered = json.loads(self.cli(self.vault, 'doctor'))
        self.assertEqual(recovered['global_bridge_paths']['status'], 'ok')
        self.assertEqual(recovered['status'], report['status'])
        self.assertNotIn('Genel kopru yollari', self.cli(self.vault, 'doctor', '--human'))

    def test_custom_client_configuration_directories(self):
        for harness, key in (('claude', 'CLAUDE_CONFIG_DIR'), ('codex', 'CODEX_HOME')):
            self.env[key] = str(self.home / ('custom ' + harness))
            self.bridge(self.old, harness)
        self.move()
        before = snapshot(self.home)
        report = json.loads(self.cli(self.vault, 'doctor')).get('global_bridge_paths', {})
        self.assertEqual(report.get('status'), 'stale', report)
        self.assertEqual({row['harness'] for row in report['stale']}, {'claude', 'codex'})
        self.assertEqual(snapshot(self.home), before)

    def test_other_state_bridge_and_invalid_or_missing_settings_are_unchanged(self):
        other, other_state = self.root / 'Other Brain', self.root / 'other state'
        other.mkdir()
        done = install(other, other_state, self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.bridge(other, 'claude')
        self.assertNotIn('global_bridge_paths', json.loads(self.cli(self.old, 'doctor')))
        target = self.home / '.claude/settings.json'
        for raw in (b'{invalid', b'[]', b'{"hooks": []}', b'\xff'):
            target.write_bytes(raw)
            self.assertNotIn('global_bridge_paths', json.loads(self.cli(self.old, 'doctor')))
            self.assertEqual(target.read_bytes(), raw)
        target.unlink()
        self.assertNotIn('global_bridge_paths', json.loads(self.cli(self.old, 'doctor')))

    def test_encoded_commands_are_parsed_without_execution(self):
        target = self.home / '.codex/hooks.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        marker = self.root / 'must-not-run'
        arguments = [sys.executable, str(self.old / '.claude/scripts/beyin_v3_bridge.py'),
                     '--vault', str(self.old), '--state', str(self.state), '--harness', 'codex']
        powershell = '& ' + ' '.join("'" + arg.replace("'", "''") + "'" for arg in arguments)
        powershell += "; New-Item '" + str(marker).replace("'", "''") + "'"
        command = 'powershell.exe -EncodedCommand ' + base64.b64encode(powershell.encode('utf-16le')).decode()
        target.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [
            {'type': 'command', 'command': command, 'commandWindows': command}]}]}}), encoding='utf-8')
        self.move()
        before = snapshot(self.home)
        report = json.loads(self.cli(self.vault, 'doctor')).get('global_bridge_paths', {})
        self.assertEqual(report.get('status'), 'stale', report)
        self.assertEqual(report['checked'], 1)
        self.assertFalse(marker.exists())
        self.assertEqual(snapshot(self.home), before)
        self.assertNotIn(str(self.root), json.dumps(report))

    def test_duplicate_or_incomplete_options_are_not_guessed(self):
        target = self.home / '.claude/settings.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        script = str(self.old / '.claude/scripts/beyin_v3_bridge.py')
        for arguments in ([sys.executable, script, '--vault', str(self.old), '--state'],
                          [sys.executable, script, '--vault', str(self.old), '--state', str(self.state),
                           '--state', str(self.state)],
                          [sys.executable, script, '--state', str(self.state)],
                          [sys.executable, script, '--vault', str(self.old), '--state', 'relative-state']):
            command = shlex.join(arguments)
            target.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [
                {'command': command, 'commandWindows': command}]}]}}), encoding='utf-8')
            with self.subTest(arguments=arguments):
                self.assertNotIn('global_bridge_paths', json.loads(self.cli(self.old, 'doctor')))

    def test_reinstalled_bridge_accepts_a_symlinked_scripts_directory(self):
        scripts = self.old / '.claude/scripts'
        destination = self.old / 'runtime-scripts'
        scripts.rename(destination)
        try:
            scripts.symlink_to(destination, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('directory symlinks unavailable: ' + type(exc).__name__)
        for harness in ('claude', 'codex', 'antigravity'):
            self.bridge(self.old, harness)
        before_home, before_vault = snapshot(self.home), snapshot(self.old)
        report = json.loads(self.cli(self.old, 'doctor')).get('global_bridge_paths', {})
        self.assertEqual(report.get('status'), 'ok', report)
        self.assertEqual(snapshot(self.home), before_home)
        self.assertEqual(snapshot(self.old), before_vault)

    @unittest.skipIf(os.name == 'nt', 'POSIX file permissions required')
    def test_unreadable_optional_settings_do_not_change_doctor(self):
        self.bridge(self.old, 'claude')
        target = self.home / '.claude/settings.json'
        raw = target.read_bytes()
        mode = target.stat().st_mode
        target.chmod(0)
        self.addCleanup(target.chmod, mode)
        try:
            target.read_bytes()
        except PermissionError:
            pass
        else:
            self.skipTest('current user bypasses file permissions')
        self.assertNotIn('global_bridge_paths', json.loads(self.cli(self.old, 'doctor')))
        target.chmod(mode)
        self.assertEqual(target.read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
