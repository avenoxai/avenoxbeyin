"""Tests for git-devir. Run from the repo root:

    python -m unittest discover -s ege/hooks -p "test_*.py" -v

Everything happens in a temporary directory: local bare repos stand in for GitHub, and
CLAUDE_CONFIG_DIR/HOME point at temp folders, so the real ~/.claude is never touched.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "git_devir.py"
INSTALLER = HERE / "install_git_devir.py"
sys.path.insert(0, str(HERE))
import git_devir  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
OWN = "https://github.com/kutluege/proj.git"
FOREIGN = "https://git.example.invalid/team/proj.git"


def _force_remove(func, path, _info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


class GitDevirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gitdevir-")).resolve()
        home = self.tmp / "home"
        home.mkdir()
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n"
                             "[init]\n\tdefaultBranch = main\n[core]\n\tautocrlf = false\n"
                             "[advice]\n\tdetachedHead = false\n", encoding="utf-8")
        env = dict(os.environ)
        for key in ("GIT_DEVIR_OFF", "CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR", "GIT_DIR",
                    "GIT_INDEX_FILE", "GIT_WORK_TREE"):
            env.pop(key, None)
        env.update({"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
                    "HOME": str(home), "USERPROFILE": str(home), "PYTHONIOENCODING": "utf-8"})
        self.env = env
        self.bare = self.tmp / "remote" / "proj.git"
        self.bare.parent.mkdir()
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.url = self.bare.as_posix()
        seed = self.tmp / "seed"
        self.git(self.tmp, "clone", "-q", self.url, str(seed))
        (seed / "README.md").write_text("hello\n", encoding="utf-8")
        (seed / "other.txt").write_text("one\n", encoding="utf-8")
        (seed / ".gitignore").write_text("*.log\n", encoding="utf-8")
        self.git(seed, "add", "-A")
        self.git(seed, "commit", "-q", "-m", "init")
        self.git(seed, "push", "-q", "origin", "main")
        self.roots = {"A": self.tmp / "A", "B": self.tmp / "B"}
        self.machines = {"A": "is-5090", "B": "ev"}
        self.clones = {}
        for key, root in self.roots.items():
            root.mkdir()
            clone = root / "proj"
            self.git(self.tmp, "clone", "-q", self.url, str(clone))
            self.clones[key] = clone
            self.write_config(key)

    def tearDown(self):
        deadline = time.time() + 30
        while time.time() < deadline and any(any((self.cfg(k) / "git-devir" / "locks").glob("*.lock"))
                                             for k in self.roots):
            time.sleep(0.2)
        shutil.rmtree(self.tmp, onerror=_force_remove)

    # ------------------------------------------------------------------ helpers

    def cfg(self, key):
        return self.tmp / f"cfg{key}"

    def write_config(self, key, alias=OWN, **overrides):
        data = {"machine": self.machines[key], "project_roots": [str(self.roots[key])],
                "vault_path": str(self.tmp / "vault"), "auto_owners": ["kutluege"],
                "auto_hosts": ["github.com"], "max_file_mb": 50,
                "remote_aliases": {self.url: alias}}
        data.update(overrides)
        self.cfg(key).mkdir(exist_ok=True)
        (self.cfg(key) / "git-devir.json").write_text(json.dumps(data), encoding="utf-8")

    def git(self, cwd, *args, check=True):
        proc = subprocess.run(["git", "--no-optional-locks", *args], cwd=str(cwd), env=self.env,
                              capture_output=True, creationflags=NO_WINDOW)
        if check and proc.returncode != 0:
            raise AssertionError(f"git {args} failed: {proc.stderr.decode('utf-8', 'replace')}")
        return proc.stdout.decode("utf-8", "replace").strip()

    def remote(self, *args, check=True):
        return self.git(self.tmp, "--git-dir", str(self.bare), *args, check=check)

    def remote_ref(self, ref):
        return self.remote("rev-parse", "-q", "--verify", f"refs/heads/{ref}", check=False)

    def run_hook(self, key, mode, cwd=None, sync=True, env=None, payload=None):
        cwd = cwd or self.clones[key]
        run_env = dict(self.env, CLAUDE_CONFIG_DIR=str(self.cfg(key)))
        run_env.update(env or {})
        args = [sys.executable, str(HOOK), mode] + (["--sync"] if sync and mode == "end" else [])
        body = payload if payload is not None else {"cwd": str(cwd), "source": "startup"}
        proc = subprocess.run(args, input=json.dumps(body).encode(), env=run_env, cwd=str(self.tmp),
                              capture_output=True, timeout=180, creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return proc

    def start(self, key, **kwargs):
        out = self.run_hook(key, "start", **kwargs).stdout.decode("utf-8").strip()
        if not out:
            return ""
        data = json.loads(out)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "SessionStart")
        text = data["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(text), 800)
        return text

    def notices(self, key):
        texts = []
        for path in (self.cfg(key) / "git-devir" / "notices").glob("*.json"):
            texts += [item["text"] for item in json.loads(path.read_text(encoding="utf-8"))]
        return "\n".join(texts)

    def commit(self, key, name, content, message="change"):
        clone = self.clones[key]
        (clone / name).write_text(content, encoding="utf-8")
        self.git(clone, "add", name)
        self.git(clone, "commit", "-q", "-m", message)
        return self.git(clone, "rev-parse", "HEAD")

    def state_of(self, clone):
        index = Path(self.git(clone, "rev-parse", "--absolute-git-dir")) / "index"
        return {
            "head": self.git(clone, "rev-parse", "HEAD"),
            "branch": self.git(clone, "symbolic-ref", "HEAD"),
            "status": self.git(clone, "status", "--porcelain", "-uall"),
            "diff": self.git(clone, "diff"),
            "cached": self.git(clone, "diff", "--cached"),
            "index": index.read_bytes(),
            "files": {p.relative_to(clone).as_posix(): p.read_bytes()
                      for p in clone.rglob("*") if p.is_file() and ".git" not in p.relative_to(clone).parts},
        }

    # ------------------------------------------------------------------ end: snapshot

    def test_snapshot_leaves_branch_index_and_worktree_unchanged(self):
        clone = self.clones["A"]
        (clone / "README.md").write_bytes(b"hello\nunstaged edit\n")
        (clone / "staged.txt").write_text("staged\n", encoding="utf-8")
        self.git(clone, "add", "staged.txt")
        (clone / "new.txt").write_text("untracked\n", encoding="utf-8")
        (clone / "debug.log").write_text("ignored\n", encoding="utf-8")
        (clone / "other.txt").unlink()
        before = self.state_of(clone)
        self.run_hook("A", "end")
        after = self.state_of(clone)
        self.assertEqual(before, after)
        wip = self.remote_ref("wip/is-5090/main")
        self.assertTrue(wip, "wip ref was not pushed")
        self.assertEqual(self.remote("rev-parse", f"{wip}^1"), before["head"])
        names = self.remote("ls-tree", "-r", "--name-only", wip).splitlines()
        self.assertIn("new.txt", names)
        self.assertIn("staged.txt", names)
        self.assertNotIn("debug.log", names)
        self.assertNotIn("other.txt", names)
        self.assertEqual(self.remote("show", f"{wip}:README.md"), "hello\nunstaged edit")
        self.assertIn("wip(is-5090): main", self.remote("log", "-1", "--format=%s", wip))
        self.assertEqual(self.remote_ref("main"), before["head"])

    def test_snapshot_from_subdirectory_and_unchanged_tree_is_skipped(self):
        clone = self.clones["A"]
        sub = clone / "pkg"
        sub.mkdir()
        (sub / "mod.py").write_text("x = 1\n", encoding="utf-8")
        self.run_hook("A", "end", cwd=sub)
        self.assertTrue(self.remote_ref("wip/is-5090/main"))
        clean = self.clones["B"]
        self.run_hook("B", "end")
        self.assertFalse(self.remote_ref("wip/ev/main"), "clean tree must not create a wip ref")

    def test_snapshot_skips_large_files(self):
        self.write_config("A", max_file_mb=0.01)  # ~10 KB
        clone = self.clones["A"]
        (clone / "big.bin").write_bytes(b"x" * 20000)
        (clone / "staged-big.bin").write_bytes(b"y" * 20000)
        self.git(clone, "add", "staged-big.bin")
        (clone / "small.txt").write_text("small\n", encoding="utf-8")
        before = self.state_of(clone)
        self.run_hook("A", "end")
        self.assertEqual(before, self.state_of(clone))
        wip = self.remote_ref("wip/is-5090/main")
        self.assertTrue(wip)
        names = self.remote("ls-tree", "-r", "--name-only", wip).splitlines()
        self.assertIn("small.txt", names)
        self.assertNotIn("big.bin", names)
        self.assertNotIn("staged-big.bin", names)
        self.assertIn("big.bin", self.notices("A"))

    def test_snapshot_aborts_on_secret_files(self):
        clone = self.clones["A"]
        (clone / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
        (clone / "work.txt").write_text("work\n", encoding="utf-8")
        before = self.state_of(clone)
        self.run_hook("A", "end")
        self.assertEqual(before, self.state_of(clone))
        self.assertFalse(self.remote_ref("wip/is-5090/main"))
        self.assertIn(".env", self.notices("A"))
        # .env.example is allowed
        (clone / ".env").unlink()
        (clone / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
        self.run_hook("A", "end")
        wip = self.remote_ref("wip/is-5090/main")
        self.assertTrue(wip)
        self.assertIn(".env.example", self.remote("ls-tree", "-r", "--name-only", wip).splitlines())

    def test_secret_patterns(self):
        for path in (".env", ".env.local", "conf/server.pem", "a.key", "id_rsa", "id_rsa.pub",
                     "x.p12", "x.pfx", "credentials.json", "credentials-prod.json", "secrets.yaml"):
            self.assertTrue(git_devir.is_secret(path), path)
        for path in (".env.example", "env.txt", "keys.md", "readme.md", "credentials.txt"):
            self.assertFalse(git_devir.is_secret(path), path)

    # ------------------------------------------------------------------ end: push

    def test_end_pushes_ahead_branch(self):
        head = self.commit("A", "feature.txt", "feature\n")
        self.run_hook("A", "end")
        self.assertEqual(self.remote_ref("main"), head)
        self.assertFalse(self.remote_ref("wip/is-5090/main"))
        self.assertEqual(self.notices("A"), "")

    def test_end_diverged_only_leaves_notice(self):
        theirs = self.commit("B", "b.txt", "from B\n")
        self.git(self.clones["B"], "push", "-q", "origin", "main")
        self.commit("A", "a.txt", "from A\n")
        self.git(self.clones["A"], "fetch", "-q", "origin")
        self.run_hook("A", "end")
        self.assertEqual(self.remote_ref("main"), theirs)
        self.assertIn("ayrışmış", self.notices("A"))

    def test_end_rejected_push_leaves_notice(self):
        theirs = self.commit("B", "b.txt", "from B\n")
        self.git(self.clones["B"], "push", "-q", "origin", "main")
        self.commit("A", "a.txt", "from A\n")  # A has not fetched: looks ahead-only
        self.run_hook("A", "end")
        self.assertEqual(self.remote_ref("main"), theirs)
        self.assertIn("push edilemedi", self.notices("A"))

    def test_own_wip_deleted_when_clean(self):
        clone = self.clones["A"]
        (clone / "wip.txt").write_text("half\n", encoding="utf-8")
        self.run_hook("A", "end")
        self.assertTrue(self.remote_ref("wip/is-5090/main"))
        self.git(clone, "add", "-A")
        self.git(clone, "commit", "-q", "-m", "done")
        self.run_hook("A", "end")
        self.assertEqual(self.remote_ref("main"), self.git(clone, "rev-parse", "HEAD"))
        self.assertFalse(self.remote_ref("wip/is-5090/main"))
        self.assertFalse(self.git(clone, "rev-parse", "-q", "--verify",
                                  "refs/remotes/origin/wip/is-5090/main", check=False))

    def test_end_async_returns_fast_and_worker_finishes(self):
        clone = self.clones["A"]
        (clone / "async.txt").write_text("async\n", encoding="utf-8")
        began = time.monotonic()
        self.run_hook("A", "end", sync=False)
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 1.5, f"end hook took {elapsed:.2f}s")
        deadline = time.time() + 60
        while time.time() < deadline and not self.remote_ref("wip/is-5090/main"):
            time.sleep(0.3)
        self.assertTrue(self.remote_ref("wip/is-5090/main"), "detached worker did not push the wip ref")

    # ------------------------------------------------------------------ start

    def test_start_clean_and_behind_fast_forwards(self):
        theirs = self.commit("B", "b.txt", "from B\n")
        self.git(self.clones["B"], "push", "-q", "origin", "main")
        text = self.start("A")
        self.assertEqual(self.git(self.clones["A"], "rev-parse", "HEAD"), theirs)
        self.assertIn("çekildi", text)

    def test_start_dirty_and_behind_only_warns(self):
        self.commit("B", "b.txt", "from B\n")
        self.git(self.clones["B"], "push", "-q", "origin", "main")
        clone = self.clones["A"]
        head = self.git(clone, "rev-parse", "HEAD")
        (clone / "README.md").write_text("local edit\n", encoding="utf-8")
        text = self.start("A")
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), head)
        self.assertEqual((clone / "README.md").read_text(encoding="utf-8"), "local edit\n")
        self.assertIn("gerisinde", text)
        self.assertIn("git pull --ff-only", text)

    def test_start_reports_other_machines_wip_without_applying(self):
        other = self.clones["B"]
        (other / "half.txt").write_text("half-done on B\n", encoding="utf-8")
        self.run_hook("B", "end")
        self.assertTrue(self.remote_ref("wip/ev/main"))
        clone = self.clones["A"]
        before = self.state_of(clone)
        text = self.start("A")
        self.assertIn("Başka makinede (`ev`)", text)
        self.assertIn("origin/wip/ev/main", text)
        self.assertIn("git diff HEAD origin/wip/ev/main", text)
        self.assertEqual(before, self.state_of(clone))
        # B itself does not see its own wip as "another machine"
        self.assertNotIn("Başka makinede", self.start("B"))

    def test_start_shows_and_clears_pending_notices(self):
        self.commit("B", "b.txt", "from B\n")
        self.git(self.clones["B"], "push", "-q", "origin", "main")
        self.commit("A", "a.txt", "from A\n")
        self.run_hook("A", "end")  # rejected push -> notice
        text = self.start("A")
        self.assertIn("Önceki kapanış", text)
        self.assertIn("ayrışmış", text)
        self.assertEqual(self.notices("A"), "")
        self.assertNotIn("Önceki kapanış", self.start("A"))

    def test_start_nothing_to_say_prints_nothing(self):
        proc = self.run_hook("A", "start")
        self.assertEqual(proc.stdout.strip(), b"")

    def test_start_on_compact_is_skipped(self):
        self.commit("B", "b.txt", "from B\n")
        self.git(self.clones["B"], "push", "-q", "origin", "main")
        proc = self.run_hook("A", "start", payload={"cwd": str(self.clones["A"]), "source": "compact"})
        self.assertEqual(proc.stdout.strip(), b"")

    # ------------------------------------------------------------------ policy and gates

    def test_non_auto_owner_is_warn_only(self):
        self.write_config("A", alias=FOREIGN)
        clone = self.clones["A"]
        head = self.commit("A", "a.txt", "local\n")
        (clone / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        remote_main = self.remote_ref("main")
        self.run_hook("A", "end")
        self.assertEqual(self.remote_ref("main"), remote_main)
        self.assertFalse(self.remote_ref("wip/is-5090/main"))
        notes = self.notices("A")
        self.assertIn("yalnız uyarı", notes)
        self.assertIn("push edilmemiş 1 commit", notes)
        # behind on a foreign remote: warn, never pull
        self.git(self.tmp, "clone", "-q", self.url, str(self.tmp / "C"))
        (self.tmp / "C" / "c.txt").write_text("c\n", encoding="utf-8")
        self.git(self.tmp / "C", "add", "-A")
        self.git(self.tmp / "C", "commit", "-q", "-m", "c")
        self.git(self.tmp / "C", "push", "-q", "origin", "main")
        (clone / "dirty.txt").unlink()
        self.git(clone, "reset", "-q", "--hard", "origin/main")
        self.git(clone, "fetch", "-q", "origin")
        before = self.git(clone, "rev-parse", "HEAD")
        text = self.start("A")
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), before)
        self.assertIn("yalnız uyarı", text)

    def test_unknown_remote_without_alias_is_warn_only(self):
        self.write_config("A", remote_aliases={})
        self.commit("A", "a.txt", "local\n")
        remote_main = self.remote_ref("main")
        self.run_hook("A", "end")
        self.assertEqual(self.remote_ref("main"), remote_main)

    def assert_silent(self, key, cwd=None, env=None):
        self.commit("B", f"b{time.time_ns()}.txt", "from B\n")
        self.git(self.clones["B"], "push", "-q", "origin", "main")
        clone = cwd or self.clones[key]
        head = self.git(clone, "rev-parse", "HEAD")
        (clone / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        self.assertEqual(self.start(key, cwd=clone, env=env), "")
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), head)
        self.run_hook(key, "end", cwd=clone, env=env)
        self.assertFalse(self.remote_ref(f"wip/{self.machines[key]}/main"))
        self.assertEqual(self.notices(key), "")
        (clone / "dirty.txt").unlink()

    def test_vault_repo_is_skipped(self):
        self.write_config("A", vault_path=str(self.roots["A"]))
        self.assert_silent("A")

    def test_beyin_py_repo_is_skipped(self):
        (self.clones["A"] / "beyin.py").write_text("# vault marker\n", encoding="utf-8")
        self.assert_silent("A")

    def test_off_marker_is_skipped(self):
        (self.clones["A"] / ".git-devir-off").write_text("", encoding="utf-8")
        self.assert_silent("A")

    def test_env_off_is_skipped(self):
        self.assert_silent("A", env={"GIT_DEVIR_OFF": "1"})

    def test_repo_outside_project_roots_is_skipped(self):
        outside = self.tmp / "elsewhere"
        self.git(self.tmp, "clone", "-q", self.url, str(outside))
        self.assert_silent("A", cwd=outside)

    def test_missing_or_broken_config_is_silent(self):
        (self.cfg("A") / "git-devir.json").unlink()
        self.assert_silent("A")
        (self.cfg("A") / "git-devir.json").write_text("{not json", encoding="utf-8")
        self.assert_silent("A")
        self.write_config("A", machine="has space")
        self.assert_silent("A")

    def test_detached_head_only_leaves_notice(self):
        clone = self.clones["A"]
        self.git(clone, "checkout", "-q", "--detach", "HEAD")
        (clone / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        self.run_hook("A", "end")
        self.assertFalse(self.remote("for-each-ref", "refs/heads/wip"))
        self.assertIn("detached", self.notices("A"))
        self.assertIn("detached", self.start("A"))

    def test_merge_in_progress_only_leaves_notice(self):
        self.commit("B", "README.md", "B version\n")
        self.git(self.clones["B"], "push", "-q", "origin", "main")
        self.commit("A", "README.md", "A version\n")
        self.git(self.clones["A"], "fetch", "-q", "origin")
        self.git(self.clones["A"], "merge", "-q", "origin/main", check=False)  # conflict
        self.assertTrue((self.clones["A"] / ".git" / "MERGE_HEAD").exists())
        self.run_hook("A", "end")
        self.assertFalse(self.remote("for-each-ref", "refs/heads/wip"))
        self.assertIn("merge sürüyor", self.notices("A"))

    # ------------------------------------------------------------------ parsing and rendering

    def test_parse_remote(self):
        cases = {
            "https://github.com/kutluege/proj.git": ("github.com", "kutluege", "proj"),
            "https://user@github.com/kutluege/proj": ("github.com", "kutluege", "proj"),
            "git@github.com:kutluege/proj.git": ("github.com", "kutluege", "proj"),
            "ssh://git@github.com:22/kutluege/proj.git": ("github.com", "kutluege", "proj"),
            "https://gitlab.example.invalid/group/sub/proj.git": ("gitlab.example.invalid", "group/sub", "proj"),
        }
        for url, expected in cases.items():
            self.assertEqual(git_devir.parse_remote(url), expected, url)
        for url in ("C:/repos/proj.git", "/srv/proj.git", "", "https://github.com/onlyone"):
            self.assertIsNone(git_devir.parse_remote(url), url)

    def test_render_is_bounded(self):
        repo = git_devir.Repo(Path("proj"), Path(".git"), "", True, "main", "x", None)
        text = git_devir._render(repo, ["- " + "ç" * 300] * 10)
        self.assertLessEqual(len(text), 800)
        self.assertTrue(text.endswith("…"))
        self.assertEqual(git_devir._render(repo, []), "")


class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gitdevir-inst-")).resolve()
        self.cfg = self.tmp / "claude"
        self.cfg.mkdir()
        (self.tmp / "Kod").mkdir()
        (self.tmp / "vault").mkdir()
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.cfg), HOME=str(self.tmp),
                        USERPROFILE=str(self.tmp))
        self.existing = {
            "model": "opus",
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "timeout": 20,
                                             "command": "python /x/beyin_v3_bridge.py --event SessionStart"}]}],
                "SessionEnd": [{"hooks": [{"type": "command", "timeout": 3,
                                           "command": "python /x/beyin_v3_bridge.py --event SessionEnd"}]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}],
            },
        }
        self.settings = self.cfg / "settings.json"
        self.settings.write_text(json.dumps(self.existing, indent=2), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, onerror=_force_remove)

    def install(self, *extra):
        args = [sys.executable, str(INSTALLER), "--machine", "is-5090", "--project-root", str(self.tmp / "Kod"),
                "--vault", str(self.tmp / "vault"), *extra]
        proc = subprocess.run(args, env=self.env, capture_output=True, timeout=60, creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8"))

    def backups(self):
        return sorted(self.cfg.glob("settings.json.git-devir-*.bak"))

    def test_dry_run_writes_nothing(self):
        report = self.install("--dry-run")
        self.assertEqual(report["status"], "dry-run")
        self.assertTrue(report["settings_changed"])
        self.assertIn("SessionStart", report["added_handlers"])
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), self.existing)
        self.assertFalse((self.cfg / "git-devir.json").exists())
        self.assertEqual(self.backups(), [])

    def test_install_merges_is_idempotent_and_uninstalls(self):
        self.install()
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"]["PreToolUse"], self.existing["hooks"]["PreToolUse"])
        for event, mode in (("SessionStart", "start"), ("SessionEnd", "end")):
            groups = data["hooks"][event]
            self.assertEqual(len(groups), 2)
            self.assertEqual(groups[0], self.existing["hooks"][event][0])  # bridge kept, first
            command = groups[1]["hooks"][0]["command"]
            decoded = git_devir_installer_decoded(command)
            self.assertIn("git_devir.py", decoded)
            self.assertTrue(f"git_devir.py {mode}" in decoded or f"git_devir.py' '{mode}'" in decoded, decoded)
        config = json.loads((self.cfg / "git-devir.json").read_text(encoding="utf-8"))
        self.assertEqual(config["machine"], "is-5090")
        self.assertEqual(config["auto_owners"], ["kutluege"])
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(json.loads(self.backups()[0].read_text(encoding="utf-8")), self.existing)

        before = self.settings.read_bytes()
        report = self.install()
        self.assertFalse(report["settings_changed"])
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(len(self.backups()), 1)

        proc = subprocess.run([sys.executable, str(INSTALLER), "--uninstall"], env=self.env,
                              capture_output=True, timeout=60, creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), self.existing)
        self.assertEqual(len(self.backups()), 2)

    def test_install_without_existing_settings_and_uninstall_cleans_up(self):
        self.settings.unlink()
        self.install()
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(sorted(data["hooks"]), ["SessionEnd", "SessionStart"])
        proc = subprocess.run([sys.executable, str(INSTALLER), "--uninstall"], env=self.env,
                              capture_output=True, timeout=60, creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), {})

    def test_invalid_machine_label_is_rejected(self):
        args = [sys.executable, str(INSTALLER), "--machine", "my host", "--project-root", str(self.tmp / "Kod")]
        proc = subprocess.run(args, env=self.env, capture_output=True, timeout=60, creationflags=NO_WINDOW)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), self.existing)


def git_devir_installer_decoded(command):
    import install_git_devir
    return install_git_devir._decoded(command)


if __name__ == "__main__":
    unittest.main()
