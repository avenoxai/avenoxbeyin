"""Doctor warns when git can rewrite receipt line endings in the vault (#205).

Receipts are compared byte for byte. With core.autocrlf=true (the Git for Windows
default) a receipt pulled from another machine is checked out as CRLF unless the vault
pins receipts/ with `-text` or `eol=lf`. These tests pin that git behaviour, the report
shape for each attribute and the doctor wiring. Information only: the status is untouched.
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

from v3_package_helpers import isolated_env

ROOT = Path(os.environ.get("BEYIN_TEST_REPO", Path(__file__).resolve().parents[1]))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = load("scripts/beyin_v3.py", "beyin_v3_cli_line_endings")
ENTRY = load("scripts/beyin_entry.py", "beyin_entry_line_endings")
# No user or system git config: the tests set core.autocrlf themselves.
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class ReceiptLineEndingsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="v3-receipt-eol-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.vault = self.base / "vault"
        self.vault.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")

    def git(self, *args, cwd=None):
        done = subprocess.run(("git",) + args, cwd=str(cwd or self.vault), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def attributes(self, text):
        (self.vault / ".gitattributes").write_text(text, encoding="utf-8", newline="\n")

    def report(self):
        return CLI.receipt_line_endings(self.vault)

    def test_autocrlf_without_an_attribute_warns(self):
        self.git("config", "core.autocrlf", "true")
        report = self.report()
        self.assertEqual(report["status"], "warning")
        self.assertIn("receipts/** -text", report["warning"])

    def test_unrelated_attributes_still_warn(self):
        self.git("config", "core.autocrlf", "true")
        self.attributes("**/Journal.md merge=union\n")
        self.assertEqual(self.report()["status"], "warning")

    def test_text_auto_does_not_pin_receipts(self):
        self.git("config", "core.autocrlf", "true")
        self.attributes("receipts/** text=auto\n")
        self.assertEqual(self.report()["status"], "warning")

    def test_minus_text_is_quiet(self):
        self.git("config", "core.autocrlf", "true")
        self.attributes("receipts/** -text\n")
        self.assertEqual(self.report()["status"], "ok")

    def test_eol_lf_is_quiet(self):
        self.git("config", "core.autocrlf", "true")
        self.attributes("receipts/** eol=lf\n")
        self.assertEqual(self.report()["status"], "ok")

    def test_autocrlf_off_or_input_is_quiet(self):
        for value in ("false", "input"):
            self.git("config", "core.autocrlf", value)
            self.assertEqual(self.report()["status"], "ok", value)

    def test_unset_autocrlf_is_quiet(self):
        self.assertEqual(self.report()["status"], "ok")

    def test_a_vault_outside_git_is_not_applicable(self):
        plain = self.base / "plain"
        plain.mkdir()
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.base)}):
            self.assertEqual(CLI.receipt_line_endings(plain)["status"], "not_applicable")

    def test_a_missing_git_never_raises(self):
        with mock.patch.object(CLI.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(self.report(), {"status": "unavailable", "error": "FileNotFoundError"})

    def checkout_in_autocrlf_clone(self):
        """Commit an LF receipt, clone it with core.autocrlf=true and return the cloned bytes."""
        receipts = self.vault / "receipts"
        receipts.mkdir()
        (receipts / ("a" * 64 + ".md")).write_bytes(b"---\nkind: receipt\n---\nsummary\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "receipt")
        clone = self.base / "clone"
        self.git("clone", "-q", "-c", "core.autocrlf=true", str(self.vault), str(clone), cwd=self.base)
        return (clone / "receipts" / ("a" * 64 + ".md")).read_bytes()

    def test_git_rewrites_receipts_without_the_attribute_and_keeps_bytes_with_it(self):
        # The behaviour the warning is about, and the two ways the docs say to stop it.
        self.assertIn(b"\r\n", self.checkout_in_autocrlf_clone())
        for index, attribute in enumerate(("receipts/** -text\n", "receipts/** eol=lf\n")):
            with self.subTest(attribute=attribute):
                self.base = self.base / str(index)
                self.base.mkdir()
                self.vault = self.base / "vault"
                self.vault.mkdir()
                self.git("init", "-q")
                self.git("config", "user.email", "t@example.com")
                self.git("config", "user.name", "t")
                self.attributes(attribute)
                self.assertNotIn(b"\r", self.checkout_in_autocrlf_clone())

    def doctor(self, human=False):
        state = self.base / "state"
        state.mkdir(exist_ok=True)
        env = isolated_env(self.base / "home")
        env.update(GIT_ENV)
        # isolated_env's PATH is the interpreter folder plus os.defpath; on Windows git lives
        # elsewhere (C:/Program Files/Git/cmd) and doctor would report `unavailable`.
        env["PATH"] = str(Path(shutil.which("git")).parent) + os.pathsep + env["PATH"]
        done = subprocess.run([sys.executable, str(ROOT / "scripts/beyin_v3.py"), "--vault", str(self.vault),
                               "--state", str(state), "doctor"], capture_output=True, text=True,
                              encoding="utf-8", env=env, cwd=str(self.base), timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_doctor_carries_the_warning_and_keeps_its_status(self):
        quiet = self.doctor()
        self.assertEqual(quiet["receipt_line_endings"]["status"], "ok")
        self.git("config", "core.autocrlf", "true")
        warned = self.doctor()
        self.assertEqual(warned["receipt_line_endings"]["status"], "warning")
        self.assertEqual(warned["status"], quiet["status"])

    def test_human_doctor_names_the_warning_in_ascii_only_when_it_applies(self):
        warning = {"status": "warning", "warning": "line_endings: x"}
        text = ENTRY.human_result({"status": "never_seen", "receipt_line_endings": warning}, "doctor", "3.7.1")
        lines = [line for line in text.splitlines() if line.startswith("Receipt satir sonu (bilgi): ")]
        self.assertEqual(len(lines), 1)
        lines[0].encode("ascii")
        self.assertIn("receipts/** -text", lines[0])
        quiet = ENTRY.human_result({"status": "never_seen", "receipt_line_endings": {"status": "ok"}}, "doctor", "3.7.1")
        self.assertNotIn("Receipt satir sonu", quiet)


if __name__ == "__main__":
    unittest.main()
