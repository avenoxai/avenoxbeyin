# V3 local foundation quickstart

V3 is an **opt-in project-local installation**. The Python installer wires Claude,
Codex, Antigravity and Kiro to one source-backed engine. It installs no system service,
connects no account, calls no model and does not enable Mem0. Existing legacy
installers are separate; run the V3 installer below to choose this runtime.

Use Python 3.11 or newer. No provider package or API key is required. Run the
commands below from this repository root. On Windows, `py -3` can replace
`python3`. All command arguments are portable; the longer smoke example uses
Python rather than shell-specific temporary directory commands.

## Initialize an existing vault

```text
python3 scripts/beyin_v3.py --vault /absolute/path/to/vault init
```

The vault must exist. State is stored outside it, in a per-vault directory under
`~/Library/Application Support/beyin-v3` on macOS, `$XDG_STATE_HOME/beyin-v3`
(or `~/.local/state/beyin-v3`) on Linux, and `%LOCALAPPDATA%/beyin-v3` on Windows.
Moving a vault changes the default state key. The database is bound to its
original vault root, so rebuild fresh local state from Markdown after moving;
do not reuse the old database with `--state`. Do not put state in a synced
vault. `--state /absolute/local/state` overrides the default and is rejected if
it resolves inside the vault. Moving the state directory of an unchanged vault is
a different, supported operation: copy it, leave any `update-journal.json`
behind, and reinstall with the new `--state`; `doctor` reports the pinned and
effective roots under `state_location`. See [UPDATE.md](UPDATE.md). Global
`--vault` and `--state` options precede the command.

## Synthetic smoke, with no real vault

Save the following as a temporary Python file and run it from the repository
root. It creates only synthetic sources and state in a temporary directory,
asserts equivalent harness contexts, and removes those temporary files on exit.
It exercises this CLI, not installed Codex/Claude hook delivery.

```python
import json
from pathlib import Path
import subprocess
import sys
import tempfile

cli = Path("scripts/beyin_v3.py").resolve()
with tempfile.TemporaryDirectory(prefix="beyin-v3-smoke-") as temporary:
    base = Path(temporary)
    vault = base / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes/demo.md").write_text(
        "---\n" + json.dumps({"id": "demo-task", "kind": "task", "project": "demo", "revision": 1,
                            "status": "active", "visibility": "public", "facts": {"owner": "Ada"}})
        + "\n---\nDemo launch owner is Ada. Prepare the checklist.\n", encoding="utf-8")

    def run(*args, payload=None):
        result = subprocess.run(
            [sys.executable, str(cli), "--vault", str(vault),
             "--state", str(base / "state"), *args],
            input=json.dumps(payload) if payload is not None else None,
            text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    run("init")
    run("sync")
    codex = run("context", "demo launch owner", "--project", "demo",
                "--harness", "codex")
    claude = run("context", "demo launch owner", "--project", "demo",
                 "--harness", "claude")
    assert codex == claude
    assert any(record["id"] == "demo-task" for record in codex["records"])
    receipt = {"event_id": "demo-smoke-receipt",
               "summary": "Prepared synthetic demo source.",
               "refs": ["notes/demo.md"]}
    run("receipt", payload=receipt)
    run("receipt", payload=receipt)  # Same event ID is an idempotent retry.
    run("task-update", payload={"id": "demo-task", "expected_revision": 1,
                                "changes": {"status": "done"}})
    history = run("history", "demo-task")
    assert len(history) == 2
    print("Synthetic CLI smoke passed; no hooks installed.")
```

## JSON input and retrieval

`ingest`, `receipt`, and `task-update` accept `--file input.json`; omitting the
option reads JSON from stdin when no receipt field flags are supplied. `ingest`
accepts one record or a list; a list is processed sequentially and is not an
all-or-nothing transaction. Sources must
already exist as relative paths within the vault. A path establishes provenance,
not independent verification that the record's assertion is true.

```text
python3 scripts/beyin_v3.py --vault /absolute/vault ingest --file record.json
python3 scripts/beyin_v3.py --vault /absolute/vault context "launch owner" --project demo --audience public --harness claude
python3 scripts/beyin_v3.py --vault /absolute/vault context "launch owner" --project demo --no-sync
python3 scripts/beyin_v3.py --vault /absolute/vault context --file query.json --harness codex
python3 scripts/beyin_v3.py --vault /absolute/vault receipt --harness codex --event-id EVENT_ID --summary "Work result" --ref notes/source.md
python3 scripts/beyin_v3.py --vault /absolute/vault receipt --file receipt.json --harness codex
python3 scripts/beyin_v3.py --vault /absolute/vault task-update --file patch.json
python3 scripts/beyin_v3.py --vault /absolute/vault history demo-task
python3 scripts/beyin_v3.py --vault /absolute/vault recap --days 7 --limit 20
python3 scripts/beyin_v3.py --vault /absolute/vault recap --since 2026-09-24 --until 2026-09-24
```

Retrieval JSON accepts `query`, `project`, `audience`, `statuses`, `limit`, and
`budget_chars`. JSON fields override corresponding command-line retrieval
options. Command-line `--status` can be repeated. `--limit` defaults to 5 and
`--budget-chars` to 8000. Receipt JSON requires `event_id`, `summary`, and `refs`;
choose its harness using `--harness`. Reuse an event ID only for the same outcome.
Prefer receipt flags to avoid preparing JSON: `--event-id`, exactly one of
`--summary` or `--summary-file PATH` (UTF-8), and at least one repeatable `--ref`.
`--session` is optional. `--link URL` (JSON `links`) names an external result such as
a pull request or a published page; it is repeatable, only http(s), never read and never
a vault source, so it adds to `--ref` rather than replacing it. A link with credentials or
a known secret pattern is refused. Summary text and line endings are preserved; literal
`\n` in `--summary` is not expanded. `--file` cannot be combined with these flags.
Task patch JSON requires `id`, `expected_revision`, and `changes`. A revision
conflict requires reading current state and reconciling the intended change.
`history RECORD_ID` synchronizes first, like `context`, and returns ordered revision
snapshots, including the original source synchronization, subsequent updates and a
final `delete` event for removed sources, so changes can be reviewed from local state.

`recap` is an on-demand activity view over dated receipts. It infers nothing: the
result lists the most recent agent-authored outcome claims, their receipt source
paths, and the references submitted with each receipt. The default covers today
plus the previous six UTC calendar days and returns at most 20 entries; use
`--days 1..366` and `--limit 1..100` to adjust. Older receipts without `created_at`
are counted as omitted, never assigned an invented date. A receipt whose own file
was removed is counted in `missing_source_omitted`; references that no longer exist
or that point at `visibility: private` or untrusted notes are withheld and counted in
`refs_withheld`, the same boundary internal `context` applies. Like `context`, the
command synchronizes local sources first (refreshing the generated `daily/v3/`
views); it creates no receipt or note and calls no model. The installed `beyin.py
recap` prints a compact terminal rendering on a terminal, or with `--human`; stored
control characters are shown as `?`.

`recap --since DATE` and `--until DATE` replace the `--days` window with an explicit
range; `--days` cannot be combined with either. `DATE` is `YYYY-MM-DD` or a full ISO
8601 timestamp ending in `Z` or a UTC offset (`2026-09-24T18:30:00+03:00`); a time
without a zone is rejected rather than guessed. A bare date is a calendar day on this
machine's clock, the day that names `daily/v3/YYYY-MM-DD.md`: a receipt belongs to the
date its instant shows on the local clock, and that date is compared with the bound.
No midnight is computed, so a day with a clock change needs no special handling.
`--until 2026-09-24` runs through the end of that day, so `--since 2026-09-24 --until
2026-09-24` returns exactly that day. A timestamp is an exact, inclusive instant.
With only `--until` there is no lower bound and `from` is `null`; with only `--since`
the range ends with today. In this mode `from` and `through` remain dates (the local
day of each bound), `timezone` is `local` instead of `UTC`, and every entry carries
`day`, its local calendar day, while `created_at` stays in UTC. A value in another
form, `--since` after `--until`, and `--days` together with either flag are usage
errors: exit status 2, the message on stderr, nothing on stdout.

By default, `context` refreshes the local index before retrieval. `--no-sync`
instead opens an already initialized SQLite index in read-only mode and does not
write to either the vault or runtime. It can therefore miss newly added sources;
changed or deleted sources are still excluded by the normal source-hash checks.
The option fails when no bound index exists and cannot be combined with `--jev`.

Results are JSON; command failures return a nonzero exit code. Check both process
status and returned data. An abstained retrieval is a valid empty answer, not
evidence that a fact is false. `--audience internal` includes internal and public
records but excludes records marked `visibility: private`. Internal records may
still be sensitive; use synthetic inputs for public demonstrations.

This release establishes local source-backed retrieval and state semantics.
It does not establish broad natural-language intelligence, installed lifecycle
parity, remote synchronization, or automatic third-party memory integration.
See [the frozen test contract](SEMANTIC-TEST-CONTRACT.md) for the bounded gates.

## Install lifecycle adapters

```text
python3 scripts/install_v3.py --vault /absolute/path/to/vault
```

This copies self-contained Python modules into `.claude/scripts`, enables project
Codex `[features] hooks = true`, preserves unrelated hook handlers and settings,
and adds a bounded managed instruction block to AGENTS.md. Claude Code by default
reads AGENTS.md only when no CLAUDE.md exists, so a missing CLAUDE.md is created as
a single `@AGENTS.md` import, a CLAUDE.md that already imports AGENTS.md gets no
second copy, and any other CLAUDE.md receives the same block.
Recognized legacy adapters are retired to prevent duplicate writes. An exact
pre-install backup and installed hashes live in the external runtime directory.
Repeating installation is idempotent; edits to managed files require reconciliation.

In Codex, open `/hooks` and review/trust the installed definitions. The installer
never fabricates trust hashes or bypasses review. Antigravity also requires
trusting the vault folder before project hooks can run; review its folder trust
prompt in the client. Kiro IDE 1.x and CLI V3 load `.kiro/hooks/beyin-v3.json`;
review it in the Agent Hooks panel or CLI `/hooks` (see [KIRO.md](KIRO.md)).
Start a fresh Claude/Codex/Antigravity/Kiro session. Files
existing on disk do not prove actual client delivery.

For Antigravity print mode, explicitly attach the vault workspace. Running from
its directory alone may leave the CLI on its default project and skip project
hooks:

```text
agy --add-dir /absolute/path/to/vault --model claude-sonnet-4-6 --mode plan --sandbox -p "Without tools, summarize the injected V3 context."
```

Use the vault's canonical absolute path and trust that folder in Antigravity
first. In a local synthetic verification, `--add-dir` caused real PreInvocation
and final-idle Stop events to reach the adapter; the same print run with only a
working directory did not. This does not imply every client version behaves
identically.

From the installed vault, use the copied CLI independently of this repository:

```text
python3 .claude/scripts/beyin_v3_cli.py --vault . sync
python3 .claude/scripts/beyin_v3_cli.py --vault . doctor
python3 .claude/scripts/beyin_v3_cli.py --vault . skill-sync
python3 .claude/scripts/beyin_v3_cli.py --vault . skill-import --source /chosen/skill-folder
```

Markdown files are source records. JSON frontmatter can specify `id`, `kind`,
`project`, `status`, `visibility` and `facts`; the note body is preserved on task
updates. Plain notes are indexed too. Receipts write a source note before local
state acknowledgement. `sync` handles edited and deleted sources. This supersedes
using low-level `ingest` as the routine source management path.

Shared skills live in `.agents/skills`; Claude and Kiro see project-local mirrors.
POSIX defaults to symlinks and Windows to tracked copies. Copy reconciliation
preserves simultaneous conflicting changes and reports them. `skill-import`
imports only the explicitly selected directory; no global skill scan occurs.
Uninstall preserves canonical skills and skill mirror state as user content.
Entries beside the skills that are not skill directories, such as a license file
or a shared helper folder, are never owned: `doctor` lists them under
`skill_unmanaged` as information. A skill that was mirrored before and then lost
its `SKILL.md` stays a conflict, because that is a removal signal. Skill
conflicts appear in `doctor` under `skill_conflicts` and in session context as a
warning, but they no longer stop queued event metadata from being processed;
only source synchronization failures do that.

`doctor` also reads `AGENTS.md`, `CLAUDE.md`, the companion `Kurallar.md` and
every Markdown file under `.agents/skills`, `.claude/skills` and `.kiro/skills` for `[[wikilink]]`
and `[text](path)` links that resolve to nothing inside the vault, and lists them
with file and line under `instruction_references`. Code blocks, inline code,
URLs and links that leave the vault are skipped. It is information only: it
makes no model call, writes nothing and does not change the doctor status.

Hooks enqueue only event metadata, never raw prompts or transcripts. They start
an opportunistic detached Python worker. Startup waits at most 1.5 seconds for
source synchronization before context; a slower sync keeps running and the turn
reads the previous index, labeled as such, never as fresh context. Before the
first index exists the hook reports pending only. A client that runs its hook
commands without write access to the runtime state — a sandboxed lifecycle hook
or a read-only state folder — can still read the vault and the previous index,
so the turn answers from that index with a one-line note that automatic sync
cannot run there. Without an index the failure notice stays. Stop and edit hooks
return promptly. If a worker fails, metadata stays queued and the next event
retries. Explicit repeated `event_id` values deduplicate queue acknowledgement;
client events without an ID
receive new local IDs. This is not a blanket exactly-once transport guarantee. There is no
always-on daemon or guarantee of updates while every client is closed.
`doctor` shows pending counts and the latest worker result. A synthetic/manual
hook event is not proof of real client lifecycle delivery.

For deterministic offline tests, `BEYIN_V3_NO_SPAWN=1` suppresses background
workers; use the hook's `--drain-queue` option to drain explicitly. This testing
mode may return previously indexed context and should not be used for normal
interactive operation.

Set `BEYIN_V3_SKIP=1` in a delegated or headless agent process to keep the V3
memory hooks out of that run; the hook and bridge return `{}` without queuing an
event, injecting context or starting a worker (a non-idle Antigravity `Stop`
still answers `decision: stop`). Only the exact value `1` skips; any other value,
including `0`, `false` or an empty value, leaves the hooks on. Child processes
inherit the variable, so set it on the delegated process only, not in a shell
profile or shared environment file where it would also silence the main session.
`BEYIN_V3_INTERNAL` remains available as the internal recursion guard.

Windows command definitions use an encoded PowerShell invocation solely to
quote the Python executable and arguments safely, including spaces and Unicode.
The Python implementation does not require Bash or third-party modules; Windows
PowerShell is used as the native command launcher. Codex also receives an explicit
`commandWindows` override. Native Windows execution must be verified on Windows;
POSIX success does not prove it. Newline-containing command paths are rejected.

To exclude unneeded components during installation (repeatable):

```text
python3 scripts/install_v3.py --vault /absolute/path/to/vault --exclude-component skills/beyin-doktor --exclude-component launchers
```

To roll back adapter installation using the saved exact originals:

```text
python3 scripts/install_v3.py --vault /absolute/path/to/vault --uninstall
```

Use the same `--state` if installation used an override. Uninstall refuses files
whose managed part changed since installation rather than overwriting edits. Text
you added outside that part (below the block in `AGENTS.md`, permissions in
`.claude/settings.local.json`, tables in `.codex/config.toml`) stays, also after a
reinstall or update; a file the installer created is removed only when nothing
else is left in it. Source notes, receipt
notes, canonical skills and the memory database remain. Reverted Codex definitions
may require renewed `/hooks` trust review.

Client references: [Codex hooks](https://learn.chatgpt.com/docs/hooks) and
[Claude hooks](https://code.claude.com/docs/en/hooks).


## Read-only triage of open topics

Run `python3 beyin.py triage --stale-days 14` (add `--human` for readable output).
The additive `threads` section reports stale and undated `###` topics in the
configured/discovered companion `Threads.md`, alongside the existing task report.
Topics under active sections (Active/Open/Aktif/Açık) or the top-level title are
considered open; closed/other sections and fenced code examples are ignored.

For each topic, `last_date` is the newest valid `YYYY-MM-DD` mention in its heading
or body. This is a freshness hint, **not evidence that its status was verified**:
a quoted historical date or future plan is still a date mention. Future dates
have age zero. Written-out dates are not inferred, and topics without a valid ISO
date have `last_date: null` and `age_days: null`; file mtime is never substituted.
Check the actual source of the work before repeating an old status as current.

This report does not edit, archive, or refresh the notes and does not change
opening-context budgets. Private/untrusted or excluded sources and symlinks are
not scanned. An ambiguous identity, invalid directory marker, or unsafe/unreadable
source is reported as `status: unavailable` with a reason, not a successful empty
scan; a missing `Threads.md` reports `status: absent`.
