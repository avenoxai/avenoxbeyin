"""devices: the opt-in staged multi-device layout.

Synthetic vaults and local bare repositories only; no network, no real scheduler is touched
(scheduler files go to a temporary home and the load commands are stubbed).
"""
import json
import os
from pathlib import Path
import plistlib
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
    env = inherited_env(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    done = subprocess.run([GIT, "-C", str(folder), *args], capture_output=True, text=True, env=env)
    if done.returncode != 0:
        raise AssertionError("git " + " ".join(args) + ": " + done.stderr)
    return done.stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="beyin-devices-test-")).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        # Commits need an identity and must not read the developer's own git configuration.
        patcher = mock.patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def machine(self, name):
        vault, state = self.tmp / name / "vault", self.tmp / name / "state"
        vault.mkdir(parents=True)
        state.mkdir()
        return vault, state

    def apply(self, vault, state, **options):
        with mock.patch.object(devices, "_run", return_value=(0, "")):
            return devices.apply(vault, state, options, home=self.home)


class SettingsTest(Base):
    def test_off_by_default_and_doctor_shows_only_the_switch(self):
        vault, state = self.machine("a")
        self.assertEqual(devices.status(vault, state)["level"], "solo")
        self.assertFalse(devices.status(vault, state)["enabled"])
        self.assertEqual(devices.doctor(state), {"enabled": False})
        self.assertEqual(list(state.iterdir()), [])

    def test_sync_once_refuses_while_off_and_writes_nothing(self):
        vault, state = self.machine("a")
        with self.assertRaisesRegex(ValueError, "devices sync is off"):
            devices.sync_once(vault, state)
        self.assertEqual(list(state.iterdir()), [])

    def test_broken_settings_read_as_off_and_are_not_overwritten(self):
        vault, state = self.machine("a")
        (state / devices.SETTINGS_FILE).write_text("{not json", encoding="utf-8")
        settings, valid = devices.read_settings(state)
        self.assertEqual((settings["level"], valid), ("solo", False))
        with self.assertRaisesRegex(ValueError, "invalid"):
            devices.plan(vault, state, {"level": "phone", "phone_sync": "other"})
        self.assertEqual((state / devices.SETTINGS_FILE).read_text(encoding="utf-8"), "{not json")

    def test_unknown_level_in_settings_is_invalid(self):
        _, state = self.machine("a")
        (state / devices.SETTINGS_FILE).write_text(json.dumps({**devices.default_settings(), "level": "cluster"}), encoding="utf-8")
        self.assertFalse(devices.read_settings(state)[1])


class DetectTest(Base):
    def test_sync_location_recognises_services_from_the_path_only(self):
        home = "/Users/x"
        cases = {
            home + "/Library/Mobile Documents/iCloud~md~obsidian/Documents/Beyin": ("icloud", True),
            home + "/Library/Mobile Documents/com~apple~CloudDocs/Beyin": ("icloud", False),
            home + "/Library/CloudStorage/Dropbox/Beyin": ("dropbox", False),
            home + "/Library/CloudStorage/OneDrive-Personal/Beyin": ("onedrive", False),
            "C:/Users/x/OneDrive/Beyin": ("onedrive", False),
            home + "/Dropbox/Beyin": ("dropbox", False),
            home + "/Documents/Beyin": (None, False),
        }
        for path, (service, phone) in cases.items():
            found = devices.sync_location(Path(path))
            self.assertEqual((found["service"], found["obsidian_ios_folder"]), (service, phone), path)

    def test_detect_writes_nothing_and_reads_obsidian_sync_in_both_formats(self):
        vault, state = self.machine("a")
        (vault / ".obsidian").mkdir()
        for content, expected in (('["file-explorer", "sync"]', True), ('{"sync": true}', True),
                                  ('{"sync": false}', False), ('["graph"]', False), ("broken", False)):
            (vault / ".obsidian/core-plugins.json").write_text(content, encoding="utf-8")
            found = devices.detect(vault, state)
            self.assertEqual(found["obsidian"]["sync_plugin"], expected, content)
        self.assertEqual(list(state.iterdir()), [])
        self.assertFalse(found["git"]["repository"])
        self.assertEqual(found["current_fit"], {"level": "solo", "phone_sync": None})
        self.assertTrue(found["levels"]["solo"]["ready"])

    @unittest.skipUnless(GIT, "git required")
    def test_detect_never_prints_remote_credentials(self):
        vault, state = self.machine("a")
        git(vault, "init", "-q", "-b", "main")
        git(vault, "remote", "add", "origin", "https://user:s3cret-token@example.invalid/x/y.git")
        git(vault, "remote", "add", "hub", "ssh://me@hub.example.invalid/srv/v.git")
        found = devices.detect(vault, state)
        text = json.dumps(found)
        self.assertNotIn("s3cret-token", text)
        self.assertEqual(found["git"]["remotes"]["origin"], {"transport": "https", "url": "https://example.invalid/x/y.git"})
        self.assertEqual(found["git"]["remotes"]["hub"]["transport"], "ssh")
        self.assertEqual(found["current_fit"]["level"], "hub")

    def test_scheduler_kind_per_platform(self):
        self.assertEqual(devices.scheduler_kind("darwin", {}), "launchd")
        self.assertEqual(devices.scheduler_kind("win32", {}), "schtasks")
        with mock.patch.object(devices.shutil, "which", return_value=None):
            self.assertEqual(devices.scheduler_kind("linux", {}), "none")


class PlanTest(Base):
    def test_level_rules(self):
        vault, state = self.machine("a")
        for options, message in (
                ({"level": "phone"}, "needs --phone-sync"),
                ({"level": "hub"}, "needs --role"),
                ({"level": "phone", "phone_sync": "other", "remote": "x"}, "belongs to --level computers or hub"),
                ({"level": "solo", "capture": True}, "needs a level above solo"),
                ({"level": "computers", "interval": 2, "remote": "x", "init_git": True}, "--interval"),
                ({"level": "computers", "bare": str(self.tmp / "b.git")}, "belongs to --level hub --role hub"),
                ({"level": "hub", "role": "client", "hub_host": "hub"}, "needs --hub-path"),
                ({"level": "hub", "role": "client", "hub_host": "me@hub", "hub_path": "/srv/v.git"}, "host name"),
                ({"level": "hub", "role": "hub", "bare": str(vault / "inside.git")}, "outside the vault"),
                ({"level": "computers", "remote": "x", "device_name": "bad/name", "init_git": True}, "--device-name")):
            with self.assertRaisesRegex(ValueError, message, msg=str(options)):
                devices.plan(vault, state, options)
        self.assertEqual(list(state.iterdir()), [])

    def test_git_directories_are_refused_inside_a_file_sync_folder(self):
        vault, state = self.machine("a")
        target = self.tmp / "Library/CloudStorage/Dropbox/git/v.git"
        with self.assertRaisesRegex(ValueError, "file sync folder"):
            devices.plan(vault, state, {"level": "computers", "remote": "x", "git_dir": str(target), "init_git": True})

    def test_phone_levels_plan_only_guidance(self):
        vault, state = self.machine("a")
        for choice in devices.PHONE_SYNCS:
            planned = devices.plan(vault, state, {"level": "phone", "phone_sync": choice})
            self.assertEqual(planned["actions"], [], choice)
            self.assertTrue(planned["manual_steps"], choice)
            self.assertEqual(planned["settings"]["interval"], 0)
        applied = self.apply(vault, state, level="phone", phone_sync="obsidian-sync")
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(devices.status(vault, state)["phone_sync"], "obsidian-sync")
        self.assertFalse((vault / ".gitignore").exists())
        self.assertFalse((vault / ".git").exists())

    @unittest.skipUnless(GIT, "git required")
    def test_computers_requires_a_repository_or_an_explicit_init(self):
        vault, state = self.machine("a")
        with self.assertRaisesRegex(ValueError, "--init-git"):
            devices.plan(vault, state, {"level": "computers", "remote": str(self.tmp / "r.git")})
        git(vault, "init", "-q", "-b", "main")
        with self.assertRaisesRegex(ValueError, 'no remote named "origin"'):
            devices.plan(vault, state, {"level": "computers"})

    @unittest.skipUnless(GIT, "git required")
    def test_plan_writes_nothing_and_hides_file_bodies(self):
        vault, state = self.machine("a")
        before = sorted(p.name for p in vault.iterdir())
        planned = devices.public_plan(devices.plan(vault, state, {
            "level": "computers", "remote": "https://u:pw@example.invalid/v.git", "init_git": True, "interval": 15}, home=self.home))
        self.assertEqual(sorted(p.name for p in vault.iterdir()), before)
        self.assertEqual(list(state.iterdir()), [])
        text = json.dumps(planned)
        self.assertNotIn("pw@", text)
        self.assertNotIn("content", text)
        self.assertNotIn("settings", planned)
        self.assertIn("git_init", [a["kind"] for a in planned["actions"]])
        self.assertTrue(any("HTTPS" in w for w in planned["warnings"]))

    def test_managed_block_is_idempotent_and_keeps_user_lines(self):
        user = "# mine\nsecret/\n.beyin-version\n"
        once = devices._merged_block(user, ["beyin.py", ".beyin-version", "daily/v3/"])
        self.assertTrue(once.startswith(user))
        self.assertEqual(once.count(".beyin-version"), 1)
        self.assertIn("beyin.py", once)
        self.assertEqual(devices._merged_block(once, ["beyin.py", ".beyin-version", "daily/v3/"]), once)
        shrunk = devices._merged_block(once, ["beyin.py"])
        self.assertNotIn("daily/v3/", shrunk)
        self.assertIn("secret/", shrunk)
        self.assertEqual(devices._merged_block("a\r\nb\r\n", ["c"]).count("\r\n"), 6)
        self.assertEqual(devices._merged_block("keep\n", ["keep"]), "keep\n")

    def test_user_files_in_the_scripts_folder_are_not_ignored(self):
        vault, _ = self.machine("a")
        scripts = vault / ".claude/scripts"
        scripts.mkdir(parents=True)
        (scripts / "beyin_v3_sync.py").write_text("", encoding="utf-8")
        self.assertIn(".claude/scripts/", devices._ignore_lines(vault)[0])
        (scripts / "kendi-senkron.py").write_text("", encoding="utf-8")
        lines, foreign = devices._ignore_lines(vault)
        self.assertEqual(foreign, ["kendi-senkron.py"])
        self.assertNotIn(".claude/scripts/", lines)
        self.assertIn(".claude/scripts/beyin_v3*.py", lines)


class SchedulerTest(Base):
    def spec(self, kind, interval=15):
        vault, state = self.tmp / "v", self.tmp / "s"
        return devices.scheduler_spec(vault, state, interval, kind=kind, home=self.home,
                                      command=["/usr/bin/python3", "/v a/beyin.py", "devices", "--sync-once"])

    def test_launchd_plist(self):
        spec = self.spec("launchd")
        (path, content), = spec["files"].items()
        data = plistlib.loads(content.encode("utf-8"))
        self.assertEqual(data["StartInterval"], 900)
        self.assertEqual(data["ProgramArguments"][-1], "--sync-once")
        self.assertTrue(path.startswith(str(self.home / "Library/LaunchAgents")))
        self.assertIn(spec["label"], path)

    def test_systemd_units_quote_arguments(self):
        spec = self.spec("systemd-user", 30)
        service = next(v for k, v in spec["files"].items() if k.endswith(".service"))
        timer = next(v for k, v in spec["files"].items() if k.endswith(".timer"))
        self.assertIn('ExecStart="/usr/bin/python3" "/v a/beyin.py" "devices" "--sync-once"', service)
        self.assertIn("OnUnitActiveSec=30min", timer)

    def test_windows_task_and_fallbacks(self):
        spec = self.spec("schtasks")
        self.assertEqual(spec["load"][0][:2], ["schtasks", "/Create"])
        self.assertIn('"/v a/beyin.py"', spec["load"][0][-1])
        self.assertIn("*/15 * * * *", self.spec("cron")["manual"])
        self.assertIn("sleep 900", self.spec("none")["manual"])
        self.assertEqual(self.spec("none")["files"], {})

    def test_remove_ignores_records_that_do_not_look_like_ours(self):
        victim = self.tmp / "keep.txt"
        victim.write_text("x", encoding="utf-8")
        with mock.patch.object(devices, "_run") as run:
            devices._remove_scheduler({"label": "beyin-devices-abc", "files": [str(victim)],
                                       "unload": [["rm", "-rf", "/"], ["systemctl", "--user", "disable", "other.timer"]]})
            devices._remove_scheduler({"label": "x", "files": [str(victim)], "unload": [["launchctl", "bootout", "x"]]})
        run.assert_not_called()
        self.assertTrue(victim.exists())


@unittest.skipUnless(GIT, "git required")
class SyncRoundTest(Base):
    def setUp(self):
        super().setUp()
        self.bare = self.tmp / "hub/vault.git"
        self.a, self.a_state = self.machine("a")
        (self.a / "notes").mkdir()
        (self.a / "notes/one.md").write_text("# One\n", encoding="utf-8")
        (self.a / "beyin.py").write_text("# machine file\n", encoding="utf-8")
        # Installer output that carries this machine's paths; a second machine's installer stops with
        # "Unmanaged file conflict" when one of these arrives through git.
        for name in ("Beyni Güncelle.sh", "Beyni Güncelle.desktop", "Beyni Güncelle.command", ".pi/extensions/beyin-v3.ts",
                     ".beyin-runtime.json", ".claude/scripts/beyin_v3_sync.py"):
            (self.a / name).parent.mkdir(parents=True, exist_ok=True)
            (self.a / name).write_text("machine\n", encoding="utf-8")
        result = self.apply(self.a, self.a_state, level="hub", role="hub", bare=str(self.bare), init_git=True,
                            device_name="hub")
        self.assertEqual(result["status"], "applied", result)
        self.assertEqual(devices.sync_once(self.a, self.a_state)["status"], "ok")
        self.b, self.b_state = self.machine("b")
        git(self.b, "init", "-q", "-b", "main")
        result = self.apply(self.b, self.b_state, level="computers", remote=str(self.bare), device_name="laptop")
        self.assertEqual(result["status"], "applied", result)

    def test_hub_apply_creates_the_bare_repository_and_ignores_machine_files(self):
        self.assertTrue((self.bare / "HEAD").exists())
        self.assertEqual(git(self.bare, "ls-tree", "-r", "--name-only", "main").splitlines(),
                         [".gitattributes", ".gitignore", "notes/one.md"])
        self.assertIn("receipts/** -text", (self.a / ".gitattributes").read_text(encoding="utf-8"))
        self.assertEqual(devices.status(self.a, self.a_state)["role"], "hub")

    def test_second_machine_joins_and_changes_travel_both_ways(self):
        first = devices.sync_once(self.b, self.b_state)
        self.assertEqual(first["status"], "ok", first)
        self.assertEqual((self.b / "notes/one.md").read_text(encoding="utf-8"), "# One\n")
        (self.b / "notes/two.md").write_text("# Two\n", encoding="utf-8")
        sent = devices.sync_once(self.b, self.b_state)
        self.assertEqual((sent["status"], sent["committed"], sent["pushed"]), ("ok", True, True))
        self.assertIn("laptop", git(self.b, "log", "-1", "--format=%s"))
        received = devices.sync_once(self.a, self.a_state)
        self.assertEqual((received["status"], received["pulled"], received["pushed"]), ("ok", True, False))
        self.assertTrue((self.a / "notes/two.md").exists())
        idle = devices.sync_once(self.a, self.a_state)
        self.assertEqual((idle["status"], idle["committed"], idle["pulled"], idle["pushed"]), ("ok", False, False, False))

    def test_different_notes_edited_on_both_sides_merge(self):
        devices.sync_once(self.b, self.b_state)
        (self.a / "notes/a.md").write_text("a\n", encoding="utf-8")
        (self.b / "notes/b.md").write_text("b\n", encoding="utf-8")
        self.assertEqual(devices.sync_once(self.a, self.a_state)["status"], "ok")
        merged = devices.sync_once(self.b, self.b_state)
        self.assertEqual((merged["status"], merged["pulled"], merged["pushed"]), ("ok", True, True))
        self.assertTrue((self.b / "notes/a.md").exists())

    def test_session_cards_from_two_machines_are_both_kept(self):
        card = self.a / "Companion/Last-Session.md"
        card.parent.mkdir()
        card.write_text("# Son oturum\n", encoding="utf-8")
        devices.sync_once(self.a, self.a_state)
        devices.sync_once(self.b, self.b_state)
        card.write_text("# Son oturum\n\n## A karti\n", encoding="utf-8")
        (self.b / "Companion/Last-Session.md").write_text("# Son oturum\n\n## B karti\n", encoding="utf-8")
        devices.sync_once(self.a, self.a_state)
        result = devices.sync_once(self.b, self.b_state)
        self.assertEqual(result["status"], "ok", result)
        text = (self.b / "Companion/Last-Session.md").read_text(encoding="utf-8")
        self.assertIn("## A karti", text)
        self.assertIn("## B karti", text)

    def test_same_line_conflict_keeps_both_sides_and_leaves_no_markers(self):
        devices.sync_once(self.b, self.b_state)
        (self.a / "notes/one.md").write_text("# One from A\n", encoding="utf-8")
        (self.b / "notes/one.md").write_text("# One from B\n", encoding="utf-8")
        devices.sync_once(self.a, self.a_state)
        result = devices.sync_once(self.b, self.b_state)
        self.assertEqual(result["status"], "conflict", result)
        self.assertEqual(result["conflicts"], ["notes/one.md"])
        self.assertEqual((self.b / "notes/one.md").read_text(encoding="utf-8"), "# One from B\n")
        self.assertIsNone(devices._in_progress(devices._git_dir(self.b)))
        self.assertEqual(git(self.b, "status", "--porcelain"), "")
        self.assertEqual(git(self.bare, "show", "main:notes/one.md"), "# One from A")
        shown = devices.doctor(self.b_state)
        self.assertEqual(shown["last_sync"]["status"], "conflict")
        self.assertIn("age_minutes", shown["last_sync"])
        # The user resolves by hand; the next round then goes through.
        git(self.b, "fetch", "-q", "origin")
        git(self.b, "reset", "-q", "--hard", "origin/main")
        self.assertEqual(devices.sync_once(self.b, self.b_state)["status"], "ok")

    def test_unreachable_remote_keeps_the_local_commit(self):
        devices.sync_once(self.b, self.b_state)
        git(self.b, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        (self.b / "notes/offline.md").write_text("x\n", encoding="utf-8")
        result = devices.sync_once(self.b, self.b_state)
        self.assertEqual((result["status"], result["committed"]), ("offline", True))
        self.assertEqual(git(self.b, "status", "--porcelain"), "")

    def test_refuses_while_a_merge_is_open_or_on_another_branch(self):
        devices.sync_once(self.b, self.b_state)
        git(self.b, "checkout", "-q", "-b", "deneme")
        self.assertEqual(devices.sync_once(self.b, self.b_state)["status"], "blocked")
        git(self.b, "checkout", "-q", "main")
        (devices._git_dir(self.b) / "MERGE_HEAD").write_text("0" * 40 + "\n", encoding="utf-8")
        blocked = devices.sync_once(self.b, self.b_state)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("merge", blocked["reason"])

    def test_overlapping_rounds_do_not_run_together(self):
        with open(self.b_state / devices.LOCK_FILE, "a+b") as handle:
            self.assertTrue(devices._lock(handle))
            self.assertEqual(devices.sync_once(self.b, self.b_state), {"status": "already_running"})

    def test_git_directory_moves_out_of_the_vault(self):
        target = self.tmp / "b/gitdir/vault.git"
        result = self.apply(self.b, self.b_state, level="computers", git_dir=str(target))
        self.assertEqual(result["status"], "applied", result)
        self.assertTrue((self.b / ".git").is_file())
        self.assertTrue((target / "HEAD").exists())
        self.assertEqual(devices.sync_once(self.b, self.b_state)["status"], "ok")
        self.assertFalse(devices.detect(self.b, self.b_state)["git"]["git_dir_inside_vault"])

    def test_schedule_is_installed_recorded_and_removed(self):
        with mock.patch.object(devices, "scheduler_kind", return_value="systemd-user"):
            result = self.apply(self.b, self.b_state, level="computers", interval=15)
            self.assertEqual(result["status"], "applied", result)
            units = sorted(p.name for p in (self.home / ".config/systemd/user").iterdir())
            self.assertEqual(len(units), 2)
            stored = devices.status(self.b, self.b_state)
            self.assertEqual((stored["interval"], stored["scheduler"]["kind"]), (15, "systemd-user"))
            with mock.patch.object(devices, "_run", return_value=(0, "")) as run:
                removed = devices.remove_scheduler(self.b, self.b_state)
            self.assertEqual(removed["status"], "removed")
            self.assertEqual(run.call_args_list[0].args[0][:4], ["systemctl", "--user", "disable", "--now"])
        self.assertEqual(list((self.home / ".config/systemd/user").iterdir()), [])
        self.assertEqual(devices.status(self.b, self.b_state)["interval"], 0)

    def test_failed_action_leaves_settings_unchanged(self):
        before = (self.b_state / devices.SETTINGS_FILE).read_text(encoding="utf-8")
        with mock.patch.object(devices, "scheduler_kind", return_value="systemd-user"), \
                mock.patch.object(devices, "_run", return_value=(1, "Failed to connect to bus")):
            result = devices.apply(self.b, self.b_state, {"level": "computers", "interval": 15}, home=self.home)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Failed to connect to bus", result["failed"]["error"])
        self.assertEqual((self.b_state / devices.SETTINGS_FILE).read_text(encoding="utf-8"), before)

    def test_capture_token_is_private_and_never_printed(self):
        with mock.patch.object(devices, "scheduler_kind", return_value="none"), \
                mock.patch.object(devices, "_tailscale_ip", return_value="100.64.0.7"):
            result = self.apply(self.a, self.a_state, level="hub", role="hub", capture=True)
        token_file = self.a_state / devices.TOKEN_FILE
        token = token_file.read_text(encoding="utf-8").split("=", 1)[1].strip()
        self.assertGreaterEqual(len(token), 40)
        self.assertNotIn(token, json.dumps(result))
        if os.name != "nt":
            self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(devices.status(self.a, self.a_state)["capture"], {"host": "100.64.0.7", "port": 8484})
        self.assertTrue(any("100.64.0.7:8484/ingest" in step for step in result["manual_steps"]))
        self.apply(self.a, self.a_state, level="hub", role="hub", capture=True)
        self.assertEqual(token_file.read_text(encoding="utf-8").split("=", 1)[1].strip(), token)

    def test_capture_without_private_network_stays_on_loopback(self):
        with mock.patch.object(devices, "_tailscale_ip", return_value=None):
            planned = devices.plan(self.a, self.a_state, {"level": "hub", "role": "hub", "capture": True}, home=self.home)
        self.assertEqual(planned["settings"]["capture"]["host"], "127.0.0.1")
        self.assertTrue(any("127.0.0.1" in w for w in planned["warnings"]))

    def test_going_back_to_solo_turns_sync_off(self):
        self.apply(self.b, self.b_state, level="solo")
        self.assertEqual(devices.status(self.b, self.b_state)["enabled"], False)
        with self.assertRaisesRegex(ValueError, "devices sync is off"):
            devices.sync_once(self.b, self.b_state)
        self.assertTrue((self.b / ".git").exists())


@unittest.skipUnless(GIT, "git required")
class CliTest(Base):
    def run_cli(self, vault, state, *args):
        env = inherited_env(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
                            GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
                            GIT_COMMITTER_EMAIL="t@example.invalid", HOME=str(self.home), USERPROFILE=str(self.home))
        done = subprocess.run([sys.executable, str(CLI), "--vault", str(vault), "--state", str(state), "devices", *args],
                              capture_output=True, text=True, env=env)
        return done.returncode, done.stdout, done.stderr

    def test_status_detect_plan_apply_and_sync_once(self):
        vault, state = self.machine("a")
        (vault / "note.md").write_text("# Not\n", encoding="utf-8")
        code, out, err = self.run_cli(vault, state)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["level"], "solo")
        code, out, err = self.run_cli(vault, state, "--detect")
        self.assertEqual(code, 0, err)
        self.assertIn("levels", json.loads(out))
        self.assertFalse((state / devices.SETTINGS_FILE).exists())
        bare = self.tmp / "hub.git"
        arguments = ["--level", "hub", "--role", "hub", "--bare", str(bare), "--init-git"]
        code, out, err = self.run_cli(vault, state, "--plan", *arguments)
        self.assertEqual(code, 0, err)
        self.assertFalse(bare.exists())
        code, out, err = self.run_cli(vault, state, "--apply", *arguments)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "applied")
        code, out, err = self.run_cli(vault, state, "--sync-once")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "ok")
        self.assertIn("note.md", git(bare, "ls-tree", "-r", "--name-only", "main"))

    def test_options_without_plan_or_apply_are_rejected(self):
        vault, state = self.machine("a")
        code, out, err = self.run_cli(vault, state, "--level", "phone")
        self.assertEqual(code, 1)
        self.assertIn("only valid with --plan or --apply", err)

    def test_doctor_reports_the_layout(self):
        vault, state = self.machine("a")
        env = inherited_env(HOME=str(self.home), USERPROFILE=str(self.home))
        def doctor():
            done = subprocess.run([sys.executable, str(CLI), "--vault", str(vault), "--state", str(state), "doctor"],
                                  capture_output=True, text=True, env=env)
            self.assertEqual(done.returncode, 0, done.stderr)
            return json.loads(done.stdout)
        self.assertEqual(doctor()["devices"], {"enabled": False})
        self.apply(vault, state, level="phone", phone_sync="icloud")
        shown = doctor()["devices"]
        self.assertEqual((shown["enabled"], shown["level"], shown["phone_sync"]), (True, "phone", "icloud"))


if __name__ == "__main__":
    unittest.main()
