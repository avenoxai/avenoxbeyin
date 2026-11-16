"""Tests for proje.py. Run from the repo root:

    python -m unittest discover -s ege/hooks -p "test_*.py" -v

Everything happens in a temporary directory: a temp vault, temp repos with fake remote URLs (proje.py
never talks to the network) and a temp CLAUDE_CONFIG_DIR/HOME. The real ~/.claude and the real
vault are never touched.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "proje.py"
sys.path.insert(0, str(HERE))
import proje  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
P = proje.PROJECTS_DIR


def _force_remove(func, path, _info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


class ProjeCase(unittest.TestCase):
    machine = "is-5090"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="proje-")).resolve()
        home = self.tmp / "home"
        home.mkdir()
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n"
                             "[init]\n\tdefaultBranch = main\n[core]\n\tautocrlf = false\n", encoding="utf-8")
        self.cfgdir = self.tmp / "claude"
        self.cfgdir.mkdir()
        self.vault = self.tmp / "vault"
        (self.vault / P).mkdir(parents=True)
        self.root = self.tmp / "Kod"
        self.root.mkdir()
        env = {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(home),
               "USERPROFILE": str(home), "PYTHONIOENCODING": "utf-8", "CLAUDE_CONFIG_DIR": str(self.cfgdir)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("GIT_DEVIR_OFF", "CLAUDE_PROJECT_DIR", "GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
            os.environ.pop(key, None)
        self.addCleanup(shutil.rmtree, self.tmp, onerror=_force_remove)
        self.write_config()

    # ------------------------------------------------------------------ helpers

    def write_config(self, **overrides):
        data = {"machine": self.machine, "project_roots": [str(self.root)], "vault_path": str(self.vault)}
        data.update(overrides)
        (self.cfgdir / "git-devir.json").write_text(json.dumps(data), encoding="utf-8")

    def git(self, cwd, *args):
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return proc.stdout.decode("utf-8", "replace").strip()

    def repo(self, folder, url="", commit=True, parent=None):
        path = (parent or self.root) / folder
        path.mkdir(parents=True)
        self.git(path, "init", "-q")
        if url:
            self.git(path, "remote", "add", "origin", url)
        if commit:
            (path / "a.txt").write_text("a\n", encoding="utf-8")
            self.git(path, "add", "-A")
            self.git(path, "commit", "-q", "-m", "init")
        return path

    def folder(self, name):
        path = self.vault / P / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def mapping(self, data=None):
        path = self.vault / proje.MAP_FILE
        if data is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def cli(self, *args, code=0):
        """Run proje.main in-process; returns the parsed JSON (or text) and checks the exit code."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            got = proje.main([str(a) for a in args])
        text = out.getvalue()
        self.assertEqual(got, code, text)
        try:
            return json.loads(text)
        except ValueError:
            return text

    def run_script(self, *args, payload=None, env=None, cwd=None):
        run_env = dict(os.environ)
        run_env.update(env or {})
        data = payload if isinstance(payload, bytes) else json.dumps(payload or {}).encode()
        return subprocess.run([sys.executable, str(SCRIPT), *[str(a) for a in args]], input=data, env=run_env,
                              cwd=str(cwd or self.tmp), capture_output=True, timeout=60, creationflags=NO_WINDOW)

    def start(self, cwd, source="startup", session="s1", env=None, raw=None):
        payload = raw if raw is not None else {"cwd": str(cwd), "source": source, "session_id": session}
        proc = self.run_script("start", payload=payload, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(proc.stderr, b"")
        out = proc.stdout.decode("utf-8").strip()
        if not out:
            return ""
        data = json.loads(out)
        self.assertEqual(list(data), ["hookSpecificOutput"])
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "SessionStart")
        text = data["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(text), 1500)
        return text

    def card(self, folder, name, machine, open_steps="- adım", next_step="sonraki", branch="main"):
        cards = folder / proje.CARDS_DIR
        cards.mkdir(parents=True, exist_ok=True)
        (cards / name).write_text(
            f'---\nproje: "x"\nmakine: "{machine}"\ndal: "{branch}"\n---\n\n## Hedef\nh\n\n## Yapılan\n- y\n\n'
            f"## Açık adımlar\n{open_steps}\n\n## Sonraki somut adım\n{next_step}\n", encoding="utf-8")

    def vault_files(self):
        return sorted(p.relative_to(self.vault).as_posix() for p in self.vault.rglob("*") if p.is_file())


class ResolveCase(ProjeCase):
    def test_https_remote(self):
        repo = self.repo("klon", "https://github.com/Kutluege/My-Repo.git")
        self.folder("My-Repo")
        info = self.cli("resolve", "--cwd", repo / ".")
        self.assertEqual((info["id"], info["name"], info["host"]), ("Kutluege/My-Repo", "My-Repo", "github.com"))
        self.assertTrue(info["resolved"])
        self.assertEqual((info["folder"], info["source"], info["machine"]), (f"{P}/My-Repo", "name", "is-5090"))

    def test_ssh_remotes_and_subdirectory(self):
        repo = self.repo("a", "git@github.com:owner/proj.git")
        (repo / "src").mkdir()
        info = self.cli("resolve", "--cwd", repo / "src", code=proje.EXIT_UNRESOLVED)
        self.assertEqual((info["id"], info["name"], info["host"]), ("owner/proj", "proj", "github.com"))
        self.assertEqual(Path(info["root"]), repo)
        other = self.repo("b", "ssh://git@git.example.invalid:2222/team/sub/tool.git")
        info = self.cli("resolve", "--cwd", other, code=proje.EXIT_UNRESOLVED)
        self.assertEqual((info["id"], info["name"], info["host"]), ("team/sub/tool", "tool", "git.example.invalid"))

    def test_no_remote_falls_back_to_folder_name(self):
        repo = self.repo("yerel-proje")
        self.folder("Yerel Proje")
        info = self.cli("resolve", "--cwd", repo)
        self.assertEqual((info["id"], info["name"], info["host"]), ("local/yerel-proje", "yerel-proje", ""))
        self.assertEqual(info["folder"], f"{P}/Yerel Proje")
        plain = self.root / "duz-klasor"
        plain.mkdir()
        info = self.cli("resolve", "--cwd", plain, code=proje.EXIT_UNRESOLVED)
        self.assertEqual((info["id"], info["git"]), ("local/duz-klasor", False))

    def test_mapping_file_wins_over_name_match(self):
        repo = self.repo("proj", "https://github.com/owner/proj.git")
        self.folder("proj")
        self.folder("Başka Ad")
        self.mapping({"OWNER/Proj": f"{P}/Başka Ad"})
        info = self.cli("resolve", "--cwd", repo)
        self.assertEqual((info["folder"], info["source"]), (f"{P}/Başka Ad", "mapping"))

    def test_mapping_to_missing_or_unsafe_folder_is_ignored(self):
        repo = self.repo("proj", "https://github.com/owner/proj.git")
        self.folder("proj")
        for value in (f"{P}/yok", "../disari", str(self.tmp), 7):
            self.mapping({"owner/proj": value})
            info = self.cli("resolve", "--cwd", repo)
            self.assertEqual((info["folder"], info["source"]), (f"{P}/proj", "name"), value)

    def test_folder_mapped_to_another_owner_is_not_matched_by_name(self):
        repo = self.repo("proj", "https://github.com/ikinci/proj.git")
        self.folder("proj")
        self.mapping({"birinci/proj": f"{P}/proj"})
        info = self.cli("resolve", "--cwd", repo, code=proje.EXIT_UNRESOLVED)
        self.assertFalse(info["resolved"])

    def test_name_key_is_turkish_safe(self):
        key = proje.name_key
        self.assertEqual(key("İstanbul İşleri"), key("istanbul_işleri"))
        self.assertEqual(key("ISTANBUL-İŞLERİ"), key("istanbul işleri"))
        self.assertEqual(key("I\u0307stanbul"), key("İSTANBUL"))  # decomposed dotted capital I
        self.assertEqual(key("IŞIK"), key("ışık"))
        self.assertEqual(key("Isik"), key("isik"))
        self.assertEqual(key("My  Proje_Adı-2"), key("myprojeadı2"))
        self.assertNotEqual(key("Öğrenme Koçu"), key("ogrenme-kocu"))
        self.assertEqual(key("Öğrenme Koçu", True), key("ogrenme-kocu", True))
        self.assertNotEqual(key("proje-a", True), key("proje-b", True))

    def test_name_matching_with_spaces_hyphens_and_turkish_letters(self):
        cases = (("my_proje", "My Proje"), ("isik-takip", "IŞIK Takip"), ("ogrenme-kocu", "Öğrenme Koçu"),
                 ("İstanbul-Notları", "istanbul notları"))
        for index, (repo_name, folder_name) in enumerate(cases):
            repo = self.repo(f"klon{index}", f"https://github.com/owner/{repo_name}.git")
            self.folder(folder_name)
            info = self.cli("resolve", "--cwd", repo)
            self.assertEqual(info["folder"], f"{P}/{folder_name}", repo_name)

    def test_ambiguous_name_is_unresolved_unless_one_is_exact(self):
        self.folder("my-proj")
        self.folder("My Proj")
        repo = self.repo("a", "https://github.com/owner/my_proj.git")
        info = self.cli("resolve", "--cwd", repo, code=proje.EXIT_UNRESOLVED)
        self.assertEqual(info["source"], "ambiguous")
        self.assertIn("My Proj", info["note"])
        exact = self.repo("b", "https://github.com/owner/my-proj.git")
        self.assertEqual(self.cli("resolve", "--cwd", exact)["folder"], f"{P}/my-proj")

    def test_unresolved(self):
        repo = self.repo("yeni", "https://github.com/owner/yeni.git")
        self.folder("baska")
        (self.vault / P / "yeni.md").write_text("dosya, klasör değil\n", encoding="utf-8")
        info = self.cli("resolve", "--cwd", repo, code=proje.EXIT_UNRESOLVED)
        self.assertEqual((info["resolved"], info["folder"]), (False, None))
        self.assertIn("sor", info["note"])

    def test_missing_config_and_vault(self):
        repo = self.repo("proj", "https://github.com/owner/proj.git")
        self.write_config(vault_path=str(self.tmp / "yok"))
        info = self.cli("resolve", "--cwd", repo, code=proje.EXIT_UNRESOLVED)
        self.assertIn("Vault", info["note"])
        (self.cfgdir / "git-devir.json").unlink()
        result = self.cli("resolve", "--cwd", repo, code=proje.EXIT_ERROR)
        self.assertFalse(result["ok"])
        self.assertEqual(self.cli("context", "--cwd", repo), "")

    def test_remote_alias_from_config(self):
        repo = self.repo("klon", "/srv/git/proj.git")
        self.assertEqual(self.cli("resolve", "--cwd", repo, code=proje.EXIT_UNRESOLVED)["id"], "local/klon")
        self.write_config(remote_aliases={"/srv/git/proj.git": "https://github.com/owner/proj.git"})
        self.assertEqual(self.cli("resolve", "--cwd", repo, code=proje.EXIT_UNRESOLVED)["id"], "owner/proj")


class InitCase(ProjeCase):
    def test_init_creates_files_and_mapping(self):
        repo = self.repo("klon", "https://github.com/owner/proj.git")
        result = self.cli("init", "--cwd", repo)
        self.assertEqual(result["folder"], f"{P}/proj")
        self.assertEqual(self.vault_files(), [f"{P}/proj/Kararlar.md", f"{P}/proj/Kurallar.md", proje.MAP_FILE])
        self.assertTrue((self.vault / P / "proj" / "Oturumlar").is_dir())
        self.assertEqual(self.mapping(), {"owner/proj": f"{P}/proj"})
        self.assertIn("# proj · Kararlar", (self.vault / P / "proj" / "Kararlar.md").read_text(encoding="utf-8"))
        info = self.cli("resolve", "--cwd", repo)
        self.assertEqual((info["folder"], info["source"]), (f"{P}/proj", "mapping"))

    def test_init_never_overwrites(self):
        repo = self.repo("klon", "https://github.com/owner/proj.git")
        folder = self.folder("Proj")
        (folder / "Kurallar.md").write_text("- benim kuralım\n", encoding="utf-8")
        self.mapping({"baska/repo": f"{P}/x"})
        result = self.cli("init", "--cwd", repo)
        self.assertEqual(result["folder"], f"{P}/Proj")  # the existing folder is adopted
        self.assertEqual(result["kept"], [f"{P}/Proj/Kurallar.md"])
        self.assertEqual(result["created"], [f"{P}/Proj/Kararlar.md"])
        self.assertEqual((folder / "Kurallar.md").read_text(encoding="utf-8"), "- benim kuralım\n")
        self.assertEqual(self.mapping(), {"baska/repo": f"{P}/x", "owner/proj": f"{P}/Proj"})
        (folder / "Kararlar.md").write_text("elle\n", encoding="utf-8")
        before = (self.vault / proje.MAP_FILE).read_bytes()
        again = self.cli("init", "--cwd", repo)
        self.assertEqual((again["created"], again["mapping"]), ([], "unchanged"))
        self.assertEqual((folder / "Kararlar.md").read_text(encoding="utf-8"), "elle\n")
        self.assertEqual((self.vault / proje.MAP_FILE).read_bytes(), before)

    def test_init_with_custom_name_and_existing_mapping(self):
        repo = self.repo("klon", "https://github.com/owner/proj.git")
        result = self.cli("init", "--cwd", repo, "--ad", "Özel Ad")
        self.assertEqual(result["folder"], f"{P}/Özel Ad")
        self.assertEqual(self.cli("resolve", "--cwd", repo)["folder"], f"{P}/Özel Ad")
        refused = self.cli("init", "--cwd", repo, "--ad", "Üçüncü", code=proje.EXIT_ERROR)
        self.assertIn("zaten", refused["message"])
        self.assertFalse((self.vault / P / "Üçüncü").exists())
        for bad in ("a/b", "..", "ad."):
            self.cli("init", "--cwd", repo, "--ad", bad, code=proje.EXIT_ERROR)

    def test_init_leaves_broken_mapping_file_alone(self):
        repo = self.repo("klon", "https://github.com/owner/proj.git")
        path = self.vault / proje.MAP_FILE
        path.parent.mkdir(parents=True)
        path.write_text("{bozuk", encoding="utf-8")
        result = self.cli("init", "--cwd", repo)
        self.assertTrue(result["mapping"].startswith("error:"))
        self.assertEqual(path.read_text(encoding="utf-8"), "{bozuk")
        self.assertTrue((self.vault / P / "proj" / "Kararlar.md").exists())

    def test_init_refuses_inside_vault_and_sustur_mutes(self):
        inside = self.repo("ic", "https://github.com/owner/ic.git", parent=self.vault)
        self.cli("init", "--cwd", inside, code=proje.EXIT_ERROR)
        repo = self.repo("klon", "https://github.com/owner/proj.git")
        self.assertIn("klasör yok", self.start(repo))
        self.cli("sustur", "--cwd", repo)
        self.assertEqual(self.mapping(), {"owner/proj": False})
        self.assertEqual(self.start(repo, session="s2"), "")
        self.assertEqual(self.cli("resolve", "--cwd", repo, code=proje.EXIT_UNRESOLVED)["source"], "muted")
        self.cli("init", "--cwd", repo)  # explicit consent replaces the mute
        self.assertEqual(self.mapping(), {"owner/proj": f"{P}/proj"})


class CardCase(ProjeCase):
    def setUp(self):
        super().setUp()
        self.repo_path = self.repo("klon", "https://github.com/owner/proj.git")
        self.project = self.folder("proj")

    def write(self, *extra, code=0, **fields):
        args = ["kart", "--cwd", self.repo_path, "--zaman", "2026-11-16 17:42"]
        for key, value in fields.items():
            for item in value if isinstance(value, list) else [value]:
                args += [f"--{key}", item]
        return self.cli(*args, *extra, code=code)

    def test_card_name_and_content(self):
        result = self.write(hedef="Kart düzeni", yapilan=["proje.py yazıldı", "testler geçti"],
                            acik=["makineye kur"], sonraki="kur.py --dry-run", karar=["Kart = yeni dosya"])
        self.assertEqual(result["card"], f"{P}/proj/Oturumlar/2026-11-16-1742-is-5090.md")
        self.assertEqual(result["event_id"], "ege-owner-proj-2026-11-16-1742-is-5090")
        self.assertNotIn(str(self.vault).replace("\\", "/"), result["summary_file"])
        text = (self.vault / result["card"]).read_text(encoding="utf-8")
        meta, body = proje.split_note(text)
        self.assertEqual(list(meta), ["proje", "proje_id", "makine", "tarih", "dal", "commit"])
        self.assertEqual((meta["proje"], meta["proje_id"], meta["makine"]), ("proj", "owner/proj", "is-5090"))
        self.assertEqual((meta["tarih"], meta["dal"]), ("2026-11-16T17:42", "main"))
        self.assertEqual(meta["commit"], self.git(self.repo_path, "rev-parse", "--short", "HEAD"))
        self.assertEqual([line for line in body.splitlines() if line.startswith("## ")],
                         ["## Hedef", "## Yapılan", "## Açık adımlar", "## Sonraki somut adım", "## Kararlar"])
        self.assertIn("- proje.py yazıldı\n- testler geçti", body)
        self.assertLessEqual(len(body), 1200)
        self.assertNotIn("\r", text)

    def test_card_without_commit_or_decisions(self):
        repo = self.repo("bos", "https://github.com/owner/bos.git", commit=False)
        self.folder("bos")
        result = self.cli("kart", "--cwd", repo, "--yapilan", "ilk iş")
        text = (self.vault / result["card"]).read_text(encoding="utf-8")
        self.assertNotIn("commit:", text)
        self.assertNotIn("## Kararlar", text)
        self.assertIn("## Açık adımlar\n- yok", text)
        self.assertRegex(Path(result["card"]).name, r"^\d{4}-\d{2}-\d{2}-\d{4}-is-5090\.md$")

    def test_collision_gets_numeric_suffix_and_order(self):
        names = [Path(self.write(yapilan=f"iş {i}")["card"]).name for i in range(3)]
        self.assertEqual(names, ["2026-11-16-1742-is-5090.md", "2026-11-16-1742-is-5090-2.md",
                                 "2026-11-16-1742-is-5090-3.md"])
        self.assertIn("iş 0", (self.project / "Oturumlar" / names[0]).read_text(encoding="utf-8"))
        self.assertEqual(proje.list_cards(self.project), names)
        for name in ("2026-11-16-1742-is-5090-10.md", "2026-11-15-2359-ev.md", "2026-11-16-1743-ev.md", "not.md"):
            (self.project / "Oturumlar" / name).write_text("x", encoding="utf-8")
        self.assertEqual(proje.list_cards(self.project),
                         ["2026-11-15-2359-ev.md", *names, "2026-11-16-1742-is-5090-10.md", "2026-11-16-1743-ev.md"])
        self.assertEqual(proje.newest_card(self.project)["file"], "2026-11-16-1743-ev.md")

    def test_content_limit_refuses_and_writes_nothing(self):
        result = self.write(yapilan=["uzun " * 300], code=proje.EXIT_CONFLICT)
        self.assertGreater(result["chars"], 1200)
        self.assertIn("1200", result["message"])
        self.assertEqual(proje.list_cards(self.project), [])
        self.write(yapilan=["k" * 1100])

    def test_json_from_file_and_stdin(self):
        fields = {"hedef": "Şifreleme çalışması", "yapilan": ["İlk adım", "- ikinci\nüçüncü satır"],
                  "acik": "- ağ testi\n- gözden geçirme", "sonraki": "çalıştır", "kararlar": []}
        source = self.tmp / "kart.json"
        source.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")
        result = self.write("--dosya", source)
        text = (self.vault / result["card"]).read_text(encoding="utf-8")
        self.assertIn("## Hedef\nŞifreleme çalışması", text)
        self.assertIn("- İlk adım\n- ikinci üçüncü satır", text)
        self.assertIn("## Açık adımlar\n- ağ testi\n- gözden geçirme", text)
        proc = self.run_script("kart", "--cwd", self.repo_path, "--dosya", "-",
                               payload=json.dumps(fields, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        out = json.loads(proc.stdout.decode("utf-8"))
        self.assertIn("Şifreleme çalışması", (self.vault / out["card"]).read_text(encoding="utf-8"))

    def test_refusals(self):
        self.write(hedef="yalnız hedef", code=proje.EXIT_ERROR)
        fake = "gh" + "p_" + "a1B2" * 9
        self.assertIn("anahtar", self.write(yapilan=[f"token {fake} eklendi"], code=proje.EXIT_ERROR)["message"])
        self.cli("kart", "--cwd", self.repo_path, "--yapilan", "x", "--zaman", "dün", code=proje.EXIT_ERROR)
        other = self.repo("yeni", "https://github.com/owner/yeni.git")
        result = self.cli("kart", "--cwd", other, "--yapilan", "x", code=proje.EXIT_UNRESOLVED)
        self.assertFalse(result["resolved"])
        self.assertEqual(self.vault_files(), [])


class DecisionCase(ProjeCase):
    def setUp(self):
        super().setUp()
        self.repo_path = self.repo("klon", "https://github.com/owner/proj.git")
        self.cli("init", "--cwd", self.repo_path)
        self.path = self.vault / P / "proj" / "Kararlar.md"

    def add(self, text, *extra, code=0, date="2026-11-16", why="çünkü"):
        return self.cli("karar", "--cwd", self.repo_path, "--karar", text, "--neden", why, "--tarih", date,
                        *extra, code=code)

    def test_prepend_keeps_the_rest_of_the_file(self):
        header = self.path.read_text(encoding="utf-8")
        first = self.add("Kart yeni dosya", "--alternatif", "tek Durum.md", "--alternatif", "receipt'e gömmek",
                         date="2026-11-15", why="Sync çakışmasın")
        self.assertEqual((first["status"], first["file"]), ("written", f"{P}/proj/Kararlar.md"))
        after_first = self.path.read_text(encoding="utf-8")
        self.assertEqual(after_first, header + "\n## 2026-11-15 · Kart yeni dosya\n- Neden: Sync çakışmasın\n"
                         "- Alternatifler: tek Durum.md; receipt'e gömmek\n- Makine: is-5090\n")
        self.path.write_text(after_first + "\nElle yazılmış not.\n", encoding="utf-8")
        second = self.add("İkinci karar")
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(second["sha256"], proje.sha256(self.path.read_bytes()))
        self.assertTrue(text.startswith(header))
        self.assertTrue(text.endswith(after_first[len(header) + 1:] + "\nElle yazılmış not.\n"))
        self.assertLess(text.index("İkinci karar"), text.index("Kart yeni dosya"))
        self.assertIn("- Alternatifler: belirtilmedi", text)
        self.assertEqual(proje.last_decisions(self.path.parent),
                         ["2026-11-16 · İkinci karar (neden: çünkü)",
                          "2026-11-15 · Kart yeni dosya (neden: Sync çakışmasın)"])

    def test_duplicate_is_unchanged_and_crlf_is_kept(self):
        self.path.write_bytes("# Kararlar\r\n\r\n## 2026-11-01 · Eski\r\n- Neden: n\r\n".encode("utf-8"))
        self.add("Yeni")
        raw = self.path.read_bytes()
        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"))
        self.assertTrue(raw.decode("utf-8").startswith("# Kararlar\r\n\r\n## 2026-11-16 · Yeni\r\n"))
        self.assertEqual(self.add("Yeni")["status"], "unchanged")
        self.assertEqual(self.path.read_bytes(), raw)

    def test_stale_hash_is_refused(self):
        stale = proje.sha256(self.path.read_bytes())
        self.add("Birinci", "--sha256", stale)
        before = self.path.read_bytes()
        result = self.add("İkinci", "--sha256", stale, code=proje.EXIT_CONFLICT)
        self.assertIn("yazılmadı", result["message"])
        self.assertEqual(result["sha256"], proje.sha256(before))
        self.assertEqual(self.path.read_bytes(), before)
        self.add("İkinci", "--sha256", result["sha256"])

    def test_change_between_read_and_write_is_refused(self):
        def other_machine(path):
            if path.name == "Kararlar.md":
                path.write_bytes(path.read_bytes() + "\nBaşka makineden satır\n".encode("utf-8"))

        with mock.patch.object(proje, "_before_write", other_machine):
            result = self.add("Çakışan", code=proje.EXIT_CONFLICT)
        self.assertIn("değişti", result["message"])
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("Başka makineden satır", text)
        self.assertNotIn("Çakışan", text)
        self.assertEqual([p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_missing_file_is_created_and_inputs_are_checked(self):
        self.path.unlink()
        self.add("İlk")
        self.assertIn("# proj · Kararlar", self.path.read_text(encoding="utf-8"))
        self.add("x", date="16.11.2026", code=proje.EXIT_ERROR)
        self.add("  ", code=proje.EXIT_ERROR)
        other = self.repo("yeni", "https://github.com/owner/yeni.git")
        self.cli("karar", "--cwd", other, "--karar", "k", "--neden", "n", code=proje.EXIT_UNRESOLVED)


class StartCase(ProjeCase):
    def setUp(self):
        super().setUp()
        self.repo_path = self.repo("klon", "https://github.com/owner/proj.git")

    def prepared(self):
        self.cli("init", "--cwd", self.repo_path)
        folder = self.vault / P / "proj"
        for day, text in (("12", "En eski"), ("13", "Eski"), ("14", "Orta"), ("15", "Yeni")):
            self.cli("karar", "--cwd", self.repo_path, "--karar", f"{text} karar", "--neden", f"{text} neden",
                     "--tarih", f"2026-11-{day}")
        self.cli("kart", "--cwd", self.repo_path, "--zaman", "2026-11-16 09:30", "--yapilan", "çözücü",
                 "--acik", "installer testi", "--acik", "belgeler", "--sonraki", "kur.py --dry-run çalıştır")
        return folder

    def test_resolved_output(self):
        folder = self.prepared()
        text = self.start(self.repo_path)
        lines = text.splitlines()
        self.assertEqual(lines[0], f"Proje: proj (owner/proj) → {P}/proj")
        self.assertIn("Son kart: 2026-11-16 09:30 · makine `is-5090`", text)
        self.assertNotIn("BAŞKA MAKİNEDEN", text)
        self.assertIn("Açık adımlar:\n- installer testi\n- belgeler", text)
        self.assertIn("Sonraki somut adım: kur.py --dry-run çalıştır", text)
        self.assertIn("Son kararlar:\n- 2026-11-15 · Yeni karar (neden: Yeni neden)\n- 2026-11-14 · Orta karar", text)
        self.assertIn("2026-11-13 · Eski karar", text)
        self.assertNotIn("En eski karar", text)
        self.assertNotIn("kuralları", text)  # Kurallar.md holds only the template
        self.assertIn("`beyin-yazici` alt ajanına ver", lines[-1])
        with (folder / "Kurallar.md").open("a", encoding="utf-8") as handle:
            handle.write("\n- 2026-11-16 · testsiz commit yok\n")
        text = self.start(self.repo_path, session="s2")
        rules = (folder / "Kurallar.md").as_posix()
        self.assertIn(f"Bu projenin kuralları: {rules} (işe başlamadan oku)", text)
        self.assertEqual(self.cli("context", "--cwd", self.repo_path).strip(), text)

    def test_newest_card_from_another_machine_is_labelled(self):
        folder = self.prepared()
        self.card(folder, "2026-11-16-2210-ev.md", "ev", "- evde yarım kalan iş", "evdeki dalı çek")
        text = self.start(self.repo_path)
        self.assertIn("Son kart: 2026-11-16 22:10 · makine `ev` · BAŞKA MAKİNEDEN (bu makine `is-5090`", text)
        self.assertIn("- evde yarım kalan iş", text)
        self.assertIn("Sonraki somut adım: evdeki dalı çek", text)
        self.assertNotIn("installer testi", text)

    def test_length_limit_with_huge_content(self):
        folder = self.prepared()
        self.card(folder, "2026-11-17-0800-ev.md", "ev", "\n".join("- " + "çok uzun adım " * 40 for _ in range(30)),
                  "sonraki " * 300)
        for day in ("20", "21", "22"):
            self.cli("karar", "--cwd", self.repo_path, "--karar", "karar " * 60, "--neden", "neden " * 60,
                     "--tarih", f"2026-11-{day}")
        (folder / "Kurallar.md").write_text("- kural\n", encoding="utf-8")
        text = self.start(self.repo_path)  # start() asserts <= 1500
        self.assertGreater(len(text), 900)
        self.assertTrue(text.startswith("Proje: proj (owner/proj)"))
        self.assertIn("Bu projenin kuralları:", text)
        self.assertIn("`beyin-yazici`", text.splitlines()[-1])
        self.assertIn("2026-11-22", text)

    def test_resolved_without_cards(self):
        self.folder("proj")
        text = self.start(self.repo_path)
        self.assertIn("henüz oturum kartı yok", text)
        self.assertNotIn("Son kararlar", text)

    def test_unresolved_asks_and_creates_nothing(self):
        text = self.start(self.repo_path)
        self.assertLess(len(text), 700)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("Proje: proj (owner/proj) için vault'ta klasör yok", text)
        self.assertIn(f"Kullanıcıya sor: \"{P}/proj klasörünü açayım mı?\"", text)
        self.assertIn("Sormadan oluşturma", text)
        command = f"{SCRIPT.as_posix()} init --cwd {self.repo_path.as_posix()}"
        self.assertIn(command if " " not in command else "init --cwd", text)
        self.assertIn("proje.py", text)
        self.assertEqual(self.vault_files(), [])
        self.assertEqual([p.name for p in (self.vault / P).iterdir()], [])

    def test_gates(self):
        self.prepared()
        self.assertTrue(self.start(self.repo_path))
        (self.repo_path / "alt").mkdir()
        self.assertTrue(self.start(self.repo_path / "alt", session="alt"))
        self.assertEqual(self.start(self.repo_path, env={"GIT_DEVIR_OFF": "1"}), "")
        (self.repo_path / ".git-devir-off").write_text("", encoding="utf-8")
        self.assertEqual(self.start(self.repo_path, session="off"), "")
        (self.repo_path / ".git-devir-off").unlink()
        outside = self.repo("proj", "https://github.com/owner/proj.git", parent=self.tmp / "baska")
        self.assertEqual(self.start(outside), "")
        in_vault = self.repo("proj", "https://github.com/owner/proj.git", parent=self.vault / "repolar")
        self.write_config(project_roots=[str(self.root), str(self.vault)])
        self.assertEqual(self.start(in_vault), "")
        self.assertTrue(self.start(self.repo_path, session="x"))
        (self.repo_path / "beyin.py").write_text("", encoding="utf-8")
        self.assertEqual(self.start(self.repo_path, session="y"), "")

    def test_missing_config_vault_or_payload_is_silent(self):
        self.prepared()
        self.assertEqual(self.start(self.repo_path, raw=b"not json"), "")
        self.assertEqual(self.start(self.repo_path, raw={"cwd": str(self.tmp / "yok")}), "")
        self.write_config(vault_path=str(self.tmp / "yok"))
        self.assertEqual(self.start(self.repo_path), "")
        (self.cfgdir / "git-devir.json").write_text("{bozuk", encoding="utf-8")
        self.assertEqual(self.start(self.repo_path), "")
        (self.cfgdir / "git-devir.json").unlink()
        self.assertEqual(self.start(self.repo_path), "")

    def test_plain_folder_never_asks(self):
        plain = self.root / "duz"
        plain.mkdir()
        self.assertEqual(self.start(plain), "")
        self.folder("duz")
        self.assertIn("Proje: duz (local/duz)", self.start(plain))

    def test_compact_and_resume_skip_unchanged_block(self):
        folder = self.prepared()
        first = self.start(self.repo_path, source="startup", session="a")
        self.assertTrue(first)
        self.assertEqual(self.start(self.repo_path, source="resume", session="a"), "")
        self.assertEqual(self.start(self.repo_path, source="compact", session="a"), "")
        self.assertEqual(self.start(self.repo_path, source="resume", session="b"), first)  # never shown to b
        self.assertEqual(self.start(self.repo_path, source="startup", session="a"), first)
        self.card(folder, "2026-11-18-0900-ev.md", "ev")
        changed = self.start(self.repo_path, source="resume", session="a")
        self.assertIn("2026-11-18 09:00", changed)
        self.assertEqual(self.start(self.repo_path, source="compact", session="a"), "")

    def test_start_is_fast(self):
        self.prepared()
        best = 99.0
        for index in range(3):
            began = time.perf_counter()
            self.assertTrue(self.start(self.repo_path, session=f"t{index}"))
            best = min(best, time.perf_counter() - began)
        self.assertLess(best, 2.0)


class WriterAgentCase(ProjeCase):
    """The agent definition is prose, but the commands it quotes must exist."""

    AGENT = HERE.parent / "agents" / "beyin-yazici.md"

    def test_frontmatter(self):
        meta, body = proje.split_note(self.AGENT.read_text(encoding="utf-8"))
        self.assertEqual((meta["name"], meta["model"], meta["effort"]), ("beyin-yazici", "sonnet", "medium"))
        self.assertEqual(meta["tools"], "Bash, Read, Write, Edit, Glob, Grep")
        for expected in ("receipt", "Receipt session=", "KULLANMAK ZORUNDADIR"):
            self.assertIn(expected, meta["description"])
        self.assertIn("kaydetme", body)

    def test_quoted_commands_match_the_cli(self):
        text = self.AGENT.read_text(encoding="utf-8")
        choices = proje.build_parser()._subparsers._group_actions[0].choices
        seen = set()
        for line in text.splitlines():
            match = re.search(r'"PROJE" (\w+)(.*)', line)
            if match:
                seen.add(match.group(1))
                options = choices[match.group(1)]._option_string_actions
                for flag in re.findall(r"--[a-z0-9-]+", match.group(2)):
                    self.assertIn(flag, options, line)
        self.assertEqual(seen, {"resolve", "kart", "karar"})
        example = re.search(r"<<'JSON'\n(.*?)\nJSON", text, re.DOTALL).group(1)
        source = self.tmp / "ornek.json"
        source.write_text(example, encoding="utf-8")
        repo = self.repo("klon", "https://github.com/owner/proj.git")
        self.folder("proj")
        result = self.cli("kart", "--cwd", repo, "--dosya", source)
        for key in ("card", "event_id", "summary_file", "name", "beyin_py"):  # the fields the agent is told to use
            self.assertIn(key, result)
        info = self.cli("resolve", "--cwd", repo)
        for key in ("resolved", "name", "id", "folder", "beyin_py", "note"):
            self.assertIn(key, info)
        decision = self.cli("karar", "--cwd", repo, "--karar", "k", "--neden", "n")
        self.assertEqual(decision["file"], f"{P}/proj/Kararlar.md")


if __name__ == "__main__":
    unittest.main()
