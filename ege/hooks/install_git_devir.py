#!/usr/bin/env python3
"""Install or remove the git-devir hook in the global Claude Code settings.

    install_git_devir.py --machine <ad> --project-root <yol> [--project-root ...] --vault <yol> [--dry-run]
    install_git_devir.py --uninstall [--dry-run]

Writes ``<config>/git-devir.json`` and merges one SessionStart and one SessionEnd handler into
``<config>/settings.json`` (``<config>`` = CLAUDE_CONFIG_DIR or ~/.claude). Every other hook is
preserved; a timestamped backup is made before settings.json is changed; reinstalling is a no-op.
Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HOOK_SCRIPT = Path(__file__).resolve().with_name("git_devir.py")
SCRIPT_MARK = "git_devir.py"
MACHINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
TIMEOUTS = {"SessionStart": 15, "SessionEnd": 5}
MODES = {"SessionStart": "start", "SessionEnd": "end"}
WINDOWS_SAFE = re.compile(r"^[A-Za-z0-9_.:/\\-]+$")


def config_dir() -> Path:
    custom = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(custom).expanduser() if custom else Path.home() / ".claude"


def shell_command(argv) -> str:
    """A command line that works whichever shell Claude Code uses for hooks."""
    argv = [str(a) for a in argv]
    if any(c in a for a in argv for c in "\r\n\x00"):
        raise ValueError("Unsupported path")
    if os.name != "nt":
        return shlex.join(argv)
    if all(WINDOWS_SAFE.match(a) for a in argv):
        # Forward slashes and no quotes: valid in Git Bash, cmd and PowerShell alike, and fast.
        return " ".join(a.replace("\\", "/") for a in argv)
    # Paths with spaces or special characters: the same PowerShell form as the avenox bridge.
    script = "& " + " ".join("'" + a.replace("'", "''") + "'" for a in argv) + "; exit $LASTEXITCODE"
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    system = Path(os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows")
    launcher = str(system / "System32/WindowsPowerShell/v1.0/powershell.exe").replace("\\", "/")
    return subprocess.list2cmdline([launcher]) + " -NoProfile -NonInteractive -EncodedCommand " + encoded


def _decoded(command) -> str:
    if not isinstance(command, str):
        return ""
    match = re.search(r"-EncodedCommand\s+(\S+)", command)
    if match:
        try:
            return command + " " + base64.b64decode(match.group(1)).decode("utf-16le")
        except (ValueError, UnicodeDecodeError):
            pass
    return command


def is_own(handler, mark=SCRIPT_MARK) -> bool:
    """``mark`` is a substring of the hook command, or a tuple of alternatives (other installers
    in this folder reuse these helpers with their own script name)."""
    marks = (mark,) if isinstance(mark, str) else tuple(mark)
    return isinstance(handler, dict) and any(
        m in _decoded(handler.get(key)) for key in ("command", "commandWindows") for m in marks)


def without_own(groups, mark=SCRIPT_MARK):
    """Drop git-devir handlers from an event's group list; returns (groups, removed_count)."""
    cleaned, removed = [], 0
    for group in groups:
        handlers = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(handlers, list):
            cleaned.append(group)
            continue
        remaining = [h for h in handlers if not is_own(h, mark)]
        removed += len(handlers) - len(remaining)
        if len(remaining) == len(handlers):
            cleaned.append(group)
        elif remaining:
            cleaned.append(dict(group, hooks=remaining))
    return cleaned, removed


def read_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"settings.json okunamadı, dokunulmadı: {exc}")
    if not isinstance(data, dict):
        raise SystemExit("settings.json bir JSON nesnesi değil, dokunulmadı.")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict) or any(not isinstance(v, list) for v in hooks.values()):
        raise SystemExit("settings.json içindeki hooks beklenen biçimde değil, dokunulmadı.")
    return data


def own_handlers(data: dict, mark=SCRIPT_MARK) -> dict:
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    found = {}
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            for handler in handlers if isinstance(handlers, list) else []:
                if is_own(handler, mark):
                    found.setdefault(event, []).append(handler)
    return found


def merged_settings(data: dict, python: str, install: bool, script=None, modes=None, timeouts=None,
                    mark=SCRIPT_MARK):
    """Merge (or remove) one script's handlers; defaults are git-devir's own."""
    script, modes, timeouts = script or HOOK_SCRIPT, modes or MODES, timeouts or TIMEOUTS
    if install:
        wanted = {event: [{"type": "command", "command": shell_command([python, script, mode]),
                           "timeout": timeouts[event]}] for event, mode in modes.items()}
        if own_handlers(data, mark) == wanted:  # already installed exactly like this: change nothing
            return data, 0, {event: h[0]["command"] for event, h in wanted.items()}
    data = json.loads(json.dumps(data))  # deep copy
    hooks = data.setdefault("hooks", {})
    removed = 0
    for event in list(hooks):
        cleaned, count = without_own(hooks[event], mark)
        removed += count
        if count:
            if cleaned:
                hooks[event] = cleaned
            else:
                del hooks[event]
    added = {}
    if install:
        for event, mode in modes.items():
            command = shell_command([python, script, mode])
            group = {"hooks": [{"type": "command", "command": command, "timeout": timeouts[event]}]}
            hooks.setdefault(event, []).append(group)
            added[event] = command
    if not hooks:
        del data["hooks"]
    return data, removed, added


def backup(path: Path) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.git-devir-{stamp}.bak")
    index = 1
    while target.exists():
        target = path.with_name(f"{path.name}.git-devir-{stamp}-{index}.bak")
        index += 1
    target.write_bytes(path.read_bytes())
    return target


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def build_config(args, existing: dict) -> dict:
    if not MACHINE_RE.match(args.machine or ""):
        raise SystemExit("--machine kısa bir etiket olmalı (harf, rakam, . _ -; en çok 32), ör. is-5090, ev, mac.")
    if not args.project_root:
        raise SystemExit("En az bir --project-root gerekli.")
    roots = []
    for raw in args.project_root:
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            raise SystemExit(f"Proje kökü bulunamadı: {raw}")
        roots.append(str(path))
    config = dict(existing)  # keep unknown keys, e.g. remote_aliases
    config.update({
        "machine": args.machine,
        "project_roots": roots,
        "vault_path": str(Path(args.vault).expanduser().resolve()) if args.vault else "",
        "auto_owners": args.auto_owner or existing.get("auto_owners") or ["kutluege"],
        "auto_hosts": args.auto_host or existing.get("auto_hosts") or ["github.com"],
        "max_file_mb": args.max_file_mb if args.max_file_mb is not None else existing.get("max_file_mb", 50),
    })
    return config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="git-devir hook'unu kurar veya kaldırır.")
    parser.add_argument("--machine", help="Makine etiketi (ör. is-5090, ev, mac); hostname değil.")
    parser.add_argument("--project-root", action="append", default=[], help="Repoların durduğu kök (tekrarlanabilir).")
    parser.add_argument("--vault", help="Obsidian vault yolu; içindeki repolara dokunulmaz.")
    parser.add_argument("--auto-owner", action="append", help="Tam otomatik GitHub sahibi (varsayılan kutluege).")
    parser.add_argument("--auto-host", action="append", help="Tam otomatik git sunucusu (varsayılan github.com).")
    parser.add_argument("--max-file-mb", type=float, help="wip yedeğine alınacak en büyük dosya (MB, varsayılan 50).")
    parser.add_argument("--python", default=sys.executable, help="Hook'u çalıştıracak Python (varsayılan: bu Python).")
    parser.add_argument("--uninstall", action="store_true", help="Yalnız git-devir hook girdilerini kaldırır.")
    parser.add_argument("--dry-run", action="store_true", help="Hiçbir şey yazmadan ne yapılacağını gösterir.")
    args = parser.parse_args(argv)

    base = config_dir()
    settings_path = base / "settings.json"
    cfg_path = base / "git-devir.json"
    data = read_settings(settings_path)
    new_data, removed, added = merged_settings(data, args.python, install=not args.uninstall)

    report = {"settings": str(settings_path), "removed_handlers": removed, "dry_run": args.dry_run}
    config = None
    if not args.uninstall:
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
            existing = existing if isinstance(existing, dict) else {}
        except (OSError, ValueError):
            existing = {}
        config = build_config(args, existing)
        report.update({"config": str(cfg_path), "config_content": config, "added_handlers": added})

    new_text = json.dumps(new_data, indent=2, ensure_ascii=False) + "\n"
    settings_changed = new_data != data
    report["settings_changed"] = settings_changed

    if not args.dry_run:
        if config is not None:
            write_atomic(cfg_path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
        if settings_changed:
            if settings_path.exists():
                report["backup"] = str(backup(settings_path))
            write_atomic(settings_path, new_text)
        report["status"] = "uninstalled" if args.uninstall else "installed"
        if args.uninstall and cfg_path.exists():
            report["note"] = f"Ayar dosyası bırakıldı; istersen sil: {cfg_path}"
    else:
        report["status"] = "dry-run"
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
