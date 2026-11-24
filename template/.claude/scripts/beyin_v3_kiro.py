"""Adapt Kiro's plain-text command hooks to the shared V3 lifecycle handler."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")


def context(vault, state, event, payload):
    """Return only context text; Kiro inserts successful stdout verbatim."""
    payload = dict(payload) if isinstance(payload, dict) else {}
    payload["hook_event_name"] = event
    if event == "UserPromptSubmit" and not payload.get("prompt"):
        payload["prompt"] = os.environ.get("USER_PROMPT", "")
    hook = Path(__file__).with_name("beyin_v3_hook.py")
    command = [sys.executable, str(hook), "--vault", str(vault), "--state", str(state),
               "--harness", "kiro", "--event", event]
    try:
        result = subprocess.run(command, input=json.dumps(payload), text=True,
                                encoding="utf-8", capture_output=True,
                                timeout=18 if os.name == "nt" else 7)
        if result.returncode:
            return ""
        output = json.loads(result.stdout)
        specific = output.get("hookSpecificOutput") if isinstance(output, dict) else None
        value = specific.get("additionalContext") if isinstance(specific, dict) else None
        return value if isinstance(value, str) else ""
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # A hook transport failure cannot block a prompt or leak runtime JSON into context.
        return ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--event", required=True, choices=EVENTS)
    args = parser.parse_args()
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8-sig", errors="replace")
    try:
        payload = json.loads(sys.stdin.read(1_000_000).lstrip("\ufeff").strip() or "{}")
    except ValueError:
        payload = {}
    value = context(args.vault, args.state, args.event, payload)
    if value:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(value)


if __name__ == "__main__":
    main()
