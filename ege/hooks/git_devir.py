#!/usr/bin/env python3
"""git-devir: Claude Code hook that hands git work over between machines.

SessionStart -> ``git_devir.py start``: fetch, fast-forward when safe, report other machines' wip.
SessionEnd   -> ``git_devir.py end``:   spawn a detached worker that snapshots uncommitted work to
                                        ``wip/<machine>/<branch>`` and pushes already-committed work.

Design rules (see README.md next to this file):
- never touches the user's branch, index or working tree when snapshotting (temporary index);
- never force-pushes the user's branch (only this machine's own wip ref);
- never applies another machine's wip automatically;
- remotes outside ``auto_owners``/``auto_hosts`` are warn-only;
- never breaks a session: every error is swallowed and the process exits 0.

Standard library only; runs on Windows, macOS and Linux.
"""
from __future__ import annotations

import datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

NETWORK_TIMEOUT = 30.0
FETCH_TIMEOUT = 4.0
LOCAL_TIMEOUT = 20.0
CONTEXT_LIMIT = 800
LOCK_STALE_SECONDS = 600
LOCK_WAIT_SECONDS = 90
LOG_LIMIT_BYTES = 512 * 1024
MAX_NOTICES = 8

OFF_MARKER = ".git-devir-off"
MACHINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
SECRET_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.p12", "*.pfx",
                   "credentials*.json", "secrets.*")
SECRET_ALLOWED = (".env.example",)
IN_PROGRESS = (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge"),
               ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect"))
DEFAULTS = {"auto_owners": ["kutluege"], "auto_hosts": ["github.com"], "max_file_mb": 50}

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# --------------------------------------------------------------------------- config and state

def config_dir() -> Path:
    custom = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(custom).expanduser() if custom else Path.home() / ".claude"


def config_path() -> Path:
    return config_dir() / "git-devir.json"


def state_dir() -> Path:
    return config_dir() / "git-devir"


def _str_list(value, default):
    if value is None:
        return list(default)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return list(default)
    return [str(v) for v in value if isinstance(v, str) and v.strip()]


def load_config():
    """Return the validated config dict, or None when the hook should stay silent."""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    machine = str(data.get("machine") or "")
    roots = _str_list(data.get("project_roots"), [])
    if not MACHINE_RE.match(machine) or not roots:
        return None
    try:
        max_mb = float(data.get("max_file_mb", DEFAULTS["max_file_mb"]))
    except (TypeError, ValueError):
        max_mb = float(DEFAULTS["max_file_mb"])
    aliases = data.get("remote_aliases")
    return {
        "machine": machine,
        "project_roots": roots,
        "vault_path": str(data.get("vault_path") or ""),
        "auto_owners": [o.lower() for o in _str_list(data.get("auto_owners"), DEFAULTS["auto_owners"])],
        "auto_hosts": [h.lower() for h in _str_list(data.get("auto_hosts"), DEFAULTS["auto_hosts"])],
        "max_file_mb": max_mb if max_mb > 0 else float(DEFAULTS["max_file_mb"]),
        "remote_aliases": aliases if isinstance(aliases, dict) else {},
    }


def log(message: str) -> None:
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "log.txt"
        if path.exists() and path.stat().st_size > LOG_LIMIT_BYTES:
            os.replace(path, directory / "log.1.txt")
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} [{os.getpid()}] {message}\n")
    except Exception:
        pass


def repo_key(root) -> str:
    return hashlib.sha1(norm(root).encode("utf-8")).hexdigest()[:16]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def notice_path(root) -> Path:
    return state_dir() / "notices" / f"{repo_key(root)}.json"


def add_notice(root, text: str) -> None:
    path = notice_path(root)
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            items = []
    except (OSError, ValueError):
        items = []
    items = [item for item in items if isinstance(item, dict) and item.get("text") != text]
    items.append({"time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "text": text})
    _atomic_write(path, json.dumps(items[-MAX_NOTICES:], ensure_ascii=False, indent=1))
    log(f"notice {Path(root).name}: {text}")


def pop_notices(root) -> list:
    path = notice_path(root)
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    try:
        path.unlink()
    except OSError:
        pass
    if not isinstance(items, list):
        return []
    return [f"{item.get('time', '')} {item.get('text', '')}".strip()
            for item in items if isinstance(item, dict) and item.get("text")]


def lock_path(root) -> Path:
    return state_dir() / "locks" / f"{repo_key(root)}.lock"


def lock_is_fresh(root) -> bool:
    try:
        return time.time() - lock_path(root).stat().st_mtime < LOCK_STALE_SECONDS
    except OSError:
        return False


def acquire_lock(root, wait_seconds: float):
    path = lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return path
        except FileExistsError:
            if not lock_is_fresh(root):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(1.0)


# --------------------------------------------------------------------------- paths and remotes

def norm(path) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(path))))


def is_under(child, parent) -> bool:
    if not str(parent).strip():
        return False
    c, p = norm(child), norm(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives on Windows
        return False


def _alias(url: str, aliases: dict) -> str:
    if url in aliases:
        return str(aliases[url])

    def simple(value):
        value = str(value).strip().replace("\\", "/").rstrip("/")
        return value.lower() if os.name == "nt" else value

    wanted = simple(url)
    for key, value in aliases.items():
        if simple(key) == wanted:
            return str(value)
    return url


def parse_remote(url: str):
    """Return (host, owner, repo) for https/ssh/scp-like URLs, else None."""
    url = (url or "").strip()
    match = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url)
    if not match:
        match = re.match(r"^(?:[^@/\\]+@)?([^/:\\]{2,}):(?![/\\])(.+)$", url)
    if not match:
        return None
    host, path = match.group(1).lower(), match.group(2).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host, "/".join(parts[:-1]), parts[-1]


# --------------------------------------------------------------------------- git

class Result:
    def __init__(self, code, out="", err="", timed_out=False):
        self.code, self.out, self.err, self.timed_out = code, out, err, timed_out

    @property
    def ok(self):
        return self.code == 0


def git_env(extra=None) -> dict:
    env = dict(os.environ)
    for key in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE"):
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"   # never ask for a password on a terminal
    env["GCM_INTERACTIVE"] = "never"   # Git Credential Manager: no login windows
    if extra:
        env.update(extra)
    return env


def git(cwd, *args, timeout=LOCAL_TIMEOUT, env=None) -> Result:
    command = ["git", "--no-optional-locks", "-c", "core.quotepath=false", *args]
    try:
        proc = subprocess.run(command, cwd=str(cwd), env=env or git_env(), stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return Result(124, "", "timeout", timed_out=True)
    except OSError as exc:
        return Result(127, "", str(exc))
    return Result(proc.returncode, proc.stdout.decode("utf-8", "replace"),
                  proc.stderr.decode("utf-8", "replace"))


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:160]
    return ""


class Repo:
    """A repository that passed the gates. ``blocked`` is set when only a notice is allowed."""

    def __init__(self, root, gitdir, url, auto, branch, head, blocked):
        self.root, self.gitdir, self.url, self.auto = root, gitdir, url, auto
        self.branch, self.head, self.blocked = branch, head, blocked

    @property
    def name(self):
        return Path(self.root).name


def resolve_repo(cwd, cfg):
    """Apply every gate. Returns a Repo, or None when git-devir must stay silent."""
    if os.environ.get("GIT_DEVIR_OFF") == "1" or not cwd or not os.path.isdir(cwd):
        return None
    if not any(is_under(cwd, root) for root in cfg["project_roots"]):
        return None
    top = git(cwd, "rev-parse", "--show-toplevel", "--absolute-git-dir")
    if not top.ok:
        return None
    lines = top.out.splitlines()
    if len(lines) < 2:
        return None
    root, gitdir = Path(lines[0].strip()), Path(lines[1].strip())
    if not any(is_under(root, r) for r in cfg["project_roots"]):
        return None
    vault = cfg["vault_path"]
    if vault and (is_under(root, vault) or is_under(vault, root)):
        return None
    if (root / "beyin.py").exists() or (root / OFF_MARKER).exists():
        return None
    remote = git(root, "remote", "get-url", "origin")
    if not remote.ok or not remote.out.strip():
        return None
    url = remote.out.strip()
    parsed = parse_remote(_alias(url, cfg["remote_aliases"]))
    auto = bool(parsed and parsed[0] in cfg["auto_hosts"]
                and parsed[1].split("/")[0].lower() in cfg["auto_owners"])
    blocked = None
    for name, label in IN_PROGRESS:
        if (gitdir / name).exists():
            blocked = f"{label} sürüyor"
            break
    branch = git(root, "symbolic-ref", "-q", "--short", "HEAD")
    branch_name = branch.out.strip() if branch.ok else ""
    head = git(root, "rev-parse", "-q", "--verify", "HEAD^{commit}")
    head_sha = head.out.strip() if head.ok else ""
    if not blocked and not branch_name:
        blocked = "HEAD bağımsız (detached)"
    if not blocked and not head_sha:
        blocked = "dalda henüz commit yok"
    return Repo(root, gitdir, url, auto, branch_name, head_sha, blocked)


def upstream(repo):
    """(remote, merge_ref, ahead, behind) or None when the branch has no upstream."""
    remote = git(repo.root, "config", "--get", f"branch.{repo.branch}.remote")
    merge = git(repo.root, "config", "--get", f"branch.{repo.branch}.merge")
    if not remote.ok or not merge.ok:
        return None
    counts = git(repo.root, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    if not counts.ok:
        return None
    try:
        ahead, behind = (int(x) for x in counts.out.split())
    except ValueError:
        return None
    return remote.out.strip(), merge.out.strip(), ahead, behind


def tracked_dirty(repo) -> bool:
    status = git(repo.root, "status", "--porcelain", "--untracked-files=no")
    return bool(status.ok and status.out.strip())


def any_dirty(repo) -> bool:
    status = git(repo.root, "status", "--porcelain")
    return bool(not status.ok or status.out.strip())


def wip_ref(machine, branch) -> str:
    return f"wip/{machine}/{branch}"


# --------------------------------------------------------------------------- snapshot

def is_secret(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in SECRET_ALLOWED:
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_PATTERNS)


def _split_z(text):
    return [item for item in text.split("\0") if item]


def large_worktree_files(repo, max_bytes):
    listing = git(repo.root, "ls-files", "-z", "-m", "-o", "--exclude-standard")
    big = []
    for rel in dict.fromkeys(_split_z(listing.out)):
        try:
            if (Path(repo.root) / rel).stat().st_size > max_bytes:
                big.append(rel)
        except OSError:
            continue
    return big


def _pathspec_file(directory: Path, lines) -> Path:
    path = directory / f"pathspec-{os.getpid()}-{time.time_ns()}"
    path.write_bytes(b"\0".join(line.encode("utf-8") for line in lines) + b"\0")
    return path


def snapshot(repo, cfg):
    """Commit the working tree on a temporary index. Returns a dict with 'status'.

    status: same | secret | ok | error. The user's index, branch and files are never written.
    """
    max_bytes = int(cfg["max_file_mb"] * 1024 * 1024)
    tmp_dir = state_dir() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    index = tmp_dir / f"index-{repo_key(repo.root)}-{os.getpid()}"
    cleanup = [index, index.with_name(index.name + ".lock")]
    env = git_env({"GIT_INDEX_FILE": str(index)})
    try:
        head_tree = git(repo.root, "rev-parse", "HEAD^{tree}").out.strip()
        real_index = git(repo.root, "rev-parse", "--git-path", "index").out.strip()
        real_index_path = Path(real_index) if os.path.isabs(real_index) else Path(repo.root) / real_index
        if real_index_path.is_file():
            shutil.copyfile(real_index_path, index)  # start from the stat cache: fast `add -A`
        big = large_worktree_files(repo, max_bytes)
        add_args = ["add", "-A"]
        if big:
            spec = _pathspec_file(tmp_dir, ["."] + [f":(exclude,literal){p}" for p in big])
            cleanup.append(spec)
            add_args += [f"--pathspec-from-file={spec}", "--pathspec-file-nul"]
        added = git(repo.root, *add_args, env=env, timeout=120)
        if not added.ok and index.exists():
            index.unlink()  # e.g. split index: retry from an empty temporary index
            added = git(repo.root, *add_args, env=env, timeout=120)
        if not added.ok:
            return {"status": "error", "detail": "git add: " + first_line(added.err)}
        changed = _split_z(git(repo.root, "diff", "--cached", "--name-only", "-z", "--no-renames",
                               "--diff-filter=ACMRT", "HEAD", env=env).out)
        secrets = [p for p in changed if is_secret(p)]
        if secrets:
            return {"status": "secret", "paths": secrets}
        tree = git(repo.root, "write-tree", env=env)
        if not tree.ok:
            return {"status": "error", "detail": "write-tree: " + first_line(tree.err)}
        tree_sha = tree.out.strip()
        # Large blobs that were already staged in the real index: put HEAD's version back.
        changed_set = set(changed)
        staged_big = []
        for entry in _split_z(git(repo.root, "ls-tree", "-r", "-l", "-z", tree_sha).out):
            meta, _, path = entry.partition("\t")
            fields = meta.split()
            if path in changed_set and len(fields) == 4 and fields[3].isdigit() and int(fields[3]) > max_bytes:
                staged_big.append(path)
        if staged_big:
            spec = _pathspec_file(tmp_dir, [f":(literal){p}" for p in staged_big])
            cleanup.append(spec)
            git(repo.root, "reset", "-q", "HEAD", f"--pathspec-from-file={spec}", "--pathspec-file-nul", env=env)
            tree = git(repo.root, "write-tree", env=env)
            if not tree.ok:
                return {"status": "error", "detail": "write-tree: " + first_line(tree.err)}
            tree_sha = tree.out.strip()
        big_all = big + staged_big
        if tree_sha == head_tree:
            return {"status": "same", "big": big_all}
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        message = f"wip({cfg['machine']}): {repo.branch} {stamp}"
        commit = git(repo.root, "commit-tree", tree_sha, "-p", "HEAD", "-m", message)
        if not commit.ok:
            fallback = {"GIT_AUTHOR_NAME": "git-devir", "GIT_COMMITTER_NAME": "git-devir",
                        "GIT_AUTHOR_EMAIL": "git-devir@localhost", "GIT_COMMITTER_EMAIL": "git-devir@localhost"}
            commit = git(repo.root, "commit-tree", tree_sha, "-p", "HEAD", "-m", message, env=git_env(fallback))
        if not commit.ok:
            return {"status": "error", "detail": "commit-tree: " + first_line(commit.err)}
        return {"status": "ok", "sha": commit.out.strip(), "tree": tree_sha, "big": big_all}
    finally:
        for path in cleanup:
            try:
                path.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- end (worker)

def end_worker(cwd, cfg):
    repo = resolve_repo(cwd, cfg)
    if repo is None:
        return
    if repo.blocked:
        add_notice(repo.root, f"Kapanışta {repo.blocked}; wip yedeği ve push yapılmadı.")
        return
    lock = acquire_lock(repo.root, LOCK_WAIT_SECONDS)
    if lock is None:
        log(f"{repo.name}: lock busy, skipped")
        return
    try:
        if repo.auto:
            _end_auto(repo, cfg)
        else:
            _end_warn_only(repo)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _end_warn_only(repo):
    if any_dirty(repo):
        add_notice(repo.root, f"Kapanışta `{repo.branch}` dalında commit edilmemiş değişiklik vardı "
                              "(bu remote yalnız uyarı modunda; yedek alınmadı).")
    state = upstream(repo)
    if state is None:
        return
    remote, merge, ahead, behind = state
    if ahead and behind:
        add_notice(repo.root, f"`{repo.branch}` uzak dalla ayrışmış (yerel +{ahead}, uzak +{behind}); "
                              "yalnız uyarı modu.")
    elif ahead:
        add_notice(repo.root, f"`{repo.branch}` dalında push edilmemiş {ahead} commit var "
                              "(yalnız uyarı modu): `git push`.")


def _end_auto(repo, cfg):
    machine, branch = cfg["machine"], repo.branch
    ref = wip_ref(machine, branch)
    tracking = f"refs/remotes/origin/{ref}"

    # 1. snapshot of uncommitted work
    snap = snapshot(repo, cfg)
    status = snap["status"]
    if snap.get("big"):
        names = ", ".join(f"`{p}`" for p in snap["big"][:3])
        more = f" (+{len(snap['big']) - 3})" if len(snap["big"]) > 3 else ""
        add_notice(repo.root, f"{cfg['max_file_mb']:g} MB üstü dosyalar wip yedeğine alınmadı: {names}{more}.")
    if status == "secret":
        names = ", ".join(f"`{p}`" for p in snap["paths"][:3])
        add_notice(repo.root, f"wip yedeği iptal: gizli olabilecek dosya var ({names}). "
                              "Dosyayı .gitignore'a ekle veya kendin karar ver.")
    elif status == "error":
        add_notice(repo.root, f"wip yedeği alınamadı: {snap['detail']}")
    elif status == "ok":
        old_tree = git(repo.root, "rev-parse", "-q", "--verify", f"{tracking}^{{tree}}").out.strip()
        old_parent = git(repo.root, "rev-parse", "-q", "--verify", f"{tracking}^1").out.strip()
        if old_tree == snap["tree"] and old_parent == repo.head:
            log(f"{repo.name}: wip unchanged, push skipped")
        else:
            pushed = git(repo.root, "push", "--force", "--quiet", "origin",
                         f"{snap['sha']}:refs/heads/{ref}", timeout=NETWORK_TIMEOUT)
            if pushed.ok:
                log(f"{repo.name}: wip pushed to {ref} ({snap['sha'][:10]})")
            else:
                reason = "zaman aşımı" if pushed.timed_out else first_line(pushed.err)
                add_notice(repo.root, f"wip yedeği push edilemedi (`{ref}`): {reason}")

    # 2. push already-committed work (never force)
    state = upstream(repo)
    all_pushed = False
    if state is None:
        if git(repo.root, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}").ok:
            add_notice(repo.root, f"`{branch}` dalının upstream'i ayarlı değil; push yapılmadı: "
                                  f"`git branch --set-upstream-to=origin/{branch}`.")
        else:
            add_notice(repo.root, f"`{branch}` dalının upstream'i yok; commit'ler push edilmedi: "
                                  f"`git push -u origin {branch}`.")
    else:
        remote, merge, ahead, behind = state
        if remote != "origin":
            if ahead:
                add_notice(repo.root, f"`{branch}` upstream'i origin değil ({remote}); push yapılmadı.")
        elif ahead and behind:
            add_notice(repo.root, f"`{branch}` uzak dalla ayrışmış (yerel +{ahead}, uzak +{behind}); push "
                                  "yapılmadı. Çözüm: `git pull --rebase` (kullanıcıya sor).")
        elif ahead:
            pushed = git(repo.root, "push", "--quiet", "origin", f"HEAD:{merge}", timeout=NETWORK_TIMEOUT)
            if pushed.ok:
                log(f"{repo.name}: pushed {ahead} commit(s) on {branch}")
                all_pushed = True
            else:
                reason = "zaman aşımı" if pushed.timed_out else "reddedildi (uzakta yeni commit olabilir)"
                add_notice(repo.root, f"`{branch}` push edilemedi: {reason}. Açılışta `git pull --rebase` "
                                      "gerekebilir.")
        else:
            all_pushed = True  # nothing local is missing on the remote (it may be ahead of us)

    # 3. clean up this machine's wip when nothing is left to protect
    if all_pushed and status == "same" and not any_dirty(repo):
        exists = git(repo.root, "ls-remote", "--heads", "origin", f"refs/heads/{ref}", timeout=NETWORK_TIMEOUT)
        if exists.ok and exists.out.strip():
            deleted = git(repo.root, "push", "--quiet", "origin", f":refs/heads/{ref}", timeout=NETWORK_TIMEOUT)
            if deleted.ok:
                log(f"{repo.name}: deleted own {ref}")
            else:
                add_notice(repo.root, f"Eski wip dalı silinemedi (`{ref}`): {first_line(deleted.err)}")
        git(repo.root, "update-ref", "-d", tracking)


# --------------------------------------------------------------------------- start

def _fetch(repo):
    """Fetch origin; on timeout leave git running in the background (never kill it mid-write)."""
    tmp_dir = state_dir() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    err_path = tmp_dir / f"fetch-{repo_key(repo.root)}.err"
    options = {"cwd": str(repo.root), "env": git_env(), "stdin": subprocess.DEVNULL,
               "stdout": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        with err_path.open("wb") as err:
            proc = subprocess.Popen(["git", "fetch", "--quiet", "origin"], stderr=err, **options)
    except OSError:
        return "fail", ""
    try:
        code = proc.wait(timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "timeout", ""
    if code == 0:
        return "ok", ""
    try:
        detail = first_line(err_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        detail = ""
    return "fail", detail


def other_wips(repo, machine):
    prefix = "refs/remotes/origin/wip/"
    head = git(repo.root, "log", "-1", "--format=%ct %T", "HEAD").out.split()
    if len(head) != 2:
        return []
    head_time, head_tree = int(head[0]), head[1]
    listing = git(repo.root, "for-each-ref", "--format=%(refname)%00%(committerdate:unix)%00%(tree)", prefix)
    found = []
    for line in listing.out.splitlines():
        parts = line.split("\0")
        if len(parts) != 3 or not parts[0].startswith(prefix):
            continue
        other, _, rest = parts[0][len(prefix):].partition("/")
        if other == machine or rest != repo.branch:
            continue
        try:
            when = int(parts[1])
        except ValueError:
            continue
        if when >= head_time and parts[2] != head_tree:
            found.append(other)
    return found


def start(cwd, cfg):
    repo = resolve_repo(cwd, cfg)
    if repo is None:
        return ""
    notices = [f"- Önceki kapanış: {text}" for text in pop_notices(repo.root)[-4:]]
    lines = []
    if repo.blocked:
        lines.append(f"- Repoda {repo.blocked}; git-devir bu açılışta pull yapmadı.")
        return _render(repo, lines + notices)
    fetched, detail = _fetch(repo)
    if fetched == "timeout":
        lines.append(f"- `git fetch` {FETCH_TIMEOUT:g} sn'de bitmedi (arka planda sürüyor); uzak durum eski olabilir.")
    elif fetched == "fail":
        lines.append("- `git fetch` başarısız" + (f": {detail}" if detail else "") + ".")
    state = upstream(repo)
    if state is not None:
        remote, merge, ahead, behind = state
        dirty = tracked_dirty(repo)
        if ahead and behind:
            lines.append(f"- `{repo.branch}` uzak dalla ayrışmış (yerel +{ahead}, uzak +{behind}). "
                         "Çözüm: `git pull --rebase` (kullanıcıya sor).")
        elif behind:
            if not repo.auto:
                lines.append(f"- `{repo.branch}` uzak dalın {behind} commit gerisinde (yalnız uyarı modu). "
                             "Çek: `git pull --ff-only`.")
            elif dirty:
                lines.append(f"- `{repo.branch}` uzak dalın {behind} commit gerisinde ama çalışma alanında "
                             "değişiklik var; pull yapılmadı. Önce commit/stash, sonra `git pull --ff-only`.")
            elif fetched == "timeout":
                lines.append(f"- `{repo.branch}` en az {behind} commit geride; fetch bitmediği için pull "
                             "yapılmadı: `git pull --ff-only`.")
            elif lock_is_fresh(repo.root):
                lines.append("- Önceki kapanışın git-devir işçisi hâlâ çalışıyor; pull yapılmadı: "
                             "`git pull --ff-only`.")
            else:
                merged = git(repo.root, "merge", "--ff-only", "--quiet", "@{u}")
                if merged.ok:
                    lines.append(f"- Uzak daldan {behind} commit çekildi (fast-forward).")
                else:
                    lines.append(f"- Otomatik fast-forward başarısız ({first_line(merged.err)}). "
                                 "Elle: `git pull --ff-only`.")
        if ahead and not behind:
            lines.append(f"- `{repo.branch}` dalında push edilmemiş {ahead} commit var: `git push`.")
    for other in other_wips(repo, cfg["machine"]):
        ref = f"origin/{wip_ref(other, repo.branch)}"
        lines.append(f"- Başka makinede (`{other}`) yarım iş var: `{ref}`. Bakmak için: "
                     f"`git diff HEAD {ref}`; almak için kullanıcıya sor.")
    return _render(repo, lines + notices)


def _render(repo, lines):
    if not lines:
        return ""
    text = f"git-devir ({repo.name}, {repo.branch or 'HEAD'}):"
    for line in lines:
        if len(text) + 1 + len(line) > CONTEXT_LIMIT - 2:
            room = CONTEXT_LIMIT - len(text) - 3
            if room > 80:  # cut a long line; drop a short leftover
                text += "\n" + line[:room]
            return text + "\n…"
        text += "\n" + line
    return text


# --------------------------------------------------------------------------- entry point

def read_payload():
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.buffer.read().decode("utf-8-sig", "replace").strip()
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def payload_cwd(payload):
    value = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR")
    if not value:
        try:
            value = os.getcwd()
        except OSError:
            return None
    return str(value)


def spawn_worker(cwd):
    command = [sys.executable, str(Path(__file__).resolve()), "worker", "--cwd", cwd]
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "close_fds": True, "cwd": cwd}
    if os.name == "nt":
        # CREATE_NO_WINDOW (not DETACHED_PROCESS): the worker gets a hidden console that its git
        # children inherit, so no console window flashes. Try to leave Claude Code's job object
        # first, so the worker outlives the session; fall back when breakaway is not allowed.
        base = subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        try:
            subprocess.Popen(command, creationflags=base | breakaway, **options)
            return
        except OSError:
            subprocess.Popen(command, creationflags=base, **options)
            return
    options["start_new_session"] = True
    subprocess.Popen(command, **options)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if not argv or argv[0] not in ("start", "end", "worker"):
            return 0
        mode = argv[0]
        sync = "--sync" in argv
        cwd_arg = argv[argv.index("--cwd") + 1] if "--cwd" in argv and argv.index("--cwd") + 1 < len(argv) else None
        if os.environ.get("GIT_DEVIR_OFF") == "1":
            return 0
        cfg = load_config()
        if cfg is None:
            return 0
        if mode == "worker":
            if cwd_arg:
                end_worker(cwd_arg, cfg)
            return 0
        payload = read_payload()
        cwd = cwd_arg or payload_cwd(payload)
        if not cwd or not any(is_under(cwd, root) for root in cfg["project_roots"]):
            return 0
        if mode == "end":
            if sync:
                end_worker(cwd, cfg)
            else:
                spawn_worker(cwd)
            return 0
        if str(payload.get("source", "")) == "compact":
            return 0
        text = start(cwd, cfg)
        if text:
            output = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
            sys.stdout.write(json.dumps(output, ensure_ascii=True) + "\n")
            sys.stdout.flush()
    except Exception as exc:  # the hook must never break a session
        log(f"error: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
