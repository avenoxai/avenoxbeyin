# V3 local memory foundation

This module is an opt-in foundation, not a replacement installer or lifecycle migration. Existing hooks and the isolated compiler are unchanged. It makes no model calls and imports no remote provider. It must be given an existing synthetic or explicitly authorized vault and a runtime directory outside that vault.

`template/.claude/scripts/beyin_v3.py` exposes `MemoryStore(state_dir, vault_root)`. SQLite records and receipts persist in `state_dir/memory.sqlite3`; the runtime directory is private to the local account. Each operation owns and closes its connection. Write operations use `BEGIN IMMEDIATE` transactions; this is local process coordination, not distributed cloud synchronization.

## Records and source references

`ingest(record)` requires `id`, `text`, and `source`. The source must be an existing relative file resolving inside the vault. Traversal and escaping symlinks are rejected. Records preserve all supplied structured fields, including `facts`, `kind`, `project`, `status`, `visibility`, `updated_at`, and `supersedes`. Facts default to an empty mapping, revision to 1, visibility to internal, and supersedes to an empty list. When `updated_at` is absent or blank it is filled from the first of `updated`, `modified`, `last_modified`, `date_modified` whose value is a string beginning with an ISO date; other values are ignored and file modification time is never used, because synchronized vaults rewrite it. The alias stays in the record and the source file is not rewritten.

Ingesting the exact same record is idempotent. Reusing an existing ID for different content fails. `update_task(id, expected_revision, changes)` performs a locked revision comparison and increments the revision; it does not permit changing the record ID or directly setting its revision. These updates affect the local record store, not source Markdown files. They do not claim external verification or completion.

`submit_receipt(event_id, summary, refs, harness)` requires existing source references. Replaying the same semantic payload, including from the other harness, returns the original receipt. Changing summary or references under an existing event ID fails. First-writer harness attribution is retained. Receipts are stored locally and do not automatically append to daily notes or project views.

## Retrieval and harness parity

`retrieve(query, project=None, audience='internal', statuses=None, limit=5, budget_chars=8000)` returns records, source citations, abstention, truncation, omitted count, and a visible character budget. Retrieval uses normalized Turkish/English lexical tokens, conservative query stopwords and removal of the explicitly selected project token. It has no embeddings, natural-language reasoning, or inferred task completion.

Turkish is agglutinative, so every token is reduced to one canonical stem by a small suffix stripper before scoring, on the query side and the document side alike. It peels an ordered longest-match table of nominal suffixes to a fixpoint, refuses to touch words shorter than five characters, keeps a stem floor of three characters for plurals and four or five for the rest, and gates each suffix on the preceding sound plus a weak vowel-harmony check. That is why `farkı` finds a note about `fark` and `notlar` finds a note about `not`. Known limits: it is a heuristic with no lexicon, so it does not model consonant mutation (`kitap`/`kitabı` stay apart), it cannot strip suffixes off three-letter roots other than plurals (`ev`/`evden`), it over-peels some words into non-words that only have to be consistent (`araba` and `arabanın` both become `arab`), and it merges a small number of English words that happen to end in a Turkish suffix (`quota`/`quote`, `viola`/`violin`); measured over 198k dictionary words, 4.5 percent land in a merged group, while `table`/`tab`, `handle`/`hand`, `golden`/`gol`, `garden`/`gar` and `state`/`sta` stay apart. Verb inflection is out of scope. Stems replace tokens, they are never added alongside them, so a score still counts matched query words and `STRICT_MIN_SHARED` keeps its meaning. Nothing is stored: tokens are computed per query, so an existing vault needs no reindex after upgrading.

Filters apply before scoring. Public sees public; internal sees internal and public; private is explicit. Untrusted records never enter context. Project filtering is exact. Explicit supersession removes superseded eligible records. Status filters are explicit. Matches are ranked by token overlap with timestamp as a tie-breaker. Unknown queries abstain. Structured facts and citations are preserved; oversized text may be clipped with an explicit marker and text_truncated flag when the metadata fits, otherwise the record is omitted; the budget measures serialized record/citation characters, not tokenizer tokens or the entire response envelope.

The per-turn hook asks for strict context (`context_for(..., strict=True)`), which ranks Markdown passages instead of whole notes (`beyin_v3_passage.py`, #83). Each record's text is split into heading-aware blocks (oversized blocks become overlapping windows of about 900 characters), a block is admitted with the same relative-idf weight family scaled by query coverage, and the best block per source is delivered with its heading path through the same `pack_context` budget and citation contract. Admission uses block frequency, ranking among admitted sources uses source frequency, and the coverage denominator is capped at four terms so a long conversational prompt is not rejected for its length. Frontmatter is never read (records carry the Markdown body), `daily/` and `receipts/` are always excluded, supersession, visibility, trust, project and freshness gates are the same `_eligible()` gates as the note-level path, and explicit project words do not count as matches. The derived index lives in the runtime directory as JSON (`passages.json`, 0600), re-tokenised only for changed records; a cold build runs in one-second steps and meanwhile, like any real error, falls back to the note-level path. An empty passage result is an answer, not a fallback. `retrieval.json` in the runtime directory may set `strict_floor` (default 0.20) and extra `strict_exclude` prefixes; see PREFERENCES.md. `scripts/evaluate_v3_passages.py` measures both paths on a deterministic synthetic corpus or, read-only, on your own vault.

Both clients call `shared_context(store, harness, query, **kwargs)` or `store.context_for(harness, query, **kwargs)`. Codex and Claude use exactly the same retrieval implementation and normalized output. `codex_context` and `claude_context` are convenience wrappers.

`optional_provider()` returns None by default without imports or calls. An enabled provider requires an explicitly supplied factory; integrations must call this intentionally. The local store never depends on the provider's existence or state.

## Validation boundary

The frozen semantic contract and an independent test lane own acceptance. The implementation does not read holdout scenarios or hardcode fixture IDs. Small synthetic lexical gates cannot establish broad language understanding. Real lifecycle delivery, user-facing approval, installer migration, compiler execution and multi-machine reconciliation remain outside this foundation. Existing compiler staging, output allowlists, source hashes and restricted model execution must remain intact if a later migration integrates this store.

## Vault binding and source freshness

Each database is bound to the canonical vault root. Reusing a runtime for a different vault fails rather than mixing contexts. Ingest captures a source SHA256; retrieval excludes missing or changed sources and returns `stale_count` for eligible records. Explicit revision updates refresh the source snapshot; this is not automatic external verification. This foundation's indexed records are authoritative for its state API, not a replacement for canonical Markdown task synchronization. A project-only query lists eligible records in that scope; an unscoped query without meaningful terms abstains.

## Transactional history

Record ingestion and revision updates append an immutable snapshot event inside the same SQLite transaction as the record projection. `history(record_id, audience="internal")` returns committed `{sequence,event_type,record_id,revision,record}` entries in global monotonic sequence order; event types are `ingest` and `update`, and Markdown synchronization adds `delete` when a source is removed or no longer validates. Duplicate ingestion, rejected revisions and validation failures create no events. History is empty when the current record is outside the audience or untrusted, or its source hash is stale; a deleted record is judged by the snapshot in its final `delete` event. Individual events outside the audience are omitted. The CLI `history` command synchronizes first. Returned JSON objects are fresh copies. Receipts retain their separate idempotency table.

Initialization adds the events table to existing foundation databases. Prior mutations remain explicitly unrecorded prehistory; the module does not invent baseline events for old rows. This is a local audit history, not a distributed log or a complete event-replay migration tool.

Supply a dedicated application runtime directory. Initialization sets that exact directory to account-private permissions; never pass a general home, cache, or shared parent directory. Its parent permissions are not modified.

## Managed paths and unmanaged user layer

V3 writes exclusively to its explicit managed vault files:
- Entry point and metadata: `beyin.py`, `.beyin-runtime.json`, `.beyin-version`.
- Scripts: `.claude/scripts/beyin_v3*.py`.
- Managed starter skills: `.agents/skills/{beyin,beyin-doktor,beyin-guncelle}/` and `.claude/skills/...`.
- Platform launchers: `Beyni Guncelle.cmd`, `Beyni Güncelle.sh`, `Beyni Guncelle.command`, `Beyni Güncelle.desktop`.
- Adapter shims: `.claude/hermes-plugin/`, `.opencode/plugins/`, `.omp/hooks/`.
- Harness hook declarations: `.claude/settings.local.json`, `.codex/hooks.json`, `.codex/config.toml`, `.agents/hooks.json`.
- Companion instruction blocks: `AGENTS.md`, `CLAUDE.md`.

All other paths belong to the user. In particular, `.brain/` and `custom/` directories are reserved as an untouched, unmanaged user layer. Installer, updater, and `doctor` checks never treat files under `.brain/` or `custom/` as conflicts or unmanaged system intrusions.
