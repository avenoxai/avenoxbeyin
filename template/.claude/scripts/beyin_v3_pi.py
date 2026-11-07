"""Pi extension planner; lifecycle and memory stay in the shared Python runtime."""
import json
import os
from pathlib import Path
import sys

PLUGIN = r'''// Beyin V3 Pi extension; installer-owned, removed by rollback and uninstall.
import { execFile } from "node:child_process"
import { readFileSync, realpathSync } from "node:fs"
import { isAbsolute, join, resolve } from "node:path"

const PYTHON = process.env.BEYIN_PYTHON || __PYTHON__
const VAULT = __VAULT__
const TIMEOUT = __TIMEOUT__

function canonical(path) {
  if (typeof path !== "string" || !path) return ""
  let real
  try { real = (realpathSync.native || realpathSync)(path) }
  catch { real = resolve(path) }
  const key = real.replace(/[\\/]+/g, "/").replace(/\/$/, "") || "/"
  return process.platform === "win32" ? key.toLowerCase() : key
}

function runtimeState() {
  try {
    const data = JSON.parse(readFileSync(join(VAULT, ".beyin-runtime.json"), "utf8"))
    return typeof data?.state === "string" && isAbsolute(data.state) ? data.state : null
  } catch { return null }
}

// Pi exits at startup when two extension files register one flag, e.g. a personal-settings
// path to one vault plus the trusted vault in cwd. The first Beyin instance owns the flags and
// lends its reader over pi.events; later instances serve only their own vault cwd, and a
// second copy of an already loaded vault stays inert.
const INSTANCE_CHANNEL = "beyin-v3:pi-instance"

export default function (pi) {
  const vaultKey = canonical(VAULT)
  const probe = { vault: vaultKey, getFlag: undefined, duplicate: false }
  try { pi.events?.emit(INSTANCE_CHANNEL, probe) } catch {}
  if (probe.duplicate === true) return
  const borrowed = typeof probe.getFlag === "function"
  const getFlag = borrowed ? probe.getFlag : (name) => pi.getFlag(name)
  if (!borrowed) {
    pi.registerFlag("beyin-project-root", {
      description: "Opt in to the scoped Beyin bridge outside the vault (absolute project root)",
      type: "string",
    })
    pi.registerFlag("beyin-project-roots", {
      description: "Opt in to multiple Beyin bridge roots (JSON array of absolute paths)", type: "string",
    })
    pi.registerFlag("beyin-bridge-context-chars", {
      description: "External bridge startup budget, 0–4000 characters (default 1500)", type: "string",
    })
    pi.registerFlag("beyin-bridge-events", {
      description: "External bridge events, comma-separated (default all four lifecycle boundaries)", type: "string",
    })
    pi.registerFlag("beyin-no-memory", {
      description: "Disable all Beyin context and lifecycle bookkeeping for this invocation",
      type: "boolean", default: false,
    })
    const claimed = new Set([vaultKey])
    try {
      pi.events?.on(INSTANCE_CHANNEL, (request) => {
        if (!request || typeof request !== "object" || typeof request.vault !== "string") return
        if (claimed.has(request.vault)) { request.duplicate = true; return }
        claimed.add(request.vault)
        request.getFlag = getFlag
      })
    } catch {}
  }
  const state = runtimeState()
  if (!state) return
  let pinned = ""
  let firstPromptSeen = false

  function scope(ctx) {
    if (getFlag("beyin-no-memory") === true) return null
    const cwd = canonical(ctx?.cwd)
    if (!cwd) return null
    if (cwd === vaultKey) return { external: false }
    // The bridge flags belong to the owning instance; one bridge per process.
    if (borrowed) return null
    const root = getFlag("beyin-project-root")
    const rawRoots = getFlag("beyin-project-roots")
    let roots
    try { roots = rawRoots === undefined ? [] : JSON.parse(rawRoots) }
    catch { return null }
    if (!Array.isArray(roots)) return null
    if (root !== undefined) roots.push(root)
    if (!roots.length || !roots.every((path) => typeof path === "string" && isAbsolute(path))) return null
    const rawBudget = getFlag("beyin-bridge-context-chars") ?? "1500"
    if (typeof rawBudget !== "string" || !/^\d+$/.test(rawBudget) || Number(rawBudget) > 4000) return null
    const rawEvents = getFlag("beyin-bridge-events")
    const events = rawEvents === undefined ? ["SessionStart", "Stop", "PreCompact", "SessionEnd"]
      : typeof rawEvents === "string" ? rawEvents.split(",").map((event) => event.trim()) : []
    if (!events.length || !events.every((event) => ["SessionStart", "Stop", "PreCompact", "SessionEnd"].includes(event))) return null
    const matches = roots.some((path) => {
      const key = canonical(path)
      return cwd === key || cwd.startsWith(key.endsWith("/") ? key : key + "/")
    })
    return matches ? { external: true, roots, contextChars: Number(rawBudget), events } : null
  }

  function sessionOf(ctx) {
    try {
      const id = ctx?.sessionManager?.getSessionId?.()
      return typeof id === "string" && id && id !== "unknown" ? id : null
    } catch { return null }
  }

  function send(event, ctx, extra = {}) {
    const target = scope(ctx)
    const session = sessionOf(ctx)
    if (!target || !session) return Promise.resolve({})
    const script = target.external ? "beyin_v3_bridge.py" : "beyin_v3_hook.py"
    const args = [join(VAULT, ".claude", "scripts", script),
      "--vault", VAULT, "--state", state, "--harness", "pi"]
    if (target.external) {
      for (const root of target.roots) args.push("--project-root", root)
      args.push("--context-chars", String(target.contextChars))
      for (const event of target.events) args.push("--event", event)
    }
    return new Promise((done) => {
      try {
        const child = execFile(PYTHON, args,
          { timeout: TIMEOUT, maxBuffer: 4 * 1024 * 1024, windowsHide: true,
            env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONDONTWRITEBYTECODE: "1" } },
          (error, stdout) => {
            try { done(error ? {} : JSON.parse(String(stdout || "{}")) || {}) }
            catch { done({}) }
          })
        child.on("error", () => done({}))
        child.stdin.on("error", () => {})
        child.stdin.end(JSON.stringify({ hook_event_name: event, session_id: session, cwd: ctx.cwd, ...extra }))
      } catch { done({}) }
    })
  }

  function context(result) {
    const text = result?.hookSpecificOutput?.additionalContext
    return typeof text === "string" ? text : ""
  }

  pi.on("session_start", async (_event, ctx) => {
    firstPromptSeen = false
    pinned = context(await send("SessionStart", ctx))
  })

  pi.on("before_agent_start", async (event, ctx) => {
    // Custom-message runs are not user prompts and must not start retrieval.
    if (event.messageType !== "user" || !scope(ctx) || !sessionOf(ctx)) return
    let text = context(await send("UserPromptSubmit", ctx, { prompt: event.prompt }))
    if (!firstPromptSeen) {
      firstPromptSeen = true
      text = pinned + (pinned && text ? "\n\n" : "") + text
      pinned = ""
    }
    if (text) return { message: { customType: "beyin-v3-context", content: text, display: false } }
  })

  pi.on("tool_result", async (event, ctx) => {
    if (event.isError || !["edit", "write"].includes(event.toolName)) return
    const text = context(await send("PostToolUse", ctx, { tool_name: event.toolName, tool_input: event.input }))
    if (text) return { content: [...event.content, { type: "text", text }], structuredContent: event.structuredContent }
  })

  pi.on("agent_before_settle", async (event, ctx) => {
    // canContinue describes the current tail; adding the reminder makes an assistant tail continuable.
    const actionable = event.outcome === "completed"
    const result = await send("Stop", ctx, { stop_hook_active: !actionable })
    if (!actionable) return
    // The shared reminder marks each receipt/learning reminder once; never continue unconditionally.
    if (result?.decision === "block" && typeof result.reason === "string" && result.reason) {
      return { entries: [...event.entries, { type: "custom_message", customType: "beyin-v3-reminder",
        content: result.reason, display: true }], continue: true }
    }
  })

  pi.on("session_before_compact", async (_event, ctx) => {
    await send("PreCompact", ctx)
  })

  pi.on("session_shutdown", async (_event, ctx) => {
    await send("SessionEnd", ctx)
    pinned = ""
  })
}
'''


def plan_plugin(vault, state):
    """Pin vault/interpreter, but read machine-local state from the installed locator."""
    if any(char in sys.executable for char in '\r\n\x00'):
        raise ValueError('Unsupported interpreter path')
    source = (PLUGIN.replace('__PYTHON__', json.dumps(sys.executable))
              .replace('__VAULT__', json.dumps(str(Path(vault).resolve())))
              .replace('__TIMEOUT__', '20000' if os.name == 'nt' else '6000'))
    return {'.pi/extensions/beyin-v3.ts': source.encode('utf-8')}
