"""Vault-local OpenCode plugin around the single installed lifecycle adapter."""
import json
import os
import sys

WRITE_TOOLS = ('edit', 'write', 'patch', 'apply_patch', 'multiedit')

PLUGIN = r'''// Beyin V3 OpenCode plugin; installer-owned, removed by rollback and uninstall.
// One default export serves both plugin APIs: OpenCode 1.x calls server(), OpenCode 2.x calls setup(ctx).
import { execFile, execFileSync } from "node:child_process"
import { readFileSync } from "node:fs"
import { dirname, isAbsolute, join } from "node:path"
import { fileURLToPath } from "node:url"

const PYTHON = process.env.BEYIN_PYTHON || __PYTHON__
const VAULT = dirname(dirname(dirname(fileURLToPath(import.meta.url))))
const HOOK = join(VAULT, ".claude", "scripts", "beyin_v3_hook.py")
const WRITE_TOOLS = new Set(__WRITE_TOOLS__)
const HOOK_ENV = () => ({ ...process.env, PYTHONIOENCODING: "utf-8", PYTHONDONTWRITEBYTECODE: "1" })
const hookArgs = (state) => [HOOK, "--vault", VAULT, "--state", state, "--harness", "opencode"]

function runtimeState() {
  try {
    const data = JSON.parse(readFileSync(join(VAULT, ".beyin-runtime.json"), "utf8"))
    return typeof data?.state === "string" && isAbsolute(data.state) ? data.state : null
  } catch {
    return null
  }
}

function runHook(state, payload) {
  // Fail open: any transport failure means no context, never a broken OpenCode turn.
  return new Promise((resolve) => {
    try {
      const child = execFile(PYTHON, hookArgs(state),
        { timeout: __TIMEOUT__, maxBuffer: 4 * 1024 * 1024, windowsHide: true, env: HOOK_ENV() },
        (error, stdout) => {
          try {
            const text = error ? "" : JSON.parse(String(stdout || "{}"))?.hookSpecificOutput?.additionalContext
            resolve(typeof text === "string" ? text : "")
          } catch {
            resolve("")
          }
        })
      child.on("error", () => resolve(""))
      child.stdin.on("error", () => {})
      child.stdin.end(JSON.stringify(payload))
    } catch {
      resolve("")
    }
  })
}

// Session bookkeeping shared by both APIs; parentOf(id) resolves a session's parent or throws.
function sessionTracker(state, parentOf) {
  const sessions = new Map()
  const send = (event, id, prompt) => runHook(state, { hook_event_name: event, session_id: id, ...(prompt === undefined ? {} : { prompt }) })
  const tracked = (id) => {
    const info = typeof id === "string" && id ? sessions.get(id) : undefined
    return info && !info.child ? info : undefined
  }
  async function open(id) {
    if (!sessions.has(id)) {
      let child = false
      try {
        child = Boolean(await parentOf(id))
      } catch {}
      // Sub-agent sessions never submit receipts; tracking them would report false gaps.
      sessions.set(id, { child, start: null, turn: "" })
    }
    return sessions.get(id)
  }
  async function prompt(id, text) {
    if (typeof id !== "string" || !id) return
    const info = await open(id)
    if (info.child) return
    if (info.start === null) info.start = await send("SessionStart", id, text)
    else info.turn = await send("UserPromptSubmit", id, text)
  }
  const context = (id) => {
    const info = tracked(id)
    return info ? [info.start, info.turn].filter(Boolean) : []
  }
  return { sessions, send, tracked, prompt, context }
}

// OpenCode 1.x: hooks are returned as a map keyed by hook name.
async function server({ client } = {}) {
  const state = runtimeState()
  if (!state) return {}
  const { sessions, send, tracked, prompt, context } =
    sessionTracker(state, async (id) => (await client?.session?.get?.({ path: { id } }))?.data?.parentID)

  return {
    "chat.message": async (input, output) => {
      const text = (output?.parts || [])
        .filter((part) => part?.type === "text" && !part.synthetic && typeof part.text === "string")
        .map((part) => part.text).join("\n")
      await prompt(input?.sessionID, text)
    },
    // The only request-time injection channel; SessionStart context stays for the whole session.
    "experimental.chat.system.transform": async (input, output) => {
      if (Array.isArray(output?.system)) output.system.push(...context(input?.sessionID))
    },
    "tool.execute.after": async (input) => {
      if (tracked(input?.sessionID) && WRITE_TOOLS.has(input?.tool)) await send("PostToolUse", input.sessionID)
    },
    "experimental.session.compacting": async (input) => {
      if (tracked(input?.sessionID)) await send("PreCompact", input.sessionID)
    },
    event: async ({ event } = {}) => {
      if (event?.type === "session.idle" && tracked(event.properties?.sessionID)) {
        await send("Stop", event.properties.sessionID)
      } else if (event?.type === "session.deleted") {
        const id = event.properties?.info?.id
        if (tracked(id)) await send("SessionEnd", id)
        sessions.delete(id)
      }
    },
  }
}

// OpenCode 2.x no longer publishes session.idle: a finished turn is session.execution.*, with data.sessionID.
const TURN_END = new Set(["session.execution.succeeded", "session.execution.failed", "session.execution.interrupted", "session.idle"])

// OpenCode 2.x: hooks are registered on ctx. Plugins live in the long-running `opencode serve`
// process, which outlives TUI and `opencode run` clients, so SessionEnd is sent once per session
// on session.deleted, on dispose (reload or shutdown) or when that process exits.
async function setup(ctx) {
  const state = runtimeState()
  if (!state) return
  const { sessions, send, tracked, prompt, context } =
    sessionTracker(state, async (id) => (await ctx.session.get({ sessionID: id }))?.parentID)

  await ctx.session.hook("prompt", async (event) => {
    await prompt(event?.sessionID, typeof event?.prompt?.text === "string" ? event.prompt.text : "")
  })
  await ctx.session.hook("context", (event) => {
    if (Array.isArray(event?.system))
      for (const text of context(event?.sessionID)) event.system.push({ type: "text", text })
  })
  await ctx.tool.hook("execute.after", async (event) => {
    if (tracked(event?.sessionID) && WRITE_TOOLS.has(event?.tool)) await send("PostToolUse", event.sessionID)
  })
  await ctx.session.hook("compaction", async (event) => {
    if (tracked(event?.sessionID)) await send("PreCompact", event.sessionID)
  })

  const ended = new Set()
  const open = () => [...sessions.keys()].filter((id) => tracked(id) && !ended.has(id))
  async function end(id) {
    if (!tracked(id) || ended.has(id)) return
    ended.add(id)
    await send("SessionEnd", id)
  }
  // Exit handlers cannot await; the adapter only enqueues, so a short synchronous call is enough.
  function endAllSync() {
    for (const id of open()) {
      ended.add(id)
      try {
        execFileSync(PYTHON, hookArgs(state), { input: JSON.stringify({ hook_event_name: "SessionEnd", session_id: id }),
          timeout: 3000, stdio: ["pipe", "ignore", "ignore"], windowsHide: true, env: HOOK_ENV() })
      } catch {}
    }
  }
  process.once("exit", endAllSync)

  const controller = new AbortController()
  void (async () => {
    try {
      for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
        const id = event?.data?.sessionID
        if (TURN_END.has(event?.type)) {
          if (tracked(id)) await send("Stop", id)
        } else if (event?.type === "session.deleted") {
          await end(id)
          if (typeof id === "string") sessions.delete(id)
        }
      }
    } catch {}
  })()

  return async () => {
    process.removeListener("exit", endAllSync)
    controller.abort()
    for (const id of open()) await end(id)
  }
}

export default { id: "beyin-v3", server, setup }
'''


def plan_plugin(vault, state):
    """Return plugin bytes; vault comes from the file location and state from .beyin-runtime.json."""
    if any(char in sys.executable for char in '\r\n\x00'):
        raise ValueError('Unsupported interpreter path')
    source = (PLUGIN.replace('__PYTHON__', json.dumps(sys.executable))
              .replace('__TIMEOUT__', '20000' if os.name == 'nt' else '6000')
              .replace('__WRITE_TOOLS__', json.dumps(list(WRITE_TOOLS))))
    return {'.opencode/plugins/beyin-v3.js': source.encode('utf-8')}
