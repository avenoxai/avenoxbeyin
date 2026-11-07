"""Doctor reports the pinned state root against the one the process reads (#113 follow-up).

An MSIX-packaged client has %LOCALAPPDATA% redirected into Packages/<id>/LocalCache/Local.
A pin written before that redirect is applied is the same string and two directories for
processes inside and outside the package, so each side sees half the memory. These tests
pin the report shape and each warning. Information only: the doctor status is untouched.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import install, isolated_env, run_python
from v3_transaction_root_test import link_directory

ROOT = Path(os.environ.get("BEYIN_TEST_REPO", Path(__file__).resolve().parents[1]))
SPEC = importlib.util.spec_from_file_location("beyin_v3_cli_under_test", ROOT / "scripts/beyin_v3.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)
CORE_SPEC = importlib.util.spec_from_file_location(
    "state_location_core_under_test", ROOT / "template/.claude/scripts/beyin_v3.py")
CORE = importlib.util.module_from_spec(CORE_SPEC)
CORE_SPEC.loader.exec_module(CORE)

SEP = chr(92)
CONTAINER = "C:" + SEP + "Users" + SEP + "ada" + SEP + "AppData" + SEP + "Local" + SEP + "Packages" + SEP + "Client_pzs8sxrjxfjjc" + SEP + "LocalCache" + SEP + "Local" + SEP + "beyin-v3" + SEP + "ab12"


class StateLocationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="v3-state-location-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.vault = base / "vault"
        self.state = base / "state"
        self.vault.mkdir()
        self.state.mkdir()

    def pin(self, value):
        (self.vault / ".beyin-runtime.json").write_text(
            json.dumps({"state": value, "schema": 1}), encoding="utf-8")

    def installed(self, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / "v3-install.json").write_text("{}", encoding="utf-8")

    def report(self, state=None):
        return CLI.state_location(self.vault, Path(state or self.state))

    def codes(self, report):
        return [warning.split(":", 1)[0] for warning in report["warnings"]]

    def test_pinned_and_installed_state_is_quiet(self):
        self.installed(self.state)
        self.pin(str(self.state))
        report = self.report()
        self.assertEqual(report["pin_status"], "present")
        self.assertEqual(report["warnings"], [])
        self.assertTrue(report["installed_at_pinned_state"])
        self.assertFalse(report["pin_resolves_elsewhere"])

    def test_missing_pin_reports_absent_without_warning(self):
        self.installed(self.state)
        report = self.report()
        self.assertEqual(report["pin_status"], "absent")
        self.assertIsNone(report["pinned_state"])
        self.assertIsNone(report["installed_at_pinned_state"])
        self.assertEqual(report["warnings"], [])

    def test_unreadable_pin_is_reported_not_raised(self):
        (self.vault / ".beyin-runtime.json").write_text("{not json", encoding="utf-8")
        report = self.report()
        self.assertEqual(report["pin_status"], "unreadable")
        self.assertEqual(report["warnings"], [])

    def test_package_container_pin_warns_on_every_platform(self):
        self.pin(CONTAINER)
        report = self.report()
        self.assertTrue(report["pinned_in_package_container"])
        self.assertIn("pinned_in_package_container", self.codes(report))
        self.assertTrue(any("https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/UPDATE.md" in warning
                            for warning in report["warnings"]))

    def test_plain_local_pin_is_not_a_container(self):
        self.installed(self.state)
        self.pin(str(self.state))
        self.assertFalse(self.report()["pinned_in_package_container"])

    def test_localcache_before_packages_is_not_a_container(self):
        self.pin("D:" + SEP + "LocalCache" + SEP + "Packages" + SEP + "beyin-v3")
        self.assertFalse(self.report()["pinned_in_package_container"])

    def test_empty_pinned_state_warns(self):
        self.pin(str(self.state / "elsewhere"))
        report = self.report(self.state / "elsewhere")
        self.assertFalse(report["installed_at_pinned_state"])
        self.assertIn("pinned_state_empty", self.codes(report))

    def test_effective_state_differing_from_pin_warns(self):
        other = self.state.parent / "other"
        self.installed(self.state)
        self.installed(other)
        self.pin(str(self.state))
        self.assertIn("effective_state_differs", self.codes(self.report(other)))

    def default_root(self, base, key, package=None):
        """Build a default-layout state root, optionally behind a package container."""
        parts = [base, "Packages", package, "LocalCache", "Local"] if package else [base]
        root = Path(*[str(part) for part in parts]) / "beyin-v3" / key
        self.installed(root)
        return root

    def test_one_default_root_is_not_a_split(self):
        local = self.state.parent / "Local"
        root = self.default_root(local, "ab12")
        self.pin(str(root))
        report = self.report(root)
        self.assertEqual(report["sibling_state_roots"], [str(root)])
        self.assertNotIn("state_split", self.codes(report))

    def test_both_sides_of_the_redirect_are_reported_as_a_split(self):
        local = self.state.parent / "Local"
        plain = self.default_root(local, "ab12")
        contained = self.default_root(local, "ab12", package="Client_pzs8sxrjxfjjc")
        self.pin(str(plain))
        report = self.report(plain)
        self.assertEqual(report["sibling_state_roots"], [str(plain), str(contained)])
        self.assertIn("state_split", self.codes(report))

    def test_a_split_is_found_from_the_contained_side_too(self):
        local = self.state.parent / "Local"
        plain = self.default_root(local, "ab12")
        contained = self.default_root(local, "ab12", package="Client_pzs8sxrjxfjjc")
        self.pin(str(contained))
        self.assertIn("state_split", self.codes(self.report(contained)))

    def test_another_vault_key_is_not_this_vault_split(self):
        local = self.state.parent / "Local"
        mine = self.default_root(local, "ab12")
        self.default_root(local, "cd34", package="Client_pzs8sxrjxfjjc")
        self.pin(str(mine))
        report = self.report(mine)
        self.assertEqual(report["sibling_state_roots"], [str(mine)])
        self.assertNotIn("state_split", self.codes(report))

    def test_explicit_state_root_has_no_key_layout_to_compare(self):
        self.installed(self.state)
        self.pin(str(self.state))
        self.assertEqual(self.report()["sibling_state_roots"], [])

    def test_report_shape_is_stable_with_and_without_a_pin(self):
        without = set(self.report())
        self.pin(str(self.state))
        self.assertEqual(without, set(self.report()))

    def test_redirected_spelling_of_one_root_is_not_a_split(self):
        # Inside the package the plain spelling reaches the container: one directory, two strings.
        local = self.state.parent / "Local"
        contained = self.default_root(local, "ab12", package="Client_pzs8sxrjxfjjc")
        (local / "beyin-v3").mkdir(parents=True)
        if link_directory(contained, local / "beyin-v3" / "ab12") is None:
            self.skipTest("directory links unavailable on this runner")
        self.pin(str(contained))
        report = self.report(contained)
        self.assertEqual(len(report["sibling_state_roots"]), 1)
        self.assertNotIn("state_split", self.codes(report))

    def test_pin_resolving_elsewhere_warns_only_on_windows(self):
        target = self.state.parent / "real-state"
        self.installed(target)
        link = self.state.parent / "linked-state"
        if link_directory(target, link) is None:
            self.skipTest("directory links unavailable on this runner")
        self.pin(str(link))
        posix = CLI.state_location(self.vault, target, windows=False)
        self.assertTrue(posix["pin_resolves_elsewhere"])
        self.assertEqual(posix["warnings"], [])
        windows = CLI.state_location(self.vault, target, windows=True)
        self.assertEqual(self.codes(windows), ["pin_resolves_elsewhere"])

    def test_runtime_config_that_is_not_an_object_is_unreadable(self):
        (self.vault / ".beyin-runtime.json").write_text("[1]", encoding="utf-8")
        report = self.report()
        self.assertEqual(report["pin_status"], "unreadable")
        self.assertEqual(report["warnings"], [])

    def test_unresolvable_pin_is_reported_without_rethrowing(self):
        invalid = str(self.state / "bad") + "\0state"
        self.pin(invalid)
        report = self.report()
        self.assertEqual(report["pinned_state"], invalid)
        self.assertIn("invalid_state_path", self.codes(report))
        self.assertFalse(report["installed_at_pinned_state"])

    def test_invalid_pin_falls_back_even_when_resolve_accepts_it(self):
        # Windows 3.13 non-strict realpath can return a NUL-containing path unchanged.
        invalid = self.state / "bad\0state"
        self.pin(str(invalid))
        original = Path.resolve

        def nonstrict(path, *args, **kwargs):
            return path if path == invalid else original(path, *args, **kwargs)

        for resolve in (CLI.resolve_state, CORE.resolve_state):
            with self.subTest(resolve=resolve.__module__), \
                    patch.object(Path, "resolve", nonstrict), \
                    patch.dict(os.environ, isolated_env(self.state.parent / "home"), clear=True):
                expected = CLI.default_state(self.vault).resolve()
                self.assertEqual(resolve(self.vault, invalid), expected)
                report = self.report()
                self.assertEqual(self.codes(report), ["invalid_state_path"])
                self.assertIsNone(report["pinned_resolves_here_to"])

    def test_invalid_pin_stat_error_is_reported_but_missing_state_is_allowed(self):
        candidate = self.state / "not-created"
        self.pin(str(candidate))
        original = Path.stat

        def invalid_stat(path, *args, **kwargs):
            if path == candidate:
                raise OSError(22, "invalid state path")
            return original(path, *args, **kwargs)

        for resolve in (CLI.resolve_state, CORE.resolve_state):
            with self.subTest(resolve=resolve.__module__), \
                    patch.dict(os.environ, isolated_env(self.state.parent / "home"), clear=True):
                self.assertEqual(resolve(self.vault, candidate), candidate.resolve())
                with patch.object(Path, "stat", invalid_stat):
                    self.assertEqual(resolve(self.vault, candidate), CLI.default_state(self.vault).resolve())
                    self.assertEqual(self.codes(self.report()), ["invalid_state_path"])

    def test_installed_doctor_and_hook_keep_the_invalid_pin_fallback(self):
        home = self.state.parent / "home"
        env = isolated_env(home)
        installed = install(self.vault, self.state, env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.pin(str(self.state / "bad") + "\0state")
        doctor = run_python(self.vault / "beyin.py", ["doctor"], self.vault, env)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        location = json.loads(doctor.stdout)["state_location"]
        self.assertIn("invalid_state_path", self.codes(location))
        self.assertIn(home, Path(location["effective_state"]).parents)
        self.assertIn(b"install_v3.py", doctor.stderr)
        # argv cannot contain NUL through the OS; inject the invalid parsed state inside a
        # fresh interpreter to exercise the same hook fallback used for inaccessible UNC pins.
        driver = """
import sys
from pathlib import Path
vault = Path(sys.argv[1])
sys.path.insert(0, str(vault / '.claude/scripts'))
import beyin_v3_hook
sys.argv = ['hook', '--vault', str(vault), '--state', str(vault.parent / 'bad') + chr(0),
            '--harness', 'claude', '--metadata-only']
beyin_v3_hook.main()
"""
        hook = subprocess.run([sys.executable, "-c", driver, str(self.vault)],
                              input=json.dumps({"hook_event_name": "SessionEnd", "session_id": "invalid-pin"}),
                              text=True, encoding="utf-8", capture_output=True, cwd=self.vault,
                              env=env, timeout=30)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertEqual(json.loads(hook.stdout), {})
        self.assertTrue((Path(location["effective_state"]) / "hook-queue").is_dir())

    def test_foreign_paths_in_state_location(self):
        paths = [
            r"C:\Users\User\AppData\Local\beyin-v3\1234567890abcdef",
            r"/home/user/.local/state/beyin-v3/1234567890abcdef",
            r"\\server\share\state",
            r"//server/share/state",
            r"C:\Path with spaces\state",
            r"/path with spaces\state",
            r"\\?\C:\state",
            r"relative/path",
            r"C:",
            r"state",
            r" ",
            r"",
        ]
        for path in paths:
            self.pin(path)
            try:
                report = self.report()
                self.assertIn("pin_status", report)
                CLI.resolve_state(self.vault, Path(path))
            except Exception as e:
                self.fail(f"Path {path!r} caused a crash: {e}")

    def doctor(self, driver):
        home = self.state.parent / "home"
        result = subprocess.run([sys.executable, "-c", driver, str(ROOT / "scripts/beyin_v3.py"),
                                 "--vault", str(self.vault), "--state", str(self.state), "doctor"],
                                capture_output=True, text=True, encoding="utf-8", env=isolated_env(home),
                                cwd=str(self.state.parent), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_doctor_reports_a_quiet_location_and_keeps_its_status(self):
        self.installed(self.state)
        self.pin(str(self.state))
        doctor = self.doctor(DRIVER)
        self.assertEqual(doctor["state_location"]["warnings"], [])
        self.assertNotEqual(doctor.get("status"), "needs_attention")

    def test_a_failing_location_report_never_hides_doctor(self):
        doctor = self.doctor(BOOM_DRIVER)
        self.assertEqual(doctor["state_location"]["status"], "unavailable")
        self.assertIn("lifecycle", doctor)

    def test_human_doctor_names_each_warning_in_ascii(self):
        spec = importlib.util.spec_from_file_location("state_location_entry", ROOT / "scripts/beyin_entry.py")
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        location = {"warnings": ["state_split: x", "pinned_in_package_container: x",
                                 "pin_resolves_elsewhere: x", "pinned_state_empty: x",
                                 "pinned_state_not_absolute: x"],
                    "sibling_state_roots": ["A", "B"], "pinned_resolves_here_to": "C"}
        text = entry.human_result({"status": "observed_metadata", "state_location": location}, "doctor", "3.4.0")
        lines = [line for line in text.splitlines() if line.startswith("State konumu (bilgi): ")]
        self.assertEqual(len(lines), 5)
        self.assertIn("(A, B)", lines[0])
        self.assertIn("https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/UPDATE.md", lines[0])
        self.assertIn("https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/UPDATE.md", lines[1])
        self.assertIn("(C)", lines[2])
        self.assertIn("https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/MULTI-MACHINE.md", lines[3])
        self.assertTrue(all(line.isascii() for line in lines))
        quiet = entry.human_result({"status": "observed_metadata",
                                    "state_location": {"status": "unavailable", "warnings": []}}, "doctor", "3.4.0")
        self.assertNotIn("State konumu", quiet)


# Runs the source CLI in a clean interpreter; BOOM_DRIVER makes the location report fail.
DRIVER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("beyin_v3_cli_doctor", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
"""
BOOM_DRIVER = DRIVER + """
def boom(*args, **kwargs):
    raise OSError("boom")
module.state_location = boom
"""
DRIVER += "raise SystemExit(module.main(sys.argv[2:]))\n"
BOOM_DRIVER += "raise SystemExit(module.main(sys.argv[2:]))\n"


if __name__ == "__main__":
    unittest.main()
