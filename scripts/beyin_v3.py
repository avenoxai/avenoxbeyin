#!/usr/bin/env python3
"""Local-first CLI for the shared V3 memory foundation."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time


def default_state(vault: Path) -> Path:
    """Keep mutable state outside the vault, separated by canonical vault path."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    key = hashlib.sha256(str(Path(vault).resolve()).encode()).hexdigest()[:16]
    return base / "beyin-v3" / key


SOURCE_WRITE_COMMANDS = ("note-create", "note-edit", "task-create", "task-update")
NOTE_JSON_COMMANDS = ("note-read", "note-edit")


class NoteUsageError(ValueError):
    pass


def mark_session_edit(state: Path) -> None:
    """Open the Stop reminder's edit window for the Claude Code session that ran this write (#212).

    Bash is not a PostToolUse matcher, so a note or task written through `beyin.py` from the Bash
    tool was invisible to the receipt reminder. Claude Code sets CLAUDE_CODE_SESSION_ID in Bash
    subprocesses to the session_id its hooks receive, so the marker names that exact session;
    other shells and harnesses do not set it and nothing is guessed. Never costs the write itself.
    """
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session or os.environ.get("BEYIN_V3_NO_RECEIPT_REMINDER") == "1":
        return
    try:
        folder = Path(state) / "receipt-reminders"
        edited = folder / (hashlib.sha256(session.encode("utf-8")).hexdigest() + ".edited")
        if edited.exists():
            return  # the window opened by the first edit stays, as for PostToolUse
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / (edited.name + "." + str(os.getpid()) + ".tmp")
        temporary.write_text(json.dumps({"at": time.time()}), encoding="utf-8")
        os.replace(temporary, edited)
    except OSError:
        pass


def _is_absolute_cross_platform(raw: str) -> bool:
    """True if a path is absolute on any supported platform (Windows or POSIX)."""
    return bool(re.match(r"^([A-Za-z]:[/\\]|[/\\])", raw))


def _resolve_state_path(pinned: Path) -> Path:
    """Validate a pin even when non-strict realpath suppresses invalid-path errors."""
    if "\0" in str(pinned):
        raise ValueError("embedded null character in state path")
    resolved = Path(pinned).expanduser().resolve()
    try:
        resolved.stat()
    except FileNotFoundError:
        pass  # A valid state directory may not have been created yet.
    return resolved


def resolve_state(vault: Path, pinned: Path) -> Path:
    """Resolve a foreign OS/user's conventional default; preserve custom and local pins."""
    expected = default_state(vault).resolve()
    raw = str(pinned)
    if not raw or not raw.strip():
        return expected
    if _in_package_container(raw):
        try:
            resolved = _resolve_state_path(pinned)
            if _is_absolute_cross_platform(str(resolved)):
                return resolved
        except (OSError, ValueError, RuntimeError):
            pass
        return expected
    portable = raw.replace("\\", "/")
    # Match complete standard layouts, rather than a generic /state/ or /local/ suffix.
    # A copied vault normally changes its absolute path and therefore its hash.
    conventional = re.fullmatch(
        r"(?:[A-Za-z]:/Users/[^/]+/AppData/Local|/home/[^/]+/\.local/state|"
        r"/root/\.local/state|/Users/[^/]+/Library/Application Support)/beyin-v3/[0-9a-f]{16}",
        portable, re.IGNORECASE)
    local_parent = str(expected.parent).replace("\\", "/").casefold()
    if conventional and portable.rsplit("/", 1)[0].casefold() != local_parent:
        return expected
    try:
        resolved = _resolve_state_path(pinned)
    except (OSError, ValueError, RuntimeError):
        return expected

    if not Path(pinned).expanduser().is_absolute():
        if re.match(r"^[A-Za-z]:[/\\]", raw) or (sys.platform == "win32" and raw.startswith(("/", "\\"))):
            return expected
    return resolved


def _in_package_container(pinned: str) -> bool:
    """True when a pinned path runs through an MSIX package container.

    Reads the string, not the local path flavour: a Windows pin is inspected the
    same way when the tests run on POSIX.
    """
    parts = [part.lower() for part in pinned.replace("\\", "/").split("/") if part]
    return "packages" in parts and "localcache" in parts[parts.index("packages") + 1:]


def _sibling_state_roots(pinned: Path) -> list:
    """Installed state roots for the same vault key on both sides of a package redirect.

    Default roots end in beyin-v3/<vault key>. When %LOCALAPPDATA% is redirected,
    an install run inside the package and one run outside it produce two such
    roots for the same vault, each with its own database. Both are real and
    populated, so neither looks broken on its own. An explicit --state root has
    no key layout and is skipped. Returns an empty list on any lookup error.
    """
    parts = list(pinned.parts)
    lowered = [part.lower() for part in parts]
    if "beyin-v3" not in lowered or lowered.index("beyin-v3") + 2 != len(parts):
        return []
    index = lowered.index("beyin-v3")
    base = parts[:index]
    if "packages" in lowered[:index]:
        base = parts[:lowered.index("packages")]
    if not base:
        return []
    local, key = Path(*base), parts[index + 1]
    candidates = [local / "beyin-v3" / key]
    try:
        packages = local / "Packages"
        if packages.is_dir():
            # Literal path per package, not a glob: a key is never read as a pattern.
            candidates += [entry / "LocalCache" / "Local" / "beyin-v3" / key
                           for entry in sorted(packages.iterdir())]
    except OSError:
        return []
    found, identities = [], []
    for candidate in candidates:
        try:
            if not (candidate / "v3-install.json").is_file():
                continue
            # Inside the package the plain spelling is redirected into the container, so
            # both spellings reach one directory there. Count directories, not strings.
            identity = os.path.normcase(str(candidate.resolve()))
            if identity in identities or any(os.path.samefile(candidate, other) for other in found):
                continue
        except (OSError, ValueError, RuntimeError):
            continue
        found.append(str(candidate))
        identities.append(identity)
    return found


def state_location(vault: Path, state: Path, windows=None) -> dict:
    """Compare the pinned state root with the one this process actually reads.

    Windows redirects %LOCALAPPDATA% for an MSIX-packaged client into
    Packages/<id>/LocalCache/Local. A pin inside that container is one string and
    two directories for processes in and out of the package, so each side can end
    up holding half the memory. Information only: it writes nothing, makes no
    model call and does not raise the doctor status. A pin that resolves elsewhere
    is only warned about on Windows: a POSIX symlink resolves the same for every
    process, so a relocated state behind one is not a split.
    """
    windows = sys.platform == "win32" if windows is None else windows
    report = {"effective_state": str(state),
              "installed_at_effective_state": (state / "v3-install.json").is_file(),
              "pin_status": "absent", "pinned_state": None, "pinned_resolves_here_to": None,
              "pinned_in_package_container": False, "pin_resolves_elsewhere": False,
              "installed_at_pinned_state": None, "sibling_state_roots": [], "warnings": []}
    config = vault / ".beyin-runtime.json"
    pinned = None
    if config.is_file():
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
            value = data.get("state") if isinstance(data, dict) else None
        except (ValueError, OSError):
            value = None
        pinned = value if isinstance(value, str) and value.strip() else None
        report["pin_status"] = "present" if pinned else "unreadable"
    if pinned is None:
        return report
    try:
        resolved = _resolve_state_path(pinned)
    except (OSError, ValueError, RuntimeError):
        report.update(pinned_state=pinned, installed_at_pinned_state=False)
        report["warnings"].append(
            "invalid_state_path: the pinned state root cannot be resolved on this machine; "
            "the local default is used. Reinstall here to bind lifecycle commands.")
        return report
    if not Path(pinned).expanduser().is_absolute():
        report["warnings"].append(
            "pinned_state_not_absolute: the pinned state root is not an absolute path on this "
            "machine. The installer pins an absolute path, so this one was written by another OS "
            "through a synced vault or edited by hand (#249); the installed beyin.py reads this "
            "machine's default state instead. Installation files are per machine and stay out of "
            "the sync; see https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/MULTI-MACHINE.md.")
    elif resolve_state(vault, Path(pinned)) != resolved:
        report['warnings'].append('foreign_default_state: the synced default state belongs to another user or OS; '
                                  'this machine uses its local default. Reinstall here to bind lifecycle commands.')
    same = os.path.normcase(str(resolved)) == os.path.normcase(pinned)
    report.update({"pinned_state": pinned, "pinned_resolves_here_to": str(resolved),
                   "pinned_in_package_container": _in_package_container(pinned),
                   "pin_resolves_elsewhere": not same,
                   "installed_at_pinned_state": (resolved / "v3-install.json").is_file()})
    if report["pinned_in_package_container"]:
        report["warnings"].append(
            "pinned_in_package_container: the pinned state root runs through an MSIX package "
            "container (Packages\\...\\LocalCache). That path carries the package "
            "identity and goes away when the package is reset or reinstalled under another "
            "identity. Moving the state to a plain local directory is the durable fix; "
            "see https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/UPDATE.md.")
    if report["pin_resolves_elsewhere"] and windows:
        report["warnings"].append(
            "pin_resolves_elsewhere: this process resolves the pinned path to " + str(resolved) +
            ". A process on the other side of an MSIX redirect reads the same string and may "
            "reach a different directory; run doctor inside and outside the package and compare.")
    if not report["installed_at_pinned_state"]:
        report["warnings"].append(
            "pinned_state_empty: no v3-install.json under the pinned state root as this process "
            "reads it. Either the pin names a directory this process cannot see, or the state "
            "was moved without reinstalling with the new --state.")
    if os.path.normcase(str(resolved)) != os.path.normcase(str(state)):
        report["warnings"].append(
            "effective_state_differs: this run reads " + str(state) + " rather than the pinned "
            "root. Pass the pinned --state, or use the installed beyin.py, which reads the pin.")
    report["sibling_state_roots"] = _sibling_state_roots(resolved)
    if len(report["sibling_state_roots"]) > 1:
        report["warnings"].append(
            "state_split: this vault has an installed state root on both sides of the package "
            "redirect (" + ", ".join(report["sibling_state_roots"]) + "). Each one carries its own "
            "database, so which half a session reads depends on whether it runs inside the "
            "package. Keep one and move it out of the container; see https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/UPDATE.md.")
    return report


def receipt_line_endings(vault: Path) -> dict:
    """Report whether git can rewrite receipt line endings in this vault (#205).

    Receipts are compared byte for byte. With core.autocrlf=true (the Git for Windows
    default) a pulled receipt is checked out as CRLF unless the vault stops that for
    receipts/ with `-text` or `eol=lf`; the index then reads a different summary and the
    same receipt resubmitted from the other machine fails as an event id collision.
    Read-only, information only; a vault that is not a git work tree reports not_applicable.
    """
    def git(*args):
        done = subprocess.run(("git", "-C", str(vault)) + args, capture_output=True, text=True, timeout=10)
        return done.returncode, done.stdout.strip()
    try:
        code, inside = git("rev-parse", "--is-inside-work-tree")
        if code != 0 or inside != "true":
            return {"status": "not_applicable", "reason": "vault is not a git work tree"}
        _, autocrlf = git("config", "--type=bool", "--get", "core.autocrlf")
        if autocrlf != "true":
            return {"status": "ok", "autocrlf": autocrlf or "unset"}
        _, attrs = git("check-attr", "text", "eol", "--", "receipts/x.md")
        found = dict((line.split(": ")[1], line.split(": ")[2]) for line in attrs.splitlines() if line.count(": ") == 2)
        if found.get("text") == "unset" or found.get("eol") == "lf":
            return {"status": "ok", "autocrlf": "true", "receipts_attributes": found}
        return {"status": "warning", "autocrlf": "true", "receipts_attributes": found,
                "warning": "line_endings: core.autocrlf=true and receipts/ is not pinned, so a receipt synced "
                           "from another machine can be checked out as CRLF, which changes its bytes and makes "
                           "the same receipt fail as an event id collision. Add 'receipts/** -text' to the "
                           "vault's .gitattributes and check the receipts out again; see https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/MULTI-MACHINE.md."}
    except Exception as exc:  # no git, a hung git or a broken config must never hide the rest of doctor
        return {"status": "unavailable", "error": type(exc).__name__}


def load_engine():
    _ensure_scripts_path()
    adjacent = Path(__file__).resolve().parent / "beyin_v3.py"
    path = adjacent if adjacent != Path(__file__).resolve() and adjacent.exists() else Path(__file__).resolve().parents[1] / "template/.claude/scripts/beyin_v3.py"
    spec = importlib.util.spec_from_file_location("beyin_v3_shared_engine", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Shared V3 engine could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _ensure_scripts_path():
    """Put the runtime scripts directory first on sys.path without importing the sync engine.
    Helpers that only need sibling modules (skills, preferences, the advisor client, compaction)
    call this instead of load_sync(); the directory must stay first so `beyin_v3` resolves to the
    runtime, never to this CLI file."""
    adjacent = Path(__file__).resolve().parent
    directory = str(adjacent if (adjacent / "beyin_v3_sync.py").exists() else Path(__file__).resolve().parents[1] / "template/.claude/scripts")
    if sys.path[:1] != [directory]:
        sys.path.insert(0, directory)


def load_sync():
    _ensure_scripts_path()
    from beyin_v3_sync import SyncEngine
    return SyncEngine


# Mirror beyin_v3_jev_client.FEATURES and PROVIDERS and beyin_v3_laya.CHECKPOINTS; duplicated
# so argument parsing never imports the optional client. tests/v3_jev_toggle_test.py pins them.
JEV_FEATURES = ("context", "review", "answer", "auto_context")
JEV_PROVIDERS = ("typesafe", "vercel", "laya")
LAYA_MODELS = ("multilingual", "english")
JEV_NOTICE = ("auto_context is on: every turn sends the prompt plus the title and first 600 characters of up to 8 "
              "candidate internal/public notes to the provider. Private notes are never sent.")
JEV_NOTICE_LAYA = ("auto_context is on: every turn sends the prompt plus the title and first 600 characters of up to 8 "
                   "candidate internal/public notes to the local Laya server, one request per note. Private notes are never sent. "
                   "Laya is shadow-only: its scores are only logged and never change the context.")
JEV_WARNING = "TYPESAFE_API_KEY is not set; calls degrade to local results."
LAYA_NOTICE = ("provider laya: calls go only to the local laya-serve at {base_url}; start it with LAYA_HOST=127.0.0.1. "
               "Laya is shadow-only: scores are logged for measurement and never change results.")
# Coded refusals that deserve a next step. ASCII Turkish, like the other human lines.
ERROR_HINTS = {
    "laya_shadow_only": ("Laya yalniz golge modda calisir: puanlari olcum icin kaydedilir, gordugun sonucu degistirmez. "
                         "Laya icin: jev shadow --provider laya. Acik mod icin: jev on --provider typesafe."),
}


def jev_client():
    _ensure_scripts_path()
    import beyin_v3_jev_client as client
    return client


def jev_status(state: Path):
    """Doctor path: with no config and no kill switch the optional client is not imported."""
    if (not (state / "jev.json").exists() and not (state / "jev.disabled").exists()
            and os.environ.get("BEYIN_JEV_DISABLE") is None):
        return {"mode": "off", "configured": False}
    return jev_client().status(state)


def jev_context_mode(vault):
    """The vault's context_mode for the Jev notice only. Jev status and its off switch never depend on a
    valid preferences file: a malformed or newer file (a key this version does not know) gives no notice."""
    try:
        import beyin_v3_preferences as preferences
        return preferences.read(vault).get("context_mode")
    except (ValueError, OSError):
        return None


def jev_advice(result):
    laya = result.get("provider") == "laya"
    if result.get("automatic_model_calls"):
        result["notice"] = JEV_NOTICE_LAYA if laya else JEV_NOTICE
        if result.get("context_mode") and result.get("context_mode") != "turn":
            result["context_mode_notice"] = (
                f"Automatic context requires context_mode 'turn', but current mode is '{result['context_mode']}'. "
                "Enable with: beyin.py preferences --context-mode turn"
            )
    if laya:
        result["provider_notice"] = LAYA_NOTICE.format(base_url=result.get("laya", {}).get("base_url", "?"))
    elif result.get("mode") != "off" and not result.get("key_present"):
        result["warning"] = JEV_WARNING
    return result


HOOK_FILES = (".claude/settings.local.json", ".codex/hooks.json", ".agents/hooks.json",
              ".kiro/hooks/beyin-v3.json")


def _hook_arguments(command: str) -> list:
    """Arguments of one installed hook command: POSIX shell text or the Windows EncodedCommand."""
    import base64
    import re
    import shlex
    match = re.search(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", command, re.I)
    if match:
        try:
            script = base64.b64decode(match.group(1), validate=True).decode("utf-16le")
        except (ValueError, UnicodeError):
            return []
        return [item.replace("''", "'") for item in re.findall(r"'((?:[^']|'')*)'", script)]
    try:
        return shlex.split(command)
    except ValueError:
        return []


def hook_paths(vault: Path) -> dict:
    """Read-only (#204): hook commands that still name another install location.

    A vault moved to another account or machine together with its hook files keeps commands
    such as C:\\Users\\<old>\\...\\beyin_v3_hook.py; every lifecycle hook then fails with a
    generic error. Reads only managed hook files; never writes and never walks the vault."""
    stale, checked = [], 0
    try:
        here = vault.resolve()
    except OSError:
        here = vault
    for name in HOOK_FILES:
        path = vault / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            stale.append({"file": name, "reason": "unreadable"})
            continue
        handlers = []
        if name == ".kiro/hooks/beyin-v3.json":
            for item in data.get("hooks", []) if isinstance(data.get("hooks"), list) else ():
                if isinstance(item, dict) and isinstance(item.get("action"), dict):
                    command = item["action"].get("command")
                    if isinstance(command, str):
                        handlers.append((item.get("trigger", ""), command))
        else:
            events = data.get("beyin-v3", {}) if name == ".agents/hooks.json" else data.get("hooks", {})
            for event, groups in (events.items() if isinstance(events, dict) else ()):
                for group in groups if isinstance(groups, list) else ():
                    if not isinstance(group, dict):
                        continue
                    for handler in group.get("hooks", [group]) if "hooks" in group else [group]:
                        if isinstance(handler, dict) and isinstance(handler.get("command"), str):
                            handlers.append((event, handler["command"]))
        for event, command in handlers:
            arguments = _hook_arguments(command)
            expected = "/beyin_v3_kiro.py" if name == ".kiro/hooks/beyin-v3.json" else "/beyin_v3_hook.py"
            script = next((a for a in arguments if a.replace("\\", "/").endswith(expected)), None)
            if script is None:
                continue
            checked += 1
            reasons = []
            if not Path(script).is_file():
                reasons.append(("hook_script_missing", script))
            if "--vault" in arguments[:-1]:
                bound = arguments[arguments.index("--vault") + 1]
                try:
                    same = Path(bound).resolve() == here
                except OSError:
                    same = False
                if not same:
                    reasons.append(("other_vault", bound))
            if arguments and Path(arguments[0]).is_absolute() and not Path(arguments[0]).exists():
                reasons.append(("python_missing", arguments[0]))
            for reason, value in reasons:
                entry = {"file": name, "event": event, "reason": reason, "path": value}
                if entry not in stale:
                    stale.append(entry)
    if not checked and not stale:
        return {"status": "not_installed", "checked": 0}
    result = {"status": "stale" if stale else "ok", "checked": checked}
    if stale:
        result["stale"] = stale[:20]
        result["hint"] = ("Hook commands point to another install location (vault moved, or hook files synced "
                          "from another machine). Run the installer again on this machine with this vault; if the "
                          "vault moved from another account or folder, pass the old state folder (or a copy) with "
                          "--state. Keep hook files out of sync: https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/MULTI-MACHINE.md")
    return result


def _hook_commands(argv):
    """Build POSIX and Windows command strings for hook invocations."""
    import base64
    import shlex
    if any(any(c in str(value) for c in "\n\r\x00") for value in argv):
        raise ValueError("Newlines or NUL in command paths are unsupported")
    portable_argv = [str(value).replace("\\", "/") if os.name == "nt" else str(value) for value in argv]
    posix = shlex.join(portable_argv)
    script = "& " + " ".join("'" + str(value).replace("'", "''") + "'" for value in argv)
    script += "; exit $LASTEXITCODE"
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    launcher = "powershell.exe"
    if os.name == "nt":
        windows_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
        launcher_path = str(Path(windows_root) / "System32/WindowsPowerShell/v1.0/powershell.exe").replace("\\", "/")
        launcher = subprocess.list2cmdline([launcher_path])
    windows = launcher + " -NoProfile -NonInteractive -EncodedCommand " + encoded
    return posix, windows


def _local_hook_file(root, path):
    root = root.resolve()
    path.relative_to(root)
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError('hook configuration paths must not be symlinks')
        cursor = cursor.parent
    return path


def _backup_hook_file(path, raw):
    # Existing backups are user data too. Exclusive creation preserves all prior copies.
    index = 0
    while True:
        backup = path.with_name(path.name + '.bak' + ('.' + str(index) if index else ''))
        try:
            with backup.open('xb') as out:
                out.write(raw); out.flush(); os.fsync(out.fileno())
            return backup
        except FileExistsError:
            index += 1


def _write_hook_json(path, data):
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix='.hook-reconcile-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def hook_reconcile(vault: Path, state: Path, force: bool = False) -> dict:
    """Explicitly reconcile hook commands pointing to another install location (#205).

    Creates a .bak backup of each modified hook file before changing it.
    Replaces stale Python and vault paths with current local interpreter and vault paths.
    Never runs automatically or silently.
    """
    vault = vault.resolve()
    state = state.resolve()
    hook_script = vault / ".claude/scripts/beyin_v3_hook.py"
    kiro_script = vault / ".claude/scripts/beyin_v3_kiro.py"
    _local_hook_file(vault, hook_script)
    if not hook_script.is_file():
        raise RuntimeError(f"Hook script not found at {hook_script}. Reinstall or repair the vault first.")
    if (vault / ".kiro/hooks/beyin-v3.json").is_file():
        _local_hook_file(vault, kiro_script)
        if not kiro_script.is_file():
            raise RuntimeError(f"Kiro hook adapter not found at {kiro_script}. Reinstall or repair the vault first.")

    for name in HOOK_FILES:
        _local_hook_file(vault, vault / name)
    _local_hook_file(state, state / "v3-install.json")
    paths_report = hook_paths(vault)
    if paths_report.get("status") == "not_installed":
        return {
            "status": "not_installed",
            "checked": 0,
            "reconciled_count": 0,
            "files": [],
            "message": "No installed hooks found to reconcile.",
        }

    expected_state = str(state.resolve())
    runtime_stale = False
    runtime_path = vault / ".beyin-runtime.json"
    if runtime_path.is_file():
        _local_hook_file(vault, runtime_path)
        try:
            rdata = json.loads(runtime_path.read_text(encoding="utf-8"))
            if isinstance(rdata, dict) and rdata.get("state") != expected_state:
                runtime_stale = True
        except (OSError, ValueError):
            pass

    launchers_stale = False
    planned_launchers = {}
    launcher_baselines = {}
    try:
        manifest = json.loads((state / "v3-install.json").read_text(encoding="utf-8"))
        launcher_baselines = {name: base64.b64decode(info.get("installed_content") or "")
                              for name, info in manifest.get("files", {}).items() if isinstance(info, dict)}
    except (OSError, ValueError):
        pass
    launcher_script = vault / ".claude/scripts/beyin_v3_launchers.py"
    if not launcher_script.is_file():
        launcher_script = Path(__file__).resolve().parent / "beyin_v3_launchers.py"
        if not launcher_script.is_file():
            launcher_script = Path(__file__).resolve().parents[1] / "template/.claude/scripts/beyin_v3_launchers.py"
    if launcher_script.is_file():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("beyin_reconcile_launchers", launcher_script)
            lmod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(lmod)
            planned_launchers = lmod.plan_launchers(vault, state)
            for lname, lbytes in planned_launchers.items():
                lpath = vault / lname
                if lpath.is_file():
                    _local_hook_file(vault, lpath)
                    original = lpath.read_bytes()
                    baseline = launcher_baselines.get(lname)
                    if baseline is not None and original.replace(b"\r\n", b"\n") == baseline.replace(b"\r\n", b"\n") and original != lbytes:
                        launchers_stale = True
        except Exception:
            pass

    if paths_report.get("status") != "stale" and not force and not runtime_stale and not launchers_stale:
        return {
            "status": "ok",
            "checked": paths_report.get("checked", 0),
            "reconciled_count": 0,
            "files": [],
            "message": "Hook paths are already valid; no reconciliation needed.",
        }

    stale_files = {item["file"] for item in paths_report.get("stale", [])} if not force else set(HOOK_FILES)
    reconciled_files = []
    total_reconciled = 0
    all_new_commands = []
    reconciled_originals = {}

    for name in HOOK_FILES:
        path = vault / name
        if not path.is_file():
            continue
        if name not in stale_files and not force:
            continue

        try:
            original_bytes = path.read_bytes()
            data = json.loads(original_bytes.decode("utf-8"))
            if not isinstance(data, dict):
                continue
        except (OSError, ValueError, UnicodeError):
            continue

        file_reconciled = 0
        changes = []

        if name == ".kiro/hooks/beyin-v3.json":
            for item in data.get("hooks", []) if isinstance(data.get("hooks"), list) else ():
                if not isinstance(item, dict) or not isinstance(item.get("action"), dict):
                    continue
                event = item.get("trigger")
                old_cmd = item["action"].get("command")
                if event not in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop") or not isinstance(old_cmd, str):
                    continue
                args = _hook_arguments(old_cmd)
                if not any(a.replace("\\", "/").endswith("/beyin_v3_kiro.py") for a in args):
                    continue
                posix, windows = _hook_commands([sys.executable, kiro_script, "--vault", vault, "--state", state,
                                                  "--event", event])
                all_new_commands.extend([posix, windows])
                target_cmd = windows if os.name == "nt" else posix
                if old_cmd != target_cmd or force:
                    changes.append({"event": event, "old_command": old_cmd, "new_command": target_cmd,
                                    "old_summary": " ".join(args[:2]),
                                    "new_summary": f"{sys.executable} {kiro_script}"})
                    item["action"]["command"] = target_cmd
                    item["timeout"] = 25 if os.name == "nt" else 10
                    file_reconciled += 1
        elif name == ".agents/hooks.json":
            managed = data.get("beyin-v3")
            if isinstance(managed, dict):
                for event in ("PreInvocation", "Stop"):
                    if event in managed and isinstance(managed[event], list):
                        posix, windows = _hook_commands([sys.executable, hook_script, "--vault", vault, "--state", state,
                                                         "--harness", "antigravity", "--event", event])
                        all_new_commands.extend([posix, windows])
                        target_cmd = windows if os.name == "nt" else posix
                        timeout = 20 if os.name == "nt" else 5
                        for handler in managed[event]:
                            if isinstance(handler, dict):
                                old_cmd = handler.get("command", "")
                                if not isinstance(old_cmd, str):
                                    continue
                                args = _hook_arguments(old_cmd)
                                if not any(a.replace("\\", "/").endswith("/beyin_v3_hook.py") for a in args):
                                    continue
                                if old_cmd != target_cmd or force:
                                    old_args = _hook_arguments(old_cmd)
                                    changes.append({
                                        "event": event,
                                        "old_command": old_cmd,
                                        "new_command": target_cmd,
                                        "old_summary": " ".join(old_args[:2]) if len(old_args) >= 2 else old_cmd[:60],
                                        "new_summary": f"{sys.executable} {hook_script}",
                                    })
                                    handler["command"] = target_cmd
                                    handler["timeout"] = timeout
                                    file_reconciled += 1
        else:
            harness = "claude" if "claude" in name else "codex"
            hooks_map = data.get("hooks", {})
            if isinstance(hooks_map, dict):
                posix, windows = _hook_commands([sys.executable, hook_script, "--vault", vault, "--state", state,
                                                 "--harness", harness])
                all_new_commands.extend([posix, windows])
                target_cmd = windows if os.name == "nt" else posix

                for event, groups in hooks_map.items():
                    if not isinstance(groups, list):
                        continue
                    timeout = 3 if event == "SessionEnd" else (20 if os.name == "nt" else 5)
                    for group in groups:
                        if not isinstance(group, dict):
                            continue
                        handlers = group.get("hooks", [group]) if "hooks" in group else [group]
                        for handler in handlers:
                            if not isinstance(handler, dict):
                                continue
                            old_cmd = handler.get("command", "")
                            if not isinstance(old_cmd, str):
                                continue
                            args = _hook_arguments(old_cmd)
                            script = next((a for a in args if a.replace("\\", "/").endswith("/beyin_v3_hook.py")), None)
                            if script is None:
                                continue
                            needs_update = (old_cmd != target_cmd or force or
                                            (harness == "codex" and handler.get("commandWindows") != windows))
                            if needs_update:
                                changes.append({
                                    "event": event,
                                    "old_command": old_cmd,
                                    "new_command": target_cmd,
                                    "old_summary": " ".join(args[:2]) if len(args) >= 2 else old_cmd[:60],
                                    "new_summary": f"{sys.executable} {hook_script}",
                                })
                                handler["command"] = target_cmd
                                handler["timeout"] = timeout
                                if harness == "codex":
                                    handler["commandWindows"] = windows
                                file_reconciled += 1

        if file_reconciled > 0:
            if path.read_bytes() != original_bytes:
                raise RuntimeError("Hook configuration changed during reconciliation; preserved")
            backup_path = _backup_hook_file(path, original_bytes)
            _local_hook_file(vault, path)
            if path.read_bytes() != original_bytes:
                raise RuntimeError("Hook configuration changed after backup; preserved")
            reconciled_originals[name] = original_bytes
            _write_hook_json(path, data)
            reconciled_files.append({
                "file": name,
                "backup": str(backup_path.relative_to(vault).as_posix()),
                "reconciled": file_reconciled,
                "changes": changes,
            })
            total_reconciled += file_reconciled

    if runtime_path.is_file() and (runtime_stale or force):
        try:
            original_bytes = runtime_path.read_bytes()
            rdata = json.loads(original_bytes.decode("utf-8"))
            old_state = rdata.get("state", "")
            if old_state != expected_state or force:
                rdata["state"] = expected_state
                target_bytes = (json.dumps(rdata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                if runtime_path.read_bytes() != original_bytes:
                    raise RuntimeError("Runtime configuration changed during reconciliation; preserved")
                backup_path = _backup_hook_file(runtime_path, original_bytes)
                _local_hook_file(vault, runtime_path)
                if runtime_path.read_bytes() != original_bytes:
                    raise RuntimeError("Runtime configuration changed after backup; preserved")
                runtime_path.write_bytes(target_bytes)
                reconciled_originals[".beyin-runtime.json"] = original_bytes
                reconciled_files.append({
                    "file": ".beyin-runtime.json",
                    "backup": str(backup_path.relative_to(vault).as_posix()),
                    "reconciled": 1,
                    "changes": [{
                        "event": "runtime_state",
                        "old_command": str(old_state),
                        "new_command": expected_state,
                        "old_summary": str(old_state),
                        "new_summary": expected_state,
                    }],
                })
                total_reconciled += 1
        except (OSError, ValueError, UnicodeError):
            pass

    for lname, expected_bytes in planned_launchers.items():
        lpath = vault / lname
        if not lpath.is_file():
            continue
        try:
            original_bytes = lpath.read_bytes()
            baseline = launcher_baselines.get(lname)
            if baseline is None or original_bytes.replace(b"\r\n", b"\n") != baseline.replace(b"\r\n", b"\n"):
                continue  # A user-edited launcher remains user-owned, including with --force.
            if original_bytes != expected_bytes or force:
                if lpath.read_bytes() != original_bytes:
                    raise RuntimeError("Launcher changed during reconciliation; preserved")
                backup_path = _backup_hook_file(lpath, original_bytes)
                _local_hook_file(vault, lpath)
                if lpath.read_bytes() != original_bytes:
                    raise RuntimeError("Launcher changed after backup; preserved")
                lpath.write_bytes(expected_bytes)
                reconciled_originals[lname] = original_bytes
                reconciled_files.append({
                    "file": lname,
                    "backup": str(backup_path.relative_to(vault).as_posix()),
                    "reconciled": 1,
                    "changes": [{
                        "event": "launcher",
                        "old_command": "",
                        "new_command": "",
                        "old_summary": lname,
                        "new_summary": lname,
                    }],
                })
                total_reconciled += 1
        except OSError:
            pass

    manifest_path = state / "v3-install.json"
    if manifest_path.is_file() and (all_new_commands or reconciled_files):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            cmds = set(manifest.get("commands", []))
            cmds.update(all_new_commands)
            manifest["commands"] = sorted(cmds)
            files = manifest.get("files", {})
            for rec in reconciled_files:
                name = rec["file"]
                if name in files and isinstance(files[name], dict):
                    file_path = vault / name
                    baseline = base64.b64decode(files[name].get("installed_content") or "")
                    original = reconciled_originals.get(name, b"")
                    # Never adopt user edits as installer-owned content.
                    adopt = False
                    if baseline.replace(b"\r\n", b"\n") == original.replace(b"\r\n", b"\n"):
                        adopt = True
                    elif name == ".beyin-runtime.json":
                        try:
                            orig_json = json.loads(original.decode("utf-8"))
                            base_json = json.loads(baseline.decode("utf-8"))
                            if isinstance(orig_json, dict) and isinstance(base_json, dict) and dict(orig_json, state=None) == dict(base_json, state=None):
                                adopt = True
                        except Exception:
                            pass
                    if file_path.is_file() and adopt:
                        file_bytes = file_path.read_bytes()
                        files[name]["installed_hash"] = hashlib.sha256(file_bytes).hexdigest()
                        files[name]["installed_content"] = base64.b64encode(file_bytes).decode("ascii")
            _write_hook_json(manifest_path, manifest)
        except (OSError, ValueError, UnicodeError):
            pass

    return {
        "status": "reconciled" if total_reconciled > 0 else "noop",
        "checked": paths_report.get("checked", 0),
        "reconciled_count": total_reconciled,
        "files": reconciled_files,
    }


def read_json(filename: str):
    if filename == "-":
        if hasattr(sys.stdin, "reconfigure"):
            try:
                sys.stdin.reconfigure(encoding="utf-8-sig")
            except Exception:
                pass
        raw = sys.stdin.read()
        return json.loads(raw.lstrip("\ufeff").strip() or "{}")
    with Path(filename).open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def load_skills():
    _ensure_scripts_path()
    import beyin_v3_skills
    return beyin_v3_skills


# Hours and minutes are bounded here so every supported Python accepts the same strings
# (datetime.fromisoformat alone also takes 20260924, 2026-W39-4, a zone-less time or, on newer
# versions, 24:00).
RECAP_BOUND = (r"\d{4}-\d{2}-\d{2}"
               r"(T(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2}))?")


def recap_bound(value: str):
    """argparse type of recap --since/--until: a local calendar day or an exact instant.

    YYYY-MM-DD returns a date; the projection compares it with each receipt's local day, the
    day daily/v3 is named by. A full ISO 8601 timestamp must end in Z or a UTC offset and
    returns that instant in UTC. A zone-less time is refused instead of guessed.
    """
    import re
    from datetime import date, datetime, timezone
    try:
        match = re.fullmatch(RECAP_BOUND, value, re.ASCII)
        if not match:
            raise ValueError(value)
        if match.group(1) is None:
            return date.fromisoformat(value)
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        stamp.astimezone()  # from/through report its local day; Windows cannot place every instant
        return stamp
    except (ValueError, OverflowError, OSError):
        raise argparse.ArgumentTypeError(
            "invalid date format: %r (use YYYY-MM-DD or an ISO 8601 timestamp with Z or a UTC offset, "
            "such as 2026-09-24T18:30:00+03:00)" % value) from None


def recap_bounds_inverted(since, until):
    """True when --since lies after --until. Two instants compare directly; with a bare date
    on either side both are compared as local calendar days, the unit a bare date selects."""
    from datetime import datetime
    if isinstance(since, datetime) and isinstance(until, datetime):
        return since > until
    first, last = (bound.astimezone().date() if isinstance(bound, datetime) else bound for bound in (since, until))
    return first > last


class ArgumentParsingError(Exception):
    pass


def parser(note_namespace=None, is_json=False):
    class ResultParser(argparse.ArgumentParser):
        def error(self, message):
            # argparse assigns the root command before parsing the subparser.
            # The shared namespace also covers unknown options reported by root.
            if getattr(note_namespace, 'command', None) in NOTE_JSON_COMMANDS:
                raise NoteUsageError(message)
            if is_json:
                raise ArgumentParsingError(message)
            super().error(message)

    root = ResultParser(description=__doc__)
    root.add_argument("--vault", required=True, type=Path, help="Existing vault directory")
    root.add_argument("--state", type=Path, help="Local state directory outside the vault")
    root.add_argument("--json", action="store_true", help="Format output as JSON")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize local state; installs no hooks or services")
    sync_parser = sub.add_parser("sync", help="Reconcile Markdown sources into local state")
    sync_parser.add_argument("--full", action="store_true",
                             help="Read every source instead of trusting unchanged file signatures")
    sync_parser.add_argument("--recover-views", action="store_true",
                             help="Archive conflicting receipt views and regenerate from immutable receipts")
    sub.add_parser("recover-views", help="Archive conflicting receipt views and regenerate from immutable receipts")
    sub.add_parser("skill-sync", help="Reconcile project-local shared skills")
    sub.add_parser("doctor", help="Read local hook health and pending metadata counts")
    scheduled = sub.add_parser("scheduled-check", help="Sync then doctor for an OS scheduler you set up; no model call, no service")
    scheduled.add_argument("--notify", action="store_true",
                           help="Opt-in OS notification when the result changes; issue kinds and counts only")
    hook_rec = sub.add_parser("hook-reconcile", help="Reconcile stale hook paths with backup")
    hook_rec.add_argument("--force", action="store_true", help="Reconcile hook paths even if not reported stale")
    recap = sub.add_parser("recap", help="Read recent source-linked outcomes without a model call")
    recap.add_argument("--days", type=int, help="Calendar days in UTC, including today (1..366, default 7); "
                                                "not allowed with --since/--until")
    recap.add_argument("--limit", type=int, default=20, help="Maximum recent receipts to return (1..100)")
    recap.add_argument("--since", type=recap_bound, metavar="DATE",
                       help="Explicit range start: YYYY-MM-DD (local calendar day, as daily/v3 is named) or an "
                            "ISO 8601 timestamp with Z or a UTC offset")
    recap.add_argument("--until", type=recap_bound, metavar="DATE",
                       help="Explicit range end, inclusive: YYYY-MM-DD covers that whole local day; without "
                            "--since there is no lower bound")
    recap.set_defaults(usage_error=recap.error)  # cross-flag checks in main() report with recap's own usage
    triage = sub.add_parser("triage", help="Vault digestion report: inbox items, stale tasks, and weekly knowledge summary")
    triage.add_argument("--stale-days", type=int, default=14, help="Days of inactivity before an open task is considered stale (default: 14)")
    triage.add_argument("--digest-days", type=int, default=7, help="Days to look back for knowledge notes and outcomes (default: 7)")
    audit_parser = sub.add_parser("audit", help="Advisory memory auditor: detect near-duplicates, staleness, and contradictions")
    audit_parser.add_argument("target", nargs="?", default=None, help="Specific markdown note to audit against vault memory (optional)")
    audit_parser.add_argument("--threshold", type=float, default=0.60, help="Lexical overlap threshold 0.0..1.0 (default: 0.60)")
    audit_parser.add_argument("--min-shared", type=int, default=5, help="Minimum shared topical terms (default: 5)")
    audit_parser.add_argument("--limit", type=int, default=20, help="Maximum findings to return (default: 20)")
    settings = sub.add_parser("preferences", help="Control automatic local checks and injected context")
    settings.add_argument("--profile", choices=("normal", "economical", "manual"))
    settings.add_argument("--auto-sync", choices=("on", "off"))
    settings.add_argument("--interval-minutes", type=int)
    settings.add_argument("--context-mode", choices=("turn", "session", "off"))
    settings.add_argument("--context-chars", type=int)
    settings.add_argument("--companion-context-chars", type=int,
                          help="Opening context budget (SessionStart, continuity questions), 1000..24000; 0 uses --context-chars")
    settings.add_argument("--secret-filter", choices=("on", "off"))
    settings.add_argument("--daily-layout", choices=("flat", "v3"),
                          help="Vault-wide daily receipt view layout (default v3); applied by the next sync")
    settings.add_argument("--update-notifications", choices=("on", "off"))
    settings.add_argument("--last-session-chars", type=int, help="Hygiene limit for Last-Session.md; 0 turns it off")
    settings.add_argument("--threads-chars", type=int, help="Hygiene limit for Threads.md; 0 turns it off")
    settings.add_argument("--exclude-component", action="append", default=[], metavar="COMPONENT",
                          help="Disable/exclude a managed component or skill (repeatable)")
    settings.add_argument("--include-component", action="append", default=[], metavar="COMPONENT",
                          help="Re-enable a previously excluded component (repeatable)")
    settings.add_argument("--project-context", choices=("on", "off"),
                          help="Inject scoped project receipt and due tasks at SessionStart")
    settings.add_argument("--word-cap-warning", choices=("on", "off"),
                          help="Opt-in PostToolUse split signal for a long note (Claude and Codex only)")
    settings.add_argument("--max-words", type=int, help="Word cap for --word-cap-warning (10..100000, default 500)")
    settings.add_argument("--folder-questions", choices=("on", "off"),
                          help="Opt-in SessionStart question for a long-quiet top-level folder")
    settings.add_argument("--promotion", choices=("on", "off"),
                          help="Opt-in touch log for the doctor's hot/cold folder report")
    settings.add_argument("--inbox-report", choices=("on", "off"),
                          help="Opt-in doctor report of notes waiting in top-level inbox folders")
    settings.add_argument("--inbox-max-items", type=int, help="Inbox report threshold in notes (1..100000, default 10)")
    settings.add_argument("--inbox-max-days", type=int, help="Inbox report threshold in days (1..3650, default 7)")
    settings.add_argument("--inbox-folder", action="append",
                          help="Top-level inbox folder name for the report (repeat for several; replaces the "
                               "generic name detection; an empty value returns to it)")
    settings.add_argument("--index-exclude-dir", action="append", default=[], metavar="NAME",
                          help="Keep directories with this bare name out of the index, at any depth (repeatable); applied by the next sync")
    settings.add_argument("--index-include-dir", action="append", default=[], metavar="NAME",
                          help="Remove a name from the index exclusion list (repeatable)")
    settings.add_argument("--parallel-sessions", choices=("on", "off"),
                          help="Opt-in one-line notice when another session is open on this vault")
    settings.add_argument("--context-dedupe", choices=("on", "off"),
                          help="Prevent re-injecting already delivered records within the same session (#363)")
    settings.add_argument("--hermes-attended-platform", action="append", default=[], metavar="PLATFORM",
                          help="Treat platform as attended/interactive in Hermes (e.g. slack); repeatable")
    settings.add_argument("--hermes-unattended-platform", action="append", default=[], metavar="PLATFORM",
                          help="Treat platform as unattended in Hermes (e.g. custom bot); repeatable")
    settings.add_argument("--hermes-reset-platforms", action="store_true",
                          help="Reset Hermes platform overrides to defaults")
    compact = sub.add_parser("companion-compact", help="Move older Last-Session/Threads entries verbatim into a private archive; deletes nothing")
    compact.add_argument("--dry-run", action="store_true", help="Report what would move without writing")
    trace = sub.add_parser("chat-trace", help="Opt-in short conversation checkpoints; never saves transcripts")
    trace.add_argument("action", choices=("status", "on", "off", "write"))
    trace.add_argument("--harness", choices=("claude", "codex"))
    trace.add_argument("--session", help="The 24-character Receipt session value shown by the hook")
    trace_text = trace.add_mutually_exclusive_group()
    trace_text.add_argument("--text", help="Explicit short summary; dated paragraph must fit 450 characters")
    trace_text.add_argument("--file", help="UTF-8 summary file, or - for stdin")
    skill = sub.add_parser("skill-import", help="Import one explicitly chosen skill directory")
    skill.add_argument("--source", type=Path, required=True)
    skill.add_argument("--name")
    ingest = sub.add_parser("ingest", help="Ingest one JSON record or a JSON list")
    ingest.add_argument("--file", default="-", help="JSON input path, or - for stdin")
    note = sub.add_parser("note-create", help="Create a semantic Markdown source without overwriting")
    note.add_argument("--file", default="-", help="JSON {source, text, metadata}")
    note_read = sub.add_parser("note-read", help="Read a human Markdown source and its byte SHA-256")
    note_read.add_argument("source")
    note_edit = sub.add_parser("note-edit", help="Edit a human Markdown source with an expected SHA-256")
    note_edit.add_argument("--file", default="-", help="JSON {source, expected_sha256, edits} or new content; - for stdin")
    task = sub.add_parser("task-create", help="Create and verify an explicit new task")
    task.add_argument("--file", default="-", help="JSON {source: tasks/name.md, text, metadata: {id,status,owner}}")
    context = sub.add_parser("context", help="Retrieve source-backed shared context")
    context.add_argument("query", nargs="?", help="Query; alternatively use --file")
    context.add_argument("--file", help="JSON retrieval arguments, or - for stdin")
    context.add_argument("--harness", choices=("codex", "claude", "antigravity", "hermes", "opencode", "omp", "kiro"), default="codex")
    context.add_argument("--project")
    context.add_argument("--audience", choices=("internal", "public"), default="internal")
    context.add_argument("--status", action="append", dest="statuses")
    context.add_argument("--limit", type=int, default=5)
    context.add_argument("--budget-chars", type=int, default=8000)
    context.add_argument("--no-sync", action="store_true", help="Read an existing index without writing to the vault or runtime")
    context.add_argument("--jev", action="store_true", help="Explicit optional remote advisor; requires state/jev.json and --project")
    review = sub.add_parser("jev-review", help="Advisory source review; never saves or approves a candidate")
    review.add_argument("--file", required=True, help="JSON proposal (maximum 24,000 characters)")
    review.add_argument("--project", required=True)
    memory = sub.add_parser("jev-memory", help="Optional typed memory triage; never writes or approves")
    memory.add_argument("--file", required=True, help="Source-backed JSON proposal, optional prior_record_ids (max 4)")
    memory.add_argument("--project", required=True)
    jev = sub.add_parser("jev", help="Read or set the optional remote advisor; never accepts a key")
    jev.add_argument("mode", choices=("status", "off", "shadow", "on"))
    jev.add_argument("--enable", action="append", choices=JEV_FEATURES, default=[])
    jev.add_argument("--disable", action="append", choices=JEV_FEATURES, default=[])
    jev.add_argument("--provider", choices=JEV_PROVIDERS, help="typesafe (default) or laya, a local laya-serve (shadow only)")
    jev.add_argument("--base-url", dest="base_url", help="laya only: loopback URL, default http://127.0.0.1:8765")
    jev.add_argument("--model", choices=LAYA_MODELS, help="laya only: pinned checkpoint, default multilingual")
    jev.add_argument("--check", action="store_true", help="with status: one GET /health to the local Laya server")
    answer = sub.add_parser("jev-answer", help="Advisory answer claim verification against exact source quotes")
    answer.add_argument("--file", required=True, help="JSON list of claims (maximum 32,000 characters)")
    answer.add_argument("--project", required=True)
    receipt = sub.add_parser("receipt", help="Submit an idempotent source-linked receipt")
    receipt.add_argument("--file", help="JSON input path, or - for stdin (default without receipt flags)")
    receipt.add_argument("--harness", choices=("codex", "claude", "antigravity", "hermes", "opencode", "omp", "kiro"), default="codex")
    receipt.add_argument("--event-id", help="Required receipt event identifier in flag mode")
    receipt.add_argument("--summary", help="Receipt summary text, preserving literal newlines")
    receipt.add_argument("--summary-file", type=Path, metavar="PATH", help="Read the receipt summary from a UTF-8 file")
    receipt.add_argument("--ref", action="append", help="Vault-relative source path; repeat for multiple refs")
    receipt.add_argument("--link", action="append",
                         help="External result as an http(s) URL (pull request, published page); repeatable, never a vault source")
    receipt.add_argument("--session", help="Optional session identifier")
    update = sub.add_parser("task-update", help="Update task with expected revision")
    update.add_argument("--file", default="-", help="JSON {id, expected_revision, changes}")
    history = sub.add_parser("history", help="Read ordered revision snapshots for a record")
    history.add_argument("record_id")
    pscope = sub.add_parser("project-scope", help="Manage folder project scopes in .beyin-projects.json")
    pscope.add_argument("action", choices=["list", "add", "remove", "prune"], nargs="?", default="list",
                        help="Action to perform: list, add, remove, prune")
    pscope.add_argument("--folder", help="Vault-relative folder path")
    pscope.add_argument("--project", help="Project identifier for the folder")
    pscope.add_argument("--shared-unscoped", choices=["on", "off"],
                        help="Toggle whether unassigned notes are visible in project scope")
    ingest_server = sub.add_parser("ingest-server", help="Local-first HTTP ingest server for mobile captures")
    ingest_server.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default 127.0.0.1)")
    ingest_server.add_argument("--port", type=int, default=8484, help="Port to listen on (default 8484)")
    ingest_server.add_argument("--token", help="Optional Bearer token for authentication")
    bridge = sub.add_parser("bridge", help="Manage global lifecycle bridge configuration")
    bridge.add_argument("--harness", choices=("claude", "codex", "antigravity"), required=True)
    bridge.add_argument("--project-root", type=Path, action="append", help="Project root to bridge (repeatable)")
    bridge.add_argument("--install", action="store_true", help="Install global bridge hooks")
    bridge.add_argument("--uninstall", action="store_true", help="Uninstall global bridge hooks")
    bridge.add_argument("--print-config", action="store_true", help="Print global hook JSON without installing")
    bridge.add_argument("--event", choices=("SessionStart", "Stop", "PreCompact", "SessionEnd"), action="append")
    bridge.add_argument("--context-chars", type=int, default=1500)
    learn = sub.add_parser("learn", help="Stage or inspect learning candidates before distillation (RFC #368)")
    learn.add_argument("--candidate", help="Candidate learning content text")
    learn.add_argument("--source", default="", help="Source file or note path linking to this learning")
    learn.add_argument("--scope", default=None, help="Scope of the learning: global or project name")
    learn.add_argument("--session", default="", help="Session ID associated with candidate")
    learn.add_argument("--list", action="store_true", help="List active candidate learnings")
    return root


def main(argv=None, return_result=False):
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8-sig")
    args_list = list(sys.argv[1:] if argv is None else argv)
    args = argparse.Namespace()
    argument_parser = parser(note_namespace=args, is_json="--json" in args_list)
    try:
        args = argument_parser.parse_args(args_list, namespace=args)
        receipt_flags = args.command == "receipt" and any(
            getattr(args, name) is not None for name in ("event_id", "summary", "summary_file", "ref", "link", "session"))
        if receipt_flags:
            for stream in (sys.stdout, sys.stderr):
                if hasattr(stream, "reconfigure"):
                    stream.reconfigure(encoding="utf-8")
            if args.file is not None:
                argument_parser.error("--file cannot be combined with receipt flags")
            if args.event_id is None:
                argument_parser.error("--event-id is required in flag mode")
            if args.summary is not None and args.summary_file is not None:
                argument_parser.error("use exactly one of --summary and --summary-file")
            if args.summary is None and args.summary_file is None:
                argument_parser.error("--summary or --summary-file is required in flag mode")
            if not args.ref:
                argument_parser.error("--ref is required in flag mode (at least one)")
        if args.command == "recap":
            # Usage errors, raised before any state is opened or synced.
            if args.days is not None and (args.since is not None or args.until is not None):
                args.usage_error("argument --days: not allowed with argument --since/--until")
            if args.since is not None and args.until is not None and recap_bounds_inverted(args.since, args.until):
                args.usage_error("argument --since: must not be after --until")
    except ArgumentParsingError as exc:
        error = {"error": "SystemExit", "message": str(exc)}
        if return_result:
            return error, 2
        print(json.dumps(error, ensure_ascii=True), file=sys.stderr)
        return 2
    except NoteUsageError as exc:
        result = {'status': 'usage_error', 'error': type(exc).__name__, 'message': str(exc)}
        if return_result:
            return result, 64
        print(json.dumps(result, ensure_ascii=True))
        return 64
    try:
        vault = args.vault.expanduser().resolve()
        if not vault.is_dir():
            raise ValueError("--vault must be an existing directory")
        state = (args.state.expanduser() if args.state else default_state(vault)).resolve()
        if state == vault or vault in state.parents:
            raise ValueError("--state must be outside the vault")
        read_only_context = args.command == "context" and args.no_sync
        if read_only_context:
            # The source CLI can run without the installed entry point, which
            # already disables bytecode. Importing the engine must not write.
            sys.dont_write_bytecode = True
        if read_only_context and args.jev:
            raise ValueError("context --no-sync cannot be combined with --jev")
        # The advisor switch reads and writes one small file; it needs no index or sync engine.
        engine = load_engine() if args.command not in ("jev", "project-scope", "ingest-server", "bridge", "chat-trace") else None
        store = engine.MemoryStore(state, vault, read_only=read_only_context) if engine else None
        sync = load_sync()(vault, state) if args.command in ("sync", "recover-views", "recap", "receipt", "task-update", "note-create", "note-read", "note-edit", "task-create", "context", "jev-review", "jev-answer", "jev-memory", "history") and not read_only_context else None
        if args.command == "bridge":
            if sum([bool(args.print_config), bool(args.install), bool(args.uninstall)]) > 1:
                argument_parser.error("--print-config, --install, and --uninstall are mutually exclusive")
            _ensure_scripts_path()
            import beyin_v3_bridge as bridge_mod
            events = list(dict.fromkeys(args.event or bridge_mod.EVENTS))
            if args.harness == "antigravity":
                events = [e for e in events if e in bridge_mod.ANTIGRAVITY.values()]
                if not events:
                    argument_parser.error("Antigravity offers only SessionStart and Stop")
            if args.uninstall:
                result = bridge_mod.uninstall_bridge(args.harness)
            elif args.install:
                if not args.project_root:
                    argument_parser.error("--project-root is required with --install")
                roots = [p.expanduser().resolve() for p in args.project_root]
                result = bridge_mod.install_bridge(vault, state, args.harness, roots, events, args.context_chars)
            elif args.print_config:
                if not args.project_root:
                    argument_parser.error("--project-root is required with --print-config")
                roots = [p.expanduser().resolve() for p in args.project_root]
                bridge_args = ["--vault", str(vault), "--state", str(state), "--harness", args.harness,
                               "--context-chars", str(args.context_chars), "--print-config"]
                for r in roots: bridge_args.extend(["--project-root", str(r)])
                code = bridge_mod.main(bridge_args)
                if return_result:
                    return None, code
                return code
            else:
                argument_parser.error("specify one of --install, --uninstall, or --print-config")
            if return_result:
                return result, 0
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "ingest-server":
            _ensure_scripts_path()
            import beyin_v3_ingest_server
            beyin_v3_ingest_server.run_server(vault, host=args.host, port=args.port, token=args.token)
            return 0
        elif args.command == "init":
            result = {"initialized": True, "state": str(state), "network": False,
                      "hooks_installed": False, "optional_provider": None}
        elif args.command == "preferences":
            _ensure_scripts_path()
            import beyin_v3_preferences as preferences
            daily_layout = preferences.read_daily_layout(vault)  # validate before saving any setting
            import beyin_v3_companion as companion
            limits = {name: value for name, value in (('Last-Session.md', args.last_session_chars),
                                                      ('Threads.md', args.threads_chars)) if value is not None}
            import beyin_v3_hygiene as hygiene
            hygiene_changes = {key: getattr(args, key) == 'on' for key in ('word_cap_warning', 'folder_questions', 'promotion')
                               if getattr(args, key) is not None}
            if args.max_words is not None:
                hygiene_changes['max_words'] = args.max_words
            inbox_changes = {key: value for key, value in (
                ('enabled', None if args.inbox_report is None else args.inbox_report == 'on'),
                ('max_items', args.inbox_max_items), ('max_days', args.inbox_max_days),
                ('folders', None if args.inbox_folder is None else [name for name in args.inbox_folder if name]))
                if value is not None}
            if inbox_changes:  # validated before anything is saved
                current_inbox, inbox_valid = hygiene.read_inbox_settings(state)
                if not inbox_valid:
                    raise ValueError('inbox-report.json in the runtime state is invalid; fix or remove it first')
                hygiene.check_inbox_settings(dict(current_inbox, **inbox_changes))
            import beyin_v3_index as index_settings
            index_changes = bool(args.index_exclude_dir or args.index_include_dir)
            if index_changes:  # validated before anything is saved
                index_settings.plan(state, vault, args.index_exclude_dir, args.index_include_dir)
            if hygiene_changes:  # validated before anything is saved, like the companion limits
                current_hygiene, hygiene_valid = hygiene.read_settings(state)
                if not hygiene_valid:
                    raise ValueError('hygiene.json in the runtime state is invalid; fix or remove it first')
                hygiene.check_settings(dict(current_hygiene, **hygiene_changes))
            if args.companion_context_chars is not None:  # validated before anything is saved
                current_context, context_valid = companion.read_context(state)
                if not context_valid:
                    raise ValueError(companion.CONTEXT_FILE + ' in the runtime state is invalid; fix or remove it first')
                companion.check_context(dict(current_context, context_chars=args.companion_context_chars))
            if limits:  # validate before anything is saved, so a bad value changes nothing
                current_limits, limits_valid = companion.read_limits(state)
                if not limits_valid:
                    raise ValueError('companion-limits.json in the runtime state is invalid; fix or remove it first')
                companion.check_limits(dict(current_limits, **limits))
            changes = {key: getattr(args, key) for key in ('interval_minutes', 'context_mode', 'context_chars') if getattr(args, key) is not None}
            if args.auto_sync is not None:
                changes['auto_sync'] = args.auto_sync == 'on'
            if args.secret_filter is not None:
                changes['secret_filter'] = args.secret_filter == 'on'
            import beyin_v3_exclusions as exclusions
            exclusion_notice = None
            if args.exclude_component or args.include_component:
                exclude_args = [name.replace('\\', '/') for name in args.exclude_component]
                include_args = [name.replace('\\', '/') for name in args.include_component]
                exclusions.validate_exclusions(exclude_args + include_args)
                current_excluded = set(exclusions.read_exclusions(vault))
                current_excluded.update(exclude_args)
                current_excluded.difference_update(include_args)
                result_excluded = exclusions.save_exclusions(vault, current_excluded)
                exclusion_notice = 'Haric tutma tercihleri kaydedildi; bir sonraki kurulum ya da guncellemede uygulanir.'
            else:
                # A hand-edited typo must not block unrelated preference changes.
                try:
                    result_excluded = exclusions.read_exclusions(vault)
                except ValueError as exc:
                    result_excluded, exclusion_notice = [], 'Haric tutma dosyasi gecersiz: ' + str(exc)
            settings = preferences.save(vault, changes, args.profile) if changes or args.profile else preferences.read(vault)
            result = {'status': 'saved' if changes or args.profile or (args.exclude_component or args.include_component) else 'current',
                      'preferences': settings, 'excluded_components': result_excluded,
                      'model_calls': False, 'timer_installed': False}
            result['daily_layout'] = daily_layout
            if args.daily_layout is not None:
                result['daily_layout'] = preferences.save_daily_layout(vault, args.daily_layout)
                result['status'] = 'saved'
            if exclusion_notice:
                result['exclusion_notice'] = exclusion_notice
            # Machine-local like update notifications: rollback-safe, outside the vault schema.
            result['companion_limits'] = companion.save_limits(state, limits) if limits else companion.read_limits(state)[0]
            if limits:
                result['status'] = 'saved'
            # Machine-local like the limits (#140): a larger opening budget must survive a rollback.
            if args.companion_context_chars is not None:
                result['companion_context'] = companion.save_context(state, args.companion_context_chars)
                result['status'] = 'saved'
            else:
                result['companion_context'], context_valid = companion.read_context(state)
                if not context_valid:
                    result['companion_context_notice'] = companion.CONTEXT_FILE + ' gecersiz; oturum basi baglami context_chars ile sinirli.'
            widest = max(result['companion_context']['context_chars'], settings['context_chars'])
            if companion.client_budget('claude', widest) < widest:  # #175: say so instead of silently using less
                result['client_context_notice'] = (f"Claude Code ve Codex oturumlarinda otomatik baglam {companion.client_budget('claude', widest)} "
                                                   'karakterde tutulur: Claude Code 10.000 karakteri, Codex yaklasik 10.000 bayti '
                                                   'asan hook baglamini dosyaya tasiyip modele yalniz bir onizleme gosterir.')
            import beyin_v3_releases as releases
            result['update_notifications'] = releases.preferences(state, None if args.update_notifications is None else args.update_notifications == 'on')
            if args.update_notifications is not None:
                result['status'] = 'saved'
            from beyin_v3_bridge import read_project_context, save_project_context
            if args.project_context is not None:
                save_project_context(state, args.project_context == 'on')
                result['status'] = 'saved'
            result['project_context'] = 'on' if read_project_context(state) else 'off'
            # Machine-local and rollback-safe like companion limits: never a .beyin-preferences.json key.
            if hygiene_changes:
                result['hygiene'] = hygiene.save_settings(state, hygiene_changes)
                result['status'] = 'saved'
            else:
                result['hygiene'], hygiene_valid = hygiene.read_settings(state)
                if not hygiene_valid:
                    result['hygiene_notice'] = 'hygiene.json gecersiz; tum hijyen sinyalleri kapali sayiliyor.'
            if inbox_changes:
                result['inbox_report'] = hygiene.save_inbox_settings(state, inbox_changes)
                result['status'] = 'saved'
            else:
                result['inbox_report'], inbox_valid = hygiene.read_inbox_settings(state)
                if not inbox_valid:
                    result['inbox_report_notice'] = 'inbox-report.json gecersiz; gelen kutusu raporu kapali sayiliyor.'
            # Machine-local index setting (state/index.json); a release before it ignores the file.
            if index_changes:
                result['index_exclusions'] = {'excluded_dirs': index_settings.save(
                    state, vault, args.index_exclude_dir, args.index_include_dir)}
                result['index_exclusions_notice'] = 'Klasor haric tutma listesi kaydedildi; bir sonraki sync ile uygulanir.'
                result['status'] = 'saved'
            else:
                excluded_names, index_error, index_present = index_settings.read(state, vault)
                if index_present:  # a vault that never set this keeps its exact output
                    result['index_exclusions'] = {'excluded_dirs': excluded_names}
                if index_error is not None:
                    result['index_exclusions']['error'] = index_error
                    result['index_exclusions_notice'] = ('index.json gecersiz; klasor haric tutma listesi uygulanmiyor, '
                                                         'her sey indeksleniyor.')
            # Machine-local (#170): an older release would reject a new .beyin-preferences.json key.
            import beyin_v3_parallel as parallel
            if args.parallel_sessions is not None:
                parallel.save_settings(state, args.parallel_sessions == 'on')
                result['status'] = 'saved'
            parallel_on, parallel_valid = parallel.read_settings(state)
            result['parallel_sessions'] = 'on' if parallel_on else 'off'
            if not parallel_valid:
                result['parallel_sessions_notice'] = 'parallel-sessions.json gecersiz; paralel oturum bildirimi kapali sayiliyor.'
            # Machine-local context deduplication (#363)
            import beyin_v3_dedupe as dedupe
            if args.context_dedupe is not None:
                dedupe.save_settings(state, args.context_dedupe == 'on')
                result['status'] = 'saved'
            dedupe_on, dedupe_valid = dedupe.read_settings(state)
            result['context_dedupe'] = 'on' if dedupe_on else 'off'
            if not dedupe_valid:
                result['context_dedupe_notice'] = 'context-dedupe.json gecersiz; oturum ici tekrar onleme kapali sayiliyor.'
            # Machine-local Hermes platform settings (state/hermes-platforms.json, #350)
            try:
                import beyin_v3_hermes as hermes_settings
                if args.hermes_attended_platform or args.hermes_unattended_platform or args.hermes_reset_platforms:
                    result['hermes_platforms'] = hermes_settings.save_platforms(
                        state,
                        attended=args.hermes_attended_platform,
                        unattended=args.hermes_unattended_platform,
                        reset=args.hermes_reset_platforms
                    )
                    result['status'] = 'saved'
                else:
                    h_conf, h_valid = hermes_settings.read_platforms(state, vault)
                    effective = sorted(hermes_settings.resolve_unattended_platforms(vault, state))
                    result['hermes_platforms'] = {**h_conf, 'effective_unattended': effective}
                    if not h_valid:
                        result['hermes_platforms_notice'] = 'hermes-platforms.json gecersiz; varsayilan platformlar kullaniliyor.'
            except ImportError:
                if args.hermes_attended_platform or args.hermes_unattended_platform or args.hermes_reset_platforms:
                    raise ValueError("Hermes platform settings module is unavailable; reinstall the vault")
        elif args.command == "jev":
            laya = {key: value for key, value in (("base_url", args.base_url), ("model", args.model)) if value is not None}
            if args.mode == "status":
                if args.enable or args.disable or args.provider or laya:
                    raise ValueError("jev status reads only; use jev off/shadow/on with --enable/--disable/--provider")
                result = jev_client().status(state)
                if args.check:
                    result["server"] = jev_client().probe(state)
                result["context_mode"] = jev_context_mode(vault)
                result = jev_advice(result)
            else:
                if args.check:
                    raise ValueError("--check works only with jev status")
                result = jev_client().set_mode(state, args.mode, enable=args.enable, disable=args.disable,
                                                provider=args.provider, laya=laya or None)
                result["context_mode"] = jev_context_mode(vault)
                result = jev_advice(result)
                result["changed"] = True
        elif args.command == "hook-reconcile":
            result = hook_reconcile(vault, state, force=getattr(args, 'force', False))
        elif args.command == "doctor":
            _ensure_scripts_path()
            from beyin_v3_hook_done import summary as hook_done_summary
            done = hook_done_summary(state, ('codex', 'claude', 'antigravity', 'hermes', 'opencode', 'omp', 'kiro'))
            result = {"pending_events": len(list((state / "hook-queue").glob("*.json"))),
                      **done}
            for filename in ("hook-health.json", "hook-error.json", "receipt-gaps.json"):
                path = state / filename
                result[filename] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            result['legacy_external_schedules'] = 'not_inspected; review custom OS/compiler schedules before migration'
            try:  # information only: the last scheduled-check run, if the user set one up (#267)
                _ensure_scripts_path()
                import beyin_v3_scheduled as scheduled
                result['scheduled_check'] = scheduled.doctor(state)
            except Exception as exc:
                result['scheduled_check'] = {'status': 'unavailable', 'error': type(exc).__name__}
            try:  # information only: a stale-path report must never hide the rest of doctor
                result['hook_paths'] = hook_paths(vault)
            except Exception as exc:
                result['hook_paths'] = {'status': 'unavailable', 'error': type(exc).__name__}
            # Information only; a split or container-bound state root never raises the status.
            try:
                result['state_location'] = state_location(vault, state)
            except Exception as exc:  # a location report must never hide the rest of doctor
                result['state_location'] = {'status': 'unavailable', 'error': type(exc).__name__, 'warnings': []}
            manifest = state / 'v3-install.json'
            manifest_data = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else {}
            result['kept_legacy_runners'] = manifest_data.get('kept_legacy', [])
            result['excluded_components'] = manifest_data.get('excluded_components', [])
            load_sync()
            import beyin_v3_exclusions as exclusions
            try:
                configured_excluded = exclusions.read_exclusions(vault)
            except ValueError as exc:
                # doctor reports the broken file instead of failing; update/install stay strict.
                configured_excluded = result['excluded_components']
                result['exclusions_error'] = str(exc)
            if set(configured_excluded) != set(result['excluded_components']):
                result['pending_exclusions'] = sorted(set(configured_excluded) ^ set(result['excluded_components']))
                result['exclusions_pending'] = True
            import beyin_v3_preferences as preferences
            result['preferences'] = preferences.read(vault)
            import beyin_v3_releases as releases
            result['updates'] = releases.status(vault, state)
            from beyin_v3_secrets import health as secret_filter_health
            result['secret_filter'] = secret_filter_health(state)
            result['secrets_redacted'] = result['secret_filter']['total']
            try:
                import beyin_v3_companion as companion
                result['companion_hygiene'] = companion.hygiene(vault, state)
            except Exception as exc:  # a size report must never hide the rest of doctor
                result['companion_hygiene'] = {'status': 'unavailable', 'error': type(exc).__name__}
            # Information only (#374): how much of Kurallar.md the Claude Code opening shows whole.
            # The hook's own render on a read-only store; never raises the doctor status.
            try:
                import beyin_v3_companion as companion
                prefs = result['preferences']
                if prefs['context_mode'] == 'off':
                    result['companion_rules_opening'] = {'status': 'context_off'}
                else:
                    from beyin_v3_hook import receipt_context
                    budget = companion.client_budget('claude', companion.opening_budget(state, prefs['context_chars']))
                    rules = companion.rules_opening(engine.MemoryStore(state, vault, read_only=True), budget,
                                                    receipt_context(vault, state))
                    result['companion_rules_opening'] = dict(
                        rules or {}, status='clipped' if rules and rules['clipped'] else 'ok' if rules else 'no_rules',
                        budget=budget, harness='claude')
            except Exception as exc:  # never hides the rest of doctor
                result['companion_rules_opening'] = {'status': 'unavailable', 'error': type(exc).__name__}
            try:
                import beyin_v3_references as references
                result['instruction_references'] = references.check(vault)
            except Exception as exc:  # information only; never hides the rest of doctor
                result['instruction_references'] = {'status': 'unavailable', 'error': type(exc).__name__}
            result['jev'] = jev_status(state)
            if result['jev'].get('automatic_model_calls') and result['preferences'].get('context_mode') != 'turn':
                result['jev']['context_mode'] = result['preferences'].get('context_mode')
                result['jev'] = jev_advice(result['jev'])
            result['automatic_model_calls'] = result['jev'].get('automatic_model_calls', False)
            health = result['hook-health.json'] or {}
            gaps_info = result['receipt-gaps.json']
            result['potential_missing_receipts'] = gaps_info.get('potential_missing_receipts') if gaps_info is not None else None
            if gaps_info is not None:
                from beyin_v3_projections import receipt_coverage
                with store._connect() as db:
                    result['receipt_coverage'] = receipt_coverage(db, now=time.time())
            else:
                result['receipt_coverage'] = None
            from beyin_v3_projections import knowledge_freshness, check_instruction_conflicts
            try:  # a recency report must never hide the rest of doctor, conflicts included
                with store._connect() as db:
                    result['knowledge_freshness'] = knowledge_freshness(vault, db, now=time.time())
            except Exception as exc:
                result['knowledge_freshness'] = {'status': 'unavailable', 'error': type(exc).__name__}
            result['instruction_conflicts'] = check_instruction_conflicts(vault)
            result['skill_conflicts'] = health.get('sync', {}).get('skill_conflicts', [])
            # Entries beside the skills that this vault never owned. Information only.
            result['skill_unmanaged'] = health.get('sync', {}).get('skill_unmanaged', [])
            from beyin_v3_projections import is_receipt_view_conflict
            view_sync = health.get('sync', {})
            result['receipt_view_conflicts'] = [c for c in view_sync.get('conflicts', []) + view_sync.get('warnings', [])
                                                if is_receipt_view_conflict(c)]
            try:
                result['task_completion'] = load_sync().reader(store).completion_health()
            except Exception as exc:
                result['task_completion'] = {
                    'strict_issue_count': 0, 'strict_issues': [], 'legacy_done_count': 0,
                    'legacy_done': [], 'truncated': False,
                    'error': (type(exc).__name__ + ': ' + str(exc))[:240],
                }
            # A rejection on a plain note leaves the claim in current context; sync stays healthy.
            # Notes that link to a rejected inference are listed for review only and never raise
            # the doctor status: citing a rejected claim can be legitimate (explaining why it fell).
            try:
                result['validity'] = load_sync().reader(store).validity_health()
            except Exception as exc:
                result['validity'] = {'ignored_rejection_count': 0, 'ignored_rejections': [], 'truncated': False,
                                      'rejected_dependent_count': 0, 'rejected_dependents': [],
                                      'ambiguous_rejected_link_count': 0, 'ambiguous_rejected_links': [],
                                      'error': (type(exc).__name__ + ': ' + str(exc))[:240]}
            # Information only: a supersedes link that retires nothing keeps the old note in
            # context, as before #206; it never raises the doctor status.
            try:
                result['supersedes'] = load_sync().reader(store).supersedes_health()
            except Exception as exc:
                result['supersedes'] = {'status': 'unavailable', 'error': type(exc).__name__}
            # Information only: a note's own review_at date has come; never raises the doctor status.
            try:
                result['review'] = load_sync().reader(store).review_health()
            except Exception as exc:
                result['review'] = {'status': 'unavailable', 'error': type(exc).__name__}
            # Read-only information: each scan fails alone and never hides the rest of doctor.
            # The word cap and promotion reports follow the user's opt-in (state/hygiene.json):
            # a default install gets no new doctor lines and no whole-vault read.
            for key in ('boundary', 'closed_tasks', 'word_cap', 'promotion', 'inbox'):
                try:
                    import beyin_v3_hygiene as hygiene
                    opted, _ = hygiene.read_settings(state)
                    if key == 'word_cap':
                        result[key] = (hygiene.cap_scan(vault, cap=opted['max_words'], state=state)
                                       if opted['word_cap_warning'] else {'enabled': False})
                    elif key == 'promotion':
                        result[key] = hygiene.promotion(vault, state) if opted['promotion'] else {'enabled': False}
                    elif key == 'inbox':
                        inbox, _ = hygiene.read_inbox_settings(state)
                        result[key] = (hygiene.inbox_report(vault, state, inbox['max_items'], inbox['max_days'],
                                                            folders=inbox['folders'])
                                       if inbox['enabled'] else {'enabled': False})
                    else:
                        result[key] = getattr(hygiene, key)(vault)
                except Exception as exc:
                    result[key] = {'status': 'unavailable', 'error': type(exc).__name__}
            try:  # read-only report of the opt-in parallel-session notice (#170)
                import beyin_v3_parallel as parallel
                result['parallel_sessions'] = parallel.doctor(state, vault=vault)
            except Exception as exc:
                result['parallel_sessions'] = {'status': 'unavailable', 'error': type(exc).__name__}
            try:  # read-only report of context deduplication (#363)
                import beyin_v3_dedupe as dedupe
                result['context_dedupe'] = dedupe.doctor(state)
            except Exception as exc:
                result['context_dedupe'] = {'status': 'unavailable', 'error': type(exc).__name__}
            # Information only: the user's index exclusions, an index.json that is not applied, and
            # indexed trees that look vendored. Reads the index and one small file; no vault walk.
            try:
                import beyin_v3_index as index_settings
                try:
                    with store._connect() as db:
                        rows = db.execute('SELECT m.source, r.payload FROM markdown_sources m LEFT JOIN records r ON r.id = m.id').fetchall()
                        indexed_sources = [row[0] for row in rows]
                        indexed_records = [json.loads(row[1]) if row[1] else {'source': row[0]} for row in rows]
                except Exception:  # no sync has created the table yet
                    indexed_sources = []
                    indexed_records = []
                result['index_exclusions'] = index_settings.doctor(state, vault, indexed_sources, records=indexed_records)
                result['index_exclusions']['always_excluded'] = sorted(sys.modules['beyin_v3_sync'].EXCLUDED_DIRS)
            except Exception as exc:  # never hides the rest of doctor
                result['index_exclusions'] = {'status': 'unavailable', 'error': type(exc).__name__}
            result['receipt_line_endings'] = receipt_line_endings(vault)  # information only: the status below is untouched
            try:
                _ensure_scripts_path()
                import beyin_v3_projects as projects
                result['project_scopes'] = projects.check_project_scopes(vault)
            except Exception as exc:
                result['project_scopes'] = {'status': 'unavailable', 'error': type(exc).__name__}
            result['status'] = ('needs_attention' if health.get('sync', {}).get('status') in ('conflict', 'degraded') or result['hook_paths'].get('status') == 'stale' or result['skill_conflicts'] or result.get('instruction_conflicts') or result['hook-error.json'] or result['task_completion']['strict_issue_count'] or result['task_completion'].get('error') or result['validity']['ignored_rejection_count'] or result['validity'].get('error') or result.get('project_scopes', {}).get('status') in ('error', 'warning') else 'pending' if result['pending_events'] else 'observed_metadata' if result['acknowledged_events'] else 'never_seen')
            # Information only: a leftover global OMP hook copy predates the vault-owned plan
            # (OMP.md says the installer never updates or removes it). After an engine update the
            # copy can be older than the installed vault hook, so a session outside the vault can
            # keep running an outdated adapter without any health signal. Report a digest mismatch;
            # never mutate or delete the user file. Symlinks (user-linked to the vault hook) are
            # always fresh by construction and reported as absent.
            try:
                adjacent_scripts = Path(__file__).resolve().parent
                template_scripts = adjacent_scripts if (adjacent_scripts / 'beyin_v3_omp.py').exists() else Path(__file__).resolve().parents[1] / 'template/.claude/scripts'
                omp_planner = template_scripts / 'beyin_v3_omp.py'
                if omp_planner.exists():
                    spec = importlib.util.spec_from_file_location('beyin_doctor_omp', omp_planner)
                    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                    generated = next(iter(module.plan_plugin(vault, state).values()))
                    global_copy = Path.home() / '.omp/agent/hooks/pre/beyin-v3.ts'
                    if global_copy.exists() and not global_copy.is_symlink():
                        current_digest = hashlib.sha256(global_copy.read_bytes()).hexdigest()
                        generated_digest = hashlib.sha256(generated).hexdigest()
                        if current_digest != generated_digest:
                            result['omp_global_hook'] = {
                                'path': str(global_copy),
                                'stale': True,
                                'note': 'global copy differs from the installed vault hook; it is not updated by install/rollback, and OMP.md advises against keeping one',
                            }
                        else:
                            result['omp_global_hook'] = {'path': str(global_copy), 'stale': False}
            except Exception as exc:  # a stale-copy report must never hide the rest of doctor
                result['omp_global_hook'] = {'status': 'unavailable', 'error': type(exc).__name__}
        elif args.command == "scheduled-check":
            _ensure_scripts_path()
            import beyin_v3_scheduled as scheduled
            result, code = scheduled.run(vault, state, Path(__file__).resolve(), notify=args.notify)
            if return_result:
                return result, code
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return code
        elif args.command == "skill-sync":
            result = load_skills().sync_skills(vault, state)
        elif args.command == "skill-import":
            result = load_skills().import_skill(vault, state, args.source, name=args.name)
        elif args.command == "project-scope":
            _ensure_scripts_path()
            import beyin_v3_projects as projects
            result = projects.manage(vault, args.action, folder=args.folder, project=args.project,
                                     shared_unscoped=args.shared_unscoped)
        elif args.command == "companion-compact":
            _ensure_scripts_path()
            import beyin_v3_compact
            result = beyin_v3_compact.compact(vault, state, dry_run=args.dry_run)
            if any(entry['status'] == 'compacted' for entry in result['files'].values()):
                # Index the shorter sources and the private archive before anyone reads them.
                result['sync'] = {'status': load_sync()(vault, state).sync().get('status')}
        elif args.command == "chat-trace":
            _ensure_scripts_path()
            import beyin_v3_chat_trace as chat_trace
            if args.action == 'write':
                if not args.harness or not args.session or (args.text is None and args.file is None):
                    raise ValueError('write requires --harness, --session and --text or --file')
                text = args.text
                if args.file is not None:
                    if args.file == '-':
                        text = sys.stdin.read(4097)
                    else:
                        with Path(args.file).open(encoding='utf-8') as handle:
                            text = handle.read(4097)
                result = chat_trace.write(vault, state, args.harness, args.session, text)
            elif args.harness or args.session or args.text is not None or args.file is not None:
                raise ValueError('summary arguments are only valid with chat-trace write')
            else:
                result = (chat_trace.settings(state) if args.action == 'status' else
                          chat_trace.configure(state, args.action == 'on'))
        elif args.command in ("sync", "recover-views"):
            recover = getattr(args, "recover_views", False) or args.command == "recover-views"
            result = sync.sync(full=getattr(args, "full", False), recover_views=recover)
        elif args.command == "recap":
            days = 7 if args.days is None else args.days
            if not 1 <= days <= 366 or not 1 <= args.limit <= 100:
                raise ValueError('recap days must be 1..366 and limit must be 1..100')
            refreshed = sync.sync()
            from beyin_v3_projections import is_receipt_view_conflict
            source_conflicts = [c for c in refreshed.get('conflicts', []) if not is_receipt_view_conflict(c)]
            if source_conflicts:
                raise RuntimeError('Recap blocked: source sync conflict. Run sync to inspect sources.')
            from beyin_v3_projections import recent_receipts
            with store._connect() as db:
                # Without the range flags this is exactly the call recap made before they existed.
                bounds = {name: bound for name, bound in (('since', args.since), ('until', args.until)) if bound is not None}
                result = recent_receipts(db, days=days, limit=args.limit, vault=vault, **bounds)
            view_conflicts = [c for c in refreshed.get('conflicts', []) if is_receipt_view_conflict(c)]
            if view_conflicts:
                result['view_conflicts'] = view_conflicts
            if refreshed.get('status') == 'degraded':
                warnings = refreshed.get('warnings', [])
                result['partial'] = True
                result['source_sync'] = {'status': 'degraded', 'warnings': warnings[:20],
                                         'warning_count': len(warnings), 'truncated': len(warnings) > 20}
        elif args.command == "triage":
            _ensure_scripts_path()
            import beyin_v3_triage as triage_module
            result = triage_module.triage(vault, state=state, stale_days=args.stale_days, digest_days=args.digest_days)
        elif args.command == "audit":
            _ensure_scripts_path()
            import beyin_v3_audit as audit_module
            result = audit_module.audit_memory(
                vault,
                state=state,
                target_file=args.target,
                threshold=args.threshold,
                min_shared=args.min_shared,
                limit=args.limit,
            )
        elif args.command == "ingest":
            payload = read_json(args.file)
            result = [store.ingest(record) for record in payload] if isinstance(payload, list) else store.ingest(payload)
        elif args.command == "note-read":
            result = sync.note_read(args.source)
        elif args.command == "note-edit":
            result = sync.note_edit(read_json(args.file))
        elif args.command == "note-create":
            payload = read_json(args.file)
            result = sync.note_create(payload['source'], payload['text'], payload.get('metadata'))
        elif args.command == "task-create":
            payload = read_json(args.file)
            result = sync.task_create(payload['source'], payload['text'], payload['metadata'])
        elif args.command == "context":
            params = {"query": args.query, "project": args.project, "audience": args.audience,
                      "statuses": args.statuses, "limit": args.limit, "budget_chars": args.budget_chars}
            if args.file:
                supplied = read_json(args.file)
                if not isinstance(supplied, dict) or set(supplied) - set(params):
                    raise ValueError("Context JSON contains unsupported fields")
                params.update(supplied)
            if not isinstance(params["query"], str) or not params["query"].strip():
                raise ValueError("context requires a nonempty query")
            if read_only_context:
                result = store.context_for(args.harness, **params)
                result['source_sync'] = {'status': 'skipped', 'reason': 'explicit_no_sync'}
            else:
                refreshed = sync.sync()
                from beyin_v3_projections import is_receipt_view_conflict
                source_conflicts = [c for c in refreshed.get('conflicts', []) if not is_receipt_view_conflict(c)]
                if source_conflicts:
                    raise RuntimeError('Context blocked: source sync '+str(refreshed.get('status', 'failed'))+'. Run sync with the same vault/state to inspect and reconcile source issues, then retry context.')
                # Harness selection deliberately does not change retrieval semantics.
                if args.jev:
                    from beyin_v3_jev import advise_context
                    result = advise_context(sync.store, **params)
                else:
                    result = sync.store.context_for(args.harness, **params)
                view_conflicts = [c for c in refreshed.get('conflicts', []) if is_receipt_view_conflict(c)]
                if view_conflicts:
                    result['view_conflicts'] = view_conflicts
                if refreshed.get('status') == 'degraded':
                    warnings = refreshed.get('warnings', [])
                    result['partial'] = True
                    result['source_sync'] = {
                        'status': 'degraded',
                        'warning_count': len(warnings),
                        'warnings': warnings[:20],
                        'truncated': len(warnings) > 20,
                    }
        elif args.command in ("jev-review", "jev-answer", "jev-memory"):
            from beyin_v3_jev import review_candidate, verify_answer
            max_chars = 32000 if args.command == "jev-answer" else 24000
            # Bounded read also applies to stdin; never echo raw proposal errors.
            if args.file == "-":
                raw = sys.stdin.read(max_chars + 1)
            else:
                with Path(args.file).open(encoding="utf-8") as handle:
                    raw = handle.read(max_chars + 1)
            if len(raw) > max_chars:
                raise ValueError("proposal_too_large")
            refreshed = sync.sync()
            if refreshed.get('status') != 'succeeded':
                raise ValueError("proposal_source_sync_incomplete")
            handler = verify_answer if args.command == "jev-answer" else review_candidate
            if args.command == "jev-memory":
                from beyin_v3_memory_assessment import assess_memory
                handler = assess_memory
            result = handler(sync.store, json.loads(raw), project=args.project)
        elif args.command == "receipt":
            if receipt_flags:
                summary = args.summary
                if args.summary_file is not None:
                    # Preserve CRLF as well as LF, matching the JSON input text. utf-8-sig drops
                    # the BOM that Windows PowerShell 5.1 writes with -Encoding UTF8.
                    with args.summary_file.open(encoding="utf-8-sig", newline="") as stream:
                        summary = stream.read()
                payload = {"event_id": args.event_id, "summary": summary, "refs": args.ref}
                if args.session is not None:
                    payload["session"] = args.session
                if args.link:
                    payload["links"] = args.link
            else:
                payload = read_json(args.file if args.file is not None else "-")
            result = sync.receipt(payload["event_id"], payload["summary"],
                                          payload["refs"], args.harness, session=payload.get('session'),
                                          links=payload.get('links'))
        elif args.command == "history":
            # History is source-verified, so refresh first like context: an edit
            # made just before this command must not hide the audit trail.
            refreshed = sync.sync()
            if refreshed.get('status') == 'conflict':
                raise RuntimeError('History blocked: source sync conflict. Run sync with the same vault/state to inspect and reconcile source issues, then retry history.')
            result = sync.store.history(args.record_id)
            if refreshed.get('status') == 'degraded':
                # The public stdout contract remains a JSON list.
                warnings = refreshed.get('warnings', [])
                print(json.dumps({'source_sync': {'status': 'degraded', 'warnings': warnings[:20],
                                                  'warning_count': len(warnings), 'truncated': len(warnings) > 20}},
                                 ensure_ascii=True), file=sys.stderr)
        elif args.command == "learn":
            _ensure_scripts_path()
            from beyin_v3_learning import stage_candidate, list_candidates
            sess = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("BEYIN_V3_SESSION_ID") or "cli"
            if args.candidate:
                result = stage_candidate(state, sess, args.candidate, source=args.source, scope=args.scope or "global")
            elif args.list:
                result = {"candidates": list_candidates(state, session=args.session or None, scope=args.scope)}
            else:
                raise ValueError("either --candidate or --list is required")
        else:
            payload = read_json(args.file)
            result = sync.update_task(payload["id"], payload["expected_revision"], payload["changes"])
        if (args.command in SOURCE_WRITE_COMMANDS
                and (args.command != "note-edit" or result.get('status') == 'written')
                and os.environ.get("CLAUDE_CODE_SESSION_ID")):
            mark_session_edit(state)
        if return_result:
            return result, 0
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except Exception as exc:
        # No traceback or raw input dump: callers retain their local source files.
        error = {"error": type(exc).__name__, "message": str(exc)}
        code = 1
        if isinstance(exc, ArgumentParsingError):
            error["error"] = "SystemExit"
            code = 2
        if args.command in NOTE_JSON_COMMANDS:
            from beyin_v3_notes import NoteConflict, NotePartialWrite
            code = 2 if isinstance(exc, NoteConflict) else 1
            error['status'] = 'conflict' if code == 2 else 'error'
            if isinstance(exc, NotePartialWrite):
                error.update(exc.details)
        if isinstance(exc, ValueError) and str(exc) in ERROR_HINTS:
            error["hint"] = ERROR_HINTS[str(exc)]
        if return_result:
            return error, code
        print(json.dumps(error, ensure_ascii=True),
              file=sys.stdout if args.command in NOTE_JSON_COMMANDS else sys.stderr)
        return code


if __name__ == "__main__":
    raise SystemExit(main())
