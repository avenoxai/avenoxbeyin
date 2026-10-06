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

from v3_package_helpers import isolated_env
from v3_transaction_root_test import link_directory

ROOT = Path(os.environ.get("BEYIN_TEST_REPO", Path(__file__).resolve().parents[1]))
SPEC = importlib.util.spec_from_file_location("beyin_v3_cli_under_test", ROOT / "scripts/beyin_v3.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)

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

    def test_foreign_state_detection_across_platforms(self):
        spec = importlib.util.spec_from_file_location("entry_under_test", ROOT / "scripts/beyin_entry.py")
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        windows_path = "C:\\Users\\ada\\AppData\\Local\\beyin-v3\\ab12"
        posix_path = "/Users/ada/Library/Application Support/beyin-v3/ab12"
        linux_path = "/home/ada/.local/state/beyin-v3/ab12"
        if sys.platform == "win32":
            self.assertTrue(entry._is_foreign_or_invalid_state(posix_path))
            self.assertTrue(entry._is_foreign_or_invalid_state(linux_path))
            self.assertTrue(CLI._is_foreign_or_invalid_state(posix_path))
        else:
            self.assertTrue(entry._is_foreign_or_invalid_state(windows_path))
            self.assertTrue(CLI._is_foreign_or_invalid_state(windows_path))
        self.assertTrue(entry._is_foreign_or_invalid_state(""))
        self.assertTrue(entry._is_foreign_or_invalid_state(None))

    def test_entry_point_fallback_when_runtime_missing_or_foreign(self):
        spec = importlib.util.spec_from_file_location("entry_under_test", ROOT / "scripts/beyin_entry.py")
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        fallback = entry._default_state(self.vault)
        self.assertIn("beyin-v3", str(fallback))
        self.assertNotEqual(str(fallback), str(self.vault))


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
                                 "pin_resolves_elsewhere: x", "pinned_state_empty: x"],
                    "sibling_state_roots": ["A", "B"], "pinned_resolves_here_to": "C"}
        text = entry.human_result({"status": "observed_metadata", "state_location": location}, "doctor", "3.4.0")
        lines = [line for line in text.splitlines() if line.startswith("State konumu (bilgi): ")]
        self.assertEqual(len(lines), 4)
        self.assertIn("(A, B)", lines[0])
        self.assertIn("(C)", lines[2])
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
