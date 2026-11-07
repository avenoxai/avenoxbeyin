#!/usr/bin/env python3
"""Render Antigravity hooks without duplicating the canonical beyin engine."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from typing import Any


MANAGED_KEY = "avenox-beyin"


def _quote(value: str, platform: str) -> str:
    if platform == "windows":
        return '"' + value.replace('"', '\\"') + '"'
    return shlex.quote(value)


def _command(python: Path, adapter: Path, action: str, platform: str) -> str:
    return " ".join(
        _quote(str(part), platform) for part in (python, adapter, action)
    )


def render(
    vault: Path,
    platform: str,
    python_executable: Path | None = None,
) -> dict[str, Any]:
    vault = vault.expanduser().resolve()
    if not vault.is_dir():
        raise ValueError(f"vault-not-directory:{vault}")
    adapter = vault / ".claude" / "scripts" / "antigravity_hooks.py"
    if not adapter.is_file():
        raise ValueError(f"adapter-missing:{adapter}")
    python = (python_executable or Path(sys.executable)).resolve()

    destination = vault / ".agents" / "hooks.json"
    try:
        current = json.loads(destination.read_text(encoding="utf-8"))
    except FileNotFoundError:
        current = {}
    except ValueError as exc:
        raise ValueError("antigravity-hooks-invalid-json; existing file preserved") from exc
    if not isinstance(current, dict):
        raise ValueError("antigravity-hooks-root-not-object")

    current[MANAGED_KEY] = {
        "PreInvocation": [
            {
                "type": "command",
                "command": _command(
                    python, adapter, "pre-invocation", platform
                ),
                "timeout": 15,
            }
        ],
        "Stop": [
            {
                "type": "command",
                "command": _command(python, adapter, "stop", platform),
                "timeout": 5,
            }
        ],
    }
    return current


def write(
    vault: Path,
    platform: str,
    python_executable: Path | None = None,
) -> Path:
    payload = render(vault, platform, python_executable)
    destination = vault.expanduser().resolve() / ".agents" / "hooks.json"
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
    print(write(args.vault, args.platform))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
