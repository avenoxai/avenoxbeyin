# avenoxbeyin v2 — Master Specification

Status: BUILD CONTRACT. Implementation lanes implement EXACTLY this. Where this spec is silent, choose the simplest behavior consistent with the principles. Do NOT consult external repositories or other codebases for implementation reference — this is a clean-room build from this spec only. Do NOT read files outside this repository.

## 0. Mission & principles

v1 shipped a folder skeleton + 3 hooks + memory files whose upkeep depended entirely on the LLM remembering to write. v2's thesis: **memory must be a mechanism, not a discipline.** New in v2: automatic session flush → daily logs → once-daily knowledge compilation, pre-compaction capture, a health doctor, a history-import path, and an in-place upgrade for existing v1 vaults.

### v2.2 multi-harness addendum

v2.2 adds Google Antigravity through `.agents/hooks.json` and a thin adapter in the canonical
`.claude/scripts` engine. Claude Code, Codex and Antigravity do not carry separate copies of
`flush.py`, `compile.py`, hooks or skills.

Claude Code remains the background summarizer/compiler runtime, but the operator surface may be
Claude Code, Codex or Antigravity. On POSIX, `CLAUDE.md` is the canonical router and `AGENTS.md` points to it;
`.claude/skills` is canonical and `.agents/skills` points to it; `.claude/hooks` is canonical and
`.codex/hooks` points to it. `.codex/hooks.json` is rendered at install/upgrade time with absolute
paths because Codex sets neither project-directory environment variable. Codex `SessionEnd` is
three seconds and only detaches work. Users approve changed project hook hashes themselves via
`/hooks`; the installer never edits the global trust store. Codex rollout `event_msg` user/agent
records feed the same `flush.py` parser as Claude transcripts.

### v2.3 graph-integrity addendum

v2.3 adds an Obsidian-aware graph scanner for broken wikilinks and advisory orphan-note
reporting. Existing v2.2 vaults are upgradeable so the new script cannot be skipped by an
already-current version stamp.

Principles (binding):
1. **Zero cost to the user.** Everything runs on the user's existing Claude subscription via `claude -p`. No API keys required anywhere. No paid services. Optional things stay optional and free.
2. **Zero dependencies.** bash + python3 stdlib only. No pip, no uv, no npm. If python3 is missing, degrade loudly, never silently.
3. **Never destroy.** Upgrades never overwrite user memory files. Scripts never delete user content. All destructive-looking ops are additive or ask first.
4. **Fail loud, run quiet.** Background work is silent when healthy; failures must surface (state files the doctor reads + a visible warning line where possible). No silent no-ops.
5. **Portable.** macOS + Linux for all shell code (BSD/GNU differences handled). Windows: documented as WSL-recommended, not silently broken.
6. **Turkish-first UX.** User-facing text (nudges, reports, docs) Turkish; code comments and this spec English.

## 1. Repo layout (v2 target)

```
avenoxbeyin/
├── LICENSE                        (MIT, unchanged)
├── README.md                      (v2 rewrite — lane D)
├── SETUP.md                       (v2 rewrite: fresh install + upgrade — lane D)
├── docs/
│   └── beyin-v2.md                (new public spec for avenox.io/beyin.md — lane D)
└── template/
    ├── CLAUDE.md                  (v2 router-style — lane O1)
    ├── .beyin-version             (single line: `2.3.0` — graph-integrity release)
    ├── .gitignore                 (v2 — lane D; see §2.4)
    ├── .claude/
    │   ├── settings.json          (v2 hook wiring — lane C2)
    │   ├── hooks/
    │   │   ├── lib.sh             (shared helpers — lane C2)
    │   │   ├── session-start.sh   (lane C2)
    │   │   ├── prompt-counter.sh  (lane C2)
    │   │   ├── session-end.sh     (lane C2)
    │   │   └── pre-compact.sh     (lane C2)
    │   ├── scripts/
    │   │   ├── flush.py           (lane C1)
    │   │   ├── compile.py         (lane C1)
    │   │   └── .state/.gitkeep    (runtime state, gitignored contents)
    │   └── skills/
    │       ├── beyin-doktor/SKILL.md    (lane O1)
    │       └── gecmis-import/SKILL.md   (lane O1)
    ├── 📥 000-Inbox/Dump/.gitkeep
    ├── 🎯 100-Command-Center/Dashboard.md   (unchanged from v1)
    ├── 🏰 300-Projects/.gitkeep
    ├── 🧠 500-Knowledge/.gitkeep            (kept for human notes)
    ├── 🛠️ 600-Arsenal/.gitkeep
    ├── 🔮 850-Companion/
    │   ├── Core.md                (unchanged)
    │   ├── Journal.md             (unchanged)
    │   ├── Last-Session.md        (unchanged)
    │   ├── Threads.md             (unchanged)
    │   └── Kurallar.md            (NEW seed — lane O1; see §5.3)
    ├── daily/.gitkeep             (NEW: machine-written daily logs)
    ├── knowledge/
    │   ├── index.md               (NEW seed — lane O1)
    │   ├── log.md                 (NEW seed — lane O1)
    │   ├── concepts/.gitkeep
    │   └── connections/.gitkeep
    ├── 📦 900-Archive/.gitkeep
    └── 📋 Templates/Note.md       (unchanged)
```

The companion memory folder stays hardcoded `🔮 850-Companion` (v1 decision, hooks reference the fixed path).

## 2. Shared contracts (ALL lanes)

### 2.1 Recursion guard (CRITICAL)
Background scripts invoke `claude -p`, which fires this same vault's hooks. Every hook MUST begin (after shebang/set lines) with:
```bash
[ -n "${BEYIN_INVOKED_BY:-}" ] && exit 0
```
`flush.py` and `compile.py` MUST set `BEYIN_INVOKED_BY=beyin-scripts` in the child environment of every `claude` subprocess they spawn.

### 2.2 State directory
`template/.claude/scripts/.state/` holds ALL runtime state:
- `session_start_time`, `prompt_count` (hooks)
- `needs_reflection` (debt marker)
- `last-flush.json` — `{"session_id": str, "ts": epoch}` dedup guard
- `compile-state.json` — `{"ingested": {"<daily-file-name>": "<sha256>"}, "last_run": iso, "last_status": "ok"|"fail:<reason>", "runs": [last 20 run records]}`
- `compile.lock` — flock target
- `compile-trigger-YYYY-MM-DD` — O_CREAT|O_EXCL once-per-day claim files
- `health.json` — optional, scripts append last error for the doctor
Never store state anywhere else. Everything in `.state/` is gitignored.

### 2.3 Portability helpers (`lib.sh`, lane C2, sourced by all hooks)
- `beyin_mtime <file>` → `stat -f %m` (BSD) falling back to `stat -c %Y` (GNU)
- `beyin_json_escape` → python3-based JSON string escape; if python3 missing, print nothing and set a marker file `.state/python3-missing` (doctor reads it), and emit the hook JSON with a plain ASCII fallback message built with sed-escaping (best effort, never emit malformed JSON — if unsure, emit nothing but ALWAYS write the marker).
- `beyin_emit <event> <text>` → prints `{"hookSpecificOutput":{"hookEventName":"<event>","additionalContext":<escaped>}}`
- All hooks `exit 0` always.

### 2.4 .gitignore (template) additions
```
.claude/settings.local.json
.claude/hooks/.state/
.claude/scripts/.state/*
!.claude/scripts/.state/.gitkeep
.DS_Store
```

### 2.5 `claude -p` invocation contract (scripts)
- Binary discovery follows the invoking harness: `claude` for Claude/Codex hooks, `agy` for the
  Antigravity adapter. A missing runner writes the matching health error and exits quietly; the
  doctor surfaces it.
- Flush model: `--model haiku`. Compile model: `--model sonnet`. Use aliases, never dated model IDs.
- Flush call: `claude -p --model haiku --output-format text` with the prompt passed via stdin, cwd = a temp dir OUTSIDE the vault (so project hooks/CLAUDE.md don't load), env includes `BEYIN_INVOKED_BY`. subprocess timeout 240s.
- Compile call: `claude -p --model sonnet --output-format text --permission-mode acceptEdits --allowedTools "Read,Write,Edit,Glob,Grep"` with cwd = vault root (it must edit knowledge/), env includes `BEYIN_INVOKED_BY`, subprocess timeout 900s.
- On non-zero exit or timeout: record in `compile-state.json`/`health.json`, never retry in the same run, never crash.

## 3. Hooks (lane C2)

`settings.json` wires (paths via `"$CLAUDE_PROJECT_DIR"`):
- SessionStart → `session-start.sh` (timeout 15)
- UserPromptSubmit → `prompt-counter.sh` (timeout 5)
- SessionEnd → `session-end.sh` (timeout 10)
- PreCompact → `pre-compact.sh` (timeout 10)

### 3.1 session-start.sh
v1 behavior kept (state reset; needs_reflection surfacing; Last-Session current block ≤50 lines; Threads active ≤12 lines) PLUS:
1. **Kurallar injection:** if `🔮 850-Companion/Kurallar.md` exists, inject up to its first 60 lines under header `[Hafıza — Kurallar]`.
2. **Journal bridge (fixes v1 "write-only Journal"):** inject the LAST `## ` entry of `🔮 850-Companion/Journal.md` (max 10 lines) under `[Hafıza — Son Journal]`.
3. **Knowledge injection:** if `knowledge/index.md` exists, inject its first 150 lines under `[Bilgi Tabanı — İndeks]`; then the last 25 lines of today's (else yesterday's) `daily/*.md` under `[Bugünün Logu]`.
4. **Total cap 16,000 chars.** If over, truncate knowledge index section first, then daily tail; NEVER truncate Last-Session/Threads/Kurallar. When truncating, append line `[not: indeks kırpıldı — beyin-doktor çalıştır]`.
5. Closing identity line (v1 style) + `Hafıza protokolü zorunludur.`

### 3.2 prompt-counter.sh
Counter as v1, but nudge fires at every multiple of 15 (`COUNT % 15 == 0`), message notes the count. Text (Turkish, no em dashes): `[Hafıza] <N>. mesaj. Oturum sonunda 🔮 850-Companion/Last-Session.md ve Threads.md güncellemeyi unutma.`

### 3.3 session-end.sh
1. Reflection-debt check exactly as v1 (using `beyin_mtime`).
2. **NEW:** spawn flush detached and return immediately (SessionEnd budget is tight):
   `nohup python3 "$CLAUDE_PROJECT_DIR/.claude/scripts/flush.py" --hook-input /dev/stdin >/dev/null 2>&1 &` — pass the hook stdin JSON through (read it into a temp file first: hooks receive JSON on stdin containing `session_id` and `transcript_path`; write stdin to `.state/hookin-$$.json` and pass that path via `--hook-input`). The hook itself must complete in <1s.
3. Cleanup of session state files as v1.

### 3.4 pre-compact.sh
Same flush spawn as 3.3 step 2 (with `--reason precompact`). No other duties.

### 3.5 Quality bar
`bash -n` clean on every script; runs on macOS bash 3.2 AND Linux bash 5 (no bash-4-only features: no associative arrays, no `${var,,}`); shellcheck-clean where reasonable. Test each hook by piping a synthetic hook-input JSON and asserting output JSON parses (python3 json.loads) — include these as `tests/hooks_test.sh` at repo root (runnable, self-contained, creates a temp vault).

## 4. Scripts (lane C1) — python3 stdlib only, PEP 8, no deps

### 4.1 flush.py
CLI: `flush.py --hook-input <path-to-json> [--reason sessionend|precompact]`
1. Parse hook input JSON → `session_id`, `transcript_path`. Malformed → attempt regex repair of invalid backslash escapes, else record health error and exit 0.
2. Recursion guard: if env `BEYIN_INVOKED_BY` set → exit 0 immediately.
3. Dedup: if `last-flush.json` has same `session_id` within 60s → exit 0.
4. Read transcript JSONL. Extract user/assistant text blocks (skip tool_use/tool_result/thinking). Flatten to `**User:** …` / `**Assistant:** …` lines. Keep last 30 turns; cap 15,000 chars, cutting at a `\n**` boundary. Min turns: sessionend ≥1, precompact ≥5; below min → exit 0 (write last-flush.json first).
5. Build Turkish prompt asking for a structured summary with EXACTLY these sections: `Bağlam / Önemli Konuşmalar / Alınan Kararlar / Öğrenilenler / Yapılacaklar`. If nothing meaningful, the model must answer exactly `FLUSH_BOS`.
6. Invoke `claude -p` per §2.5. If output is `FLUSH_BOS` (or empty) → append nothing, still update `last-flush.json`.
7. Else append to `daily/YYYY-MM-DD.md` (create with skeleton `# Günlük Log: YYYY-MM-DD\n\n## Oturumlar\n` if absent) as `### Oturum (HH:MM)` (+ ` — compaction öncesi` when precompact).
8. Call `maybe_trigger_compile()`: gates in order — (a) local hour ≥ 18; (b) any daily file's sha256 differs from `compile-state.json.ingested`; (c) atomic once-per-day claim `os.open(trigger, O_CREAT|O_EXCL)`. All pass → `subprocess.Popen([sys.executable, compile.py], start_new_session=True, stdout/err → DEVNULL, env includes BEYIN_INVOKED_BY unset for compile itself but compile sets it on ITS claude children)`.
9. Every failure path writes `health.json` (`{"ts":…, "component":"flush", "error": "..."}`) and exits 0.

### 4.2 compile.py
1. `fcntl.flock(compile.lock, LOCK_EX|LOCK_NB)`; busy → exit 0.
2. Select changed daily logs by sha256 vs `ingested` (skip today's file if `--include-today` not passed: default INCLUDES today since we run evenings; keep simple: include all changed).
3. Per changed log (ordered by date): build the compile prompt (see 4.3) and invoke `claude -p` per §2.5 compile contract. On failure: record `fail:<reason>` and STOP the batch (no repeated quota burn). On success: update hash in `ingested`, append run record.
4. `--dry-run` flag: print what would compile, no calls.

### 4.3 Compile prompt (embedded in compile.py as a template string)
Contains, in order: (a) the memory schema rules below; (b) the CURRENT full text of `knowledge/index.md`; (c) the daily log body; (d) instructions:
- Extract 2-6 lasting concepts. For each: create or update `knowledge/concepts/<kebab-slug>.md` with YAML frontmatter `title, aliases, tags, sources (list of daily files), created, updated`, body: `# Title`, 2-4 sentence core, `## Önemli Noktalar` (3-5 bullets), `## Detaylar`, `## İlgili Kavramlar` (≥2 `[[wikilink]]`s each with one sentence on HOW it connects), `## Kaynaklar`.
- Where two concepts connect non-trivially: `knowledge/connections/<a>--<b>.md` with `connects: [a, b]` frontmatter and sections `## Bağlantı / ## Ana Fikir`.
- Update the `knowledge/index.md` table (columns: Makale | Özet | Kaynak | Güncellendi) — one row per article, update existing rows in place.
- Append one block to `knowledge/log.md`: `## [<ISO ts>] compile | <daily file>` + created/updated lists + 2-3 sentence note.
- Context discipline: the provided index is the only preloaded context; use Grep/Read for specific candidate articles only; NEVER bulk-read the knowledge directory.
- Language: articles in Turkish (the user's language); slugs ASCII kebab-case.
- Contradiction rule: if new info contradicts an existing article, UPDATE the article to the corrected state and note the correction in its body ("Güncelleme: ...") — do not append contradictory duplicates.

### 4.4 Tests (lane C1 delivers `tests/scripts_test.py`)
Self-contained, stdlib, runnable `python3 tests/scripts_test.py`: builds a temp vault; fakes `claude` with a stub executable on PATH echoing canned output; asserts: transcript extraction (turn cap, char cap, boundary snap), FLUSH_BOS path appends nothing, daily skeleton creation, dedup guard, trigger gates (hour gate via injectable clock — read hour from env `BEYIN_FAKE_HOUR` when set), O_EXCL single-claim, flock exclusion, hash-skip on unchanged logs, batch stop on first failure, recursion guard exit. NO network, NO real claude calls in tests.

## 5. Skills & template content (lane O1)

### 5.1 beyin-doktor/SKILL.md
Turkish skill, trigger phrases "beyin doktor", "doktor", "sağlık kontrolü". Instructs Claude to run (via Bash) a series of checks and render one table with 🟢/🟡/🔴 per row + fix-it line for each red:
hooks present+executable+wired in settings.json; recursion-guard line present in each hook; python3 and claude CLI on PATH; `.state/python3-missing` marker; daily log freshness (≤48h); compile `last_run` ≤48h and `last_status` ok; health.json recent errors; knowledge/index.md line count vs the 150-line injection window (warn >300 lines → "özet indeks zamanı"); ` 2.` iCloud-conflict files anywhere in vault (list them); git repo present + uncommitted count; `.beyin-version` value (v1 detected → offer upgrade); Kurallar.md exists. End with one-line verdict. The skill contains the exact shell one-liners for each check (portable per §2.3 rules).

### 5.2 gecmis-import/SKILL.md
Turkish skill "geçmiş import", "takeout", "chatgpt geçmişi". Handles ChatGPT export (`conversations.json` in export zip), Claude export, and Gemini Takeout. Instructions: ask user for the export file path; parse with an embedded python3 stdlib snippet (ChatGPT mapping-tree flattening documented in the skill); group conversations by month; for each month write `daily/import-YYYY-MM.md` in the daily-log section format (so the normal compiler ingests them); THEN tell the user compilation happens gradually (one compile run per evening) OR offer `python3 .claude/scripts/compile.py` manually run per batch; honest note: very large histories take several evenings and consume subscription limit share. Never upload the export anywhere; everything local. Cap: if export >50MB, sample most recent 12 months first and say so.

### 5.3 Template content
- **CLAUDE.md v2 (router style):** ≤40 lines. Identity paragraph (companion name placeholder, Turkish default, tone), then a LOAD ORDER list (Core.md → Last-Session auto-injected → Kurallar auto-injected), then ROUTE-BY-TASK table (görev tipi → hangi dosya/klasör), memory protocol section (now: "the machine writes daily/; you still update Last-Session/Threads for the relational layer"), handoff rule ("her anlamlı oturum iz bırakır: ya not, ya karar, ya güncellenmiş dosya"), and the verify clause ("bu dosya yönlendiricidir; proje gerçeği için güncel dosyaları doğrula").
- **Kurallar.md seed:** frontmatter + 3 example rules in the format `- **kural:** … **neden:** …` (one about tone, one about a workflow, one placeholder) + instruction line that the companion appends here when the user corrects it ("bunu böyle yapma" → kural olur).
- **knowledge/index.md seed:** title + empty table with headers `| Makale | Özet | Kaynak | Güncellendi |` + one line explaining the compiler fills it. **knowledge/log.md seed:** title + one line.

## 6. Docs (lane D)

### 6.1 SETUP.md v2
Two entry modes, decided FIRST: **(A) Fresh install** — v1 flow (interview → copy template → personalize placeholders) + NEW steps: `git init` + first commit in the vault; verify hooks executable; run a first `beyin-doktor`. **(B) Upgrade from v1** — detection: existing vault with `CLAUDE.md` + `🔮 850-Companion/` but no `.beyin-version`. Rules: NEVER touch existing 🔮 850-Companion/*.md, Dashboard, or user content; ADD `daily/`, `knowledge/`, `.claude/scripts/`, skills, Kurallar.md (seed only if absent), `.beyin-version`; REPLACE `.claude/hooks/*.sh` and MERGE `settings.json` hook entries idempotently (if a hook event already has the entry, skip); keep the user's chosen companion name inside file contents (folder stays `850-Companion`); end with doctor run. Both paths end with the same "sihri göster" demo: talk, exit, relaunch, next morning knowledge/ has articles.
Keep v1's rules (Turkish default, interview first, never destroy, resolve placeholders, verify each phase). Include the "be the demo" narration rule that was dropped from v1's SETUP (restore it).

### 6.2 docs/beyin-v2.md (new avenox.io/beyin.md)
Same self-contained structure as v1's public spec but v2: FAST PATH = clone repo + SETUP.md (unchanged mechanism); adds an early branch: "Mevcut beyin var mı?" → upgrade path; the from-scratch fallback phases updated to include scripts/skills (may reference the repo as required for scripts — from-scratch no longer duplicates the python code inline; instead instruct fallback users to fetch the two script files from the repo raw URLs, or degrade to v1-style hooks-only with a note). Keep: Turkish narration rule, skip-the-video framing, placeholders table, mem0 as optional free add-on (unchanged wording about free tier), final report format. Add honest cost paragraph: "Ekstra ücret yok; arka plan özetleyici ve derleyici mevcut Claude aboneliğinin günlük limitinden küçük bir pay kullanır (özet: her oturum sonunda küçük bir Haiku çağrısı; derleme: günde bir Sonnet çağrısı)."

### 6.3 README.md v2
Rewrite: what it is (2 paragraphs, thesis "hafıza rica değil mekanizma"), v1→v2 comparison table, quickstart (3 commands), upgrade note, architecture ASCII diagram of the flush→daily→compile pipeline, cost honesty paragraph (same as 6.2), credits: "Bilgi derleme mimarisi Andrej Karpathy'nin LLM bilgi tabanı desenine dayanır" with gist link. MIT.
### 6.4 `.beyin-version`: file containing `2.3.0`.

## 7. Integration gates (run by the architect after lanes land)
1. `bash tests/hooks_test.sh` green.
2. `python3 tests/scripts_test.py` green.
3. `python3 -m py_compile` on both scripts; `bash -n` on all hooks.
4. Manual smoke: temp vault, synthetic transcript, fake `claude` stub → flush writes daily entry; `BEYIN_FAKE_HOUR=19` triggers compile path.
5. grep gates: no em/en dashes in any Turkish user-facing text; no absolute paths; no personal data (grep -i 'avenox\|taha\|echo' in template/ must only hit sanctioned mentions in README credits).
6. Every `{{PLACEHOLDER}}` documented in SETUP.

## 8. Lane ownership (hard file boundaries)
- **C1 (codex):** `template/.claude/scripts/flush.py`, `compile.py`, `tests/scripts_test.py`
- **C2 (codex):** `template/.claude/hooks/*`, `template/.claude/settings.json`, `tests/hooks_test.sh`
- **O1 (opus):** `template/.claude/skills/**`, `template/CLAUDE.md`, `template/🔮 850-Companion/Kurallar.md`, `template/knowledge/index.md`, `template/knowledge/log.md`
- **D (opus):** `README.md`, `SETUP.md`, `docs/beyin-v2.md`, `template/.beyin-version`, `template/.gitignore`, gitkeeps
No lane touches another lane's files. Commit convention: each lane commits its own files with message `v2(<lane>): <what>` on branch v2.
