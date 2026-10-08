#!/usr/bin/env python3
"""Local-first CLI for the shared V3 memory foundation."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
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


def _in_package_container(pinned: str) -> bool:
    """True when a pinned path runs through an MSIX package container.

    Reads the string, not the local path flavour: a Windows pin is inspected the
    same way when the tests run on POSIX.
    """
    parts = [part.lower() for part in pinned.replace("\\", "/").split("/") if part]
    return "packages" in parts and "localcache" in parts[parts.index("packages") + 1:]


def _sibling_state_roots(pinned: Path) -> list:
    """Installed state roots for the same vault key on both sides of a package redirect.

    Default roots end in beyin-v3/<vault key>. When %LOCALAPPDATA% is redirected,
    an install run inside the package and one run outside it produce two such
    roots for the same vault, each with its own database. Both are real and
    populated, so neither looks broken on its own. An explicit --state root has
    no key layout and is skipped. Returns an empty list on any lookup error.
    """
    parts = list(pinned.parts)
    lowered = [part.lower() for part in parts]
    if "beyin-v3" not in lowered or lowered.index("beyin-v3") + 2 != len(parts):
        return []
    index = lowered.index("beyin-v3")
    base = parts[:index]
    if "packages" in lowered[:index]:
        base = parts[:lowered.index("packages")]
    if not base:
        return []
    local, key = Path(*base), parts[index + 1]
    candidates = [local / "beyin-v3" / key]
    try:
        packages = local / "Packages"
        if packages.is_dir():
            # Literal path per package, not a glob: a key is never read as a pattern.
            candidates += [entry / "LocalCache" / "Local" / "beyin-v3" / key
                           for entry in sorted(packages.iterdir())]
    except OSError:
        return []
    found, identities = [], []
    for candidate in candidates:
        try:
            if not (candidate / "v3-install.json").is_file():
                continue
            # Inside the package the plain spelling is redirected into the container, so
            # both spellings reach one directory there. Count directories, not strings.
            identity = os.path.normcase(str(candidate.resolve()))
            if identity in identities or any(os.path.samefile(candidate, other) for other in found):
                continue
        except (OSError, ValueError, RuntimeError):
            continue
        found.append(str(candidate))
        identities.append(identity)
    return found


def state_location(vault: Path, state: Path, windows=None) -> dict:
    """Compare the pinned state root with the one this process actually reads.

    Windows redirects %LOCALAPPDATA% for an MSIX-packaged client into
    Packages/<id>/LocalCache/Local. A pin inside that container is one string and
    two directories for processes in and out of the package, so each side can end
    up holding half the memory. Information only: it writes nothing, makes no
    model call and does not raise the doctor status. A pin that resolves elsewhere
    is only warned about on Windows: a POSIX symlink resolves the same for every
    process, so a relocated state behind one is not a split.
    """
    windows = sys.platform == "win32" if windows is None else windows
    report = {"effective_state": str(state),
              "installed_at_effective_state": (state / "v3-install.json").is_file(),
              "pin_status": "absent", "pinned_state": None, "pinned_resolves_here_to": None,
              "pinned_in_package_container": False, "pin_resolves_elsewhere": False,
              "installed_at_pinned_state": None, "sibling_state_roots": [], "warnings": []}
    config = vault / ".beyin-runtime.json"
    pinned = None
    if config.is_file():
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
            value = data.get("state") if isinstance(data, dict) else None
        except (ValueError, OSError):
            value = None
        pinned = value if isinstance(value, str) and value.strip() else None
        report["pin_status"] = "present" if pinned else "unreadable"
    if pinned is None:
        return report
    try:
        resolved = Path(pinned).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        resolved = Path(pinned)
    same = os.path.normcase(str(resolved)) == os.path.normcase(pinned)
    report.update({"pinned_state": pinned, "pinned_resolves_here_to": str(resolved),
                   "pinned_in_package_container": _in_package_container(pinned),
                   "pin_resolves_elsewhere": not same,
                   "installed_at_pinned_state": (resolved / "v3-install.json").is_file()})
    if report["pinned_in_package_container"]:
        report["warnings"].append(
            "pinned_in_package_container: the pinned state root runs through an MSIX package "
            "container (Packages\\...\\LocalCache). That path carries the package "
            "identity and goes away when the package is reset or reinstalled under another "
            "identity. Moving the state to a plain local directory is the durable fix; "
            "see docs/v3/UPDATE.md.")
    if report["pin_resolves_elsewhere"] and windows:
        report["warnings"].append(
            "pin_resolves_elsewhere: this process resolves the pinned path to " + str(resolved) +
            ". A process on the other side of an MSIX redirect reads the same string and may "
            "reach a different directory; run doctor inside and outside the package and compare.")
    if not report["installed_at_pinned_state"]:
        report["warnings"].append(
            "pinned_state_empty: no v3-install.json under the pinned state root as this process "
            "reads it. Either the pin names a directory this process cannot see, or the state "
            "was moved without reinstalling with the new --state.")
    if os.path.normcase(str(resolved)) != os.path.normcase(str(state)):
        report["warnings"].append(
            "effective_state_differs: this run reads " + str(state) + " rather than the pinned "
            "root. Pass the pinned --state, or use the installed beyin.py, which reads the pin.")
    report["sibling_state_roots"] = _sibling_state_roots(resolved)
    if len(report["sibling_state_roots"]) > 1:
        report["warnings"].append(
            "state_split: this vault has an installed state root on both sides of the package "
            "redirect (" + ", ".join(report["sibling_state_roots"]) + "). Each one carries its own "
            "database, so which half a session reads depends on whether it runs inside the "
            "package. Keep one and move it out of the container; see docs/v3/UPDATE.md.")
    return report


def receipt_line_endings(vault: Path) -> dict:
    """Report whether git can rewrite receipt line endings in this vault (#205).

    Receipts are compared byte for byte. With core.autocrlf=true (the Git for Windows
    default) a pulled receipt is checked out as CRLF unless the vault stops that for
    receipts/ with `-text` or `eol=lf`; the index then reads a different summary and the
    same receipt resubmitted from the other machine fails as an event id collision.
    Read-only, information only; a vault that is not a git work tree reports not_applicable.
    """
    def git(*args):
        done = subprocess.run(("git", "-C", str(vault)) + args, capture_output=True, text=True, timeout=10)
        return done.returncode, done.stdout.strip()
    try:
        code, inside = git("rev-parse", "--is-inside-work-tree")
        if code != 0 or inside != "true":
            return {"status": "not_applicable", "reason": "vault is not a git work tree"}
        _, autocrlf = git("config", "--type=bool", "--get", "core.autocrlf")
        if autocrlf != "true":
            return {"status": "ok", "autocrlf": autocrlf or "unset"}
        _, attrs = git("check-attr", "text", "eol", "--", "receipts/x.md")
        found = dict((line.split(": ")[1], line.split(": ")[2]) for line in attrs.splitlines() if line.count(": ") == 2)
        if found.get("text") == "unset" or found.get("eol") == "lf":
            return {"status": "ok", "autocrlf": "true", "receipts_attributes": found}
        return {"status": "warning", "autocrlf": "true", "receipts_attributes": found,
                "warning": "line_endings: core.autocrlf=true and receipts/ is not pinned, so a receipt synced "
                           "from another machine can be checked out as CRLF, which changes its bytes and makes "
                           "the same receipt fail as an event id collision. Add 'receipts/** -text' to the "
                           "vault's .gitattributes and check the receipts out again; see docs/v3/MULTI-MACHINE.md."}
    except Exception as exc:  # no git, a hung git or a broken config must never hide the rest of doctor
        return {"status": "unavailable", "error": type(exc).__name__}


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
    settings.add_argument("--companion-context-chars", type=int,
                          help="Opening context budget (SessionStart, continuity questions), 1000..24000; 0 uses --context-chars")
    settings.add_argument("--secret-filter", choices=("on", "off"))
    settings.add_argument("--update-notifications", choices=("on", "off"))
    settings.add_argument("--last-session-chars", type=int, help="Hygiene limit for Last-Session.md; 0 turns it off")
    settings.add_argument("--threads-chars", type=int, help="Hygiene limit for Threads.md; 0 turns it off")
    settings.add_argument("--exclude-component", action="append", default=[], metavar="COMPONENT",
                          help="Disable/exclude a managed component or skill (repeatable)")
    settings.add_argument("--include-component", action="append", default=[], metavar="COMPONENT",
                          help="Re-enable a previously excluded component (repeatable)")
    settings.add_argument("--project-context", choices=("on", "off"),
                          help="Inject scoped project receipt and due tasks at SessionStart")
    settings.add_argument("--word-cap-warning", choices=("on", "off"),
                          help="Opt-in PostToolUse split signal for a long note (Claude and Codex only)")
    settings.add_argument("--max-words", type=int, help="Word cap for --word-cap-warning (10..100000, default 500)")
    settings.add_argument("--folder-questions", choices=("on", "off"),
                          help="Opt-in SessionStart question for a long-quiet top-level folder")
    settings.add_argument("--promotion", choices=("on", "off"),
                          help="Opt-in touch log for the doctor's hot/cold folder report")
    settings.add_argument("--parallel-sessions", choices=("on", "off"),
                          help="Opt-in one-line notice when another session is open on this vault")
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
    receipt.add_argument("--file", help="JSON input path, or - for stdin (default without receipt flags)")
    receipt.add_argument("--harness", choices=("codex", "claude", "antigravity", "hermes", "opencode", "omp"), default="codex")
    receipt.add_argument("--event-id", help="Required receipt event identifier in flag mode")
    receipt.add_argument("--summary", help="Receipt summary text, preserving literal newlines")
    receipt.add_argument("--summary-file", type=Path, metavar="PATH", help="Read the receipt summary from a UTF-8 file")
    receipt.add_argument("--ref", action="append", help="Vault-relative source path; repeat for multiple refs")
    receipt.add_argument("--session", help="Optional session identifier")
    update = sub.add_parser("task-update", help="Update task with expected revision")
    update.add_argument("--file", default="-", help="JSON {id, expected_revision, changes}")
    history = sub.add_parser("history", help="Read ordered revision snapshots for a record")
    history.add_argument("record_id")
    return root


def main(argv=None):
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    receipt_flags = args.command == "receipt" and any(
        getattr(args, name) is not None for name in ("event_id", "summary", "summary_file", "ref", "session"))
    if receipt_flags:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        if args.file is not None:
            argument_parser.error("--file cannot be combined with receipt flags")
        if args.event_id is None:
            argument_parser.error("--event-id is required in flag mode")
        if args.summary is not None and args.summary_file is not None:
            argument_parser.error("use exactly one of --summary and --summary-file")
        if args.summary is None and args.summary_file is None:
            argument_parser.error("--summary or --summary-file is required in flag mode")
        if not args.ref:
            argument_parser.error("--ref is required in flag mode (at least one)")
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
            import beyin_v3_hygiene as hygiene
            hygiene_changes = {key: getattr(args, key) == 'on' for key in ('word_cap_warning', 'folder_questions', 'promotion')
                               if getattr(args, key) is not None}
            if args.max_words is not None:
                hygiene_changes['max_words'] = args.max_words
            if hygiene_changes:  # validated before anything is saved, like the companion limits
                current_hygiene, hygiene_valid = hygiene.read_settings(state)
                if not hygiene_valid:
                    raise ValueError('hygiene.json in the runtime state is invalid; fix or remove it first')
                hygiene.check_settings(dict(current_hygiene, **hygiene_changes))
            if args.companion_context_chars is not None:  # validated before anything is saved
                current_context, context_valid = companion.read_context(state)
                if not context_valid:
                    raise ValueError(companion.CONTEXT_FILE + ' in the runtime state is invalid; fix or remove it first')
                companion.check_context(dict(current_context, context_chars=args.companion_context_chars))
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
            import beyin_v3_exclusions as exclusions
            exclusion_notice = None
            if args.exclude_component or args.include_component:
                exclude_args = [name.replace('\\', '/') for name in args.exclude_component]
                include_args = [name.replace('\\', '/') for name in args.include_component]
                exclusions.validate_exclusions(exclude_args + include_args)
                current_excluded = set(exclusions.read_exclusions(vault))
                current_excluded.update(exclude_args)
                current_excluded.difference_update(include_args)
                result_excluded = exclusions.save_exclusions(vault, current_excluded)
                exclusion_notice = 'Haric tutma tercihleri kaydedildi; bir sonraki kurulum ya da guncellemede uygulanir.'
            else:
                # A hand-edited typo must not block unrelated preference changes.
                try:
                    result_excluded = exclusions.read_exclusions(vault)
                except ValueError as exc:
                    result_excluded, exclusion_notice = [], 'Haric tutma dosyasi gecersiz: ' + str(exc)
            settings = preferences.save(vault, changes, args.profile) if changes or args.profile else preferences.read(vault)
            result = {'status': 'saved' if changes or args.profile or (args.exclude_component or args.include_component) else 'current',
                      'preferences': settings, 'excluded_components': result_excluded,
                      'model_calls': False, 'timer_installed': False}
            if exclusion_notice:
                result['exclusion_notice'] = exclusion_notice
            # Machine-local like update notifications: rollback-safe, outside the vault schema.
            result['companion_limits'] = companion.save_limits(state, limits) if limits else companion.read_limits(state)[0]
            if limits:
                result['status'] = 'saved'
            # Machine-local like the limits (#140): a larger opening budget must survive a rollback.
            if args.companion_context_chars is not None:
                result['companion_context'] = companion.save_context(state, args.companion_context_chars)
                result['status'] = 'saved'
            else:
                result['companion_context'], context_valid = companion.read_context(state)
                if not context_valid:
                    result['companion_context_notice'] = companion.CONTEXT_FILE + ' gecersiz; oturum basi baglami context_chars ile sinirli.'
            widest = max(result['companion_context']['context_chars'], settings['context_chars'])
            if companion.client_budget('claude', widest) < widest:  # #175: say so instead of silently using less
                result['client_context_notice'] = (f"Claude Code ve Codex oturumlarinda otomatik baglam {companion.client_budget('claude', widest)} "
                                                   'karakterde tutulur: Claude Code 10.000 karakteri, Codex yaklasik 10.000 bayti '
                                                   'asan hook baglamini dosyaya tasiyip modele yalniz bir onizleme gosterir.')
            import beyin_v3_releases as releases
            result['update_notifications'] = releases.preferences(state, None if args.update_notifications is None else args.update_notifications == 'on')
            if args.update_notifications is not None:
                result['status'] = 'saved'
            from beyin_v3_bridge import read_project_context, save_project_context
            if args.project_context is not None:
                save_project_context(state, args.project_context == 'on')
                result['status'] = 'saved'
            result['project_context'] = 'on' if read_project_context(state) else 'off'
            # Machine-local and rollback-safe like companion limits: never a .beyin-preferences.json key.
            if hygiene_changes:
                result['hygiene'] = hygiene.save_settings(state, hygiene_changes)
                result['status'] = 'saved'
            else:
                result['hygiene'], hygiene_valid = hygiene.read_settings(state)
                if not hygiene_valid:
                    result['hygiene_notice'] = 'hygiene.json gecersiz; tum hijyen sinyalleri kapali sayiliyor.'
            # Machine-local (#170): an older release would reject a new .beyin-preferences.json key.
            import beyin_v3_parallel as parallel
            if args.parallel_sessions is not None:
                parallel.save_settings(state, args.parallel_sessions == 'on')
                result['status'] = 'saved'
            parallel_on, parallel_valid = parallel.read_settings(state)
            result['parallel_sessions'] = 'on' if parallel_on else 'off'
            if not parallel_valid:
                result['parallel_sessions_notice'] = 'parallel-sessions.json gecersiz; paralel oturum bildirimi kapali sayiliyor.'
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
            # Information only; a split or container-bound state root never raises the status.
            try:
                result['state_location'] = state_location(vault, state)
            except Exception as exc:  # a location report must never hide the rest of doctor
                result['state_location'] = {'status': 'unavailable', 'error': type(exc).__name__, 'warnings': []}
            manifest = state / 'v3-install.json'
            manifest_data = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else {}
            result['kept_legacy_runners'] = manifest_data.get('kept_legacy', [])
            result['excluded_components'] = manifest_data.get('excluded_components', [])
            load_sync()
            import beyin_v3_exclusions as exclusions
            try:
                configured_excluded = exclusions.read_exclusions(vault)
            except ValueError as exc:
                # doctor reports the broken file instead of failing; update/install stay strict.
                configured_excluded = result['excluded_components']
                result['exclusions_error'] = str(exc)
            if set(configured_excluded) != set(result['excluded_components']):
                result['pending_exclusions'] = sorted(set(configured_excluded) ^ set(result['excluded_components']))
                result['exclusions_pending'] = True
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
            # Information only: a supersedes link that retires nothing keeps the old note in
            # context, as before #206; it never raises the doctor status.
            try:
                result['supersedes'] = load_sync().reader(store).supersedes_health()
            except Exception as exc:
                result['supersedes'] = {'status': 'unavailable', 'error': type(exc).__name__}
            # Read-only information: each scan fails alone and never hides the rest of doctor.
            # The word cap and promotion reports follow the user's opt-in (state/hygiene.json):
            # a default install gets no new doctor lines and no whole-vault read.
            for key in ('boundary', 'closed_tasks', 'word_cap', 'promotion'):
                try:
                    import beyin_v3_hygiene as hygiene
                    opted, _ = hygiene.read_settings(state)
                    if key == 'word_cap':
                        result[key] = (hygiene.cap_scan(vault, cap=opted['max_words'], state=state)
                                       if opted['word_cap_warning'] else {'enabled': False})
                    elif key == 'promotion':
                        result[key] = hygiene.promotion(vault, state) if opted['promotion'] else {'enabled': False}
                    else:
                        result[key] = getattr(hygiene, key)(vault)
                except Exception as exc:
                    result[key] = {'status': 'unavailable', 'error': type(exc).__name__}
            try:  # read-only report of the opt-in parallel-session notice (#170)
                import beyin_v3_parallel as parallel
                result['parallel_sessions'] = parallel.doctor(state)
            except Exception as exc:
                result['parallel_sessions'] = {'status': 'unavailable', 'error': type(exc).__name__}
            result['receipt_line_endings'] = receipt_line_endings(vault)  # information only: the status below is untouched
            result['status'] = ('needs_attention' if health.get('sync', {}).get('status') in ('conflict', 'degraded') or result['skill_conflicts'] or result.get('instruction_conflicts') or result['hook-error.json'] or result['task_completion']['strict_issue_count'] or result['task_completion'].get('error') or result['validity']['ignored_rejection_count'] or result['validity'].get('error') else 'pending' if result['pending_events'] else 'observed_metadata' if result['acknowledged_events'] else 'never_seen')
            # Information only: a leftover global OMP hook copy predates the vault-owned plan
            # (OMP.md says the installer never updates or removes it). After an engine update the
            # copy can be older than the installed vault hook, so a session outside the vault can
            # keep running an outdated adapter without any health signal. Report a digest mismatch;
            # never mutate or delete the user file. Symlinks (user-linked to the vault hook) are
            # always fresh by construction and reported as absent.
            try:
                adjacent_scripts = Path(__file__).resolve().parent
                template_scripts = adjacent_scripts if (adjacent_scripts / 'beyin_v3_omp.py').exists() else Path(__file__).resolve().parents[1] / 'template/.claude/scripts'
                omp_planner = template_scripts / 'beyin_v3_omp.py'
                if omp_planner.exists():
                    spec = importlib.util.spec_from_file_location('beyin_doctor_omp', omp_planner)
                    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                    generated = next(iter(module.plan_plugin(vault, state).values()))
                    global_copy = Path.home() / '.omp/agent/hooks/pre/beyin-v3.ts'
                    if global_copy.exists() and not global_copy.is_symlink():
                        current_digest = hashlib.sha256(global_copy.read_bytes()).hexdigest()
                        generated_digest = hashlib.sha256(generated).hexdigest()
                        if current_digest != generated_digest:
                            result['omp_global_hook'] = {
                                'path': str(global_copy),
                                'stale': True,
                                'note': 'global copy differs from the installed vault hook; it is not updated by install/rollback, and OMP.md advises against keeping one',
                            }
                        else:
                            result['omp_global_hook'] = {'path': str(global_copy), 'stale': False}
            except Exception as exc:  # a stale-copy report must never hide the rest of doctor
                result['omp_global_hook'] = {'status': 'unavailable', 'error': type(exc).__name__}
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
                result = recent_receipts(db, days=args.days, limit=args.limit, vault=vault)
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
            if receipt_flags:
                summary = args.summary
                if args.summary_file is not None:
                    # Preserve CRLF as well as LF, matching the JSON input text. utf-8-sig drops
                    # the BOM that Windows PowerShell 5.1 writes with -Encoding UTF8.
                    with args.summary_file.open(encoding="utf-8-sig", newline="") as stream:
                        summary = stream.read()
                payload = {"event_id": args.event_id, "summary": summary, "refs": args.ref}
                if args.session is not None:
                    payload["session"] = args.session
            else:
                payload = read_json(args.file if args.file is not None else "-")
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
