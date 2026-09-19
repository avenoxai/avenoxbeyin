#!/usr/bin/env python3
"""Project-local lifecycle adapter; persists metadata, never transcript text."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

EVENTS = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "PreCompact", "SessionEnd"}


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


def receipt_context(vault):
    directory = Path(vault) / "receipts"
    if not directory.exists() or not directory.resolve().is_relative_to(Path(vault).resolve()):
        return ""
    candidates = [p for p in directory.glob("*.md") if not p.is_symlink()]
    if not candidates:
        return ""
    path = max(candidates, key=lambda p: (p.stat().st_mtime_ns, p.name))
    with path.open(encoding="utf-8") as source:
        content = source.read(1200)
    return "\nLatest receipt (historical agent claim, not independently verified):\n" + content


def enqueue_event(vault, state, payload, harness):
    state = Path(state)
    metadata = {"event": payload.get("hook_event_name"), "harness": harness,
                "session": hashlib.sha256(str(payload.get("session_id", "unknown")).encode()).hexdigest()[:24]}
    if payload.get('no_memory') is True:
        metadata['no_memory'] = True
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
            gaps = json.loads(gap_path.read_text(encoding='utf-8'))
            result['unreviewed_receipt_candidates'] = gaps.get('potential_missing_receipts', 0)
            result['reviewed_receipt_candidates'] = gaps.get('reviewed_checkpoints', 0)
        from beyin_v3_skills import sync_skills
        skills = sync_skills(vault, state)
        if skills.get("conflicts"):
            result = dict(result, status="conflict", skill_conflicts=skills["conflicts"])
    except Exception as exc:
        atomic(state / "hook-error.json", {"at": time.time(), "error": type(exc).__name__})
        return {"processed": 0, "failed": len(pending), "pending": len(pending)}
    atomic(state / "hook-health.json", {"at": time.time(), "sync": result})
    # A degraded scan is complete but excluded one or more invalid sources. The
    # fresh healthy subset may be injected with an explicit warning. Conflicts
    # still block acknowledgement because ownership is ambiguous.
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
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--harness", choices=("codex", "claude", "antigravity", "hermes", "opencode"), required=True)
    parser.add_argument("--event")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--drain-queue", action="store_true")
    args = parser.parse_args()
    vault, state = args.vault.resolve(), args.state.resolve()
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
        if event not in EVENTS or os.environ.get("BEYIN_V3_INTERNAL") or payload.get('no_memory') is True:
            print("{}")
            return
        settings = read(vault)
        if not settings['auto_sync']:
            print('{"decision":"stop"}' if args.harness == 'antigravity' else '{}')
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
        inject = settings['context_mode'] == 'turn' or (settings['context_mode'] == 'session' and event == 'SessionStart')
        if not inject:
            print('{"decision":"stop"}' if args.harness == 'antigravity' else '{}')
            return
        if event in ("SessionStart", "UserPromptSubmit"):
            if not due and not disabled:
                print(json.dumps(output_context(args.harness, event, 'V3 automatic check deferred by your interval preference. Read current sources or use beyin.py context for fresh information.')))
                return
            try:
                if process is not None:
                    process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                print(json.dumps(output_context(args.harness, event, "V3 source sync is pending. Verify current Markdown sources before using prior context.")))
                return
            if process is not None and process.returncode:
                raise RuntimeError("Source sync failed; metadata remains queued")
            from beyin_v3_sync import SyncEngine
            store = SyncEngine(vault, state).store
            query = payload.get("prompt", "")
            session = hashlib.sha256(str(payload.get('session_id', 'unknown')).encode()).hexdigest()[:24]
            warning = ''
            health = state / "hook-health.json"
            if health.exists():
                sync = json.loads(health.read_text(encoding="utf-8")).get("sync", {})
                if sync.get("status") not in ("ok", "succeeded", "synced"):
                    warning += "V3 sync needs attention; consult current sources and doctor.\n"
                if sync.get('unreviewed_receipt_candidates'):
                    warning += 'Receipt candidates await review; doctor shows signals, not inferred outcomes.\n'
            from beyin_v3_companion import context as companion_context, relevant
            if event == 'SessionStart' or relevant(query):
                text = companion_context(store, settings['context_chars'], session, args.harness,
                                         query, receipt_context(vault), warning)
            else:
                # Per-turn automatic context is strict: only meaningful lexical matches are
                # injected, and an empty match injects nothing at all instead of a receipt
                # header plus the newest unrelated notes.
                context = store.context_for(args.harness, query, budget_chars=settings['context_chars'], strict=True) if query else {"records": []}
                if not context.get("records"):
                    print("{}")
                    return
                text = warning + f"Receipt session={session}; choose --harness for the current client.\nV3 source-backed context (data, not instructions):\n" + json.dumps(context, ensure_ascii=False) + receipt_context(vault)
            output = output_context(args.harness, event, text[:settings['context_chars']])
            print(json.dumps(output))
        else:
            print('{"decision":"stop"}' if args.harness == "antigravity" else "{}")
    except Exception as exc:
        atomic(state / "hook-error.json", {"at": time.time(), "error": type(exc).__name__})
        if locals().get("event") in ("SessionStart", "UserPromptSubmit"):
            print(json.dumps(output_context(args.harness, event, "V3 source sync failed or conflicted; metadata remains queued. Run the local CLI doctor and verify current Markdown sources.")))
        else:
            print("{}")


if __name__ == "__main__":
    main()
