"""Hermes Agent plugin for Beyin V3: same hook contract, no model or network calls.

Hermes (NousResearch/hermes-agent) loads plugins from ``~/.hermes/plugins/<name>/``
and calls ``register(ctx)``. This module is the vault-owned implementation; the
plugin directory only needs a thin ``__init__.py`` that locates the vault and
imports this file (see ``plan_plugin`` and ``docs/v3/HERMES.md``).

Hook contract (verified against hermes-agent ``agent/turn_context.py`` and
``hermes_cli/cli_session_mixin.py``):

* ``pre_llm_call(session_id, turn_id, user_message, is_first_turn)`` — returning
  ``{"context": text}`` injects ``text`` into that turn's user message. This is the
  only supported injection channel, so V3 context arrives here: ``SessionStart`` on
  the first turn, ``UserPromptSubmit`` afterwards.
* ``on_session_finalize(session_id)`` — the real session boundary (shutdown,
  ``/new``, ``/reset``). It queues ``SessionEnd`` so receipt-gap tracking works the
  same way it does for Claude Code and Codex.

Everything runs through ``beyin_v3_hook.py`` as a subprocess with ``--harness
hermes``: one lifecycle adapter, one preference file, one health file for every
client. The subprocess waits at most ``HOOK_TIMEOUT`` so a stalled sync cannot hold
a Hermes turn beyond that budget. Failures the adapter handles itself land in its
``hook-error.json`` and surface through ``doctor``; transport failures (spawn,
timeout, exit status, malformed output) happen outside the adapter, so they are
logged through Hermes' plugin logger instead and the turn proceeds without context.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading

HOOK_TIMEOUT = 20 if os.name == "nt" else 6
PLUGIN_NAME = "beyin-v3"
LOGGER = logging.getLogger(__name__)
# Sessions nobody sits in front of (scheduled jobs, chat bots) never submit receipts, so
# their SessionEnd must not be counted as a checkpoint: otherwise every later interactive
# start warns about "missing receipts" for work that was never receipt-shaped. Hermes
# reports a generic platform at finalize, so the platform seen on the first turn is kept.
UNATTENDED_PLATFORMS = frozenset({"cron", "telegram", "discord", "slack", "whatsapp", "signal", "matrix", "email", "sms", "webhook"})
# Same cadence as the V2 nudge: remind a long interactive session to write a receipt.
REMINDER_EVERY = 15
REMINDER = "[Hafıza] {n}. mesaj. Anlamlı iş bittiyse `beyin.py receipt --harness hermes` ile kaynak bağlantılı makbuz yaz."


def _runtime(vault):
    """Resolve the state directory from the vault's installer-written runtime file."""
    runtime = Path(vault) / ".beyin-runtime.json"
    data = json.loads(runtime.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("state"), str) or not data["state"]:
        raise ValueError("runtime state missing")
    state = Path(data["state"]).expanduser()
    if not state.is_absolute():
        raise ValueError("runtime state must be absolute")
    return state


def hook_command(vault, state, python=None):
    hook = Path(vault) / ".claude/scripts/beyin_v3_hook.py"
    return [python or sys.executable, str(hook), "--vault", str(vault), "--state", str(state), "--harness", "hermes"]


def run_hook(vault, state, payload, python=None, timeout=HOOK_TIMEOUT):
    """Run one lifecycle event through the shared adapter; return injected text or ''."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    try:
        completed = subprocess.run(hook_command(vault, state, python), input=json.dumps(payload, ensure_ascii=False),
                                   capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOGGER.warning("Beyin V3 adapter transport failed (%s)", type(exc).__name__)
        return ""
    if completed.returncode:
        LOGGER.warning("Beyin V3 adapter exited with status %s", completed.returncode)
        return ""
    try:
        output = json.loads(completed.stdout or "{}")
    except ValueError:
        LOGGER.warning("Beyin V3 adapter returned invalid JSON")
        return ""
    specific = output.get("hookSpecificOutput") if isinstance(output, dict) else None
    text = specific.get("additionalContext") if isinstance(specific, dict) else None
    return text if isinstance(text, str) else ""


def make_hooks(vault, state=None, python=None):
    """Build the Hermes hook callables. Pure function so tests can drive them without Hermes."""
    vault = Path(vault).expanduser().resolve()
    state = Path(state).expanduser().resolve() if state else _runtime(vault)
    sessions = {}  # session_id -> {"platform": str | None, "turns": int}; process-local, no disk
    sessions_lock = threading.Lock()

    def before(**kw):
        session_id = kw.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None
        first = bool(kw.get("is_first_turn"))
        with sessions_lock:
            info = sessions.setdefault(session_id, {"platform": None, "turns": 0})
            if first or info["platform"] is None:
                platform = kw.get("platform")
                info["platform"] = platform if isinstance(platform, str) else None
            info["turns"] += 1
            turns = info["turns"]
            platform = info["platform"]
        event = "SessionStart" if first else "UserPromptSubmit"
        prompt = kw.get("user_message") if isinstance(kw.get("user_message"), str) else ""
        text = run_hook(vault, state, {"hook_event_name": event, "session_id": session_id, "prompt": prompt}, python)
        parts = [text] if text else []
        if turns % REMINDER_EVERY == 0 and platform not in UNATTENDED_PLATFORMS:
            parts.append(REMINDER.format(n=turns))
        return {"context": "\n\n".join(parts)} if parts else None

    def finalize(**kw):
        session_id = kw.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None
        with sessions_lock:
            info = sessions.pop(session_id, None)
        if info and info["platform"] in UNATTENDED_PLATFORMS:
            return None
        run_hook(vault, state, {"hook_event_name": "SessionEnd", "session_id": session_id}, python)
        return None

    return {"pre_llm_call": before, "on_session_finalize": finalize}


def register(ctx, vault=None, state=None):
    """Hermes entry point. ``vault`` defaults to ``BEYIN_VAULT`` so one plugin serves any vault."""
    vault = vault or os.environ.get("BEYIN_VAULT")
    if not vault or not (Path(vault) / ".beyin-runtime.json").is_file():
        return  # An unmounted or uninstalled vault must leave Hermes fully usable.
    try:
        hooks = make_hooks(vault, state)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Fail open: a damaged runtime file disables this plugin, never Hermes. The
        # class is enough to diagnose; the file contents stay out of the log.
        LOGGER.warning("Beyin V3 runtime unavailable (%s); memory hooks not registered", type(exc).__name__)
        return
    for name, hook in hooks.items():
        ctx.register_hook(name, hook)


def plan_plugin(vault, state):
    """Files the installer drops into the vault for the user to link into ``~/.hermes/plugins``.

    The vault copy is the source of truth; ``hermes-plugin/__init__.py`` only pins the
    vault path for this machine, like ``.beyin-runtime.json`` does for the CLI.
    """
    vault = Path(vault).resolve()
    init = (
        '"""Hermes plugin shim for Beyin V3; the implementation lives in the vault."""\n'
        "import importlib.util, os\n"
        "from pathlib import Path\n\n"
        f"VAULT = Path(os.environ.get('BEYIN_VAULT') or {str(vault)!r})\n\n\n"
        "def register(ctx):\n"
        "    module_path = VAULT / '.claude/scripts/beyin_v3_hermes.py'\n"
        "    if not module_path.is_file():\n"
        "        return\n"
        "    spec = importlib.util.spec_from_file_location('beyin_v3_hermes', module_path)\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    spec.loader.exec_module(module)\n"
        "    module.register(ctx, vault=VAULT)\n"
    )
    manifest = (
        f"name: {PLUGIN_NAME}\n"
        "version: 1.0.0\n"
        "description: Beyin V3 source-backed memory for Hermes (context on every turn, SessionEnd receipt tracking).\n"
        "provides_hooks:\n"
        "  - pre_llm_call\n"
        "  - on_session_finalize\n"
    )
    return {".claude/hermes-plugin/__init__.py": init.encode(), ".claude/hermes-plugin/plugin.yaml": manifest.encode()}
