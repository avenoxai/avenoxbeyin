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

External cron jobs, LaunchAgents, scheduled tasks and already-running clients
are not silently stopped. Review custom schedules before cutover; schedules
calling other copies of old code are outside the project-local guard. Unknown
schedules cannot be ruled out by reading project hook files.

## What replaces the compiler path

V3 has no background model dependency. The active authorized agent writes short,
source-linked semantic receipts and deliberate knowledge notes. The deterministic
worker projects new structured receipt summaries into:

- `daily/v3/YYYY-MM-DD.md`: new V3 recorded outcomes.
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

After a UserPromptSubmit followed by Stop/SessionEnd, `doctor` reports an
unreviewed receipt candidate when the exact checkpoint has no matching session
receipt. SessionStart does not create or advance a Codex/Claude turn. This is a
signal: a turn may be trivial or the user may have deliberately omitted memory.
No transcript is promoted and no summary is generated to fill the gap. Matching
uses the latest observed UserPromptSubmit boundary; a receipt from a previous
turn cannot cover a later turn. Antigravity only
provides the initial invocation boundary in this adapter, so its result is
explicitly session-limited rather than proof of per-turn completeness. Missing
prompt provenance in legacy rows is labeled `legacy_unknown`. Explicit
`no_memory` hook metadata suppresses the checkpoint signal. Respect a user's
no-memory request regardless of that signal.

An exact candidate can be closed with the source-backed `receipt-review`
command. A review records the target and reviewer identities separately and
supports `no_receipt_needed`, `documented_retrospectively`, and
`outcome_unverified`. It never fabricates a receipt for the historical session.
Reviewed candidates remain in doctor history but leave the unreviewed count; a
later checkpoint has a different identity and is not hidden by an older review.
Only unreviewed candidates cause the hook warning; reviewed history does not.

## Validation boundary

Synthetic tests cover byte preservation, idempotent watermarking, inflight
rejection, concurrent source-edit detection, private Companion filtering,
new-only outcome projections, manual-view conflicts, note-create refusal to
overwrite, and receipt-gap closure. Actual V2 workers, external schedules and
client trust still require environment-specific validation. A green migration
unit test is not evidence that every external worker has stopped.
