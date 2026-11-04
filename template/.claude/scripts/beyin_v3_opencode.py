"""Vault-local OpenCode plugin around the single installed lifecycle adapter."""
import json
import os
import sys

WRITE_TOOLS = ('edit', 'write', 'patch', 'apply_patch', 'multiedit')

PLUGIN = r'''// Beyin V3 OpenCode plugin; installer-owned, removed by rollback and uninstall.
import { execFile } from "node:child_process"
import { readFileSync } from "node:fs"
import { dirname, isAbsolute, join } from "node:path"
import { fileURLToPath } from "node:url"

const PYTHON = process.env.BEYIN_PYTHON || __PYTHON__
const VAULT = dirname(dirname(dirname(fileURLToPath(import.meta.url))))
const HOOK = join(VAULT, ".claude", "scripts", "beyin_v3_hook.py")
const WRITE_TOOLS = new Set(__WRITE_TOOLS__)

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
      const child = execFile(PYTHON, [HOOK, "--vault", VAULT, "--state", state, "--harness", "opencode"],
        { timeout: __TIMEOUT__, maxBuffer: 4 * 1024 * 1024, windowsHide: true,
          env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONDONTWRITEBYTECODE: "1" } },
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

// One session tracker behind both plugin APIs, so 1.x and 2.x queue identical events.
function lifecycle(state, infoOf) {
  const sessions = new Map()
  // The hook names the project from cwd; without it every session in every folder is
  // attributed to whichever directory the OpenCode service was started in.
  const send = (event, id, prompt) => {
    const info = sessions.get(id)
    return runHook(state, { hook_event_name: event, session_id: id, ...(info?.cwd ? { cwd: info.cwd } : {}),
      ...(prompt === undefined ? {} : { prompt }) })
  }
  const tracked = (id) => {
    const info = typeof id === "string" && id ? sessions.get(id) : undefined
    return info && !info.child ? info : undefined
  }

  async function open(id) {
    if (!sessions.has(id)) {
      let info = {}
      try {
        info = (await infoOf(id)) || {}
      } catch {}
      // Sub-agent sessions never submit receipts; tracking them would report false gaps.
      sessions.set(id, { child: Boolean(info.child), cwd: info.cwd, start: null, turn: "" })
    }
    return sessions.get(id)
  }

  return {
    sessions,
    tracked,
    async message(id, prompt) {
      if (typeof id !== "string" || !id) return
      const info = await open(id)
      if (info.child) return
      if (info.start === null) info.start = await send("SessionStart", id, prompt())
      else info.turn = await send("UserPromptSubmit", id, prompt())
      return info
    },
    // SessionStart context stays for the whole session, the latest prompt context for its turn.
    context(id) {
      const info = tracked(id)
      return info ? [info.start, info.turn].filter((text) => text) : []
    },
    async tool(id, tool) {
      if (tracked(id) && WRITE_TOOLS.has(tool)) await send("PostToolUse", id)
    },
    async compact(id) {
      if (tracked(id)) await send("PreCompact", id)
    },
    async stop(id) {
      if (tracked(id)) await send("Stop", id)
    },
    async end(id) {
      const info = tracked(id)
      if (info && !info.ended) {
        info.ended = true
        await send("SessionEnd", id)
      }
      sessions.delete(id)
    },
  }
}

// OpenCode 1.x: a factory returning named hooks.
export const BeyinV3 = async ({ client, directory } = {}) => {
  const state = runtimeState()
  if (!state) return {}
  const core = lifecycle(state, async (id) => {
    const session = (await client?.session?.get?.({ path: { id } }))?.data
    return { child: session?.parentID, cwd: typeof session?.directory === "string" ? session.directory : directory }
  })

  return {
    "chat.message": async (input, output) => {
      await core.message(input?.sessionID, () => (output?.parts || [])
        .filter((part) => part?.type === "text" && !part.synthetic && typeof part.text === "string")
        .map((part) => part.text).join("\n"))
    },
    // The only request-time injection channel.
    "experimental.chat.system.transform": async (input, output) => {
      if (Array.isArray(output?.system)) output.system.push(...core.context(input?.sessionID))
    },
    "tool.execute.after": async (input) => {
      await core.tool(input?.sessionID, input?.tool)
    },
    "experimental.session.compacting": async (input) => {
      await core.compact(input?.sessionID)
    },
    event: async ({ event } = {}) => {
      if (event?.type === "session.idle") await core.stop(event.properties?.sessionID)
      else if (event?.type === "session.deleted") await core.end(event.properties?.info?.id)
    },
  }
}

// OpenCode 2.0 ends a turn with an execution event and no longer emits session.idle;
// idle stays accepted so a release that brings it back cannot double Stop.
const TURN_END = new Set(["session.execution.succeeded", "session.execution.failed", "session.execution.interrupted", "session.idle"])

// OpenCode 2.x: setup(ctx) registers hooks on the context and returns a cleanup.
async function setup(ctx) {
  const state = runtimeState()
  if (!state) return
  const core = lifecycle(state, async (id) => {
    let session
    try {
      session = await ctx?.session?.get?.({ sessionID: id })
    } catch {}
    return { child: session?.parentID,
      cwd: typeof session?.directory === "string" ? session.directory : ctx?.directory }
  })
  const hook = async (register) => {
    try {
      await register()
    } catch {}
  }

  await hook(() => ctx.session.hook("prompt", async (event) => {
    const info = await core.message(event?.sessionID, () => (typeof event?.prompt?.text === "string" ? event.prompt.text : ""))
    if (info) info.awaitingStop = true
  }))
  await hook(() => ctx.session.hook("context", async (event) => {
    if (Array.isArray(event?.system))
      for (const text of core.context(event?.sessionID)) event.system.push({ type: "text", text })
  }))
  await hook(() => ctx.tool.hook("execute.after", async (event) => {
    if (event?.status !== "error") await core.tool(event?.sessionID, event?.tool)
  }))
  await hook(() => ctx.session.hook("compaction", async (event) => {
    await core.compact(event?.sessionID)
  }))

  const controller = new AbortController()
  ;(async () => {
    try {
      for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
        const id = event?.data?.sessionID
        const info = core.tracked(id)
        // One Stop per prompted turn, whichever end event arrives.
        if (TURN_END.has(event?.type) && info?.awaitingStop) {
          info.awaitingStop = false
          await core.stop(id)
        } else if (event?.type === "session.deleted") await core.end(id)
      }
    } catch {}
  })()

  // Plugins live in the background service, which outlives any client, so SessionEnd
  // comes from session.deleted or from this cleanup when the service stops or reloads.
  return async () => {
    controller.abort()
    await Promise.all([...core.sessions.keys()].map((id) => core.end(id)))
  }
}

// 2.x reads { id, setup }; 1.x (1.3.4+) reads { id, server } and ignores setup.
export default { id: "beyin-v3", server: BeyinV3, setup }
'''


def plan_plugin(vault, state):
    """Return plugin bytes; vault comes from the file location and state from .beyin-runtime.json."""
    if any(char in sys.executable for char in '\r\n\x00'):
        raise ValueError('Unsupported interpreter path')
    source = (PLUGIN.replace('__PYTHON__', json.dumps(sys.executable))
              .replace('__TIMEOUT__', '20000' if os.name == 'nt' else '6000')
              .replace('__WRITE_TOOLS__', json.dumps(list(WRITE_TOOLS))))
    return {'.opencode/plugins/beyin-v3.js': source.encode('utf-8')}
