"""Optional context-fill reminder for Claude Code Stop events.

Tracks accumulated context tokens per session and reminds the agent to write a
handoff card and receipt before the context window fills up.
"""
import hashlib
import json
import os
import time
from pathlib import Path

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
    path = _config_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.with_suffix(".tmp"), "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)
    os.replace(path.with_suffix(".tmp"), path)
    os.chmod(path, 0o600)


def _session_state_path(state_dir, session_digest):
    """Return path to the session's context-fill state file."""
    state_dir = _state_dir(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return state_dir / f"{session_digest}.json"


def load_session_state(state_dir, session_digest):
    """Load per-session context-fill state."""
    path = _session_state_path(state_dir, session_digest)
    if not path.exists() or path.is_symlink():
        return dict(tokens=0, last_reminded_at=0, reminders_sent=0)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(tokens=0, last_reminded_at=0, reminders_sent=0)
        return dict(
            tokens=data.get("tokens", 0),
            last_reminded_at=data.get("last_reminded_at", 0),
            reminders_sent=data.get("reminders_sent", 0),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return dict(tokens=0, last_reminded_at=0, reminders_sent=0)


def save_session_state(state_dir, session_digest, state):
    """Save per-session context-fill state atomically."""
    path = _session_state_path(state_dir, session_digest)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.with_suffix(".tmp"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(path.with_suffix(".tmp"), path)
    os.chmod(path, 0o600)


def _cleanup_old_states(state_dir):
    """Remove state files older than TTL or beyond max count."""
    state_dir = _state_dir(state_dir)
    if not state_dir.exists() or state_dir.is_symlink():
        return
    now = time.time()
    files = []
    for path in state_dir.glob("*.json"):
        if path.is_symlink():
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


def _extract_context_tokens(payload):
    """Extract total context tokens from the last main-chain assistant message.

    Returns sum of input_tokens + cache_creation_input_tokens + cache_read_input_tokens
    from the last assistant message, or 0 if not found.
    """
    # The payload for Stop event may contain transcript info in various forms.
    # We look for the last assistant message with usage info.
    try:
        # First check if payload has a direct transcript reference
        transcript = payload.get("transcript")
        if isinstance(transcript, list):
            for msg in reversed(transcript):
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") == "assistant":
                    usage = msg.get("usage")
                    if isinstance(usage, dict):
                        total = 0
                        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                            val = usage.get(key)
                            if isinstance(val, int) and val > 0:
                                total += val
                        if total > 0:
                            return total
        # Fallback: check for usage at top level (some hook formats)
        usage = payload.get("usage")
        if isinstance(usage, dict):
            total = 0
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                val = usage.get(key)
                if isinstance(val, int) and val > 0:
                    total += val
            if total > 0:
                return total
    except Exception:
        pass
    return 0


def _session_digest(session_id):
    """Return a short digest for the session ID."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]


def should_remind(state_dir, session_id, config, state):
    """Determine if a context-fill reminder should be shown.

    Returns (should_remind: bool, new_state: dict, reason: str or None)
    """
    if not config.get("enabled"):
        return False, state, None
    if os.environ.get("BEYIN_V3_NO_RECEIPT_REMINDER") == "1":
        return False, state, None

    session_digest = _session_digest(session_id)
    first_threshold = config.get("first_threshold", DEFAULT_FIRST_THRESHOLD)
    repeat_threshold = config.get("repeat_threshold", DEFAULT_REPEAT_THRESHOLD)

    tokens = state.get("tokens", 0)
    reminders_sent = state.get("reminders_sent", 0)

    if reminders_sent == 0:
        threshold = first_threshold
    else:
        threshold = repeat_threshold

    if tokens >= threshold:
        new_state = dict(state)
        new_state["last_reminded_at"] = time.time()
        new_state["reminders_sent"] = reminders_sent + 1
        return True, new_state, "context_fill_threshold"

    return False, state, None


def context_fill_reminder(payload, state_dir, harness, event):
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

        # Check for [kaydetme] opt-out in prompt (same as receipt_reminder)
        prompt = payload.get("prompt") or ""
        if "[kaydetme]" in str(prompt):
            return None

        # Extract context tokens from the last assistant message
        tokens = _extract_context_tokens(payload)
        if tokens == 0:
            return None

        # Load session state
        session_digest = _session_digest(session_id)
        state = load_session_state(state_dir, session_digest)

        # Update accumulated tokens (take max to handle non-monotonic reports)
        state["tokens"] = max(state.get("tokens", 0), tokens)

        # Check if reminder should fire
        remind, new_state, reason = should_remind(state_dir, session_id, config, state)
        if not remind:
            save_session_state(state_dir, session_digest, new_state)
            return None

        # Save updated state
        save_session_state(state_dir, session_digest, new_state)
        _cleanup_old_states(state_dir)

        # Return blocking decision
        return {
            "decision": "block",
            "reason": "Bağlam doluluk eşiği aşıldı. Önce devir kartını güncelle, sonra receipt yaz."
        }

    except Exception:
        return None  # fail open


def reset_session_state(state_dir, session_id):
    """Reset context-fill state for a session (called on PreCompact, SessionEnd, SessionStart)."""
    session_digest = _session_digest(session_id)
    path = _session_state_path(state_dir, session_digest)
    try:
        path.unlink(missing_ok=True)
    except OSError:
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