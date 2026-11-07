# OpenCode harness

OpenCode has no hook JSON. It loads project plugins from `.opencode/plugins/*.js` when it starts in that directory, so the installer writes one managed file, `.opencode/plugins/beyin-v3.js`, from `template/.claude/scripts/beyin_v3_opencode.py`. There is no link or enable step: open OpenCode in the vault. Rollback and uninstall remove the plugin like every other managed file.

The plugin derives the vault from its own location and the runtime directory from `.beyin-runtime.json`. Only the interpreter is pinned at install time; `BEYIN_PYTHON` overrides it. Every event runs `beyin_v3_hook.py --harness opencode` with a 6 second bound (20 seconds on Windows). A missing interpreter, timeout, non-zero exit or invalid output returns no context and never fails the OpenCode turn. A vault without a valid runtime file registers no hooks.

The plugin labels the project from the session directory and sends it as the hook payload's `cwd`: `session.location.directory` from `ctx.session.get` in 2.x (2.0.18 and 2.2.6 have no top-level `session.directory`), `session.directory` from `client.session.get` in 1.x. When the session cannot be read it uses the folder the plugin instance was loaded for, `ctx.location.directory` in 2.x or the factory's `directory` in 1.x. It must not fall back to the process directory: the 2.x background service runs in the home directory and serves sessions from several folders, so the process directory would attribute every session to the same project.

## Outside the vault (optional global bridge)

OpenCode also loads plugins from `~/.config/opencode/plugins/` (`$XDG_CONFIG_HOME/opencode/plugins`) in every directory it serves. After a vault install, the [global bridge](GLOBAL-BRIDGE.md) can add the managed `beyin-v3-bridge.js` there:

```sh
python3 "/tam/yol/Beynim/beyin.py" bridge --harness opencode --project-root "/tam/yol/Projelerim" --install
```

The bridge plugin forwards only `SessionStart`, `Stop`, `PreCompact` and `SessionEnd` to `beyin_v3_bridge.py`, stays silent inside a vault and outside the chosen project roots, sends nothing for a session whose folder it cannot read (the bridge also ignores an OpenCode event without `cwd`), and injects the same scoped startup text as the Claude, Codex and Antigravity bridges. The plugin module is the same for every install. The interpreter, the bridge command and the project roots are in the managed `beyin-v3-bridge.json` one folder up (`~/.config/opencode/beyin-v3-bridge.json`), which the plugin reads on every event: a running OpenCode service keeps an imported plugin module until it restarts (checked on 2.2.6), so a reinstall that narrows the roots, or `--uninstall`, applies from the next event instead of after a restart. `--uninstall` removes only these two managed files; a user-owned file at either path is never replaced or deleted. `--print-config` prints both paths and contents without writing.

The vault plugin and the global bridge plugin can be loaded at the same time. The bridge answers `{}` for a vault-owned directory, so a vault session queues its events once, through the local plugin.

## Supported versions

One plugin file serves both plugin APIs. OpenCode 2.x reads the default export `{ id: "beyin-v3", setup }`; OpenCode 1.x from 1.3.4 on reads `server` from the same default export, which is the `BeyinV3` factory that is also exported by name. Releases before 1.3.4 call every export as a function and report a load error for the default object; update OpenCode instead of editing the managed plugin, because `beyin.py update` stops on a changed managed file.

## Lifecycle mapping (OpenCode 1.x)

The first `chat.message` of a session is `SessionStart`; later ones are `UserPromptSubmit` with the non-synthetic text parts as `prompt`. `session.idle` is `Stop`, `session.deleted` is `SessionEnd`, `experimental.session.compacting` is `PreCompact`, and `tool.execute.after` for edit, write and patch tools is `PostToolUse`.

`experimental.chat.system.transform` is the only request-time injection channel. The `SessionStart` context is sent with every request of that session and the latest `UserPromptSubmit` context with its own turn, which matches what Claude keeps in its transcript. The adapter returns the Claude and Codex `hookSpecificOutput.additionalContext` shape for `opencode`; retrieval output is byte-equal to Claude for the same query.

## Lifecycle mapping (OpenCode 2.x)

`setup(ctx)` registers the same events on the context: the first `ctx.session.hook("prompt")` of a session is `SessionStart`, later ones are `UserPromptSubmit` with `prompt.text`; `ctx.session.hook("context")` pushes the same contexts as `{ type: "text", text }` system parts; `ctx.tool.hook("execute.after")` for the same write tools is `PostToolUse` unless the call failed; `ctx.session.hook("compaction")` is `PreCompact`. `ctx.event.subscribe` delivers `session.execution.succeeded`, `failed` or `interrupted` at the end of a turn (2.0.18 no longer emits `session.idle`); the first of these after a prompt is `Stop`, so each prompted turn gets exactly one. The session id is `event.data.sessionID`.

OpenCode 2.x loads plugins in the background service (`opencode serve --service`), which keeps running after the TUI or `opencode run` exits, so there is no client-exit moment. `SessionEnd` is sent once per session, on `session.deleted` or from the cleanup `setup` returns, which runs when the service stops (`opencode service stop`, SIGTERM) or reloads the plugin. A killed service sends no `SessionEnd`; `Stop` still covers every turn. Without a running service, `opencode run` serves the session from its own short-lived server and stops it right after the turn: a `Stop` still being reported can be cut by that SIGTERM, and the `SessionEnd` from the cleanup records the same receipt checkpoint.

Sub-agent sessions (`parentID` set, for example the `task` tool on 1.x or `subagent` on 2.x; 2.x reads it with `ctx.session.get({ sessionID })`) are ignored. They never submit receipts, so their checkpoints would only report false receipt gaps. Agents running in OpenCode submit receipts with `--harness opencode`.

## Validation boundary

`tests/v3_opencode_test.py` installs a synthetic vault, imports the generated plugin in Node with a fake OpenCode 1.x client and a fake 2.x `setup` context, and drives every mapped hook and event; the Node cases skip when `node` is absent. One real OpenCode 1.18.25 session in a temporary synthetic vault returned a canary fact from injected context with tool use prohibited by the prompt, and the queue acknowledged `SessionStart` and `Stop` with no hook error. For #133 the same plugin ran in real OpenCode 1.17.18 and 2.0.18 sessions in temporary synthetic vaults against a local mock OpenAI-compatible model: 1.17.18 queued the same events and injected the same system context as the previous plugin, and 2.0.18 reached `observed_metadata` for all six events in `beyin.py doctor`, with the context in the model request and none in a sub-agent request. For #592 real OpenCode 2.0.18 and 2.2.6 services (isolated HOME, local mock model) showed that the session folder is `session.location.directory` and the plugin folder `ctx.location.directory`, while the service runs in the home directory; with the global bridge installed, 2.2.6 injected the bridge text and queued `SessionStart` and `Stop` under the session folder in a chosen project root, nothing outside the roots, and a vault session once through the vault plugin. Uninstalling the bridge file unloaded it from the running 2.2.6 service, which sent `SessionEnd`; installing it again took effect without a restart, but rewriting the loaded file with narrower roots kept the old module and roots until a restart, which is why the roots are read from the settings file. OpenCode 1.19.5 read `session.directory`, loaded the same global file and queued `SessionStart` and `Stop`. This does not establish OpenCode Desktop or web delivery.
