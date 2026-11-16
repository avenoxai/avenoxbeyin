"""Tests for kur.py. Run from the repo root:

    python -m unittest discover -s ege -p "test_*.py" -v

Everything happens in a temporary directory: CLAUDE_CONFIG_DIR, HOME and USERPROFILE point at temp
folders, the vault is a temp folder with a recording ``beyin.py`` stub. The real ~/.claude and the
real vault are never touched.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "hooks"))
import install_git_devir as igd  # noqa: E402
import kur  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
USER_TEXT = "# Benim kurallarım\n\n- Türkçe konuş.\n"
STUB = r'''
import json, os, sys
from pathlib import Path

here = Path(__file__).resolve().parent
with (here / "calls.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\n")
fail = here / "fail.txt"
if fail.exists() and fail.read_text(encoding="utf-8").strip() in " ".join(sys.argv[1:]):
    print("stub: istenen hata", file=sys.stderr)
    sys.exit(3)
if sys.argv[1:2] == ["bridge"] and "--install" in sys.argv:
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    groups = data.setdefault("hooks", {}).setdefault("Stop", [])
    groups[:] = [g for g in groups if "beyin_v3_bridge.py" not in json.dumps(g)]
    groups.append({"hooks": [{"type": "command", "command": "python /v/beyin_v3_bridge.py --harness claude"}]})
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("stub ok")
'''


def _force_remove(func, path, _info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def snapshot(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


class KurCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kur-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, onerror=_force_remove)
        self.cfg = self.tmp / "claude"
        self.cfg.mkdir()
        self.kod = self.tmp / "Kod"
        self.kod.mkdir()
        self.vault = self.tmp / "vault"
        (self.vault / "30 Projeler").mkdir(parents=True)
        (self.vault / "beyin.py").write_text(STUB, encoding="utf-8")
        (self.vault / "not.md").write_text("vault notu\n", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg), "HOME": str(self.tmp),
                                               "USERPROFILE": str(self.tmp), "PYTHONIOENCODING": "utf-8"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.existing = {
            "model": "opus",
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "timeout": 20,
                                             "command": "python /x/beyin_v3_bridge.py --event SessionStart"}]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}],
            },
        }
        self.settings = self.cfg / "settings.json"
        self.settings.write_text(json.dumps(self.existing, indent=2), encoding="utf-8")
        self.claude_md = self.cfg / "CLAUDE.md"
        self.claude_md.write_bytes(USER_TEXT.encode("utf-8"))
        (self.cfg / "agents").mkdir()
        (self.cfg / "agents" / "benim.md").write_text("---\nname: benim\n---\nkendi ajanım\n", encoding="utf-8")

    # ------------------------------------------------------------------ helpers

    def kur(self, *extra, code=0, base=True):
        args = (["--machine", "is-5090", "--project-root", str(self.kod), "--vault", str(self.vault)]
                if base else [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                got = kur.main([*args, *extra])
            except SystemExit as exc:
                got = exc.code if isinstance(exc.code, int) else 1
                out.write(str(exc.code))
        self.assertEqual(got, code, out.getvalue())
        return out.getvalue()

    def calls(self):
        path = self.vault / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def hooks(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]

    def commands(self, event):
        return [igd._decoded(handler["command"]) for group in self.hooks().get(event, [])
                for handler in group["hooks"]]

    def backups(self):
        root = self.cfg / kur.BACKUP_DIR
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    def fake_ege(self):
        """A stand-in for the ege/ folder, so tests can change what the fork ships."""
        ege = self.tmp / "ege"
        (ege / "agents").mkdir(parents=True)
        (ege / "agents" / "yazici.md").write_text("---\nname: yazici\n---\nsürüm 1\n", encoding="utf-8")
        (ege / "agents" / "README.md").write_text("ajan değil\n", encoding="utf-8")
        skill = ege / "skills" / "ornek-skill"
        (skill / "ref").mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: ornek-skill\n---\nskill\n", encoding="utf-8")
        (skill / "ref" / "ek.md").write_text("ek\n", encoding="utf-8")
        vault_only = ege / "skills" / "vault-skill"
        vault_only.mkdir()
        (vault_only / "SKILL.md").write_text("---\nname: vault-skill\n---\n", encoding="utf-8")
        (vault_only / kur.VAULT_ONLY_MARK).write_text("", encoding="utf-8")
        (ege / "skills" / "README.md").write_text("belge\n", encoding="utf-8")
        patcher = mock.patch.object(kur, "EGE", ege)
        patcher.start()
        self.addCleanup(patcher.stop)
        return ege

    # ------------------------------------------------------------------ tests

    def test_dry_run_writes_nothing(self):
        before = snapshot(self.tmp)
        out = self.kur("--dry-run", "--ayarlar")
        self.assertEqual(snapshot(self.tmp), before)
        self.assertEqual(self.calls(), [])
        self.assertIn("DENEME", out)
        self.assertIn("[yapılacak]", out)
        self.assertNotIn("[yapıldı]", out)
        for expected in ("git-devir.json", "git_devir.py start", "proje.py start", "beyin-yazici.md",
                         "CLAUDE.md", "bridge --harness claude", "--inbox-folder"):
            self.assertIn(expected, out)
        self.assertIn("4 dosya değişikliği yapılacak", out)
        self.assertIn("beyin-yazici.md kopyalanacak", out)

    def test_dry_run_default_target_is_home(self):
        env = dict(os.environ)
        env.pop("CLAUDE_CONFIG_DIR")
        proc = subprocess.run([sys.executable, str(HERE / "kur.py"), "--machine", "ev", "--project-root",
                               str(self.kod), "--vault", str(self.vault), "--dry-run"], env=env,
                              capture_output=True, timeout=60, creationflags=NO_WINDOW)
        out = proc.stdout.decode("utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn(f"hedef: {self.tmp / '.claude'}", out)
        self.assertFalse((self.tmp / ".claude").exists())

    def test_install_keeps_existing_and_is_idempotent(self):
        out = self.kur()
        self.assertIn("Sıradaki adımlar", out)
        hooks = self.hooks()
        self.assertEqual(hooks["PreToolUse"], self.existing["hooks"]["PreToolUse"])
        self.assertEqual(hooks["SessionStart"][0], self.existing["hooks"]["SessionStart"][0])
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))["model"], "opus")
        start = "\n".join(self.commands("SessionStart"))
        self.assertIn("beyin_v3_bridge.py", start)
        self.assertEqual(len(self.commands("SessionStart")), 3)
        self.assertRegex(start, r"git_devir\.py'? '?start")
        self.assertRegex(start, r"proje\.py'? '?start")
        self.assertRegex("\n".join(self.commands("SessionEnd")), r"git_devir\.py'? '?end")
        self.assertIn("beyin_v3_bridge.py", "\n".join(self.commands("Stop")))  # added by the bridge stub
        config = json.loads((self.cfg / "git-devir.json").read_text(encoding="utf-8"))
        self.assertEqual((config["machine"], config["project_roots"], config["vault_path"]),
                         ("is-5090", [str(self.kod)], str(self.vault)))
        self.assertEqual(Path(config["proje_py"]), HERE / "hooks" / "proje.py")
        self.assertEqual(config["python"], sys.executable)
        agent = self.cfg / "agents" / "beyin-yazici.md"
        self.assertEqual(agent.read_bytes(), (HERE / "agents" / "beyin-yazici.md").read_bytes())
        self.assertTrue((self.cfg / "agents" / "benim.md").exists())
        self.assertFalse((self.cfg / "skills" / "beyin-skill-onerici").exists())  # vault-only skill
        self.assertIn("skills/beyin-skill-onerici: yalnız vault", out)
        text = self.claude_md.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(USER_TEXT + "\n" + kur.BLOCK_START + "\n"))
        self.assertTrue(text.endswith(kur.BLOCK_END + "\n"))
        self.assertIn("`beyin-yazici` alt ajanına ver", text)
        self.assertEqual(self.calls(), [["bridge", "--harness", "claude", "--project-root", str(self.kod),
                                         "--install"]])
        self.assertEqual(len(self.backups()), 1)
        saved = self.cfg / kur.BACKUP_DIR / self.backups()[0]
        self.assertEqual(json.loads((saved / "settings.json").read_text(encoding="utf-8")), self.existing)
        self.assertEqual((saved / "CLAUDE.md").read_bytes(), USER_TEXT.encode("utf-8"))

        before = snapshot(self.cfg)
        out = self.kur()
        self.assertEqual(snapshot(self.cfg), before)
        self.assertIn("0 dosya değişikliği yapıldı; 0 sorun", out)
        self.assertNotIn("[yapıldı]", out)
        self.assertEqual(len(self.backups()), 1)

    def test_installed_hook_command_runs(self):
        self.kur()
        repo = self.kod / "demo"
        repo.mkdir()
        (self.vault / "30 Projeler" / "demo").mkdir()
        command = next(h["command"] for g in self.hooks()["SessionStart"] for h in g["hooks"]
                       if "proje.py" in igd._decoded(h["command"]))
        proc = subprocess.run(command, shell=True, input=json.dumps({"cwd": str(repo)}).encode(),
                              capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        text = json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Proje: demo (local/demo) → 30 Projeler/demo", text)

    def test_managed_block_refresh_keeps_outside_text(self):
        self.kur()
        text = self.claude_md.read_text(encoding="utf-8")
        start, end = text.index(kur.BLOCK_START), text.index(kur.BLOCK_END)
        stale = text[:start] + kur.BLOCK_START + "\neski içerik\n" + text[end:] + "\n## Sonradan eklenen\nmetin\n"
        self.claude_md.write_text(stale, encoding="utf-8", newline="\n")
        out = self.kur()
        self.assertIn("yönetilen blok yenilendi", out)
        fresh = self.claude_md.read_text(encoding="utf-8")
        self.assertEqual(fresh, text + "\n## Sonradan eklenen\nmetin\n")
        self.assertNotIn("eski içerik", fresh)
        self.assertEqual(len(self.backups()), 2)

    def test_claude_md_created_crlf_and_damaged_markers(self):
        self.claude_md.unlink()
        self.kur()
        created = self.claude_md.read_text(encoding="utf-8")
        self.assertTrue(created.startswith(kur.BLOCK_START + "\n"))
        self.kur("--uninstall", base=False)
        self.assertFalse(self.claude_md.exists())

        self.claude_md.write_bytes(b"ilk\r\nikinci\r\n")
        self.kur()
        raw = self.claude_md.read_bytes()
        self.assertTrue(raw.startswith(b"ilk\r\nikinci\r\n\r\n" + kur.BLOCK_START.encode()))
        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"))
        self.kur("--uninstall", base=False)
        self.assertEqual(self.claude_md.read_bytes(), b"ilk\r\nikinci\r\n")

        damaged = USER_TEXT + kur.BLOCK_START + "\nyarım blok\n"
        self.claude_md.write_text(damaged, encoding="utf-8", newline="\n")
        out = self.kur(code=1)
        self.assertIn("işaretleri bozuk", out)
        self.assertEqual(self.claude_md.read_text(encoding="utf-8"), damaged)
        self.assertTrue((self.cfg / "agents" / "beyin-yazici.md").exists())  # the other steps still ran

    def test_changed_destination_needs_force(self):
        self.kur()
        agent = self.cfg / "agents" / "beyin-yazici.md"
        agent.write_text("elle değiştirdim\n", encoding="utf-8")
        out = self.kur(code=1)
        self.assertIn("dokunulmadı", out)
        self.assertIn("--force", out)
        self.assertEqual(agent.read_text(encoding="utf-8"), "elle değiştirdim\n")
        out = self.kur("--force")
        self.assertIn("üzerine yazıldı", out)
        self.assertEqual(agent.read_bytes(), (HERE / "agents" / "beyin-yazici.md").read_bytes())
        saved = [p for p in (self.cfg / kur.BACKUP_DIR).rglob("beyin-yazici.md")]
        self.assertEqual([p.read_text(encoding="utf-8") for p in saved], ["elle değiştirdim\n"])

    def test_foreign_file_with_same_name_is_not_overwritten(self):
        agent = self.cfg / "agents" / "beyin-yazici.md"
        agent.write_text("başka kaynaktan gelen ajan\n", encoding="utf-8")
        self.kur(code=1)
        self.assertEqual(agent.read_text(encoding="utf-8"), "başka kaynaktan gelen ajan\n")
        self.kur("--uninstall", base=False)
        self.assertEqual(agent.read_text(encoding="utf-8"), "başka kaynaktan gelen ajan\n")

    def test_skills_updates_and_stale_files(self):
        ege = self.fake_ege()
        out = self.kur()
        self.assertEqual(sorted(p.relative_to(self.cfg).as_posix() for p in (self.cfg / "skills").rglob("*.md")),
                         ["skills/ornek-skill/SKILL.md", "skills/ornek-skill/ref/ek.md"])
        self.assertEqual(sorted(p.name for p in (self.cfg / "agents").iterdir()), ["benim.md", "yazici.md"])
        self.assertIn("skills/vault-skill: yalnız vault", out)
        manifest = json.loads((self.cfg / kur.MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["files"]), ["agents/yazici.md", "skills/ornek-skill/SKILL.md",
                                                     "skills/ornek-skill/ref/ek.md"])
        (ege / "agents" / "yazici.md").write_text("---\nname: yazici\n---\nsürüm 2\n", encoding="utf-8")
        (ege / "skills" / "ornek-skill" / "ref" / "ek.md").unlink()
        out = self.kur()
        self.assertIn("güncellendi", out)
        self.assertIn("sürüm 2", (self.cfg / "agents" / "yazici.md").read_text(encoding="utf-8"))
        self.assertFalse((self.cfg / "skills" / "ornek-skill" / "ref").exists())
        self.assertTrue((self.cfg / "skills" / "ornek-skill" / "SKILL.md").exists())
        self.kur("--uninstall", base=False)
        self.assertFalse((self.cfg / "skills").exists())
        self.assertEqual([p.name for p in (self.cfg / "agents").iterdir()], ["benim.md"])

    def test_uninstall_removes_only_its_own_things(self):
        self.kur("--ayarlar")
        after_bridge = self.hooks()["Stop"]
        (self.cfg / "skills" / "kendi-skill").mkdir(parents=True)
        (self.cfg / "skills" / "kendi-skill" / "SKILL.md").write_text("benim\n", encoding="utf-8")
        vault_before = snapshot(self.vault)
        config_before = (self.cfg / "git-devir.json").read_bytes()
        dry = self.kur("--uninstall", "--dry-run", base=False)
        self.assertIn("[yapılacak]", dry)
        self.assertTrue((self.cfg / "agents" / "beyin-yazici.md").exists())
        out = self.kur("--uninstall", base=False)
        self.assertIn("bridge --harness claude --uninstall", out)
        expected = json.loads(json.dumps(self.existing))
        expected["hooks"]["Stop"] = after_bridge  # the avenox bridge is not ours to remove
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), expected)
        self.assertEqual(self.claude_md.read_bytes(), USER_TEXT.encode("utf-8"))
        self.assertFalse((self.cfg / "agents" / "beyin-yazici.md").exists())
        self.assertTrue((self.cfg / "agents" / "benim.md").exists())
        self.assertTrue((self.cfg / "skills" / "kendi-skill" / "SKILL.md").exists())
        self.assertFalse((self.cfg / kur.MANIFEST).exists())
        self.assertEqual((self.cfg / "git-devir.json").read_bytes(), config_before)
        self.assertEqual(snapshot(self.vault), vault_before)
        again = self.kur("--uninstall", base=False)
        self.assertIn("0 dosya değişikliği", again)

    def test_uninstall_leaves_a_changed_copy(self):
        self.kur()
        agent = self.cfg / "agents" / "beyin-yazici.md"
        agent.write_text("kurulumdan sonra değişti\n", encoding="utf-8")
        out = self.kur("--uninstall", base=False, code=1)
        self.assertIn("bırakıldı", out)
        self.assertEqual(agent.read_text(encoding="utf-8"), "kurulumdan sonra değişti\n")

    def test_ayarlar_runs_preferences_against_the_vault_cli(self):
        root2 = self.tmp / "İkinci Kök"
        root2.mkdir()
        out = self.kur("--ayarlar", "--project-root", str(root2))
        self.assertEqual(self.calls(), [
            ["bridge", "--harness", "claude", "--project-root", str(self.kod), "--project-root", str(root2),
             "--install"],
            ["preferences", "--project-context", "on"],
            ["preferences", "--secret-filter", "on"],
            ["preferences", "--context-dedupe", "on"],
            ["preferences", "--inbox-report", "on", "--inbox-folder", "00 Gelen"],
        ])
        self.assertEqual(out.count("[tamam] çıkış kodu 0"), 5)
        self.assertNotIn("stub ok", out)  # command output is shown only on failure
        self.assertEqual(snapshot(self.vault).keys(), {"beyin.py", "not.md", "calls.jsonl"})

    def test_failing_vault_command_is_reported_and_nothing_is_undone(self):
        (self.vault / "fail.txt").write_text("--secret-filter", encoding="utf-8")
        out = self.kur("--ayarlar", code=1)
        self.assertIn("başarısız (çıkış kodu 3)", out)
        self.assertIn("stub: istenen hata", out)
        self.assertEqual(len(self.calls()), 5)  # the commands after the failing one still ran
        self.assertTrue((self.cfg / "agents" / "beyin-yazici.md").exists())
        self.assertTrue((self.cfg / kur.MANIFEST).exists())
        self.assertIn(kur.BLOCK_START, self.claude_md.read_text(encoding="utf-8"))

    def test_vault_without_beyin_py_skips_the_bridge(self):
        (self.vault / "beyin.py").unlink()
        out = self.kur("--ayarlar", code=1)
        self.assertIn("köprü adımı atlandı", out)
        self.assertRegex("\n".join(self.commands("SessionStart")), r"proje\.py'? '?start")

    def test_bad_arguments_change_nothing(self):
        before = snapshot(self.tmp)
        for args in (["--machine", "my host", "--project-root", str(self.kod), "--vault", str(self.vault)],
                     ["--machine", "ev", "--project-root", str(self.tmp / "yok"), "--vault", str(self.vault)],
                     ["--machine", "ev", "--project-root", str(self.kod), "--vault", str(self.tmp / "yok")],
                     ["--machine", "ev", "--vault", str(self.vault)]):
            self.kur(*args, base=False, code=1)
            self.assertEqual(snapshot(self.tmp), before, args)
        self.settings.write_text("{bozuk", encoding="utf-8")
        before = snapshot(self.tmp)
        self.kur(code=1)
        self.assertEqual(snapshot(self.tmp), before)


if __name__ == "__main__":
    unittest.main()
