# V2 migration and outcome continuity

V3 preserves existing Markdown, daily notes, knowledge articles and Companion
files as sources. Migration does not rewrite their bytes, replay their dates or
extract new claims from old transcripts. Existing V2 state remains in place; an
external copy and hash manifest accompany the one-time cutover watermark.

The installer/updater must hold `migration_guard(vault, state)` while it retires
recognized V2 handlers and installs V3. `finalize_migration(vault, state, plan)`
checks every existing source hash and the complete V2 state inventory before
recording success. The version stamp belongs after this successful check.
Repeated migration keeps the original watermark. Installation rollback preserves
user sources and the original V2 state; the updater owns system-file rollback.

The guard takes nonblocking locks on existing V2 `.claude/scripts/.state/*.lock`
files and rejects known `inflight`, `running` or `pending` writer states. If a
worker is active, let it finish and rerun. An orphaned inflight sentinel needs
explicit reconciliation; the migration never deletes it or declares it safe
from its age alone. Source or legacy-state changes during cutover reject success.
These are local cooperating-worker protections, not a distributed filesystem lock.

The installer also replaces hash-recognized stock legacy runners with reversible
inert shims, so a cached old hook cannot start the retired writer afterward.
Custom-modified runner files require explicit reconciliation. Finalization refuses
a legacy runner without the retirement marker.

## Reviewing and accepting a customized legacy runner

`--plan` reports what an install would write, retire and preserve, and changes
nothing in the vault or the state directory:

```text
python3 scripts/install_v3.py --vault "/absolute/path/to/vault" --plan
```

A runner you edited yourself stops the install with
`Customized legacy runner requires review <path>`. Read the file, keep a copy of
anything you still need, then retire that one file by name:

```text
python3 scripts/install_v3.py --vault "/absolute/path/to/vault" \
  --accept-customized-legacy .claude/scripts/flush.py
```

The flag is repeatable, takes vault-relative paths, and only accepts the known
legacy runner paths (`.claude/scripts/flush.py`, `.claude/scripts/compile.py` and
the `.claude/hooks/` V2 handlers). Accepting one path does not accept any other;
there is no blanket override. The pre-install bytes go into the install manifest
as the file's `original`, so `--uninstall` and `rollback` put your version back.

## Keeping a customized legacy runner

A runner you rewrote for your own pipeline may still be wanted next to V3, for
example a summarizer that writes `daily/YYYY-MM-DD.md` while V3 writes
`daily/v3/`. Keep that one file by name instead of retiring it:

```text
python3 scripts/install_v3.py --vault "/absolute/path/to/vault" \
  --keep-customized-legacy .claude/scripts/flush.py \
  --keep-customized-legacy .claude/hooks/session-end.sh
```

A kept runner is unmanaged: the installer does not read, hash, plan or replace
it, it takes no manifest entry, and `--uninstall` and `rollback` leave it alone.
Hook entries in `.claude/settings.json`, `.claude/settings.local.json` and
`.codex/hooks.json` that call a kept hook file by that exact path stay in place
and are yours to edit or remove; entries for the other V2 handlers, including a
file of the same name under `.codex/hooks/` or `.agents/hooks/`, are still
retired. A kept path must exist and must not already be retired. The same path
cannot be both kept and accepted in one run, and keeping one path does not keep
any other.

The choice is recorded as `kept_legacy` in the install manifest, so a later
`update`, which takes no flags, or a reinstall without the flag does not ask for
the same review again and finalization does not demand a retirement marker for
that file. To stop keeping a runner, reinstall with
`--accept-customized-legacy` for that path: it is retired like any other
accepted runner and leaves `kept_legacy`.
`doctor` lists the kept paths as `kept_legacy_runners`. What a kept runner
writes is yours to reconcile: V3 does not read it, index it or stop it, and a
kept summarizer that writes the same file as a V3 projection is your conflict.

Retiring a runner only replaces that one file. Helper modules it imported stay on
disk, now uncalled: `_portalock.py` here, and in older vaults files such as
`autopush.py`. They are left alone because they are not hash-recognized project
files and may be yours. Nothing invokes them after cutover; delete them yourself
once you have confirmed no external schedule still calls them.

External cron jobs, LaunchAgents, scheduled tasks and already-running clients
are not silently stopped. Review custom schedules before cutover; schedules
calling other copies of old code are outside the project-local guard. Unknown
schedules cannot be ruled out by reading project hook files.

## What replaces the compiler path

V3 has no background model dependency. The active authorized agent writes short,
source-linked semantic receipts and deliberate knowledge notes. The deterministic
worker projects new structured receipt summaries into:

- `daily/v3/YYYY-MM-DD.md`: new V3 recorded outcomes, named by this machine's
  local day (the day the session-start project block calls today). Receipt
  stamps inside stay UTC. Up to 3.5.1 the name was the UTC day; the first sync
  after updating removes an old UTC-named view that was never edited, and an
  edited one is kept and reported as a conflict until you move or delete it.
- `knowledge/v3/outcomes.md`: an index linking those outcomes to their receipts.

These are explicitly generated indexes of agent-authored claims, not automatic
knowledge distillation or external verification. Existing human daily and
knowledge texts remain untouched. Pre-cutover receipt files are not recaptured.
A manually edited generated view is preserved and produces a visible conflict.
The latest receipt remains available to the next session as historical context.

A preserved V2 `gecmis-import` skill may describe the old automatic compiler.
That legacy promise no longer applies after cutover: imported transcripts and
daily source notes remain indexed, and the active `beyin` skill performs deliberate
semantic distillation. Custom skill text remains user-owned; consult the managed
V3 migration notice when older instructions conflict.

The old model compiler's autonomous distillation is therefore replaced by an
explicit active-agent workflow, not secretly reproduced by a heuristic. Users who
want to keep an external V2 compiler must opt in deliberately and isolate its
write targets/schedule; V3 does not start it or promise compatibility with
simultaneous writers.

## Agent commands

Use the installed root launcher (`python beyin.py`, or `py -3 beyin.py` on Windows)
when provided by the product installer. The equivalent shared CLI is:

```text
python .claude/scripts/beyin_v3_cli.py --vault . receipt --harness claude --file receipt.json
python .claude/scripts/beyin_v3_cli.py --vault . note-create --file knowledge-note.json
python .claude/scripts/beyin_v3_cli.py --vault . doctor
```

Receipt input includes `event_id`, `summary`, `refs`, and optionally `session` from
the injected `Receipt session=...` identity. Choose the actual current harness.
Reusing an event ID retries the same outcome; it must not change the summary or
references. A receipt source is written and checked before success.

A new semantic knowledge note can be created from UTF-8 JSON:

```json
{
  "source": "knowledge/example-lesson.md",
  "text": "A source-backed lesson with appropriate evidence and limitations.",
  "metadata": {"project": "demo", "visibility": "internal"}
}
```

`note-create` only creates new `notes/` or `knowledge/` Markdown files and refuses
an existing destination. It does not invent facts or overwrite existing prose.
Use normal deliberate source editing for subsequent revisions. Private source
metadata continues to control retrieval; `visibility: private` is excluded from
internal/public context. Migration is not permission to expose Companion data.

After Stop/SessionEnd, `doctor` reports potential receipt gaps when a checkpoint
has no matching session receipt. This is a signal: a turn may be trivial or the
user may have deliberately omitted memory. No transcript is promoted and no
summary is generated to fill the gap. Matching uses the start of the latest turn
that edited a file (a PostToolUse event from the installed edit matchers); a later
question or "thanks" turn without edits does not reopen the session, but a receipt
from an earlier turn cannot cover a later turn that edited again. Without an
observed edit the latest UserPromptSubmit or SessionStart boundary applies.
Edits made through a shell command are not observed as edits. Antigravity only
provides the initial invocation boundary in this adapter, so its result is
explicitly session-limited rather than proof of per-turn completeness. Missing
prompt events are reported as a terminal-only limitation. Explicit `no_memory`
hook metadata suppresses the
checkpoint signal. Respect a user's no-memory request regardless of that signal.

`doctor` also reports `receipt_coverage`: the share of finished sessions with a
matching receipt, for all time and for the last 7 and 30 days, measured when
`doctor` runs. Only sessions with an observed user prompt count, either a
UserPromptSubmit or a SessionStart that carries the first prompt (Hermes,
OpenCode); the queue keeps only that fact, never the prompt text. Its `missing`
count can therefore be smaller than `potential_missing_receipts`, which counts
every finished checkpoint. Checkpoints recorded before this field existed have
no prompt marker, so the ratio starts with sessions after the upgrade and stays
`null` until one is observed. A receipt whose `created_at` cannot be read never
covers a session.

## Validation boundary

Synthetic tests cover byte preservation, idempotent watermarking, inflight
rejection, concurrent source-edit detection, private Companion filtering,
new-only outcome projections, manual-view conflicts, note-create refusal to
overwrite, and receipt-gap closure. Actual V2 workers, external schedules and
client trust still require environment-specific validation. A green migration
unit test is not evidence that every external worker has stopped.
