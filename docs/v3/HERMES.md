# Hermes Agent harness

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is the fourth supported client
after Codex, Claude Code and Antigravity. It has no project-local hook file; it loads plugins from
`~/.hermes/plugins/<name>/` and calls `register(ctx)` once at startup. V3 therefore ships the
implementation inside the vault and lets the installer plan a two-file shim that the user links
into that directory once.

## What the installer writes

| File | Owner | Purpose |
| --- | --- | --- |
| `.claude/scripts/beyin_v3_hermes.py` | managed | Hook implementation; runs `beyin_v3_hook.py --harness hermes` as a bounded subprocess |
| `.claude/hermes-plugin/__init__.py` | managed | Shim that pins this machine's vault path and imports the module above |
| `.claude/hermes-plugin/plugin.yaml` | managed | Hermes plugin manifest (`pre_llm_call`, `on_session_finalize`) |

All three roll back with `beyin.py rollback` / `--uninstall` like every other managed file.

## Linking the plugin (one time per machine)

Hermes loads user plugins only after they are explicitly enabled, so linking alone is not
enough. `HERMES_HOME` must be the profile of the Hermes process you actually run
(`~/.hermes` by default on macOS/Linux, `%LOCALAPPDATA%\hermes` on native Windows; a named
profile needs `hermes -p PROFILE` for the enable step).

```sh
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VAULT="/absolute/path/to/vault"
mkdir -p "$HERMES_HOME/plugins"
ln -s "$VAULT/.claude/hermes-plugin" "$HERMES_HOME/plugins/beyin-v3"
hermes plugins enable beyin-v3
hermes plugins list        # beyin-v3 must show "enabled"
```

```powershell
$hermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA 'hermes' }
$vault = 'C:\absolute\path\to\vault'
New-Item -ItemType Directory -Force -Path (Join-Path $hermesHome 'plugins') | Out-Null
New-Item -ItemType Junction -Path (Join-Path $hermesHome 'plugins\beyin-v3') -Target (Join-Path $vault '.claude\hermes-plugin')
hermes plugins enable beyin-v3
hermes plugins list
```

Then restart the Hermes process that uses this profile (`hermes serve`, the gateway or the
CLI). `BEYIN_VAULT` in that process' environment overrides the pinned path, which is what a
shared plugin directory on a multi-vault machine would use. The installer never touches
`HERMES_HOME`; linking, enabling and removing the link on uninstall are operator steps.

## Event mapping

| Hermes hook | V3 event | Context returned |
| --- | --- | --- |
| `pre_llm_call` with `is_first_turn=True` | `SessionStart` | companion continuity + ranked context |
| `pre_llm_call` afterwards | `UserPromptSubmit` (`prompt` = user message) | ranked context for that turn, companion when the query asks for it |
| `on_session_finalize` | `SessionEnd` | none; queues the checkpoint for receipt-gap tracking |

`pre_llm_call` returning `{"context": text}` is the only Hermes channel that injects text into a
turn; the plugin uses nothing else. `on_session_end` is a *turn* outcome in Hermes, not a chat
close, so it is deliberately not bound.

### Gateway and Desktop session boundaries

For tracked sessions, Beyin queues `SessionEnd` only when Hermes delivers
`on_session_finalize`; it does not close remote sessions itself. Closing a tab, starting a new chat, losing a
WebSocket or leaving a session idle is not a portable guarantee that this callback
has already run.

In Hermes Agent [v0.23.2 (`ae2a81ccc00e`)](https://github.com/NousResearch/hermes-agent/blob/ae2a81ccc00e9b7c06301523e5bb09608397149c/gateway/session_viewer_close.py),
an explicit viewer close completes when the last viewer has left and the session's
work queue is idle. A viewer that reattaches can cancel the pending close. Orphan
cleanup after a disconnect has a configurable grace period; setting it to zero
disables that cleanup. Resource-only idle cache eviction is not the same as ending
a durable conversation. Reset and shutdown paths can finalize sessions, but their
delivery depends on the client, gateway and profile in use.

These are pinned upstream source semantics, not verification of a particular
Desktop client paired with a remote gateway. To diagnose a missing boundary,
record the client/gateway versions, whether another viewer remains, and sanitized
hook event names/timing. Do not assume a universal six-hour timeout or manufacture
`SessionEnd` events from elapsed time. See the upstream
[finalization contract](https://github.com/NousResearch/hermes-agent/blob/ae2a81ccc00e9b7c06301523e5bb09608397149c/website/docs/user-guide/features/hooks.md#on_session_finalize).

### Unattended sessions

Hermes passes `platform` to `pre_llm_call`. Sessions on platforms nobody sits in front of
(`a2a`, `cron`, `telegram`, `discord`, `slack`, `whatsapp`, `signal`, `matrix`, `email`,
`sms`, `webhook`; see `DEFAULT_UNATTENDED_PLATFORMS`) still receive context, but their finalize does **not**
queue `SessionEnd`. Those sessions never submit receipts, so counting them as checkpoints would
make every later interactive start warn about "missing receipts". The platform is remembered
from the first turn because Hermes reports a generic value at finalize. Unknown or absent
platforms are tracked normally.

#### Customizing platforms (`state/hermes-platforms.json`)

If Hermes is used with Slack (or another chat channel) as the primary interactive personal assistant,
that channel can be marked as attended so 15-turn receipt reminders and `SessionEnd` checkpoint tracking are active:

```bash
# Treat Slack as attended/interactive:
python3 beyin.py preferences --hermes-attended-platform slack

# Treat a custom bot platform as unattended:
python3 beyin.py preferences --hermes-unattended-platform custom-bot

# Reset platform overrides to default:
python3 beyin.py preferences --hermes-reset-platforms
```

The setting is stored in `state/hermes-platforms.json` (machine-local and rollback-safe; never touches `.beyin-preferences.json`).

### Receipt reminder

Every `REMINDER_EVERY` (15) user turns of an interactive session the plugin appends a one-line
`[Hafıza]` reminder to write a receipt — the same cadence the V2 Hermes adapter used. It is
plain text inside the injected context, not an instruction the adapter enforces.

In Beyin 3.13.0, the adapter keeps the turn count and first-turn platform in process memory.
Restarting the gateway or recreating the hooks loses both. The reminder neither
writes a receipt nor blocks a turn from finishing.

Retrieval semantics are identical to the other harnesses (`tests/v3_hermes_test.py` asserts the
Claude and Hermes outputs for the same query are byte-equal). Preferences (`beyin.py preferences`)
apply unchanged: `context_mode`, `context_chars`, `interval_minutes` and `auto_sync` all govern the
Hermes turns too.

## Failure behaviour

The subprocess waits at most `HOOK_TIMEOUT` (6 s POSIX / 20 s Windows, inside Hermes' default
30 s hook budget). Two failure classes are reported in two places:

- Failures the adapter handles itself (sync conflicts, degraded sources, invalid payloads) are
  written to the adapter's `hook-error.json` and surfaced by `beyin.py doctor`.
- Transport failures (interpreter missing, timeout, non-zero exit, unparsable stdout) happen
  before or outside the adapter, so they cannot reach `hook-error.json`. They return no context
  for that turn and are logged as warnings through Hermes' plugin logger (`beyin_v3_hermes`),
  without the prompt, payload or child output.

A vault without `.beyin-runtime.json` registers no hooks at all so Hermes stays usable when the
vault is unmounted. A damaged runtime file (invalid JSON, missing or relative `state`) also
registers nothing and logs the exception class only; fix the file and restart to re-enable.

Hook metadata files carry the event name, harness and a hashed session id, never the prompt.

## Receipts

Agents running inside Hermes submit receipts with `--harness hermes`; `doctor` lists `hermes`
under `lifecycle`. The `beyin` skill's receipt step already says "choose the current client", so no
skill text changes were needed beyond naming the fourth option.

### Interpreting missing lifecycle observations

A `receipt_checkpoints` row with `at = 0` means Beyin has not recorded a terminal
checkpoint for it. That alone does not prove the session is still open, that work
was completed, or that a receipt is missing. Current coverage and gap calculation
exclude those rows, and also exclude rows whose latest recorded turn is newer than their
last terminal checkpoint. They are not an extra missing-receipt denominator; there
is currently no separate open-session count in that output.

Hermes does not yet map write-tool completion events, so `edit_at = 0` is not
evidence of a read-only conversation. An elapsed-time cleanup or a retroactive
receipt cannot repair unobserved lifecycle events or establish source attribution
by itself. Keep unknown lifecycle state distinct from a closed checkpoint with no
matching structured receipt.

## Not covered

This adapter does not map compaction or tool-completion events. Hermes exposes `post_tool_call`,
but selective write-tool mapping is outside this change's scope. Prompts longer than the
adapter's 1 000 000-character stdin limit are rejected by the adapter itself (logged in
`hook-error.json`) and produce no queue event; Hermes turns are far below that.

The unittest suite drives the installed shim and the hook mapping with a fake plugin context;
it does not establish live model or remote Desktop delivery. Sanitized operator `doctor`
output for a real Hermes profile is attached to the pull request separately and is not part
of the synthetic CI canaries.

### Hook çalışma dizini

Hook payload'ındaki açık `cwd` önceliklidir. Desteklenen Hermes sürümlerinde
`PluginContext.session_cwd()` her turda okunur: oturumun kayıtlı dizini veya profilin
terminal dizini kullanılır; Desktop/CLI çalışma alanı vault ile değiştirilmez.
Geçersiz açık dizin ve context okuma hatası başka bir projeye yönlendirilmez.
Yeni turda dizin yoksa finalize için son seçim saklanır; bu dizin yeni tura taşınmaz.
Açık geçersiz dizin veya context okuma hatası önceki seçimin yeniden kullanılmasını
engeller. Finalize başka bir oturumun context'ini okumaz. Bu kayıt yalnız eklenti
sürecinin belleğindedir; yeniden başlatma sonrası, yeni bir tur görülmeden yapılan
finalize önceki dizini geri getiremez. Bu seçim yalnız proje ilişkilendirmesidir,
platform yetkisi değildir.

Dizini olmayan yerleşik gateway kanalları ve `hermes-platforms.json` içindeki özel
kanallar vault'u kullanır. Yerel `cli/tui/gui/desktop/acp/tool/oneshot` etiketleri bu
fallback'e dahil değildir; bilinmeyen, yapılandırılmamış etiketler gateway sayılmaz.
Eski Hermes'te `session_cwd` API'si yoksa mevcut terminal davranışı korunur.
Katılım/makbuz ayarları değişmez.
