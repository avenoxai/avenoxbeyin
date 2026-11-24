"""Optional context-fill reminder for Claude Code Stop events.

Tracks accumulated context tokens per session and reminds the agent to write a
handoff card and receipt before the context window fills up.
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path

from beyin_v3_notes import _unlinked
from beyin_v3_learning import _ledger_lock

CONTEXT_FILL_FILE = "context-fill.json"
CONTEXT_FILL_DIR = "context-fill"
DEFAULT_FIRST_THRESHOLD = 150000
DEFAULT_REPEAT_THRESHOLD = 75000
MAX_STATE_FILES = 128
STATE_TTL_SECONDS = 24 * 3600


def _config_path(state_dir):
    return Path(state_dir) / CONTEXT_FILL_FILE


def _state_dir(state_dir):
    return Path(state_dir) / CONTEXT_FILL_DIR


def load_config(state_dir):
    """Load context-fill configuration. Returns dict with defaults if missing/invalid."""
    path = _config_path(state_dir)
    defaults = dict(
        enabled=False,
        first_threshold=DEFAULT_FIRST_THRESHOLD,
        repeat_threshold=DEFAULT_REPEAT_THRESHOLD,
    )
    if not path.exists() or path.is_symlink():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return defaults
        result = dict(defaults, **data)
        if type(result.get("enabled")) is not bool:
            result["enabled"] = False
        for key, default in (("first_threshold", DEFAULT_FIRST_THRESHOLD),
                              ("repeat_threshold", DEFAULT_REPEAT_THRESHOLD)):
            if type(result.get(key)) is not int or result[key] < 1000:
                result[key] = default
        return result
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return defaults


def save_config(state_dir, config):
    """Save context-fill configuration atomically."""
    from beyin_v3_releases import atomic_json
    atomic_json(_config_path(state_dir), config)


def _session_state_path(state_dir, session_digest):
    folder = _state_dir(state_dir)
    _unlinked(folder)
    return folder / f"{session_digest}.json"


def load_session_state(state_dir, session_digest):
    """Load per-session context-fill state."""
    path = _session_state_path(state_dir, session_digest)
    if not path.exists() or path.is_symlink():
        return dict(tokens=0, last_reminded_at=0, reminders_sent=0)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(tokens=0, last_reminded_at=0, reminders_sent=0)
        return {key: value for key, value in data.items()
                if key in ("tokens", "last_reminded_at", "last_reminded_tokens", "reminders_sent")
                and type(value) in (float, int) and value >= 0}
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return dict(tokens=0, last_reminded_at=0, reminders_sent=0)


def save_session_state(state_dir, session_digest, state):
    """Save per-session context-fill state atomically."""
    from beyin_v3_releases import atomic_json
    atomic_json(_session_state_path(state_dir, session_digest), state)


def _cleanup_old_states(state_dir):
    """Remove state files older than TTL or beyond max count."""
    state_dir = _state_dir(state_dir)
    if not state_dir.exists() or state_dir.is_symlink():
        return
    now = time.time()
    files = []
    for path in state_dir.glob("*.json"):
        if path.is_symlink() or not re.fullmatch(r"[a-f0-9]{24}\.json", path.name):
            continue
        try:
            if path.stat().st_mtime < now - STATE_TTL_SECONDS:
                path.unlink(missing_ok=True)
            else:
                files.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if len(files) > MAX_STATE_FILES:
        files.sort()
        for _, path in files[:-MAX_STATE_FILES]:
            path.unlink(missing_ok=True)


def _usage_tokens(usage):
    if not isinstance(usage, dict):
        return 0
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(usage[k] for k in keys if type(usage.get(k)) is int and usage[k] > 0)


def _extract_context_tokens(payload, transcript_session=None):
    """Read only the bounded tail of this session. Never persist transcript content."""
    filename = payload.get("transcript_path")
    if filename:
        from beyin_v3_shell_edits import _tail, _is_prompt
        path = Path(filename)
        if not path.is_file():
            return 0
        session = transcript_session or payload.get("session_id")
        tokens = 0
        for raw in reversed(_tail(filename) or ()):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict) or row.get("isSidechain") or row.get("sessionId") != session:
                continue
            if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
                break
            if row.get("type") == "user" and _is_prompt(row):
                content = row["message"].get("content")
                texts = ([content] if isinstance(content, str) else
                         [part.get("text", "") for part in content if isinstance(part, dict)])
                if any(isinstance(text, str) and "[kaydetme]" in text for text in texts):
                    return 0
                break
            if not tokens and row.get("type") == "assistant" and not row.get("isApiErrorMessage"):
                message = row.get("message")
                if isinstance(message, dict):
                    tokens = _usage_tokens(message.get("usage"))
        return tokens
    transcript = payload.get("transcript")
    if isinstance(transcript, list):
        for row in reversed(transcript):
            if isinstance(row, dict) and row.get("role") == "assistant" and not row.get("isSidechain"):
                return _usage_tokens(row.get("usage"))
    return _usage_tokens(payload.get("usage"))


def _session_digest(session_id):
    """Return a short digest for the session ID."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]


def _threshold(config, state):
    """Token count the next reminder waits for: the first threshold, then last reminder + repeat."""
    first_threshold = config.get("first_threshold", DEFAULT_FIRST_THRESHOLD)
    if state.get("reminders_sent", 0) == 0:
        return first_threshold
    repeat_threshold = config.get("repeat_threshold", DEFAULT_REPEAT_THRESHOLD)
    return state.get("last_reminded_tokens", first_threshold) + repeat_threshold


def _format_tokens(count):
    return f"{count:,}".replace(",", ".")


def reminder_reason(tokens, threshold):
    """Say what was reached, not that the window is full: auto-compaction can come long before."""
    return (f"Bağlam {_format_tokens(tokens)} tokene ulaştı (eşik {_format_tokens(threshold)}). "
            "Otomatik sıkıştırma pencere dolmadan gelebilir; önce devir kartını güncelle, "
            "sonra receipt yaz.")


def should_remind(state_dir, session_id, config, state):
    """Determine if a context-fill reminder should be shown.

    Returns (should_remind: bool, new_state: dict, reason: str or None)
    """
    if not config.get("enabled"):
        return False, state, None
    if os.environ.get("BEYIN_V3_NO_RECEIPT_REMINDER") == "1":
        return False, state, None

    tokens = state.get("tokens", 0)
    reminders_sent = state.get("reminders_sent", 0)
    threshold = _threshold(config, state)

    if tokens >= threshold:
        new_state = dict(state)
        new_state["last_reminded_tokens"] = tokens
        new_state["last_reminded_at"] = time.time()
        new_state["reminders_sent"] = reminders_sent + 1
        return True, new_state, "context_fill_threshold"

    return False, state, None


def context_fill_reminder(payload, state_dir, harness, event, *, transcript_session=None):
    """Main entry point for context-fill reminder at Stop event.

    Returns a dict with {"decision": "block", "reason": "..."} if reminder should fire,
    or None if no reminder needed.
    """
    # Only for claude harness at Stop event
    if harness != "claude" or event != "Stop":
        return None

    # Silencing conditions
    if os.environ.get("BEYIN_V3_NO_RECEIPT_REMINDER") == "1":
        return None
    if payload.get("stop_hook_active") is True:
        return None

    try:
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None

        # Load config
        config = load_config(state_dir)
        if not config["enabled"]:
            return None

        # Check for [kaydetme] opt-out in prompt (same as receipt_reminder)
        prompt = payload.get("prompt") or ""
        if "[kaydetme]" in str(prompt):
            return None

        # Extract context tokens from the last assistant message
        tokens = _extract_context_tokens(payload, transcript_session)
        if tokens == 0:
            return None

        # Load session state
        session_digest = _session_digest(session_id)
        folder = _state_dir(state_dir)
        _unlinked(folder)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _ledger_lock(folder, timeout=0, lock_name="writer.lock"):
            state = load_session_state(state_dir, session_digest)
            state["tokens"] = tokens
            threshold = _threshold(config, state)
            remind, new_state, _ = should_remind(state_dir, session_id, config, state)
            if not remind:
                return None
            save_session_state(state_dir, session_digest, new_state)
            _cleanup_old_states(state_dir)

        # Return blocking decision
        return {
            "decision": "block",
            "reason": reminder_reason(tokens, threshold)
        }

    except Exception:
        return None  # fail open


def reset_session_state(state_dir, session_id):
    """Reset context-fill state for a session (called on PreCompact, SessionEnd, SessionStart)."""
    session_digest = _session_digest(session_id)
    try:
        path = _session_state_path(state_dir, session_digest)
        if path.is_file():
            with _ledger_lock(path.parent, timeout=0.25, lock_name="writer.lock"):
                path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def get_doctor_status(state_dir):
    """Return status info for doctor output."""
    config = load_config(state_dir)
    if not config.get("enabled"):
        return None
    # Check for invalid config
    if config.get("first_threshold", 0) < 1000 or config.get("repeat_threshold", 0) < 1000:
        return {"status": "invalid", "error": "thresholds too low"}
    return {"enabled": True, **config}