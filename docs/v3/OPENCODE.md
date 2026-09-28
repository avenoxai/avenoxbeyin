# OpenCode harness

OpenCode has no hook JSON. It loads project plugins from `.opencode/plugins/*.js` when it starts in that directory, so the installer writes one managed file, `.opencode/plugins/beyin-v3.js`, from `template/.claude/scripts/beyin_v3_opencode.py`. There is no link or enable step: open OpenCode in the vault. Rollback and uninstall remove the plugin like every other managed file.

The plugin derives the vault from its own location and the runtime directory from `.beyin-runtime.json`. Only the interpreter is pinned at install time; `BEYIN_PYTHON` overrides it. Every event runs `beyin_v3_hook.py --harness opencode` with a 6 second bound (20 seconds on Windows). A missing interpreter, timeout, non-zero exit or invalid output returns no context and never fails the OpenCode turn. A vault without a valid runtime file registers no hooks.

## Lifecycle mapping

The file has a single `export default { id: "beyin-v3", server, setup }`. OpenCode 1.x reads `server()` and ignores `setup`; OpenCode 2.x validates `{ id, setup }` and ignores `server`. There is no named export, so a loader that walks every export cannot register the hooks twice.

OpenCode 1.x: the first `chat.message` of a session is `SessionStart`; later ones are `UserPromptSubmit` with the non-synthetic text parts as `prompt`. `session.idle` is `Stop`, `session.deleted` is `SessionEnd`, `experimental.session.compacting` is `PreCompact`, and `tool.execute.after` for edit, write and patch tools is `PostToolUse`. `experimental.chat.system.transform` is the only request-time injection channel.

OpenCode 2.x registers the same events on `ctx`: `session.hook("prompt")` for `SessionStart`/`UserPromptSubmit`, `session.hook("context")` for injection (typed `{ type: "text", text }` parts), `tool.hook("execute.after")` for `PostToolUse` and `session.hook("compaction")` for `PreCompact`. 2.x no longer publishes `session.idle`; a finished turn arrives on `ctx.event.subscribe` as `session.execution.succeeded`, `failed` or `interrupted` with `data.sessionID`, and each is `Stop`. Plugins run in the long-lived `opencode serve` process, which outlives the TUI and `opencode run` clients, so there is no per-client exit. `SessionEnd` is sent once per session on `session.deleted`, when OpenCode disposes the plugin (reload or shutdown), or when that process exits.

The `SessionStart` context is sent with every request of that session and the latest `UserPromptSubmit` context with its own turn, which matches what Claude keeps in its transcript. The adapter returns the Claude and Codex `hookSpecificOutput.additionalContext` shape for `opencode`; retrieval output is byte-equal to Claude for the same query.

Sub-agent sessions (`parentID` set, for example the `task` tool) are ignored. They never submit receipts, so their checkpoints would only report false receipt gaps. Agents running in OpenCode submit receipts with `--harness opencode`.

## Validation boundary

`tests/v3_opencode_test.py` installs a synthetic vault, imports the generated plugin in Node and drives every mapped hook twice: through `server()` with a fake OpenCode 1.x client, and through `setup(ctx)` with a fake OpenCode 2.x context and event stream. The Node cases skip when `node` is absent. One real OpenCode 1.18.25 session in a temporary synthetic vault returned a canary fact from injected context with tool use prohibited by the prompt, and the queue acknowledged `SessionStart` and `Stop` with no hook error. The same check with the dual-API file passed on OpenCode 1.18.33 and on OpenCode 2.0.18 (Linux); on 2.0.18, stopping the `opencode serve` process also recorded `SessionEnd`. This does not establish OpenCode Desktop or web delivery.
