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
import time


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


# Mirror beyin_v3_jev_client.FEATURES and PROVIDERS and beyin_v3_laya.CHECKPOINTS; duplicated
# so argument parsing never imports the optional client. tests/v3_jev_toggle_test.py pins them.
JEV_FEATURES = ("context", "review", "answer", "auto_context")
JEV_PROVIDERS = ("typesafe", "vercel", "laya")
LAYA_MODELS = ("multilingual", "english")
JEV_NOTICE = ("auto_context is on: every turn sends the prompt plus the title and first 600 characters of up to 8 "
              "candidate internal/public notes to the provider. Private notes are never sent.")
JEV_NOTICE_LAYA = ("auto_context is on: every turn sends the prompt plus the title and first 600 characters of up to 8 "
                   "candidate internal/public notes to the local Laya server, one request per note. Private notes are never sent. "
                   "Laya is shadow-only: its scores are only logged and never change the context.")
JEV_WARNING = "TYPESAFE_API_KEY is not set; calls degrade to local results."
LAYA_NOTICE = ("provider laya: calls go only to the local laya-serve at {base_url}; start it with LAYA_HOST=127.0.0.1. "
               "Laya is shadow-only: scores are logged for measurement and never change results.")
# Coded refusals that deserve a next step. ASCII Turkish, like the other human lines.
ERROR_HINTS = {
    "laya_shadow_only": ("Laya yalniz golge modda calisir: puanlari olcum icin kaydedilir, gordugun sonucu degistirmez. "
                         "Laya icin: jev shadow --provider laya. Acik mod icin: jev on --provider typesafe."),
}


def jev_client():
    load_sync()
    import beyin_v3_jev_client as client
    return client


def jev_status(state: Path):
    """Doctor path: with no config and no kill switch the optional client is not imported."""
    if (not (state / "jev.json").exists() and not (state / "jev.disabled").exists()
            and os.environ.get("BEYIN_JEV_DISABLE") is None):
        return {"mode": "off", "configured": False}
    return jev_client().status(state)


def jev_advice(result):
    laya = result.get("provider") == "laya"
    if result.get("automatic_model_calls"):
        result["notice"] = JEV_NOTICE_LAYA if laya else JEV_NOTICE
    if laya:
        result["provider_notice"] = LAYA_NOTICE.format(base_url=result.get("laya", {}).get("base_url", "?"))
    elif result.get("mode") != "off" and not result.get("key_present"):
        result["warning"] = JEV_WARNING
    return result


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
    recap = sub.add_parser("recap", help="Read recent source-linked outcomes without a model call")
    recap.add_argument("--days", type=int, default=7, help="Calendar days in UTC, including today (1..366)")
    recap.add_argument("--limit", type=int, default=20, help="Maximum recent receipts to return (1..100)")
    settings = sub.add_parser("preferences", help="Control automatic local checks and injected context")
    settings.add_argument("--profile", choices=("normal", "economical", "manual"))
    settings.add_argument("--auto-sync", choices=("on", "off"))
    settings.add_argument("--interval-minutes", type=int)
    settings.add_argument("--context-mode", choices=("turn", "session", "off"))
    settings.add_argument("--context-chars", type=int)
    settings.add_argument("--secret-filter", choices=("on", "off"))
    settings.add_argument("--update-notifications", choices=("on", "off"))
    settings.add_argument("--last-session-chars", type=int, help="Hygiene limit for Last-Session.md; 0 turns it off")
    settings.add_argument("--threads-chars", type=int, help="Hygiene limit for Threads.md; 0 turns it off")
    compact = sub.add_parser("companion-compact", help="Move older Last-Session/Threads entries verbatim into a private archive; deletes nothing")
    compact.add_argument("--dry-run", action="store_true", help="Report what would move without writing")
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
    context.add_argument("--harness", choices=("codex", "claude", "antigravity", "hermes", "opencode", "omp"), default="codex")
    context.add_argument("--project")
    context.add_argument("--audience", choices=("internal", "public"), default="internal")
    context.add_argument("--status", action="append", dest="statuses")
    context.add_argument("--limit", type=int, default=5)
    context.add_argument("--budget-chars", type=int, default=8000)
    context.add_argument("--no-sync", action="store_true", help="Read an existing index without writing to the vault or runtime")
    context.add_argument("--jev", action="store_true", help="Explicit optional remote advisor; requires state/jev.json and --project")
    review = sub.add_parser("jev-review", help="Advisory source review; never saves or approves a candidate")
    review.add_argument("--file", required=True, help="JSON proposal (maximum 24,000 characters)")
    review.add_argument("--project", required=True)
    memory = sub.add_parser("jev-memory", help="Optional typed memory triage; never writes or approves")
    memory.add_argument("--file", required=True, help="Source-backed JSON proposal, optional prior_record_ids (max 4)")
    memory.add_argument("--project", required=True)
    jev = sub.add_parser("jev", help="Read or set the optional remote advisor; never accepts a key")
    jev.add_argument("mode", choices=("status", "off", "shadow", "on"))
    jev.add_argument("--enable", action="append", choices=JEV_FEATURES, default=[])
    jev.add_argument("--disable", action="append", choices=JEV_FEATURES, default=[])
    jev.add_argument("--provider", choices=JEV_PROVIDERS, help="typesafe (default) or laya, a local laya-serve (shadow only)")
    jev.add_argument("--base-url", dest="base_url", help="laya only: loopback URL, default http://127.0.0.1:8765")
    jev.add_argument("--model", choices=LAYA_MODELS, help="laya only: pinned checkpoint, default multilingual")
    jev.add_argument("--check", action="store_true", help="with status: one GET /health to the local Laya server")
    answer = sub.add_parser("jev-answer", help="Advisory answer claim verification against exact source quotes")
    answer.add_argument("--file", required=True, help="JSON list of claims (maximum 32,000 characters)")
    answer.add_argument("--project", required=True)
    receipt = sub.add_parser("receipt", help="Submit an idempotent source-linked receipt")
    receipt.add_argument("--file", default="-", help="JSON input path, or - for stdin")
    receipt.add_argument("--harness", choices=("codex", "claude", "antigravity", "hermes", "opencode", "omp"), default="codex")
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
        read_only_context = args.command == "context" and args.no_sync
        if read_only_context:
            # The source CLI can run without the installed entry point, which
            # already disables bytecode. Importing the engine must not write.
            sys.dont_write_bytecode = True
        if read_only_context and args.jev:
            raise ValueError("context --no-sync cannot be combined with --jev")
        # The advisor switch reads and writes one small file; it needs no index or sync engine.
        engine = load_engine() if args.command != "jev" else None
        store = engine.MemoryStore(state, vault, read_only=read_only_context) if engine else None
        sync = load_sync()(vault, state) if args.command in ("sync", "recap", "receipt", "task-update", "note-create", "task-create", "context", "jev-review", "jev-answer", "jev-memory", "history") and not read_only_context else None
        if args.command == "init":
            result = {"initialized": True, "state": str(state), "network": False,
                      "hooks_installed": False, "optional_provider": None}
        elif args.command == "preferences":
            load_sync()
            import beyin_v3_preferences as preferences
            import beyin_v3_companion as companion
            limits = {name: value for name, value in (('Last-Session.md', args.last_session_chars),
                                                      ('Threads.md', args.threads_chars)) if value is not None}
            if limits:  # validate before anything is saved, so a bad value changes nothing
                current_limits, limits_valid = companion.read_limits(state)
                if not limits_valid:
                    raise ValueError('companion-limits.json in the runtime state is invalid; fix or remove it first')
                companion.check_limits(dict(current_limits, **limits))
            changes = {key: getattr(args, key) for key in ('interval_minutes', 'context_mode', 'context_chars') if getattr(args, key) is not None}
            if args.auto_sync is not None:
                changes['auto_sync'] = args.auto_sync == 'on'
            if args.secret_filter is not None:
                changes['secret_filter'] = args.secret_filter == 'on'
            settings = preferences.save(vault, changes, args.profile) if changes or args.profile else preferences.read(vault)
            result = {'status': 'saved' if changes or args.profile else 'current', 'preferences': settings,
                      'model_calls': False, 'timer_installed': False}
            # Machine-local like update notifications: rollback-safe, outside the vault schema.
            result['companion_limits'] = companion.save_limits(state, limits) if limits else companion.read_limits(state)[0]
            if limits:
                result['status'] = 'saved'
            import beyin_v3_releases as releases
            result['update_notifications'] = releases.preferences(state, None if args.update_notifications is None else args.update_notifications == 'on')
            if args.update_notifications is not None:
                result['status'] = 'saved'
        elif args.command == "jev":
            laya = {key: value for key, value in (("base_url", args.base_url), ("model", args.model)) if value is not None}
            if args.mode == "status":
                if args.enable or args.disable or args.provider or laya:
                    raise ValueError("jev status reads only; use jev off/shadow/on with --enable/--disable/--provider")
                result = jev_client().status(state)
                if args.check:
                    result["server"] = jev_client().probe(state)
                result = jev_advice(result)
            else:
                if args.check:
                    raise ValueError("--check works only with jev status")
                result = jev_advice(jev_client().set_mode(state, args.mode, enable=args.enable, disable=args.disable,
                                                          provider=args.provider, laya=laya or None))
                result["changed"] = True
        elif args.command == "doctor":
            result = {"pending_events": len(list((state / "hook-queue").glob("*.json"))),
                      "acknowledged_events": len(list((state / "hook-done").glob("*.json")))}
            for filename in ("hook-health.json", "hook-error.json", "receipt-gaps.json"):
                path = state / filename
                result[filename] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            seen = {name: set() for name in ('codex', 'claude', 'antigravity', 'hermes', 'opencode', 'omp')}
            for path in (state/'hook-done').glob('*.json'):
                event = json.loads(path.read_text(encoding='utf-8'))
                if event.get('harness') in seen:
                    seen[event['harness']].add(event.get('event', 'unknown'))
            result['lifecycle'] = {name: {'status': 'observed_metadata' if events else 'never_seen', 'events': sorted(events)} for name, events in seen.items()}
            result['legacy_external_schedules'] = 'not_inspected; review custom OS/compiler schedules before migration'
            manifest = state / 'v3-install.json'
            result['kept_legacy_runners'] = json.loads(manifest.read_text(encoding='utf-8')).get('kept_legacy', []) if manifest.exists() else []
            load_sync()
            import beyin_v3_preferences as preferences
            result['preferences'] = preferences.read(vault)
            import beyin_v3_releases as releases
            result['updates'] = releases.status(vault, state)
            from beyin_v3_secrets import health as secret_filter_health
            result['secret_filter'] = secret_filter_health(state)
            result['secrets_redacted'] = result['secret_filter']['total']
            try:
                import beyin_v3_companion as companion
                result['companion_hygiene'] = companion.hygiene(vault, state)
            except Exception as exc:  # a size report must never hide the rest of doctor
                result['companion_hygiene'] = {'status': 'unavailable', 'error': type(exc).__name__}
            try:
                import beyin_v3_references as references
                result['instruction_references'] = references.check(vault)
            except Exception as exc:  # information only; never hides the rest of doctor
                result['instruction_references'] = {'status': 'unavailable', 'error': type(exc).__name__}
            result['jev'] = jev_status(state)
            result['automatic_model_calls'] = result['jev'].get('automatic_model_calls', False)
            health = result['hook-health.json'] or {}
            gaps_info = result['receipt-gaps.json']
            result['potential_missing_receipts'] = gaps_info.get('potential_missing_receipts') if gaps_info is not None else None
            if gaps_info is not None:
                from beyin_v3_projections import receipt_coverage
                with store._connect() as db:
                    result['receipt_coverage'] = receipt_coverage(db, now=time.time())
            else:
                result['receipt_coverage'] = None
            from beyin_v3_projections import knowledge_freshness, check_instruction_conflicts
            try:  # a recency report must never hide the rest of doctor, conflicts included
                with store._connect() as db:
                    result['knowledge_freshness'] = knowledge_freshness(vault, db, now=time.time())
            except Exception as exc:
                result['knowledge_freshness'] = {'status': 'unavailable', 'error': type(exc).__name__}
            result['instruction_conflicts'] = check_instruction_conflicts(vault)
            result['skill_conflicts'] = health.get('sync', {}).get('skill_conflicts', [])
            # Entries beside the skills that this vault never owned. Information only.
            result['skill_unmanaged'] = health.get('sync', {}).get('skill_unmanaged', [])
            try:
                result['task_completion'] = load_sync().reader(store).completion_health()
            except Exception as exc:
                result['task_completion'] = {
                    'strict_issue_count': 0, 'strict_issues': [], 'legacy_done_count': 0,
                    'legacy_done': [], 'truncated': False,
                    'error': (type(exc).__name__ + ': ' + str(exc))[:240],
                }
            # A rejection on a plain note leaves the claim in current context; sync stays healthy.
            try:
                result['validity'] = load_sync().reader(store).validity_health()
            except Exception as exc:
                result['validity'] = {'ignored_rejection_count': 0, 'ignored_rejections': [], 'truncated': False,
                                      'error': (type(exc).__name__ + ': ' + str(exc))[:240]}
            result['status'] = ('needs_attention' if health.get('sync', {}).get('status') in ('conflict', 'degraded') or result['skill_conflicts'] or result.get('instruction_conflicts') or result['hook-error.json'] or result['task_completion']['strict_issue_count'] or result['task_completion'].get('error') or result['validity']['ignored_rejection_count'] or result['validity'].get('error') else 'pending' if result['pending_events'] else 'observed_metadata' if result['acknowledged_events'] else 'never_seen')
        elif args.command == "skill-sync":
            result = load_skills().sync_skills(vault, state)
        elif args.command == "skill-import":
            result = load_skills().import_skill(vault, state, args.source, name=args.name)
        elif args.command == "companion-compact":
            load_sync()
            import beyin_v3_compact
            result = beyin_v3_compact.compact(vault, state, dry_run=args.dry_run)
            if any(entry['status'] == 'compacted' for entry in result['files'].values()):
                # Index the shorter sources and the private archive before anyone reads them.
                result['sync'] = {'status': load_sync()(vault, state).sync().get('status')}
        elif args.command == "sync":
            result = sync.sync()
        elif args.command == "recap":
            if not 1 <= args.days <= 366 or not 1 <= args.limit <= 100:
                raise ValueError('recap days must be 1..366 and limit must be 1..100')
            refreshed = sync.sync()
            if refreshed.get('status') == 'conflict':
                raise RuntimeError('Recap blocked: source sync conflict. Run sync to inspect sources.')
            from beyin_v3_projections import recent_receipts
            with store._connect() as db:
                result = recent_receipts(db, days=args.days, limit=args.limit)
            if refreshed.get('status') == 'degraded':
                warnings = refreshed.get('warnings', [])
                result['partial'] = True
                result['source_sync'] = {'status': 'degraded', 'warnings': warnings[:20],
                                         'warning_count': len(warnings), 'truncated': len(warnings) > 20}
        elif args.command == "ingest":
            payload = read_json(args.file)
            result = [store.ingest(record) for record in payload] if isinstance(payload, list) else store.ingest(payload)
        elif args.command == "note-create":
            payload = read_json(args.file)
            result = sync.note_create(payload['source'], payload['text'], payload.get('metadata'))
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
            if read_only_context:
                result = store.context_for(args.harness, **params)
                result['source_sync'] = {'status': 'skipped', 'reason': 'explicit_no_sync'}
            else:
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
        elif args.command in ("jev-review", "jev-answer", "jev-memory"):
            from beyin_v3_jev import review_candidate, verify_answer
            max_chars = 32000 if args.command == "jev-answer" else 24000
            # Bounded read also applies to stdin; never echo raw proposal errors.
            if args.file == "-":
                raw = sys.stdin.read(max_chars + 1)
            else:
                with Path(args.file).open(encoding="utf-8") as handle:
                    raw = handle.read(max_chars + 1)
            if len(raw) > max_chars:
                raise ValueError("proposal_too_large")
            refreshed = sync.sync()
            if refreshed.get('status') != 'succeeded':
                raise ValueError("proposal_source_sync_incomplete")
            handler = verify_answer if args.command == "jev-answer" else review_candidate
            if args.command == "jev-memory":
                from beyin_v3_memory_assessment import assess_memory
                handler = assess_memory
            result = handler(sync.store, json.loads(raw), project=args.project)
        elif args.command == "receipt":
            payload = read_json(args.file)
            result = sync.receipt(payload["event_id"], payload["summary"],
                                          payload["refs"], args.harness, session=payload.get('session'))
        elif args.command == "history":
            # History is source-verified, so refresh first like context: an edit
            # made just before this command must not hide the audit trail.
            refreshed = sync.sync()
            if refreshed.get('status') == 'conflict':
                raise RuntimeError('History blocked: source sync conflict. Run sync with the same vault/state to inspect and reconcile source issues, then retry history.')
            result = sync.store.history(args.record_id)
        else:
            payload = read_json(args.file)
            result = sync.update_task(payload["id"], payload["expected_revision"], payload["changes"])
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except Exception as exc:
        # No traceback or raw input dump: callers retain their local source files.
        error = {"error": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, ValueError) and str(exc) in ERROR_HINTS:
            error["hint"] = ERROR_HINTS[str(exc)]
        print(json.dumps(error, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
