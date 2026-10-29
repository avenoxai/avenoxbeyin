#!/usr/bin/env python3
"""Render project-local Codex hook wiring with absolute, safely quoted paths."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import tempfile
from typing import Any


HOOKS = {
    "SessionStart": ("session-start", 15, "Beyin memory bridge"),
    "UserPromptSubmit": ("prompt-counter", 5, "Beyin prompt counter"),
    "PreCompact": ("pre-compact", 10, "Beyin pre-compact flush"),
    # Codex caps SessionEnd handlers at three seconds.  The hook detaches the
    # real work immediately, so this is sufficient and honest.
    "SessionEnd": ("session-end", 3, "Beyin session flush"),
}


def _command(path: Path, platform: str) -> str:
    if platform == "windows":
        quoted = '"' + str(path).replace('"', '\\"') + '"'
        return (
            "pwsh.exe -NoProfile -ExecutionPolicy Bypass -File " + quoted
        )
    return shlex.quote(str(path))


def _managed(command: str) -> bool:
    normalized = command.replace("\\", "/")
    return any(
        f"/.claude/hooks/{name}.sh" in normalized
        or f"/.claude/hooks/{name}.ps1" in normalized
        or f"/.codex/hooks/{name}.sh" in normalized
        for name, _, _ in HOOKS.values()
    )


def render(vault: Path, platform: str) -> dict[str, Any]:
    vault = vault.expanduser().resolve()
    if not vault.is_dir():
        raise ValueError(f"vault-not-directory:{vault}")
    suffix = ".ps1" if platform == "windows" else ".sh"
    hooks_dir = (
        vault / ".claude" / "hooks"
        if platform == "windows"
        else vault / ".codex" / "hooks"
    )

    destination = vault / ".codex" / "hooks.json"
    try:
        current = json.loads(destination.read_text(encoding="utf-8"))
    except FileNotFoundError:
        current = {}
    except ValueError as exc:
        raise ValueError("codex-hooks-invalid-json; existing file preserved") from exc
    if not isinstance(current, dict):
        raise ValueError("codex-hooks-root-not-object")
    current_hooks = current.setdefault("hooks", {})
    if not isinstance(current_hooks, dict):
        raise ValueError("codex-hooks-not-object")

    # Remove only our own prior registrations.  Unrelated project hooks stay.
    for event, matchers in list(current_hooks.items()):
        if not isinstance(matchers, list):
            raise ValueError(f"codex-hook-event-not-list:{event}")
        kept_matchers = []
        for matcher in matchers:
            if not isinstance(matcher, dict):
                kept_matchers.append(matcher)
                continue
            entries = matcher.get("hooks")
            if not isinstance(entries, list):
                kept_matchers.append(matcher)
                continue
            kept_entries = [
                entry
                for entry in entries
                if not (
                    isinstance(entry, dict)
                    and _managed(str(entry.get("command", "")))
                )
            ]
            if kept_entries:
                updated = dict(matcher)
                updated["hooks"] = kept_entries
                kept_matchers.append(updated)
        if kept_matchers:
            current_hooks[event] = kept_matchers
        else:
            current_hooks.pop(event, None)

    for event, (stem, timeout, status) in HOOKS.items():
        path = hooks_dir / f"{stem}{suffix}"
        current_hooks.setdefault(event, []).append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _command(path, platform),
                        "timeout": timeout,
                        "statusMessage": status,
                    }
                ]
            }
        )

    current.setdefault("description", (
        "Avenox Beyin project hooks for Codex. Commands use absolute paths "
        "because Codex sets neither CODEX_PROJECT_DIR nor CLAUDE_PROJECT_DIR."
    ))
    return current


def write(vault: Path, platform: str) -> Path:
    payload = render(vault, platform)
    destination = vault.expanduser().resolve() / ".codex" / "hooks.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=".hooks.", suffix=".json"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument(
        "--platform",
        choices=("posix", "windows"),
        default="windows" if os.name == "nt" else "posix",
    )
    args = parser.parse_args()
    destination = write(args.vault, args.platform)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
