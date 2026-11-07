# V3 local memory foundation

This module is an opt-in foundation, not a replacement installer or lifecycle migration. Existing hooks and the isolated compiler are unchanged. It makes no model calls and imports no remote provider. It must be given an existing synthetic or explicitly authorized vault and a runtime directory outside that vault.

`template/.claude/scripts/beyin_v3.py` exposes `MemoryStore(state_dir, vault_root)`. SQLite records and receipts persist in `state_dir/memory.sqlite3`; the runtime directory is private to the local account. Each operation owns and closes its connection. Write operations use `BEGIN IMMEDIATE` transactions; this is local process coordination, not distributed cloud synchronization.

## Hook acknowledgement retention

Every processed hook event is first acknowledged as a private `state/hook-done/<sha256>.json` marker. These recent markers prevent duplicate work without needing to read the database when the same event is immediately replayed.

On successful queue drains, `beyin_v3_hook_done.py` migrates markers older than seven days into the local `memory.sqlite3` table `hook_done_ledger` (at most 256 per run). Its row stores only the event hash, harness and lifecycle event name, not prompt or session text. The SQLite transaction commits **before** the JSON is removed: if a worker crashes between those steps, both copies are harmless and `doctor` counts the event once. A failed migration never deletes its marker. When no old markers are found, the scan is limited to once per day.

Newly delivered events are checked against both the live markers and this ledger. `doctor` combines recent JSON and archived lifecycle metadata so migration does not reset previously observed clients or inflate `acknowledged_events`. This is local-only and requires no extra service or dependency.

The ledger intentionally **retains event hashes indefinitely**, because removing old hashes would allow very late event-ID replays. This change bounds the number of individual JSON files during ordinary use; it does not promise bounded SQLite storage or a measured speed-up. A state reset, backup or restore should keep `memory.sqlite3` and `hook-done/` together.

## Records and source references

`ingest(record)` requires `id`, `text`, and `source`. The source must be an existing relative file resolving inside the vault. Traversal and escaping symlinks are rejected. Records preserve all supplied structured fields, including `facts`, `kind`, `project`, `status`, `visibility`, `updated_at`, and `supersedes`. Facts default to an empty mapping, revision to 1, visibility to internal, and supersedes to an empty list. When `updated_at` is absent or blank it is filled from the first of `updated`, `modified`, `last_modified`, `date_modified` whose value is a string beginning with an ISO date; other values are ignored and file modification time is never used, because synchronized vaults rewrite it. The alias stays in the record and the source file is not rewritten.

Ingesting the exact same record is idempotent. Reusing an existing ID for different content fails. `update_task(id, expected_revision, changes)` performs a locked revision comparison and increments the revision; it does not permit changing the record ID or directly setting its revision. These updates affect the local record store, not source Markdown files. They do not claim external verification or completion.

`submit_receipt(event_id, summary, refs, harness)` requires existing source references. Replaying the same semantic payload, including from the other harness, returns the original receipt. Changing summary or references under an existing event ID fails. First-writer harness attribution is retained. Receipts are stored locally and do not automatically append to daily notes or project views.

The Claude/Codex Stop learning reminder treats knowledge references as citations, not proof that a note was updated. Its filesystem evidence accepts either a distilled Markdown note cited in the receipt's `refs` with modification time at or after the edit window began, or a PostToolUse editor write of such a note (Claude file path, Codex apply_patch header) in the same window, since that write precedes the hook that opens the window. The editor check examines all parsed paths in the hook's bounded input payload, rather than only the first five. File symlinks are rejected, and resolved paths must remain inside the vault and within the existing distillation roots, outside generated-file exclusions; the mtime check also requires the cited path itself to name a distilled note. An unrelated recent note elsewhere in the vault no longer satisfies the receipt's learning claim, and citing an older note alone does not suppress the reminder. Candidates staged for this session in the current edit window can also satisfy the separate candidate-ledger check. Filesystem evidence remains a time heuristic, not content-hash verification, proof of authorship or a semantic check that the declared learning was saved; a sync tool can change modification times without changing content.

## Retrieval and harness parity

`retrieve(query, project=None, audience='internal', statuses=None, limit=5, budget_chars=8000)` returns records, source citations, abstention, truncation, omitted count, and a visible character budget. Retrieval uses normalized Turkish/English lexical tokens, conservative query stopwords and removal of the explicitly selected project token. It has no embeddings, natural-language reasoning, or inferred task completion.

Turkish is agglutinative, so every token is reduced to one canonical stem by a small suffix stripper before scoring, on the query side and the document side alike. It peels an ordered longest-match table of nominal suffixes to a fixpoint, refuses to touch words shorter than five characters, keeps a stem floor of three characters for plurals and four or five for the rest, and gates each suffix on the preceding sound plus a weak vowel-harmony check. That is why `farkı` finds a note about `fark` and `notlar` finds a note about `not`. Consonant softening is handled for p/b and k/ğ: a stem ending in a vowel plus `b` or `g` (soft g is folded to `g`) is written back with `p` or `k`, so `kitabı`, `örneği` and `temizliği` meet `kitap`, `örnek` and `temizlik`. Known limits: it is a heuristic with no lexicon, so it does not model t/d or nk/ng softening (`ümit`/`ümidi`, `renk`/`rengi` stay apart; d would merge past tense stems such as `denedi` into `denetim`), vowel loss or doubling (`kayıt`/`kaydı`, `hak`/`hakkı`), and it cannot strip suffixes off three-letter roots other than plurals (`ev`/`evden`), it over-peels some words into non-words that only have to be consistent (`araba` and `arabanın` both become `arab`), and it merges a small number of English words that happen to end in a Turkish suffix (`quota`/`quote`, `viola`/`violin`); measured over 198k dictionary words, 4.5 percent land in a merged group, while `table`/`tab`, `handle`/`hand`, `golden`/`gol`, `garden`/`gar` and `state`/`sta` stay apart. Verb inflection is out of scope. Stems replace tokens, they are never added alongside them, so a score still counts matched query words and `STRICT_MIN_SHARED` keeps its meaning. Nothing is stored: tokens are computed per query, so an existing vault needs no reindex after upgrading.

Filters apply before scoring. Public sees public; internal sees internal and public; private is explicit. Untrusted records never enter context. Project filtering is exact. Supersession is resolved over every trusted record, not only the eligible ones: a `supersedes` value names a note by its explicit id, by vault path (with or without `.md`) or `[[link]]`, or by its file name when exactly one note has it, never by substring. A value that resolves to nothing, to several notes or to the note itself retires nothing; sync reports it as `supersedes_issues` and doctor as `supersedes`, without degrading either. Untrusted notes cannot retire another note. Without explicit statuses every route drops notes whose status starts with a retired word (`superseded`, `retired`, `archived`, `deprecated`, `obsolete`, `replaced`, `arşiv`, `eski`, `emekli`, `geçersiz`); any other status stays deliverable. Explicit status filters select exactly the requested first words, case and Turkish letters folded; a note without a status counts as active, a task without one does not. Non-strict matches (`retrieve`, `candidates`, the `context` command) are ranked by Okapi BM25 over stem counts (`k1` 1.5, `b` 0.75) with timestamp as a tie-breaker, so a long hub note that mentions every query word once no longer outranks the note that is about them; records with no shared term keep timestamp order. Strict matches keep the calibrated weight and thresholds described below. Unknown queries abstain. Structured facts and citations are preserved; oversized text may be clipped with an explicit marker and text_truncated flag when the metadata fits, otherwise the record is omitted; the budget measures serialized record/citation characters, not tokenizer tokens or the entire response envelope.

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
- Managed starter skills: `.agents/skills/{beyin,beyin-doktor,beyin-guncelle}/`, `.claude/skills/...`, and `.kiro/skills/...`.
- Platform launchers: `Beyni Guncelle.cmd`, `Beyni Güncelle.sh`, `Beyni Guncelle.command`, `Beyni Güncelle.desktop`.
- Adapter shims: `.claude/hermes-plugin/`, `.claude/scripts/beyin_v3_kiro.py`, `.opencode/plugins/`, `.omp/hooks/`.
- Harness hook declarations: `.claude/settings.local.json`, `.codex/hooks.json`, `.codex/config.toml`, `.agents/hooks.json`, `.kiro/hooks/beyin-v3.json`.
- Companion instruction blocks: `AGENTS.md`, `CLAUDE.md`.

All other paths belong to the user. In particular, `.brain/` and `custom/` directories are reserved as an untouched, unmanaged user layer. Installer, updater, and `doctor` checks never treat files under `.brain/` or `custom/` as conflicts or unmanaged system intrusions.

## Proje klasörü kapsamlarını yönetme

`.beyin-projects.json` içindeki bulunamayan klasörler uyarı ve doctor tanısı üretir; diğer
projelerin sorguları devam eder. Eksik klasör eşlemesi eski `--no-sync` indeks satırlarının
başka projelere ortak not gibi açılmaması için kapsam sınırı olarak korunur.

`project-scope list` yalnız okur. `project-scope add --folder "Projeler/Deneme" --project deneme`,
`project-scope remove --folder "Projeler/Deneme"` ve `project-scope prune` açıkça kapsam
dosyasını düzenler. `prune` yalnız artık bulunamayan klasör girdilerini çıkarır. Bozuk
veya symlink yapılandırma üzerine yazılmaz; önce dosyayı onar.

## Receipt görünümü çakışması ve kurtarma

`daily/v3/YYYY-MM-DD.md` ve `knowledge/v3/outcomes.md` değişmez receipt kaynaklarından
üretilir. Görünüm elle değişmişse veya yerel takip geçmişi yoksa mevcut baytlar korunur
ve `kind: receipt_view` uyarısı görünür kalır (`sync.status: degraded`). Sağlam kaynak
bağlamı, lifecycle kayıtları ve yeni receipt yazımı devam eder; gerçek kaynak
çakışmaları bağlamı ve geçmişi engellemeye devam eder. `receipt`, `task-update`,
`context` ve `recap` JSON çıktıları ilgili tanıyı `source_sync.warnings` altında
bildirir. `history` JSON liste biçimini korur ve uyarıyı stderr'e yazar.

Takip edilen veya mevcut receipt'lerin üreteceği görünüm, başlığı silinse bile kaynak
not olarak dizine eklenmez. Aynı dosyanın sabit bağlantıları da dışlanır. Önceden
yanlışlıkla dizine alınmış görünüm kayıtları ve bu kayıtlara ait geçmiş olayları
otomatik bağlamdan saklanır; denetim olaylarının baytları değiştirilmez. Görünümün
yerinde sonradan oluşan gerçek bir not yeniden dizine alınabilir. Yerel state tamamen
kaybolsa bile kurtarma arşivinin yanında kaydedilmiş köken bilgisi ve arşiv özeti,
halen mevcut aynı sabit bağlantıyı tanımak için kullanılır. Bu yan kayıt silinirse,
bozulursa veya bağlantı hem yer hem içerik değiştirirse eski köken yeniden
kanıtlanamaz; bu dosyaları elle inceleyin.

Açık kurtarma komutu: `python3 beyin.py sync --recover-views` (Windows: `py -3 beyin.py sync --recover-views`).
Çakışan baytlar görünümün yanındaki `.archive/` içinde her denemeye ayrı bir dosya
olarak kalıcı yazıldıktan sonra görünüm yeniden üretilir. Kurtarma başarısızsa kaynak
ve takip kaydı korunur; symlink arşiv hedefleri kabul edilmez. Komut receipt kaynaklarını
ve runtime veritabanını silmez. Arşivdeki kullanıcı eklemelerini uygun bilgi notlarına taşı.
Sabit bağlantı varsa arşivin yanında küçük bir `.provenance.json` kaydı da saklanır;
bu kaydı arşivle birlikte koruyun.
POSIX'te özgün görünüm değiştirilmeden önce arşiv dosyası, arşiv dizini ve üst dizin
`fsync` bariyerlerinden geçer; bariyer hatası kurtarmayı durdurur. Bu sıra güç kesintisi
veya uzak dosya sistemi için mutlak kalıcılık garantisi değildir. Windows'ta dizin
`fsync` desteği bulunmadığından aynı kalıcılık iddiası yapılmaz.
