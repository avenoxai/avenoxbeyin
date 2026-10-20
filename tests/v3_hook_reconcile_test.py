#!/usr/bin/env python3
"""Offline tests for explicit hook reconciliation (#205)."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import clean_environ, isolated_path, inherited_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "template/.claude/scripts"
INSTALLER = ROOT / "scripts/install_v3.py"
CLI = ROOT / "scripts/beyin_v3.py"
ENTRY = ROOT / "scripts/beyin_entry.py"


def decoded_command(command):
    import base64
    import re
    match = re.search(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", command, re.IGNORECASE)
    return base64.b64decode(match.group(1)).decode("utf-16le") if match else command


class HookReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="v3-hook-reconcile-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "Synthetic Beyin Space"
        self.vault.mkdir()
        self.state = self.root / "state outside vault"
        home = self.root / "isolated-home"
        home.mkdir()
        self.env = {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "appdata"),
            "LOCALAPPDATA": str(home / "localappdata"),
            "TEMP": str(self.root),
            "TMP": str(self.root),
            "PATH": isolated_path(),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "BEYIN_V3_NO_SPAWN": "1",
        }
        for key in ("SYSTEMROOT", "WINDIR"):
            if key in os.environ:
                self.env[key] = os.environ[key]
        self.env = inherited_env(**self.env)
        environment = clean_environ()
        environment.start()
        self.addCleanup(environment.stop)
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))

    def install(self):
        result = subprocess.run(
            [sys.executable, str(INSTALLER), "--vault", str(self.vault), "--state", str(self.state)],
            capture_output=True, text=True, encoding="utf-8", cwd=self.vault, env=self.env, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def doctor_report(self):
        doctor = subprocess.run(
            [sys.executable, str(CLI), "--vault", str(self.vault), "--state", str(self.state), "doctor"],
            capture_output=True, text=True, encoding="utf-8", cwd=self.vault, env=self.env, timeout=30,
        )
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        return json.loads(doctor.stdout)

    def stale_hook_commands(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("v3_hook_installer", INSTALLER)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        old = self.root / "other account" / "Old Vault"
        return installer.commands([
            str(self.root / "other account" / "python.exe"),
            str(old / ".claude/scripts/beyin_v3_hook.py"),
            "--vault", str(old),
            "--state", str(self.root / "other account" / "state"),
            "--harness", "claude",
        ])

    def test_hook_reconcile_noop_when_valid(self):
        self.install()
        # Hooks are valid initially; reconcile should do nothing
        import importlib.util
        spec = importlib.util.spec_from_file_location("v3_cli", CLI)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        result = cli.hook_reconcile(self.vault, self.state)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reconciled_count"], 0)
        self.assertEqual(result["files"], [])

    def test_hook_reconcile_fixes_stale_hooks_and_creates_backup(self):
        self.install()
        stale_posix, stale_win = self.stale_hook_commands()
        stale_cmd = stale_win if os.name == "nt" else stale_posix

        claude_settings = self.vault / ".claude/settings.local.json"
        data = json.loads(claude_settings.read_text(encoding="utf-8"))
        user_hook = {"type": "command", "command": "echo user-owned-custom-hook"}
        data["hooks"]["SessionStart"] = [
            {"hooks": [{"type": "command", "command": stale_cmd, "timeout": 20}]},
            {"hooks": [user_hook]},
        ]
        claude_settings.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Doctor must report stale
        report = self.doctor_report()
        self.assertEqual(report["status"], "needs_attention")
        self.assertEqual(report["hook_paths"]["status"], "stale")

        # Run hook-reconcile
        result = subprocess.run(
            [sys.executable, str(CLI), "--vault", str(self.vault), "--state", str(self.state), "hook-reconcile"],
            capture_output=True, text=True, encoding="utf-8", cwd=self.vault, env=self.env, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        recon_data = json.loads(result.stdout)
        self.assertEqual(recon_data["status"], "reconciled")
        self.assertGreaterEqual(recon_data["reconciled_count"], 1)

        # Verify backup exists and contains stale command
        backup_file = self.vault / ".claude/settings.local.json.bak"
        self.assertTrue(backup_file.is_file(), "Backup file must be created")
        backup_data = json.loads(backup_file.read_text(encoding="utf-8"))
        backup_commands = [h["command"] for g in backup_data["hooks"]["SessionStart"] for h in g["hooks"]]
        self.assertIn(stale_cmd, backup_commands)

        # Verify updated settings file
        new_data = json.loads(claude_settings.read_text(encoding="utf-8"))
        new_commands = [h["command"] for g in new_data["hooks"]["SessionStart"] for h in g["hooks"]]
        self.assertNotIn(stale_cmd, new_commands)
        # User hook is completely preserved
        self.assertIn(user_hook["command"], new_commands)

        # Doctor now reports ok
        report_after = self.doctor_report()
        self.assertEqual(report_after["hook_paths"]["status"], "ok")

    def test_hook_reconcile_human_output_and_doctor_hint(self):
        self.install()
        stale_posix, stale_win = self.stale_hook_commands()
        stale_cmd = stale_win if os.name == "nt" else stale_posix

        claude_settings = self.vault / ".claude/settings.local.json"
        data = json.loads(claude_settings.read_text(encoding="utf-8"))
        data["hooks"]["SessionStart"] = [
            {"hooks": [{"type": "command", "command": stale_cmd, "timeout": 20}]}
        ]
        claude_settings.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Doctor human output contains reconciliation hint
        doc_res = subprocess.run(
            [sys.executable, str(self.vault / "beyin.py"), "doctor", "--human"],
            capture_output=True, text=True, encoding="utf-8", cwd=self.vault, env=self.env, timeout=30,
        )
        doc_output = doc_res.stdout + doc_res.stderr
        self.assertIn("hook-reconcile", doc_output)

        # Run beyin.py hook-reconcile --human
        rec_res = subprocess.run(
            [sys.executable, str(self.vault / "beyin.py"), "hook-reconcile", "--human"],
            capture_output=True, text=True, encoding="utf-8", cwd=self.vault, env=self.env, timeout=30,
        )
        rec_output = rec_res.stdout + rec_res.stderr
        self.assertEqual(rec_res.returncode, 0, rec_res.stderr)
        self.assertIn("Hook mutabakati tamamlandi", rec_output)
        self.assertIn(".claude/settings.local.json", rec_output)
        self.assertIn(".bak", rec_output)

    def test_hook_reconcile_installer_flag(self):
        self.install()
        stale_posix, stale_win = self.stale_hook_commands()
        stale_cmd = stale_win if os.name == "nt" else stale_posix

        claude_settings = self.vault / ".claude/settings.local.json"
        data = json.loads(claude_settings.read_text(encoding="utf-8"))
        data["hooks"]["SessionStart"] = [
            {"hooks": [{"type": "command", "command": stale_cmd, "timeout": 20}]}
        ]
        claude_settings.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Run install_v3.py --reconcile-hooks
        result = subprocess.run(
            [sys.executable, str(INSTALLER), "--vault", str(self.vault), "--state", str(self.state), "--reconcile-hooks"],
            capture_output=True, text=True, encoding="utf-8", cwd=self.vault, env=self.env, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        recon_data = json.loads(result.stdout)
        self.assertEqual(recon_data["status"], "reconciled")
        self.assertTrue((self.vault / ".claude/settings.local.json.bak").is_file())

    def test_force_preserves_custom_managed_group_and_every_old_backup(self):
        self.install()
        path = self.vault / '.agents/hooks.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        custom = {'command': 'echo user-owned-agent-command', 'timeout': 77}
        data['beyin-v3']['PreInvocation'].append(custom)
        path.write_text(json.dumps(data), encoding='utf-8')
        backup = path.with_name(path.name + '.bak')
        backup.write_bytes(b'Old backup is user data')
        def reconcile():
            result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'hook-reconcile', '--force'],
                                    env=self.env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        first = reconcile()
        self.assertEqual(backup.read_bytes(), b'Old backup is user data')
        first_archive = path.with_name(path.name + '.bak.1')
        archived = first_archive.read_bytes()
        reconcile()
        self.assertEqual(first_archive.read_bytes(), archived)
        self.assertTrue(path.with_name(path.name + '.bak.2').is_file())
        self.assertIn(custom, json.loads(path.read_text(encoding='utf-8'))['beyin-v3']['PreInvocation'])

    def test_symlinked_hook_config_is_preserved(self):
        self.install()
        path = self.vault / '.codex/hooks.json'
        outside = self.root / 'outside-hooks.json'
        path.rename(outside)
        try:
            path.symlink_to(outside)
        except OSError:
            outside.rename(path)
            self.skipTest('symlink unavailable')
        raw = outside.read_bytes()
        result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'hook-reconcile', '--force'],
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(path.is_symlink())
        self.assertEqual(outside.read_bytes(), raw)

    def test_installer_reconcile_without_state_uses_existing_custom_pin(self):
        self.install()
        stale_posix, stale_win = self.stale_hook_commands()
        path = self.vault / '.claude/settings.local.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['hooks']['SessionStart'][0]['hooks'][0]['command'] = stale_win if os.name == 'nt' else stale_posix
        path.write_text(json.dumps(data), encoding='utf-8')
        result = subprocess.run([sys.executable, str(INSTALLER), '--vault', str(self.vault), '--reconcile-hooks'],
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        command = json.loads(path.read_text(encoding='utf-8'))['hooks']['SessionStart'][0]['hooks'][0]['command']
        self.assertIn(str(self.state.resolve()), decoded_command(command))

    def test_reconciled_vault_uninstalls_cleanly(self):
        # Issue #353: install, simulate stale hook or reconcile, verify uninstall succeeds without conflict
        self.install()
        import shutil
        moved = self.root / 'Moved Vault'
        shutil.move(str(self.vault), str(moved))
        self.vault = moved
        path = self.vault / '.claude/settings.local.json'

        # Reconcile hooks
        res_recon = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'hook-reconcile'],
                                   env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(res_recon.returncode, 0, res_recon.stderr)
        self.assertEqual(json.loads(res_recon.stdout)['status'], 'reconciled')

        # Verify manifest files hash was updated
        manifest = json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['files']['.claude/settings.local.json']['installed_hash'],
                         hashlib.sha256(path.read_bytes()).hexdigest())

        # Uninstall must succeed without "managed file changed" conflict
        res_uninst = subprocess.run([sys.executable, str(INSTALLER), '--vault', str(self.vault),
                                     '--state', str(self.state), '--uninstall'],
                                    env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(res_uninst.returncode, 0, res_uninst.stderr)
        self.assertEqual(json.loads(res_uninst.stdout)['status'], 'uninstalled')

    def test_reconcile_does_not_adopt_user_settings(self):
        self.install()
        import shutil
        moved = self.root / 'Moved Vault'
        shutil.move(str(self.vault), str(moved))
        self.vault = moved
        path = self.vault / '.claude/settings.local.json'
        data = json.loads(path.read_text())
        data['user_setting'] = 'preserve me'
        path.write_text(json.dumps(data))
        res = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'hook-reconcile'], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        uninstall = subprocess.run([sys.executable, str(INSTALLER), '--vault', str(self.vault), '--state', str(self.state), '--uninstall'], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(uninstall.returncode, 0)
        self.assertEqual(json.loads(path.read_text())['user_setting'], 'preserve me')


    def test_hook_reconcile_syncs_runtime_json_and_launchers_and_manifest(self):
        self.install()
        import shutil
        moved = self.root / 'Moved Space'
        shutil.move(str(self.vault), str(moved))
        self.vault = moved

        runtime_path = self.vault / '.beyin-runtime.json'
        rdata = json.loads(runtime_path.read_text(encoding='utf-8'))
        rdata['state'] = str(self.root / 'stale-state')
        runtime_path.write_text(json.dumps(rdata, indent=2) + '\n', encoding='utf-8')
        old_runtime_bytes = runtime_path.read_bytes()

        launcher_name = 'Beyni Guncelle.cmd' if os.name == 'nt' else ('Beyni Güncelle.command' if sys.platform == 'darwin' else 'Beyni Güncelle.sh')
        launcher_path = self.vault / launcher_name
        old_launcher_bytes = launcher_path.read_bytes() if launcher_path.is_file() else None

        # Run hook-reconcile
        recon = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), '--state', str(self.state), 'hook-reconcile'],
                               env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(recon.returncode, 0, recon.stderr)
        recon_data = json.loads(recon.stdout)
        self.assertEqual(recon_data['status'], 'reconciled')

        # Verify .beyin-runtime.json and launcher were reconciled
        reconciled_file_names = [f['file'] for f in recon_data['files']]
        self.assertIn('.beyin-runtime.json', reconciled_file_names)
        self.assertTrue(self.vault.joinpath('.beyin-runtime.json.bak').is_file())
        self.assertNotEqual(runtime_path.read_bytes(), old_runtime_bytes)
        self.assertEqual(json.loads(runtime_path.read_text(encoding='utf-8'))['state'], str(self.state.resolve()))

        if launcher_path.is_file():
            self.assertIn(launcher_name, reconciled_file_names)
            self.assertTrue(launcher_path.with_name(launcher_name + '.bak').is_file())
            self.assertNotEqual(launcher_path.read_bytes(), old_launcher_bytes)

        # Verify manifest was updated with new hashes
        manifest = json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['files']['.beyin-runtime.json']['installed_hash'],
                         hashlib.sha256(runtime_path.read_bytes()).hexdigest())
        if launcher_path.is_file():
            self.assertEqual(manifest['files'][launcher_name]['installed_hash'],
                             hashlib.sha256(launcher_path.read_bytes()).hexdigest())

        # Update and uninstall both succeed without conflict
        res_update = subprocess.run([sys.executable, str(INSTALLER), '--vault', str(self.vault), '--state', str(self.state)],
                                    env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(res_update.returncode, 0, res_update.stderr)

        res_uninst = subprocess.run([sys.executable, str(INSTALLER), '--vault', str(self.vault),
                                     '--state', str(self.state), '--uninstall'],
                                    env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(res_uninst.returncode, 0, res_uninst.stderr)
        self.assertEqual(json.loads(res_uninst.stdout)['status'], 'uninstalled')

    def test_semantic_unchanged_runtime_json_default_layout_and_msix(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("v3_installer", INSTALLER)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)

        base_data = {"schema": 1, "state": "C:\\Users\\Alice\\AppData\\Local\\beyin-v3\\0123456789abcdef"}
        moved_data = {"schema": 1, "state": "C:\\Users\\Bob\\AppData\\Local\\beyin-v3\\fedcba9876543210"}
        msix_data = {"schema": 1, "state": "C:\\Users\\Alice\\AppData\\Local\\Packages\\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\\LocalCache\\Local\\beyin-v3\\0123456789abcdef"}

        # Across moves with default layout: unchanged
        self.assertTrue(installer.semantic_unchanged(".beyin-runtime.json", json.dumps(moved_data).encode(), json.dumps(base_data).encode(), (), (), ()))
        # MSIX package redirect: unchanged
        self.assertTrue(installer.semantic_unchanged(".beyin-runtime.json", json.dumps(msix_data).encode(), json.dumps(base_data).encode(), (), (), ()))
        # Custom non-default state change must NOT be considered unchanged (must conflict)
        custom_data = {"schema": 1, "state": "D:\\CustomStateLocation"}
        self.assertFalse(installer.semantic_unchanged(".beyin-runtime.json", json.dumps(custom_data).encode(), json.dumps(base_data).encode(), (), (), ()))
        # Schema change or extra field is not unchanged
        altered_data = {"schema": 2, "state": "C:\\Users\\Alice\\AppData\\Local\\beyin-v3\\0123456789abcdef"}
        self.assertFalse(installer.semantic_unchanged(".beyin-runtime.json", json.dumps(altered_data).encode(), json.dumps(base_data).encode(), (), (), ()))

    def test_reconcile_preserves_edited_launcher_and_its_manifest_baseline(self):
        self.install()
        import shutil
        moved = self.root / 'Moved Vault'
        shutil.move(str(self.vault), str(moved))
        self.vault = moved
        name = 'Beyni Guncelle.cmd' if os.name == 'nt' else ('Beyni Güncelle.command' if sys.platform == 'darwin' else 'Beyni Güncelle.sh')
        launcher = self.vault / name
        edited = launcher.read_bytes() + b'\necho USER_CUSTOMIZATION\n'
        launcher.write_bytes(edited)
        manifest_path = self.state / 'v3-install.json'
        baseline = json.loads(manifest_path.read_text(encoding='utf-8'))['files'][name]
        for extra in ([], ['--force']):
            result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'hook-reconcile', *extra],
                                    env=self.env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(launcher.read_bytes(), edited)
            self.assertEqual(json.loads(manifest_path.read_text(encoding='utf-8'))['files'][name], baseline)


if __name__ == "__main__":
    unittest.main()
