#!/usr/bin/env python3
"""Project-local lifecycle adapter; persists metadata, never transcript text."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
sys.dont_write_bytecode = True
import time
import uuid

EVENTS = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "PreCompact", "SessionEnd"}
HOOK_BUDGET = 3.8  # seconds; installed POSIX hooks are killed at 5


def _default_state(vault: Path) -> Path:
    """Keep mutable state outside the vault, separated by canonical vault path."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    key = hashlib.sha256(str(vault.resolve()).encode()).hexdigest()[:16]
    return base / "beyin-v3" / key


def _is_foreign_or_invalid_state(pinned: str) -> bool:
    """Detect whether a pinned state string belongs to another OS or is unresolvable."""
    if not isinstance(pinned, str) or not pinned.strip():
        return True
    if sys.platform != 'win32':
        if re.match(r'^[a-zA-Z]:', pinned) or pinned.startswith('\\\\') or '\\' in pinned:
            return True
        try:
            p = Path(pinned).expanduser()
            if not p.is_absolute():
                return True
            parts = p.parts
            if len(parts) >= 3 and parts[1] in ('Users', 'home') and not Path(*parts[:3]).exists():
                return True
        except Exception:
            return True
    else:
        if pinned.startswith('/') or not (re.match(r'^[a-zA-Z]:[/\\]', pinned) or pinned.startswith('\\\\')):
            return True
        try:
            p = Path(pinned).expanduser()
            if not p.is_absolute():
                return True
            parts = p.parts
            if len(parts) >= 3 and parts[1].lower() == 'users' and not Path(*parts[:3]).exists():
                return True
        except Exception:
            return True
    return False

RECEIPT_REMINDER = (
    "Files were edited in this session but no receipt was written after the edits. If the work is done, write one now: "
    "python3 beyin.py receipt --file RECEIPT_JSON --harness {harness}. "
    "Receipt session={session}; put this value in the JSON session field so the receipt closes this checkpoint. "
    "If the work produced a lasting learning, distill it under knowledge/concepts/ before the receipt and list that note in refs."
)
KNOWLEDGE_REMINDER = (
    "Learnings were reported in the receipt but no note under knowledge/ was updated in this session. "
    "Distill lasting learnings into knowledge/concepts/<name>.md (or update an existing concept, then sync); "
    "if no permanent note is required, state that in one sentence to proceed."
)
HARNESS_SYNTHETIC_PROMPT_PREFIXES = (
    "<task-notification>",
    "Another Claude session sent a message:",
    "<agent-message",
    "<local-command-caveat>",
    "<command-name>",
    "<local-command-stdout>",
)
# Claude Code turn origins that no human typed. Its hook payload carries no origin field today (2.1.285), so
# the prefixes above do the work; if one appears, other kinds (channel, bridge, remote, ...) can be a person
# typing elsewhere, so only these known kinds skip retrieval and anything else falls back to the prefix check.
SYNTHETIC_ORIGIN_KINDS = frozenset({"task-notification", "peer", "coordinator"})


def is_synthetic_prompt(payload):
    """Detect automated, subagent, or notification turns in UserPromptSubmit.

    Prevents expensive search retrieval and irrelevant context injection on
    non-human turns (Claude Code subagent completions, peer agent messages,
    background task notifications).
    """
    if not isinstance(payload, dict):
        return False
    if os.environ.get("BEYIN_V3_FILTER_HARNESS_TURNS") == "0":
        return False
    origin = payload.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else origin
    if kind == "human":
        return False
    if isinstance(kind, str) and kind in SYNTHETIC_ORIGIN_KINDS:
        return True
    return prompt_text(payload).lstrip().startswith(HARNESS_SYNTHETIC_PROMPT_PREFIXES)


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def output_context(harness, event, text):
    # OpenCode's vault plugin parses the Claude/Codex shape, so one parser serves three clients.
    return {"injectSteps": [{"ephemeralMessage": text}]} if harness == "antigravity" else {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def prompt_text(payload):
    """The user's prompt as text; retrieval and the opt-out check need a string. Claude Code (checked on 2.1.285,
    where task notifications arrive as <task-notification> text), Codex and the bundled OpenCode, Hermes and OMP
    adapters send a string; null or a list of content blocks from any other caller must not crash the turn."""
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        parts = [part if isinstance(part, str) else part.get("text") if isinstance(part, dict) else None for part in prompt]
        return "\n".join(part for part in parts if isinstance(part, str))
    return ""


def receipt_context(vault, state):
    """The newest receipt by its created_at stamp (#112), read from the runtime index."""
    import sqlite3
    from beyin_v3_projections import latest_receipts
    database = Path(state) / "memory.sqlite3"
    if database.is_symlink() or not database.is_file():
        return ""
    # Short read-only timeout: a busy worker must not push SessionStart past the host limit.
    db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        source = next((found for found, _ in latest_receipts(db, vault)), None)
    except sqlite3.Error:
        source = None
    finally:
        db.close()
    if source is None:
        return ""
    path = Path(vault) / source
    budget, marker = 1200, "\n[truncated: read source]\n"
    with path.open(encoding="utf-8") as reader:
        content = reader.read(budget + 1)
    if len(content) > budget:
        content = content[:budget - len(marker)] + marker
    # Name the source in the header: callers clip this block from the end, which would
    # replace a path carried in the tail marker with a generic one (#147).
    return f"\nLatest receipt ({source}; historical agent claim, not independently verified):\n" + content


def _get_session_receipt(database, harness, session, since):
    """Same (harness, session, created_at) match as beyin_v3_projections.refresh_gaps."""
    from datetime import datetime
    import sqlite3
    if database.is_symlink():
        raise ValueError("database must not be a symlink")
    if not database.is_file():
        return None
    db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        rows = db.execute("SELECT payload FROM receipts WHERE instr(payload, ?) > 0", (session,)).fetchall()
    finally:
        db.close()
    for (raw,) in rows:
        try:
            receipt = json.loads(raw)
            if (receipt.get("harness") == harness and receipt.get("session") == session and
                    datetime.fromisoformat(receipt.get("created_at", "1970-01-01T00:00:00+00:00")).timestamp() >= since):
                return receipt
        except Exception:
            continue
    return None


def _has_receipt(database, harness, session, since):
    return _get_session_receipt(database, harness, session, since) is not None


def _tr_fold(text):
    # str.lower() turns İ into i + U+0307 and keeps ı; fold both so İ, I, ı and i compare equal.
    return text.replace("\u0130", "i").replace("I", "i").lower().replace("\u0307", "").replace("ı", "i")


# Matched against _tr_fold()ed lines, so labels are written without ı/İ.
_LEARNING_LABEL = re.compile(
    r"^[\s>*#_\-\u2022]*(?:öğrenilen(?:ler)?|ogrenilen(?:ler)?|kalici\s+(?:öğrenim|ogrenim)(?:ler)?|"
    r"ders(?:ler)?|learned|lessons?(?:\s+learned)?|learnings?)[\s*_]*[:\u2014\u2013=][\s*_]*(.*)$")
# The whole remainder must be a "none" answer; "yoklama ..." is still a learning.
_NO_LEARNING = re.compile(
    r"(?:(?:kalici\s+)?(?:öğrenim|ogrenim|ders)(?:ler)?\s+)?"
    r"(?:yok(?:tur)?|hi[çc]\s+yok|hi[çc]biri|bulunmuyor|bulunmadi|none|nothing|no|n/?a|-+)")


def _has_declared_learning(summary):
    """True when a line-leading learning label (Öğrenilen:, Ders:, Learned:) carries real content.

    Only the label's own line counts: an empty "Öğrenilen:" must not borrow the next section,
    and "machine learning:" or "ders-plan" in running text is not a declaration.
    """
    if not isinstance(summary, str):
        return False
    for line in summary.splitlines():
        match = _LEARNING_LABEL.match(_tr_fold(line).strip())
        if match:
            content = match.group(1).strip().strip(".!*_` ").strip()
            if content and not _NO_LEARNING.fullmatch(content):
                return True
    return False


# Generated views and the V2 compiler seeds change without any agent distilling.
_NOT_DISTILLATION = ("knowledge/v3/", "knowledge/index.md", "knowledge/log.md")


def _is_distilled_note(relative):
    relative = relative.replace("\\", "/")
    return (relative.startswith("knowledge/") and relative.endswith(".md") and
            not any(relative == item or relative.startswith(item) for item in _NOT_DISTILLATION))


def _has_knowledge_update(vault, receipt, since):
    if not receipt:
        return False
    if any(isinstance(ref, str) and _is_distilled_note(ref) for ref in receipt.get("refs", [])):
        return True
    k_dir = Path(vault) / "knowledge" if vault else None
    if k_dir is None or not k_dir.is_dir():
        return False
    for path in k_dir.rglob("*.md"):
        try:
            if (path.is_file() and not path.is_symlink() and path.stat().st_mtime >= since and
                    _is_distilled_note(path.relative_to(vault).as_posix())):
                return True
        except (ValueError, OSError):
            continue
    return False


def receipt_reminder(payload, state, harness, event, vault=None):
    """Track edits per session and return a one-time Stop block, or None.

    Installed PostToolUse hooks match only Edit|Write|apply_patch for Claude and
    Codex, so that event marks the session as edited without reading transcripts.
    Stop then looks for a receipt of this harness and session in the runtime store.
    If a receipt declared learnings but no note under knowledge/ was touched,
    Stop reminds once to distill into knowledge/concepts/.
    """
    if harness not in ("claude", "codex") or event not in ("UserPromptSubmit", "PostToolUse", "Stop"):
        return None
    if os.environ.get("BEYIN_V3_NO_RECEIPT_REMINDER") == "1":
        return None
    try:
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        folder = Path(state) / "receipt-reminders"
        done, edited = folder / (digest + ".done"), folder / (digest + ".edited")
        kdone = folder / (digest + ".kdone")
        if done.exists() and kdone.exists():
            return None
        if event == "UserPromptSubmit":
            if "[kaydetme]" in prompt_text(payload):
                folder.mkdir(parents=True, exist_ok=True)
                done.touch()
                kdone.touch()
                edited.unlink(missing_ok=True)
            return None
        if event == "PostToolUse":
            if not edited.exists():
                atomic(edited, {"at": time.time()})
            return None
        if payload.get("stop_hook_active") is True or not edited.exists():
            return None
        since = float(json.loads(edited.read_text(encoding="utf-8"))["at"])
        session = digest[:24]  # the queued checkpoint's session value
        db_path = Path(state) / "memory.sqlite3"
        receipt = _get_session_receipt(db_path, harness, session, since)
        if not receipt:
            if not done.exists():
                # Keep the edit window: a receipt written in answer to this reminder arrives
                # during the stop_hook_active Stop, so only the next Stop can check its learning.
                folder.mkdir(parents=True, exist_ok=True)
                with done.open("x", encoding="utf-8"):  # FileExistsError if a concurrent Stop reminded first
                    pass
                return {"decision": "block", "reason": RECEIPT_REMINDER.format(harness=harness, session=session)}
            return None

        # Receipt exists! Check if receipt declared learning but knowledge/ was not updated.
        if not kdone.exists() and _has_declared_learning(receipt.get("summary", "")) and not _has_knowledge_update(vault, receipt, since):
            folder.mkdir(parents=True, exist_ok=True)
            try:
                with kdone.open("x", encoding="utf-8"):
                    pass
            except FileExistsError:
                return None
            return {"decision": "block", "reason": KNOWLEDGE_REMINDER}

        edited.unlink(missing_ok=True)  # later edits open a new window
        return None
    except Exception:
        return None  # fail open: never block Stop on a bookkeeping error


def enqueue_event(vault, state, payload, harness):
    state = Path(state)
    metadata = {"event": payload.get("hook_event_name"), "harness": harness,
                "session": hashlib.sha256(str(payload.get("session_id", "unknown")).encode()).hexdigest()[:24]}
    # Keep a bounded label and opaque identity, never the full project path.
    from beyin_v3_bridge import origin
    project = origin(payload, harness)
    if project:
        metadata.update(project)
    if payload.get('no_memory') is True:
        metadata['no_memory'] = True
    # Hermes and OpenCode deliver the first user prompt as SessionStart; keep only the fact, never the text.
    if isinstance(payload.get('prompt'), str) and payload['prompt'].strip():
        metadata['prompted'] = True
    identity = str(payload.get("event_id") or uuid.uuid4().hex)
    key = hashlib.sha256(identity.encode()).hexdigest()
    path = state / "hook-queue" / (key + ".json")
    if not path.exists() and not (state / "hook-done" / (key + ".json")).exists():
        atomic(path, dict(metadata, at=time.time()))
    return key


def drain_queue(vault, state):
    state = Path(state)
    from beyin_v3_sync import SyncEngine
    pending = list((state / "hook-queue").glob("*.json"))
    try:
        if (state / 'v3-install.json').exists():
            from beyin_v3_companion import initialize
            initialize(vault, state)
        engine = SyncEngine(vault, state)
        from beyin_v3_projections import record_checkpoints
        record_checkpoints(engine, [json.loads(path.read_text(encoding='utf-8')) for path in pending if path.exists()])
        result = engine.sync()
        from beyin_v3_secrets import health as secret_filter_health
        result['secrets_redacted'] = secret_filter_health(state)['total']
        gap_path = state/'receipt-gaps.json'
        if gap_path.exists():
            result['potential_missing_receipts'] = json.loads(gap_path.read_text(encoding='utf-8'))['potential_missing_receipts']
        from beyin_v3_skills import sync_skills
        skills = sync_skills(vault, state)
        # Skill mirroring owns no queued event, so its outcome is reported for
        # attention but never withholds acknowledgement of drained source events.
        if skills.get("conflicts"):
            result = dict(result, skill_conflicts=skills["conflicts"])
        if skills.get("unmanaged"):
            result = dict(result, skill_unmanaged=skills["unmanaged"])
    except Exception as exc:
        atomic(state / "hook-error.json", {"at": time.time(), "error": type(exc).__name__})
        return {"processed": 0, "failed": len(pending), "pending": len(pending)}
    atomic(state / "hook-health.json", {"at": time.time(), "sync": result})
    # A degraded scan is complete but excluded one or more invalid sources. The
    # fresh healthy subset may be injected with an explicit warning. Source
    # conflicts still block acknowledgement because ownership is ambiguous.
    if result.get("status") in ("ok", "succeeded", "synced", "degraded") and not result.get("conflicts"):
        (state / "hook-error.json").unlink(missing_ok=True)
        processed = 0
        for path in pending:
            (state / "hook-done").mkdir(parents=True, exist_ok=True)
            try:
                os.replace(path, state / "hook-done" / path.name)
            except FileNotFoundError:
                continue  # Another worker already atomically acknowledged this event.
            processed += 1
        return {"processed": processed, "failed": 0, "pending": len(list((state / "hook-queue").glob("*.json")))}
    return {"processed": 0, "failed": len(pending), "pending": len(pending)}


def main():
    started = time.monotonic()
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--harness", choices=("codex", "claude", "antigravity", "hermes", "opencode", "omp"), required=True)
    parser.add_argument("--event")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--drain-queue", action="store_true")
    parser.add_argument("--metadata-only", action="store_true", help="Queue lifecycle metadata without injecting vault context")
    args = parser.parse_args()
    vault_str = str(args.vault)
    if _is_foreign_or_invalid_state(vault_str) or not args.vault.is_dir():
        if (Path.cwd() / ".claude/scripts/beyin_v3_hook.py").is_file():
            vault = Path.cwd().resolve()
        else:
            vault = Path(__file__).resolve().parents[2]
    else:
        vault = args.vault.resolve()
    state_str = str(args.state)
    if _is_foreign_or_invalid_state(state_str):
        state = _default_state(vault).resolve()
    else:
        state = args.state.resolve()
    if state == vault or vault in state.parents:
        raise ValueError("Runtime state must be outside vault")
    os.umask(0o077)
    from beyin_v3_preferences import read, claim_check
    if args.worker or args.drain_queue:
        if args.worker and not read(vault)['auto_sync']:
            print(json.dumps({'processed': 0, 'failed': 0, 'paused': True}))
            return
        result = drain_queue(vault, state)
        print(json.dumps(result))
        if result["failed"]:
            raise SystemExit(1)
        return
    notice = ''
    try:
        payload = json.loads(sys.stdin.read(1_000_000) or "{}")
        event = payload.get("hook_event_name", args.event)
        if args.harness == "hermes":
            # Hermes plugin hooks: pre_llm_call (first turn -> SessionStart, later ->
            # UserPromptSubmit), on_session_finalize -> SessionEnd. The plugin sends
            # the Claude-shaped payload, so only the session field needs mapping.
            payload["session_id"] = payload.get("session_id") or payload.get("conversationId", "unknown")
        if args.harness == "antigravity":
            if event == "PreInvocation":
                if payload.get("invocationNum") != 0:
                    print("{}")
                    return
                event = "SessionStart"
            elif event == "Stop" and payload.get("fullyIdle") is not True:
                print('{"decision":"stop"}')
                return
            payload["session_id"] = payload.get("conversationId", "unknown")
        payload["hook_event_name"] = event
        if event not in EVENTS or os.environ.get("BEYIN_V3_INTERNAL") or os.environ.get("BEYIN_V3_SKIP") == "1" or payload.get('no_memory') is True:
            print("{}")
            return
        if event == 'SessionStart' and not args.metadata_only:
            from beyin_v3_releases import session_start
            notice = session_start(vault, state)
        settings = read(vault)
        if not args.metadata_only and event in ('SessionStart', 'PostToolUse') and (state / 'hygiene.json').is_file():
            # Opt-in hygiene signals (#130), machine-local in state/hygiene.json. Independent of the
            # performance profile, and never able to cost the session or the turn itself.
            try:
                from beyin_v3_hygiene import read_settings, folder_questions, touch_log, hook_cap_warning
                hygiene = read_settings(state)[0]
                if event == 'SessionStart' and hygiene['folder_questions']:
                    questions = folder_questions(vault, state)
                    if questions:
                        # ASCII Turkish; a question is data for the agent, never exact user knowledge.
                        notice += 'Soru sirasi (bilgi): ' + ' | '.join(questions) + '\n'
                elif event == 'PostToolUse':
                    if hygiene['promotion']:
                        touch_log(state, vault, payload)
                    if hygiene['word_cap_warning']:
                        # Harness-gated inside: only Claude and Codex receive PostToolUse context.
                        notice += hook_cap_warning(vault, payload, cap=hygiene['max_words'],
                                                   harness=args.harness, state=state)
            except Exception:
                pass
        if not args.metadata_only:
            # Opt-in parallel-session notice (#170), machine-local in state/parallel-sessions.json.
            # Without that file a prompt costs one stat; SessionEnd still removes its own marker.
            try:
                if event == 'SessionEnd':
                    if (state / 'session-markers').is_dir():
                        from beyin_v3_parallel import end
                        end(state, args.harness, payload.get('session_id'))
                elif (event == 'UserPromptSubmit' or (event == 'SessionStart' and args.harness in ('hermes', 'opencode', 'antigravity'))) \
                        and (state / 'parallel-sessions.json').is_file() and not is_synthetic_prompt(payload):
                    from beyin_v3_parallel import enabled, touch
                    if enabled(state):
                        notice += touch(state, args.harness, payload.get('session_id'), vault=vault)
            except Exception:
                pass  # a marker can never cost the turn
        if not settings['auto_sync']:
            print(json.dumps(output_context(args.harness, event, notice)) if notice else ('{"decision":"stop"}' if args.harness == 'antigravity' else '{}'))
            return
        enqueue_event(vault, state, payload, args.harness)
        command = [sys.executable, str(Path(__file__).resolve()), "--vault", str(vault),
                   "--state", str(state), "--harness", args.harness, "--worker"]
        options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                   "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            options["start_new_session"] = True
        disabled = os.environ.get("BEYIN_V3_NO_SPAWN") == "1"
        due = False if disabled else claim_check(state, settings, event)
        process = subprocess.Popen(command, **options) if due else None
        # After enqueue, so reminder bookkeeping can never cost the queued checkpoint.
        # The global bridge (--metadata-only) discards stdout, so it keeps no reminder state.
        reminder = None if args.metadata_only else receipt_reminder(payload, state, args.harness, event, vault=vault)
        inject = settings['context_mode'] == 'turn' or (settings['context_mode'] == 'session' and event == 'SessionStart')
        if not inject or args.metadata_only or (event == 'UserPromptSubmit' and is_synthetic_prompt(payload)):
            print(json.dumps(reminder) if reminder else (json.dumps(output_context(args.harness, event, notice)) if notice else ('{"decision":"stop"}' if args.harness == 'antigravity' else '{}')))
            return
        if event in ("SessionStart", "UserPromptSubmit"):
            if not due and not disabled:
                print(json.dumps(output_context(args.harness, event, notice + 'V3 automatic check deferred by your interval preference. Read current sources or use beyin.py context for fresh information.')))
                return
            try:
                if process is not None:
                    process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                print(json.dumps(output_context(args.harness, event, notice + "V3 source sync is pending. Verify current Markdown sources before using prior context.")))
                return
            if process is not None and process.returncode:
                raise RuntimeError("Source sync failed; metadata remains queued")
            from beyin_v3_sync import SyncEngine
            store = SyncEngine(vault, state).store
            query = prompt_text(payload)
            project = payload.get('project')
            project = project if isinstance(project, str) and project.strip() else None
            session = hashlib.sha256(str(payload.get('session_id', 'unknown')).encode()).hexdigest()[:24]
            warning = ''
            health = state / "hook-health.json"
            if health.exists():
                sync = json.loads(health.read_text(encoding="utf-8")).get("sync", {})
                if sync.get("status") not in ("ok", "succeeded", "synced"):
                    warning += "V3 sync needs attention; consult current sources and doctor.\n"
                if sync.get('skill_conflicts'):
                    warning += 'Shared skills differ between harnesses; both versions are preserved. Run doctor before trusting skill text.\n'
                if sync.get('potential_missing_receipts'):
                    warning += 'Prior checkpoints may lack structured receipts; check Last-Session/Threads and current sources for unfinished work.\n'
            from beyin_v3_companion import (context as companion_context, relevant, opening_budget, client_budget,
                                            client_rebudget, fit_client)
            # Both budgets stop below a client's own cut-off; past it the client files the text away (#175).
            # A text over the client's own measure is rendered once more with a scaled budget.
            limit = client_budget(args.harness, settings['context_chars'])
            receipt = receipt_context(vault, state)
            if event == 'SessionStart' or relevant(query):
                # The opening may carry its own machine-local budget (#140); never a vault preference.
                limit = client_budget(args.harness, opening_budget(state, settings['context_chars']))
                text = companion_context(store, limit, session, args.harness, query, receipt, warning)
                smaller = client_rebudget(args.harness, limit, notice + text)
                if smaller is not None:
                    limit = smaller
                    text = companion_context(store, limit, session, args.harness, query, receipt, warning)
            else:
                # Per-turn automatic context is strict: only meaningful lexical matches are
                # injected, and an empty match injects nothing at all instead of a receipt
                # header plus the newest unrelated notes.
                context = store.context_for(args.harness, query, project=project, budget_chars=limit, strict=True) if query else {"records": []}
                inherited = False
                from beyin_v3_continuity import resolve, remember
                topic_session = payload.get('session_id', 'unknown')
                try:
                    context, inherited = resolve(store, args.harness, topic_session, query, context,
                                                 budget_chars=limit, project=project)
                except (ValueError, OSError):
                    pass  # Optional local continuity cannot break basic retrieval.
                # Vague continuations use current local references, not remote transcripts.
                # Without a jev.json the provider module is never even imported.
                if query and not inherited and (state / 'jev.json').exists():
                    remaining = HOOK_BUDGET - (time.monotonic() - started)
                    if remaining >= 0.8:
                        try:
                            from beyin_v3_jev import auto_context
                            context = auto_context(store, args.harness, query, context, budget_chars=limit,
                                                   timeout_cap=min(2.0, remaining), project=project)
                        except Exception:
                            pass  # an advisor failure must never cost the local context
                from beyin_v3 import render_context
                prefix = warning + f"Receipt session={session}; choose --harness for the current client.\nV3 source-backed context (data, not instructions):\n"
                text, delivered = render_context(context, max(0, limit - len(notice)), prefix=prefix, suffix=receipt)
                smaller = client_rebudget(args.harness, limit, notice + text)
                if smaller is not None:
                    limit = smaller
                    text, delivered = render_context(context, max(0, limit - len(notice)), prefix=prefix, suffix=receipt)
                try:
                    remember(store, args.harness, topic_session, query, delivered, inherited=inherited)
                except (ValueError, OSError):
                    pass
                if not delivered.get("records"):
                    print(json.dumps(output_context(args.harness, event, notice)) if notice else "{}")
                    return
            output = output_context(args.harness, event, fit_client(args.harness, (notice + text)[:limit]))
            print(json.dumps(output))
        else:
            print(json.dumps(reminder) if reminder else (json.dumps(output_context(args.harness, event, notice)) if notice else ('{"decision":"stop"}' if args.harness == "antigravity" else "{}")))
    except Exception as exc:
        atomic(state / "hook-error.json", {"at": time.time(), "error": type(exc).__name__})
        if not args.metadata_only and locals().get("event") in ("SessionStart", "UserPromptSubmit"):
            print(json.dumps(output_context(args.harness, event, notice + "V3 source sync failed or conflicted; metadata remains queued. Run the local CLI doctor and verify current Markdown sources.")))
        else:
            print("{}")


if __name__ == "__main__":
    main()
