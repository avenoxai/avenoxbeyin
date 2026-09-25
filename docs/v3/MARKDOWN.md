# Markdown sources, local index

`beyin_v3_sync.py` adds explicit in-process synchronization to the V3 foundation. It requires only Python's standard library. No package install, model, network, cloud account, or background service is involved. Existing compiler isolation and hook behavior are unchanged by this module.

Use `SyncEngine(vault_root, state_dir)`, then `sync()`. The runtime must be a dedicated directory outside the vault. Its `.store` is the existing `MemoryStore`; retrieve through that store after synchronization. The same source and context APIs serve Codex, Claude and Antigravity.

## Source format

An ordinary Markdown file is indexed by its complete body with a stable ID derived from its relative path. Its visibility defaults to internal. A rename therefore removes the old ordinary-note ID and creates a new one. Explicit IDs persist across renames.

Canonical managed metadata is a JSON object between Markdown frontmatter delimiters. JSON objects are also valid YAML:

```markdown
---
{
  "id": "example-task",
  "kind": "task",
  "revision": 1,
  "project": "demo",
  "status": "active",
  "visibility": "internal",
  "facts": {"owner": "team"}
}
---
# Example

Keep this source body when updating task metadata.
```

Flat scalar YAML fields are also accepted, including Unicode property names, quoted strings, numbers, empty values (null), single-line scalar lists (`[a, "b c"]`, `[]`) and consistently space-indented block lists; a scalar value may carry a trailing inline comment (`kaynak: olcum   # not`, also after a quoted scalar), and a value that is exactly a template placeholder (`created: {{TODAY}}`) is kept as that literal string. List items may be plain or quoted (JSON escapes in double quotes, doubled single quotes), with plain items kept as strings unless the entire inline list is valid JSON, which retains its JSON types. Unsupported multiline scalars, nested collections, flow mappings, anchors/aliases/tags, inline comments in list items or after a list, values that are only a comment, duplicate keys or ambiguous indentation produce a warning and the file is excluded; metadata is never silently discarded, and complex metadata requires JSON frontmatter. Explicit context reads still return the freshly indexed healthy subset with `partial: true` and bounded source warnings. Source-ID conflicts remain fail-closed. No task or completion status is inferred from body prose. A note dated with the common Obsidian keys `updated`, `modified`, `last_modified` or `date_modified` instead of `updated_at` keeps that key and still receives the recency tie-break, provided the value is a string starting with an ISO date.

The scanner skips hidden directories/files, node_modules, symlinks, known instruction/configuration Markdown names, receipt output directories and files explicitly marked as generated or `kind: receipt`. It does not assume that all ordinary prose is instruction-safe; untrusted content should be explicitly marked `kind: untrusted`. Visibility must be public, internal or private. Privacy-filtered context does not include private notes by default.

### Inference and preference validity

For `kind: inference` or `kind: preference`, optional `validity` is `current` (the default for existing sources) or `rejected`. When a user rejects an inference, keep its source and set `validity: rejected`; `rejected_reason` (non-empty) and `rejected_at` (ISO date or timestamp, such as `2026-09-24` or `2026-09-24T10:00:00Z`) are required in the frontmatter. A rejected inference is excluded from ordinary context, strict and candidate retrieval, current snapshots, and companion source snapshots even if it is an exact query match or a caller requests `--status rejected`. Legacy inference/preference notes with `status: rejected` receive the same exclusion without requiring new metadata. `validity` has no effect on other kinds, including a note without `kind`; `doctor` lists such records under `validity.ignored_rejections` and reports `needs_attention`, because their text is still current context. Task statuses retain their task meaning; `status: cancelled` is unaffected.

Use `beyin.py history RECORD_ID` to inspect a rejected inference and its reason/date deliberately. The command synchronizes first, like `context`, so an edit made just before it is already recorded. History requires the current source to pass the same visibility, trust and source-hash checks; older event snapshots are labeled history, not current evidence. A record whose source was deleted or no longer passes validation keeps its audit trail, ending in a `delete` event, under the visibility and trust of that last snapshot; `sync` reports why a source was dropped. If a rejected claim was also written into a broader current source such as `Core.md`, correct that source explicitly; metadata on a separate note cannot retract prose elsewhere.

## Synchronization and task updates

Markdown is authoritative. `sync()` reads sources and replaces their derived record projection in one SQLite transaction. Edits, deletions and renames converge on the next explicit call. Ingest/update/delete events are recorded for real projection changes, with no extra events for an unchanged sync. Source hashes prevent retrieval from returning a changed source before synchronization. Duplicate source IDs are quarantined rather than choosing a winning file. Warnings and conflicts are returned explicitly; a conflict pass reports `status: conflict`, a warning-only pass reports `status: degraded`, and a clean pass reports `status: succeeded`.

`update_task(id, expected_revision, changes)` only updates explicit task metadata. The source must contain an ID, kind task, and revision. The body remains byte-equivalent for ordinary UTF-8 LF Markdown. The index revision and source hash are compared before replacement. The operation increments revision, writes canonical JSON frontmatter, synchronizes and checks the intended revision/hash on readback. Unsupported keys, stale revisions and manual edits fail rather than returning success. This API never marks a task complete unless the supplied changes explicitly request that status.

Tasks can opt into a completion contract with `"completion_contract": "strict"` and a nonempty `completion_criterion` set before completion. The update that moves a strict task to `done` cannot change its `completion_criterion` or opt it into the contract, so completion is judged against the criterion that stood before it. The comparison is exact; omit `completion_criterion` from the closing update or pass the stored value unchanged. This rule applies to `task-update`: a manual Markdown edit that changes the criterion and the status together is synced as one revision, and `task-create` with `status: done` records a criterion at completion time. A strict task can move to `done` only with at least one `evidence_refs` path to an existing vault source other than the task itself; invalid, repeated, missing, or escaping paths are rejected before a task source or revision is written. Accepted references are stored as vault-relative POSIX paths, so `notes/./result.md` or a Windows `notes\result.md` is written as `notes/result.md`; letter case is kept as supplied. Two spellings that open the same file, such as a case variant on a case-insensitive volume, count as a repeat or as the task itself. `task-update` cannot remove an existing strict contract. `cancelled` remains available without evidence. The references show where to inspect the outcome; their presence does not independently verify success or run a task-specific test. Tasks without `completion_contract` retain the legacy status behavior; their other updates skip these checks unless the update itself sets a completion field. If an evidence source disappears, reopen the task by setting a non-done status and `"evidence_refs": []` in the same update, then attach a valid source before marking it done again.

`doctor` reports `task_completion.strict_issues` for indexed strict tasks whose contract is incomplete or whose evidence source is missing, and `task_completion.legacy_done` for completed tasks without the opt-in contract. Both lists are bounded to 20 with separate counts and a truncation flag. Legacy done tasks are informational and do not make doctor unhealthy; strict issues do.

The local SQLite write lock serializes participating clients across processes without a platform-specific file-lock dependency. It does not lock other editors or cloud synchronization. Source hash comparison detects observed changes; an unrelated writer can still race after the final check, so reread/sync remains the conflict-resolution path.

## Receipts and recovery

`receipt(event_id, summary, refs, harness)` writes exactly one deterministic source under `receipts/`, using a full hash of the event ID as the filename. References must exist inside the vault. The same semantic receipt replays across harnesses without another file; a changed payload or manually modified receipt source is a conflict. First-writer harness attribution stays stable. Receipts do not automatically become tasks or get copied to a second daily summary.

Before replacing a source, the module atomically writes a private intent journal outside the vault. If a replacement or projection fails, the next `sync()` retries the intent only when the existing file matches its recorded old or intended new hash. A third-party edit is preserved and returned as a conflict, with the journal retained for deliberate resolution. The projection transaction commits before successful journal cleanup. An interrupted task update can consequently finish on the next sync; callers must inspect recovery rather than assuming that an exception means cancellation.

File writes use temporary files, flush/fsync and atomic replace; directory fsync is attempted where the platform supports it. This is recoverable local intent, not a distributed transaction across arbitrary editors. No automatic destructive rollback of source records occurs.

## Scope

This layer is intentionally explicit and synchronous. It does not install a daemon, parse real chat transcripts, infer semantic outcomes, modify model settings, or replace the staged knowledge compiler. A separate adapter may call it, but a queued adapter event is not proof of successful synchronization.

## Effective revisions and session snapshots

A changed managed source advances the indexed effective revision even when its author did not edit the declared frontmatter revision. A larger explicit revision is honored. Repeated unchanged sync does not advance it. Task updates compare this effective revision and write its successor into canonical frontmatter, preventing a previously read revision from silently overwriting a manual edit after synchronization.

Skipped unsupported metadata returns `status: degraded` with warnings. Context adapters must surface this rather than treating an incomplete scan as healthy.

`store.snapshot_context(audience='internal', budget_chars=6000, limit=5)` (also available on SyncEngine) selects recent active/waiting records and statusless notes/facts using the same visibility, trust, source-freshness and budget checks as retrieval. A task without explicit status is unknown and excluded from this current-state snapshot; no active or completed state is invented. It is explicit: an empty ordinary retrieval query continues to abstain. Receipts remain separate historical agent claims; adapters may include a bounded latest receipt with that label, never convert it into verified facts. New source receipts have an immutable ISO `created_at` timestamp retained on replay.
