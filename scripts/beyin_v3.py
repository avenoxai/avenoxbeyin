#!/usr/bin/env python3
"""Local-first CLI for the shared V3 memory foundation."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys


def default_state(vault: Path) -> Path:
    """Keep mutable state outside the vault, separated by canonical vault path."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    key = hashlib.sha256(str(vault).encode()).hexdigest()[:16]
    return base / "beyin-v3" / key


def load_engine():
    adjacent = Path(__file__).resolve().parent / "beyin_v3.py"
    path = adjacent if adjacent != Path(__file__).resolve() and adjacent.exists() else Path(__file__).resolve().parents[1] / "template/.claude/scripts/beyin_v3.py"
    spec = importlib.util.spec_from_file_location("beyin_v3_shared_engine", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Shared V3 engine could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_sync():
    adjacent = Path(__file__).resolve().parent
    directory = adjacent if (adjacent / "beyin_v3_sync.py").exists() else Path(__file__).resolve().parents[1] / "template/.claude/scripts"
    sys.path.insert(0, str(directory))
    from beyin_v3_sync import SyncEngine
    return SyncEngine


def read_json(filename: str):
    if filename == "-":
        return json.load(sys.stdin)
    with Path(filename).open(encoding="utf-8") as stream:
        return json.load(stream)


def load_skills():
    load_sync()
    import beyin_v3_skills
    return beyin_v3_skills


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--vault", required=True, type=Path, help="Existing vault directory")
    root.add_argument("--state", type=Path, help="Local state directory outside the vault")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize local state; installs no hooks or services")
    sub.add_parser("sync", help="Reconcile Markdown sources into local state")
    sub.add_parser("skill-sync", help="Reconcile project-local shared skills")
    sub.add_parser("doctor", help="Read local hook health and pending metadata counts")
    settings = sub.add_parser("preferences", help="Control automatic local checks and injected context")
    settings.add_argument("--profile", choices=("normal", "economical", "manual"))
    settings.add_argument("--auto-sync", choices=("on", "off"))
    settings.add_argument("--interval-minutes", type=int)
    settings.add_argument("--context-mode", choices=("turn", "session", "off"))
    settings.add_argument("--context-chars", type=int)
    settings.add_argument("--secret-filter", choices=("on", "off"))
    skill = sub.add_parser("skill-import", help="Import one explicitly chosen skill directory")
    skill.add_argument("--source", type=Path, required=True)
    skill.add_argument("--name")
    ingest = sub.add_parser("ingest", help="Ingest one JSON record or a JSON list")
    ingest.add_argument("--file", default="-", help="JSON input path, or - for stdin")
    note = sub.add_parser("note-create", help="Create a semantic Markdown source without overwriting")
    note.add_argument("--file", default="-", help="JSON {source, text, metadata}")
    task = sub.add_parser("task-create", help="Create and verify an explicit new task")
    task.add_argument("--file", default="-", help="JSON {source: tasks/name.md, text, metadata: {id,status,owner}}")
    context = sub.add_parser("context", help="Retrieve source-backed shared context")
    context.add_argument("query", nargs="?", help="Query; alternatively use --file")
    context.add_argument("--file", help="JSON retrieval arguments, or - for stdin")
    context.add_argument("--harness", choices=("codex", "claude", "antigravity", "hermes", "opencode"), default="codex")
    context.add_argument("--project")
    context.add_argument("--audience", choices=("internal", "public"), default="internal")
    context.add_argument("--status", action="append", dest="statuses")
    context.add_argument("--limit", type=int, default=5)
    context.add_argument("--budget-chars", type=int, default=8000)
    context.add_argument("--jev", action="store_true", help="Explicit optional remote advisor; requires state/jev.json and --project")
    review = sub.add_parser("jev-review", help="Advisory source review; never saves or approves a candidate")
    review.add_argument("--file", required=True, help="JSON proposal (maximum 24,000 characters)")
    review.add_argument("--project", required=True)
    receipt = sub.add_parser("receipt", help="Submit an idempotent source-linked receipt")
    receipt.add_argument("--file", default="-", help="JSON input path, or - for stdin")
    receipt.add_argument("--harness", choices=("codex", "claude", "antigravity", "hermes", "opencode"), default="codex")
    review = sub.add_parser("receipt-review", help="Review an exact receipt checkpoint without inventing an outcome")
    review.add_argument("--file", default="-", help="JSON review input path, or - for stdin")
    review.add_argument("--harness", choices=("codex", "claude", "antigravity", "hermes", "opencode"), default="codex")
    update = sub.add_parser("task-update", help="Update task with expected revision")
    update.add_argument("--file", default="-", help="JSON {id, expected_revision, changes}")
    history = sub.add_parser("history", help="Read ordered revision snapshots for a record")
    history.add_argument("record_id")
    return root


def main(argv=None):
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        vault = args.vault.expanduser().resolve()
        if not vault.is_dir():
            raise ValueError("--vault must be an existing directory")
        state = (args.state.expanduser() if args.state else default_state(vault)).resolve()
        if state == vault or vault in state.parents:
            raise ValueError("--state must be outside the vault")
        engine = load_engine()
        store = engine.MemoryStore(state, vault)
        sync = load_sync()(vault, state) if args.command in ("sync", "receipt", "receipt-review", "task-update", "note-create", "task-create", "context", "jev-review") else None
        if args.command == "init":
            result = {"initialized": True, "state": str(state), "network": False,
                      "hooks_installed": False, "optional_provider": None}
        elif args.command == "preferences":
            load_sync()
            import beyin_v3_preferences as preferences
            changes = {key: getattr(args, key) for key in ('interval_minutes', 'context_mode', 'context_chars') if getattr(args, key) is not None}
            if args.auto_sync is not None:
                changes['auto_sync'] = args.auto_sync == 'on'
            if args.secret_filter is not None:
                changes['secret_filter'] = args.secret_filter == 'on'
            settings = preferences.save(vault, changes, args.profile) if changes or args.profile else preferences.read(vault)
            result = {'status': 'saved' if changes or args.profile else 'current', 'preferences': settings,
                      'model_calls': False, 'timer_installed': False}
        elif args.command == "doctor":
            result = {"pending_events": len(list((state / "hook-queue").glob("*.json"))),
                      "acknowledged_events": len(list((state / "hook-done").glob("*.json")))}
            for filename in ("hook-health.json", "hook-error.json", "receipt-gaps.json"):
                path = state / filename
                result[filename] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            gap_report = result['receipt-gaps.json'] or {}
            result['receipt_review'] = {
                'unreviewed_candidates': gap_report.get('potential_missing_receipts', 0),
                'reviewed_candidates': gap_report.get('reviewed_checkpoints', 0),
            }
            seen = {name: set() for name in ('codex', 'claude', 'antigravity', 'hermes', 'opencode')}
            for path in (state/'hook-done').glob('*.json'):
                event = json.loads(path.read_text(encoding='utf-8'))
                if event.get('harness') in seen:
                    seen[event['harness']].add(event.get('event', 'unknown'))
            result['lifecycle'] = {name: {'status': 'observed_metadata' if events else 'never_seen', 'events': sorted(events)} for name, events in seen.items()}
            result['legacy_external_schedules'] = 'not_inspected; review custom OS/compiler schedules before migration'
            load_sync()
            import beyin_v3_preferences as preferences
            result['preferences'] = preferences.read(vault)
            from beyin_v3_secrets import health as secret_filter_health
            result['secret_filter'] = secret_filter_health(state)
            result['secrets_redacted'] = result['secret_filter']['total']
            result['automatic_model_calls'] = False
            health = result['hook-health.json'] or {}
            result['status'] = ('needs_attention' if health.get('sync', {}).get('status') in ('conflict', 'degraded') or result['hook-error.json'] else 'pending' if result['pending_events'] else 'observed_metadata' if result['acknowledged_events'] else 'never_seen')
        elif args.command == "skill-sync":
            result = load_skills().sync_skills(vault, state)
        elif args.command == "skill-import":
            result = load_skills().import_skill(vault, state, args.source, name=args.name)
        elif args.command == "sync":
            result = sync.sync()
        elif args.command == "ingest":
            payload = read_json(args.file)
            result = [store.ingest(record) for record in payload] if isinstance(payload, list) else store.ingest(payload)
        elif args.command == "note-create":
            payload = read_json(args.file)
            result = sync.note_create(payload['source'], payload['text'], payload.get('metadata'))
        elif args.command == "receipt-review":
            payload = read_json(args.file)
            result = sync.receipt_review(payload['target_harness'], payload['target_session'],
                                         payload['turn_at'], payload['checkpoint_at'],
                                         payload['disposition'], payload['reason'], payload['refs'],
                                         args.harness, payload['reviewer_session'])
        elif args.command == "task-create":
            payload = read_json(args.file)
            result = sync.task_create(payload['source'], payload['text'], payload['metadata'])
        elif args.command == "context":
            params = {"query": args.query, "project": args.project, "audience": args.audience,
                      "statuses": args.statuses, "limit": args.limit, "budget_chars": args.budget_chars}
            if args.file:
                supplied = read_json(args.file)
                if not isinstance(supplied, dict) or set(supplied) - set(params):
                    raise ValueError("Context JSON contains unsupported fields")
                params.update(supplied)
            if not isinstance(params["query"], str) or not params["query"].strip():
                raise ValueError("context requires a nonempty query")
            refreshed = sync.sync()
            if refreshed.get('status') == 'conflict':
                raise RuntimeError('Context blocked: source sync '+str(refreshed.get('status', 'failed'))+'. Run sync with the same vault/state to inspect and reconcile source issues, then retry context.')
            # Harness selection deliberately does not change retrieval semantics.
            if args.jev:
                from beyin_v3_jev import advise_context
                result = advise_context(sync.store, **params)
            else:
                result = sync.store.context_for(args.harness, **params)
            if refreshed.get('status') == 'degraded':
                warnings = refreshed.get('warnings', [])
                result['partial'] = True
                result['source_sync'] = {
                    'status': 'degraded',
                    'warning_count': len(warnings),
                    'warnings': warnings[:20],
                    'truncated': len(warnings) > 20,
                }
        elif args.command == "jev-review":
            from beyin_v3_jev import review_candidate
            # Bounded read also applies to stdin; never echo raw proposal errors.
            if args.file == "-":
                raw = sys.stdin.read(24001)
            else:
                with Path(args.file).open(encoding="utf-8") as handle:
                    raw = handle.read(24001)
            if len(raw) > 24000:
                raise ValueError("proposal_too_large")
            refreshed = sync.sync()
            if refreshed.get('status') != 'succeeded':
                raise ValueError("proposal_source_sync_incomplete")
            result = review_candidate(sync.store, json.loads(raw), project=args.project)
        elif args.command == "receipt":
            payload = read_json(args.file)
            result = sync.receipt(payload["event_id"], payload["summary"],
                                          payload["refs"], args.harness, session=payload.get('session'))
        elif args.command == "history":
            result = store.history(args.record_id)
        else:
            payload = read_json(args.file)
            result = sync.update_task(payload["id"], payload["expected_revision"], payload["changes"])
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except Exception as exc:
        # No traceback or raw input dump: callers retain their local source files.
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
