"""devices: the opt-in staged multi-device layout (solo and phone levels).

Synthetic vaults only; no network, nothing outside the temporary folder is written.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))

import beyin_v3_devices as devices
from v3_package_helpers import inherited_env

CLI = ROOT / "scripts/beyin_v3.py"
GIT = shutil.which("git")


def git(folder, *args):
    env = inherited_env(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    done = subprocess.run([GIT, "-C", str(folder), *args], capture_output=True, text=True, env=env)
    if done.returncode != 0:
        raise AssertionError("git " + " ".join(args) + ": " + done.stderr)
    return done.stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="beyin-devices-test-")).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def machine(self, name="a"):
        vault, state = self.tmp / name / "vault", self.tmp / name / "state"
        vault.mkdir(parents=True)
        state.mkdir()
        return vault, state

    def tree(self, folder):
        return sorted(str(path.relative_to(folder)) for path in folder.rglob("*"))


class SettingsTest(Base):
    def test_off_by_default_and_doctor_shows_only_the_switch(self):
        vault, state = self.machine()
        self.assertEqual(devices.status(vault, state)["level"], "solo")
        self.assertFalse(devices.status(vault, state)["enabled"])
        self.assertEqual(devices.doctor(state), {"enabled": False})
        self.assertEqual(list(state.iterdir()), [])

    def test_broken_settings_read_as_off_and_are_not_overwritten(self):
        vault, state = self.machine()
        (state / devices.SETTINGS_FILE).write_text("{not json", encoding="utf-8")
        settings, valid = devices.read_settings(state)
        self.assertEqual((settings["level"], valid), ("solo", False))
        self.assertEqual(devices.doctor(state)["valid"], False)
        with self.assertRaisesRegex(ValueError, "invalid"):
            devices.apply(vault, state, {"level": "phone", "phone_sync": "other"})
        self.assertEqual((state / devices.SETTINGS_FILE).read_text(encoding="utf-8"), "{not json")
        (state / devices.SETTINGS_FILE).write_bytes(b"\xff\xfe{")
        self.assertEqual(devices.read_settings(state), (devices.default_settings(), False))

    def test_explicit_solo_resets_a_file_this_version_cannot_read(self):
        vault, state = self.machine()
        (state / devices.SETTINGS_FILE).write_text(json.dumps({"schema": 1, "level": "computers"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "--level solo"):
            devices.apply(vault, state, {"level": "phone", "phone_sync": "icloud"})
        self.assertEqual(devices.apply(vault, state, {"level": "solo"})["status"], "applied")
        self.assertEqual(devices.read_settings(state), (devices.default_settings(), True))

    def test_unknown_or_inconsistent_settings_are_invalid(self):
        _, state = self.machine()
        for data in ({"schema": 1, "level": "cluster"}, {"schema": 2, "level": "solo"},
                     {"schema": 1, "level": "phone", "phone_sync": None},
                     {"schema": 1, "level": "solo", "phone_sync": "icloud"}, ["solo"]):
            (state / devices.SETTINGS_FILE).write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(devices.read_settings(state)[1], data)

    def test_settings_written_by_a_later_version_keep_their_extra_fields(self):
        vault, state = self.machine()
        stored = {"schema": 1, "level": "phone", "phone_sync": "icloud", "interval": 0}
        (state / devices.SETTINGS_FILE).write_text(json.dumps(stored), encoding="utf-8")
        self.assertTrue(devices.read_settings(state)[1])
        devices.apply(vault, state, {"phone_sync": "other"})
        saved = json.loads((state / devices.SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual((saved["phone_sync"], saved["interval"]), ("other", 0))


class DetectTest(Base):
    def test_sync_location_recognises_services_from_the_path_only(self):
        home = "/Users/x"
        cases = {
            home + "/Library/Mobile Documents/iCloud~md~obsidian/Documents/Beyin": ("icloud", True),
            home + "/Library/Mobile Documents/com~apple~CloudDocs/Beyin": ("icloud", False),
            home + "/Library/CloudStorage/Dropbox/Beyin": ("dropbox", False),
            home + "/Library/CloudStorage/OneDrive-Personal/Beyin": ("onedrive", False),
            "C:/Users/x/OneDrive/Beyin": ("onedrive", False),
            "C:/Users/x/OneDrive - Contoso/Beyin": ("onedrive", False),
            "C:/Users/x/OneDriveOld/Beyin": (None, False),
            home + "/Dropbox/Beyin": ("dropbox", False),
            home + "/Documents/Beyin": (None, False),
        }
        for path, (service, phone) in cases.items():
            found = devices.sync_location(Path(path))
            self.assertEqual((found["service"], found["obsidian_ios_folder"]), (service, phone), path)

    def test_detect_writes_nothing_and_reads_obsidian_sync_in_both_formats(self):
        vault, state = self.machine()
        (vault / ".obsidian").mkdir()
        for content, expected in (('["file-explorer", "sync"]', True), ('{"sync": true}', True),
                                  ('{"sync": false}', False), ('["graph"]', False), ("broken", False)):
            (vault / ".obsidian/core-plugins.json").write_text(content, encoding="utf-8")
            before = self.tree(vault)
            found = devices.detect(vault, state)
            self.assertEqual(found["obsidian"]["sync_plugin"], expected, content)
            self.assertEqual(self.tree(vault), before)
        self.assertEqual(list(state.iterdir()), [])
        self.assertFalse(found["git"]["repository"])
        self.assertEqual(found["current_fit"], {"level": "solo", "phone_sync": None})
        self.assertEqual(set(found["levels"]), {"solo", "phone"})
        self.assertEqual(found["levels"]["phone"]["needs_per_option"]["other"], [])

    def test_current_fit_suggests_the_phone_option_already_in_use(self):
        vault, state = self.machine()
        (vault / ".obsidian").mkdir()
        (vault / ".obsidian/core-plugins.json").write_text('{"sync": true}', encoding="utf-8")
        self.assertEqual(devices.detect(vault, state)["current_fit"], {"level": "phone", "phone_sync": "obsidian-sync"})
        (vault / ".obsidian/core-plugins.json").unlink()
        (vault / ".stfolder").mkdir()
        self.assertEqual(devices.detect(vault, state)["current_fit"], {"level": "phone", "phone_sync": "other"})

    @unittest.skipUnless(GIT, "git required")
    def test_detect_reports_the_git_directory_also_through_a_symlink(self):
        vault, state = self.machine()
        git(vault, "init", "-q", "-b", "main")
        link = self.tmp / "link"
        link.symlink_to(vault, target_is_directory=True)
        found = devices.detect(link, state)["git"]
        self.assertEqual(found, {"repository": True, "git_dir_inside_vault": True, "git_file_in_vault": False})

    @unittest.skipUnless(GIT, "git required")
    def test_detect_reports_a_separate_git_dir_file(self):
        vault, state = self.machine()
        git(vault, "init", "-q", "-b", "main", "--separate-git-dir", str(self.tmp / "outside.git"))
        found = devices.detect(vault, state)["git"]
        self.assertEqual(found, {"repository": True, "git_dir_inside_vault": False, "git_file_in_vault": True})


class PlanTest(Base):
    def test_human_entry_prints_the_device_result(self):
        spec = importlib.util.spec_from_file_location("devices_entry_test", ROOT / "scripts/beyin_entry.py")
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        planned = {"level": "phone", "manual_steps": ["Open the folder in Obsidian"], "warnings": []}
        self.assertEqual(json.loads(entry.human_result(planned, "devices")), planned)

    def test_level_rules(self):
        vault, state = self.machine()
        for options, message in (
                ({"level": "phone"}, "needs --phone-sync"),
                ({"level": "solo", "phone_sync": "icloud"}, "needs --level phone"),
                ({"phone_sync": "icloud"}, "needs --level phone"),
                ({"level": "computers"}, "--level must be one of"),
                ({"level": "phone", "phone_sync": "dropbox"}, "--phone-sync must be one of")):
            with self.assertRaisesRegex(ValueError, message, msg=str(options)):
                devices.plan(vault, state, options)
        self.assertEqual(list(state.iterdir()), [])

    def test_phone_levels_give_guidance_and_write_only_the_state_settings(self):
        vault, state = self.machine()
        for choice in devices.PHONE_SYNCS:
            planned = devices.public_plan(devices.plan(vault, state, {"level": "phone", "phone_sync": choice}))
            self.assertTrue(planned["manual_steps"], choice)
            self.assertNotIn("settings", planned)
            self.assertNotIn("actions", planned)
        self.assertEqual(list(state.iterdir()), [])
        before = self.tree(vault)
        applied = devices.apply(vault, state, {"level": "phone", "phone_sync": "obsidian-sync"})
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(devices.status(vault, state)["phone_sync"], "obsidian-sync")
        self.assertEqual(self.tree(vault), before)
        self.assertEqual(devices.apply(vault, state, {"level": "phone"})["status"], "unchanged")

    def test_going_back_to_solo_clears_the_phone_option(self):
        vault, state = self.machine()
        devices.apply(vault, state, {"level": "phone", "phone_sync": "icloud"})
        devices.apply(vault, state, {"level": "solo"})
        self.assertEqual(devices.status(vault, state)["phone_sync"], None)
        self.assertEqual(devices.doctor(state), {"enabled": False})

    def test_state_inside_a_sync_folder_is_warned(self):
        vault = self.tmp / "vault"
        vault.mkdir()
        state = self.tmp / "Library/CloudStorage/Dropbox/state"
        planned = devices.plan(vault, state, {"level": "phone", "phone_sync": "other"})
        self.assertTrue(any("runtime state" in warning for warning in planned["warnings"]))

    @unittest.skipUnless(GIT, "git required")
    def test_git_folder_inside_a_synced_vault_is_warned_but_not_touched(self):
        vault = self.tmp / "Library/CloudStorage/Dropbox/vault"
        vault.mkdir(parents=True)
        state = self.tmp / "state"
        git(vault, "init", "-q", "-b", "main")
        before = self.tree(vault)
        planned = devices.plan(vault, state, {"level": "phone", "phone_sync": "other"})
        self.assertTrue(any(".git folder is inside the synced folder" in warning for warning in planned["warnings"]))
        self.assertFalse(any("--git-dir" in text for text in planned["warnings"] + planned["manual_steps"]))
        devices.apply(vault, state, {"level": "phone", "phone_sync": "other"})
        self.assertEqual(self.tree(vault), before)

    @unittest.skipUnless(GIT, "git required")
    def test_git_warnings_follow_the_chosen_service_not_only_the_current_path(self):
        vault, state = self.machine()
        git(vault, "init", "-q", "-b", "main")

        def warned(choice):
            planned = devices.plan(vault, state, {"level": "phone", "phone_sync": choice})
            return any(".git folder is inside" in warning for warning in planned["warnings"])
        self.assertTrue(warned("icloud"))  # the vault is about to move into iCloud
        self.assertTrue(warned("other"))
        self.assertFalse(warned("obsidian-sync"))  # Obsidian Sync skips hidden folders
        (vault / ".stfolder").mkdir()
        self.assertTrue(warned("obsidian-sync"))  # Syncthing carries them anyway

    @unittest.skipUnless(GIT, "git required")
    def test_a_separate_git_dir_file_is_warned(self):
        vault, state = self.machine()
        git(vault, "init", "-q", "-b", "main", "--separate-git-dir", str(self.tmp / "outside.git"))
        planned = devices.plan(vault, state, {"level": "phone", "phone_sync": "icloud"})
        self.assertTrue(any(".git file that points" in warning for warning in planned["warnings"]))


class CliTest(Base):
    def run_cli(self, vault, state, *args):
        env = inherited_env(HOME=str(self.home), USERPROFILE=str(self.home), GIT_CONFIG_GLOBAL=os.devnull,
                            GIT_CONFIG_NOSYSTEM="1")
        done = subprocess.run([sys.executable, str(CLI), "--vault", str(vault), "--state", str(state), *args],
                              capture_output=True, text=True, env=env)
        return done.returncode, done.stdout, done.stderr

    def test_status_detect_plan_and_apply(self):
        vault, state = self.machine()
        code, out, err = self.run_cli(vault, state, "devices")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["level"], "solo")
        code, out, err = self.run_cli(vault, state, "devices", "--detect")
        self.assertEqual(code, 0, err)
        self.assertIn("levels", json.loads(out))
        code, out, err = self.run_cli(vault, state, "devices", "--plan", "--level", "phone", "--phone-sync", "icloud")
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["manual_steps"])
        self.assertFalse((state / devices.SETTINGS_FILE).exists())
        code, out, err = self.run_cli(vault, state, "devices", "--apply", "--level", "phone", "--phone-sync", "icloud")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "applied")
        self.assertEqual(json.loads((state / devices.SETTINGS_FILE).read_text(encoding="utf-8"))["phone_sync"], "icloud")

    def test_options_without_plan_or_apply_are_rejected(self):
        vault, state = self.machine()
        code, _, err = self.run_cli(vault, state, "devices", "--level", "phone")
        self.assertEqual(code, 1)
        self.assertIn("only valid with --plan or --apply", err)
        code, _, err = self.run_cli(vault, state, "devices", "--plan", "--level", "computers")
        self.assertNotEqual(code, 0)
        self.assertIn("invalid choice", err)

    def test_doctor_reports_the_layout(self):
        vault, state = self.machine()

        def doctor():
            code, out, err = self.run_cli(vault, state, "doctor")
            self.assertEqual(code, 0, err)
            return json.loads(out)
        self.assertEqual(doctor()["devices"], {"enabled": False})
        devices.apply(vault, state, {"level": "phone", "phone_sync": "icloud"})
        shown = doctor()["devices"]
        self.assertEqual((shown["enabled"], shown["level"], shown["phone_sync"]), (True, "phone", "icloud"))


if __name__ == "__main__":
    unittest.main()
