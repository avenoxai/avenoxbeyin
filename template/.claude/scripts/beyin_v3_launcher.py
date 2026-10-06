"""Platform-aware launcher wrapper for Beyin V3 lifecycle hooks.

Resolves foreign/missing state directories, dynamically detects vault root,
reconciles foreign-OS hook definitions, and executes beyin_v3_hook.py.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True


def _default_state(vault: Path) -> Path:
    """Keep mutable state outside the vault, separated by canonical vault path."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    key = hashlib.sha256(str(Path(vault).resolve()).encode()).hexdigest()[:16]
    return base / "beyin-v3" / key


def _is_foreign_or_invalid_state(pinned: str) -> bool:
    """Detect whether a pinned state string belongs to another OS or is unresolvable."""
    if not isinstance(pinned, str) or not pinned.strip():
        return True
    if sys.platform != "win32":
        if re.match(r"^[a-zA-Z]:", pinned) or pinned.startswith("\\\\") or "\\" in pinned:
            return True
        try:
            p = Path(pinned).expanduser()
            if not p.is_absolute():
                return True
            parts = p.parts
            if len(parts) >= 3 and parts[1] in ("Users", "home") and not Path(*parts[:3]).exists():
                return True
        except Exception:
            return True
    else:
        if pinned.startswith("/") or not (re.match(r"^[a-zA-Z]:[/\\]", pinned) or pinned.startswith("\\\\")):
            return True
        try:
            p = Path(pinned).expanduser()
            if not p.is_absolute():
                return True
            parts = p.parts
            if len(parts) >= 3 and parts[1].lower() == "users" and not Path(*parts[:3]).exists():
                return True
        except Exception:
            return True
    return False


def _is_beyin_hook(command: str) -> bool:
    """Detect whether a command string belongs to Beyin hook or launcher, including encoded PowerShell commands."""
    if not isinstance(command, str) or not command.strip():
        return False
    if "beyin_v3_hook.py" in command or "beyin_v3_launcher.py" in command:
        return True
    match = re.search(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", command, re.IGNORECASE)
    if match:
        try:
            decoded = base64.b64decode(match.group(1)).decode("utf-16le", errors="ignore")
            return "beyin_v3_hook.py" in decoded or "beyin_v3_launcher.py" in decoded
        except Exception:
            pass
    return False


def is_foreign_command(command: str) -> bool:
    """Detect whether a hook command string was generated for a foreign operating system."""
    if not isinstance(command, str) or not command.strip():
        return False
    if sys.platform != "win32":
        if "powershell.exe" in command.lower() or "-encodedcommand" in command.lower():
            return True
        if re.search(r"^[a-zA-Z]:[/\\]", command.strip().strip("'\"")) or "\\\\" in command:
            return True
    else:
        stripped = command.strip().strip("'\"")
        if stripped.startswith("/") and not command.lower().startswith("c:"):
            return True
        if any(prefix in command for prefix in ("/usr/bin/", "/usr/local/bin/", "/opt/homebrew/", "/home/", "/Users/")):
            return True
    return False


def resolve_vault(vault_arg: str | None = None) -> Path:
    """Resolve vault path safely, correcting foreign OS paths or missing args."""
    if vault_arg and not _is_foreign_or_invalid_state(vault_arg):
        candidate = Path(vault_arg).expanduser().resolve()
        if candidate.is_dir() and (candidate / ".claude/scripts/beyin_v3_hook.py").is_file():
            return candidate
    cwd = Path.cwd().resolve()
    if (cwd / ".claude/scripts/beyin_v3_hook.py").is_file():
        return cwd
    own_vault = Path(__file__).resolve().parents[2]
    if (own_vault / ".claude/scripts/beyin_v3_hook.py").is_file():
        return own_vault
    return cwd


def resolve_state(vault: Path, state_arg: str | None = None) -> Path:
    """Resolve state directory safely, falling back to local default if foreign or missing."""
    if state_arg and not _is_foreign_or_invalid_state(state_arg):
        candidate = Path(state_arg).expanduser().resolve()
        if candidate != vault and vault not in candidate.parents:
            return candidate
    runtime_file = vault / ".beyin-runtime.json"
    if runtime_file.is_file():
        try:
            data = json.loads(runtime_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                s = data.get("state")
                if isinstance(s, str) and s and not _is_foreign_or_invalid_state(s):
                    candidate = Path(s).expanduser().resolve()
                    if candidate != vault and vault not in candidate.parents:
                        return candidate
        except Exception:
            pass
    return _default_state(vault)


def local_commands(argv: list[str]) -> tuple[str, str]:
    """Generate both POSIX and Windows shell command strings for the given argv."""
    if any(any(c in str(value) for c in "\n\r\x00") for value in argv):
        raise ValueError("Newlines or NUL in command paths are unsupported")
    portable_argv = [str(value).replace("\\", "/") if sys.platform == "win32" else str(value) for value in argv]
    posix = shlex.join(portable_argv)
    script = "& " + " ".join("'" + str(value).replace("'", "''") + "'" for value in argv)
    script += "; exit $LASTEXITCODE"
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    launcher = "powershell.exe"
    if sys.platform == "win32":
        windows_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
        launcher_path = str(Path(windows_root) / "System32/WindowsPowerShell/v1.0/powershell.exe").replace("\\", "/")
        launcher = subprocess.list2cmdline([launcher_path])
    windows = launcher + " -NoProfile -NonInteractive -EncodedCommand " + encoded
    return posix, windows


def reconcile_hook_file(vault: Path, relative_name: str, python: str | None = None) -> bool:
    """Check a hook file and update any foreign OS commands in-place to local platform commands."""
    path = Path(vault) / relative_name
    if not path.is_file():
        return False
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
    except Exception:
        return False

    changed = False
    py = python or sys.executable
    hook_script = Path(vault) / ".claude/scripts/beyin_v3_hook.py"
    state = resolve_state(Path(vault))

    if relative_name == ".agents/hooks.json":
        managed = data.get("beyin-v3")
        if isinstance(managed, dict):
            for event, handlers in managed.items():
                if isinstance(handlers, list):
                    for h in handlers:
                        if isinstance(h, dict) and is_foreign_command(h.get("command", "")):
                            posix, windows = local_commands([py, hook_script, "--vault", vault, "--state", state,
                                                             "--harness", "antigravity", "--event", event])
                            h["command"] = windows if sys.platform == "win32" else posix
                            changed = True
    elif relative_name in (".claude/settings.local.json", ".claude/settings.json"):
        hooks = data.get("hooks", {})
        if isinstance(hooks, dict):
            for event, groups in hooks.items():
                if isinstance(groups, list):
                    for g in groups:
                        for h in g.get("hooks", []):
                            if isinstance(h, dict) and _is_beyin_hook(h.get("command", "")):
                                if is_foreign_command(h.get("command", "")):
                                    posix, windows = local_commands([py, hook_script, "--vault", vault, "--state", state,
                                                                     "--harness", "claude"])
                                    h["command"] = windows if sys.platform == "win32" else posix
                                    changed = True
    elif relative_name == ".codex/hooks.json":
        hooks = data.get("hooks", {})
        if isinstance(hooks, dict):
            for event, groups in hooks.items():
                if isinstance(groups, list):
                    for g in groups:
                        for h in g.get("hooks", []):
                            cmd = h.get("command", "")
                            cmd_win = h.get("commandWindows", "")
                            if _is_beyin_hook(cmd) or _is_beyin_hook(cmd_win):
                                if is_foreign_command(cmd) or (sys.platform == "win32" and is_foreign_command(cmd_win)):
                                    posix, windows = local_commands([py, hook_script, "--vault", vault, "--state", state,
                                                                     "--harness", "codex"])
                                    h["command"] = windows if sys.platform == "win32" else posix
                                    h["commandWindows"] = windows
                                    changed = True

    if changed:
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception:
            return False
    return changed


def reconcile_all_hooks(vault: Path, python: str | None = None) -> dict[str, bool]:
    """Reconcile all managed hook files in the vault if they contain foreign-OS commands."""
    results = {}
    for name in (".agents/hooks.json", ".claude/settings.local.json", ".claude/settings.json", ".codex/hooks.json"):
        results[name] = reconcile_hook_file(vault, name, python)
    return results


def launch(argv: list[str] | None = None) -> int:
    """Parse hook args, sanitize foreign paths, reconcile hooks if needed, and execute beyin_v3_hook.py."""
    if argv is None:
        argv = sys.argv[1:]

    import argparse
    parser = argparse.ArgumentParser(description="Beyin V3 Platform-Aware Hook Launcher")
    parser.add_argument("--vault")
    parser.add_argument("--state")
    parser.add_argument("--harness", required=True)
    parser.add_argument("--event")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--drain-queue", action="store_true")
    parser.add_argument("--metadata-only", action="store_true")
    args, unknown = parser.parse_known_args(argv)

    vault = resolve_vault(args.vault)
    state = resolve_state(vault, args.state)

    hook_args = ["--vault", str(vault), "--state", str(state), "--harness", args.harness]
    if args.event:
        hook_args.extend(["--event", args.event])
    if args.worker:
        hook_args.append("--worker")
    if args.drain_queue:
        hook_args.append("--drain-queue")
    if args.metadata_only:
        hook_args.append("--metadata-only")
    hook_args.extend(unknown)

    hook_dir = vault / ".claude/scripts"
    if str(hook_dir) not in sys.path:
        sys.path.insert(0, str(hook_dir))

    import importlib.util
    hook_path = hook_dir / "beyin_v3_hook.py"
    if not hook_path.is_file():
        return 1
    spec = importlib.util.spec_from_file_location("beyin_v3_hook", hook_path)
    if spec is None or spec.loader is None:
        return 1
    hook_mod = importlib.util.module_from_spec(spec)
    sys.modules["beyin_v3_hook"] = hook_mod

    prev_argv = sys.argv
    try:
        sys.argv = ["beyin_v3_hook.py"] + hook_args
        spec.loader.exec_module(hook_mod)
        if hasattr(hook_mod, "main"):
            ret = hook_mod.main()
            return ret if isinstance(ret, int) else 0
        return 0
    finally:
        sys.argv = prev_argv


if __name__ == "__main__":
    sys.exit(launch())
