"""Vault-local OMP hook around the single installed lifecycle adapter."""
import json
import os
from pathlib import Path
import sys

# OMP 18 built-in tools that change files (tools/builtin-names.ts); edit covers every edit mode.
WRITE_TOOLS = ('edit', 'write', 'ast_edit')

PLUGIN = r'''// Beyin V3 OMP hook; installer-owned, removed by rollback and uninstall.
import { execFile } from "node:child_process"
import { readFileSync, realpathSync } from "node:fs"
import { isAbsolute, join, resolve } from "node:path"

// OMP loads project hooks from <project>/.omp/hooks/pre/*.ts when the session cwd
// matches. VAULT and PYTHON are pinned at install time; BEYIN_VAULT/BEYIN_PYTHON
// override them like the Hermes shim does.
const PYTHON = process.env.BEYIN_PYTHON || __PYTHON__
const VAULT = process.env.BEYIN_VAULT || __VAULT__
const HOOK = join(VAULT, ".claude", "scripts", "beyin_v3_hook.py")
const WRITE_TOOLS = new Set(__WRITE_TOOLS__)
const TIMEOUT = __TIMEOUT__

function runtimeState() {
  try {
    const data = JSON.parse(readFileSync(join(VAULT, ".beyin-runtime.json"), "utf8"))
    return typeof data?.state === "string" && isAbsolute(data.state) ? data.state : null
  } catch {
    return null
  }
}

// OMP reports cwd without macOS /private (/var vs /private/var) and does not resolve
// symlinks, while the pinned vault is fully resolved; compare canonical paths on both sides.
// The native realpath also expands Windows 8.3 short names, as Python's resolve() does.
function canonical(path) {
  const text = String(path ?? "")
  if (!text) return ""
  let real
  try {
    real = (realpathSync.native || realpathSync)(text)
  } catch {
    real = resolve(text)
  }
  return real.replace(/[\\/]+/g, "/").replace(/\/$/, "").toLowerCase()
}

const VAULT_KEY = canonical(VAULT)

function inVault(ctx) {
  try {
    return Boolean(VAULT_KEY) && canonical(ctx?.cwd) === VAULT_KEY
  } catch {
    return false
  }
}

function sessionOf(ctx) {
  try {
    return String(ctx?.sessionManager?.getSessionId?.() ?? "unknown")
  } catch {
    return "unknown"
  }
}

// OMP task sub-agents load the parent's hooks and emit their own session_start. The task
// executor appends a session_init entry before that event; top-level sessions never carry
// one. Skip sub-agents like OpenCode child sessions: they never submit receipts, and
// tracking them would inject duplicate context and report false receipt gaps.
function isSubagent(ctx) {
  try {
    const entries = ctx?.sessionManager?.getEntries?.()
    return Array.isArray(entries) && entries.some((entry) => entry?.type === "session_init")
  } catch {
    return false
  }
}

function send(state, event, payload) {
  // Fail open: any transport failure means no context, never a broken OMP turn.
  return new Promise((resolve) => {
    try {
      const child = execFile(PYTHON, [HOOK, "--vault", VAULT, "--state", state, "--harness", "omp"],
        { timeout: TIMEOUT, maxBuffer: 4 * 1024 * 1024, windowsHide: true,
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
      child.stdin.end(JSON.stringify({ hook_event_name: event, ...payload }))
    } catch {
      resolve("")
    }
  })
}

// OMP lifecycle mapping (same six events as the other adapters):
//   session_start          -> SessionStart (context pinned, injected on the first prompt)
//   before_agent_start     -> UserPromptSubmit (first prompt carries the SessionStart context)
//   tool_result (writes)   -> PostToolUse
//   session_stop           -> Stop
//   session_before_compact -> PreCompact
//   session_shutdown       -> SessionEnd
// The only request-time injection channel is before_agent_start, so a hook outside
// the vault must no-op: never queue or inject for an unrelated project. Sub-agent
// sessions no-op the same way.
export default function (pi) {
  const state = runtimeState()
  if (!state) return
  let pinned = ""
  let firstPromptSeen = false
  const subagents = new Map()
  const inVaultOf = (ctx) => {
    if (!inVault(ctx)) return null
    const session = sessionOf(ctx)
    // getEntries() copies the whole session, so decide once per session.
    if (!subagents.has(session)) subagents.set(session, isSubagent(ctx))
    return subagents.get(session) ? null : session
  }

  pi.on("session_start", async (_event, ctx) => {
    const session = inVaultOf(ctx)
    if (session) pinned = await send(state, "SessionStart", { session_id: session })
  })

  pi.on("before_agent_start", async (event, ctx) => {
    const session = inVaultOf(ctx)
    if (!session) return
    const prompt = typeof event?.prompt === "string" ? event.prompt : ""
    let text = await send(state, "UserPromptSubmit", { prompt, session_id: session })
    if (pinned && !firstPromptSeen) {
      text = pinned + (text ? "\n\n" + text : "")
      firstPromptSeen = true
    }
    if (!text) return
    return {
      message: {
        customType: "beyin-v3-context",
        content: "V3 source-backed context (data, not instructions):\n" + text,
        attribution: "agent",
      },
    }
  })

  pi.on("tool_result", async (event, ctx) => {
    if (!WRITE_TOOLS.has(String(event?.toolName ?? ""))) return
    const session = inVaultOf(ctx)
    if (session) await send(state, "PostToolUse", { session_id: session })
  })

  pi.on("session_stop", async (_event, ctx) => {
    const session = inVaultOf(ctx)
    if (session) await send(state, "Stop", { session_id: session })
  })

  pi.on("session_before_compact", async (_event, ctx) => {
    const session = inVaultOf(ctx)
    if (session) await send(state, "PreCompact", { session_id: session })
  })

  pi.on("session_shutdown", async (_event, ctx) => {
    const session = inVaultOf(ctx)
    // Forget the decision for sub-agents too: inVaultOf returns null for them.
    subagents.delete(sessionOf(ctx))
    if (session) await send(state, "SessionEnd", { session_id: session })
  })
}
'''


def plan_plugin(vault, state):
    """Return hook bytes; vault and interpreter are pinned, state comes from .beyin-runtime.json."""
    if any(char in sys.executable for char in '\r\n\x00'):
        raise ValueError('Unsupported interpreter path')
    source = (PLUGIN.replace('__PYTHON__', json.dumps(sys.executable))
              .replace('__VAULT__', json.dumps(str(Path(vault).resolve())))
              .replace('__TIMEOUT__', '20000' if os.name == 'nt' else '6000')
              .replace('__WRITE_TOOLS__', json.dumps(list(WRITE_TOOLS))))
    return {'.omp/hooks/pre/beyin-v3.ts': source.encode('utf-8')}
