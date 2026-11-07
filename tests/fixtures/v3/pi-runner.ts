import assert from "node:assert/strict"
import { readFileSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { pathToFileURL } from "node:url"
const base = process.env.PI_PACKAGE!
const { loadExtensions } = await import(pathToFileURL(base + "/dist/core/extensions/loader.js").href)
const { ExtensionRunner } = await import(pathToFileURL(base + "/dist/core/extensions/runner.js").href)
const { SessionManager } = await import(pathToFileURL(base + "/dist/core/session-manager.js").href)
const vault = process.env.TEST_VAULT!
const loaded = await loadExtensions([vault + "/.pi/extensions/beyin-v3.ts"], vault)
assert.deepEqual(loaded.errors, [])
assert.equal(loaded.extensions.length, 1)
const manager = SessionManager.inMemory(vault)
const runner = new ExtensionRunner(loaded.extensions, loaded.runtime, vault, manager, {})
const errors: unknown[] = []
runner.onError((error: unknown) => errors.push(error))
await runner.emit({ type: "session_start", reason: "startup" })
const result = await runner.emitBeforeAgentStart({ messageType: "user", prompt: "klima kargo DHL" }, { cwd: vault })
assert.ok(result.messages.length > 0)
assert.ok(result.messages.some((m: any) => m.content.includes("notes/klima.md")))
assert.ok(!JSON.stringify(result).includes("PRIVATE_PI_SENTINEL"))
const state = JSON.parse(readFileSync(join(vault, ".beyin-runtime.json"), "utf8")).state
writeFileSync(join(state, "hygiene.json"), JSON.stringify({ schema: 1, word_cap_warning: true }))
const longNote = join(vault, "notes", "long.md")
writeFileSync(longNote, "kelime ".repeat(600))
const toolResult = await runner.emitToolResult({ type: "tool_result", toolName: "write", toolCallId: "test-write",
  input: { path: longNote }, content: [{ type: "text", text: "Original editor result" }], structuredContent: { changed: true }, isError: false })
assert.equal(toolResult.content[0].text, "Original editor result")
assert.ok(toolResult.content.some((part: any) => part.text?.includes("Bolum SINYALI")))
assert.deepEqual(toolResult.structuredContent, { changed: true })
// A completed assistant reply cannot continue until the handler adds model-visible context.
const preview = (entries: any[]) => ({ contextEntries: [], contextMessages: [], llmMessages: [], pendingMessages: [],
  canContinue: entries.some((entry) => entry.type === "custom_message") })
const boundary = await runner.emitBoundary({ type: "agent_before_settle", outcome: "completed" }, preview)
assert.equal(boundary.continue, true)
assert.ok(boundary.entries.some((entry: any) => entry.type === "custom_message" && entry.content.includes("--harness pi")))
const again = await runner.emitBoundary({ type: "agent_before_settle", outcome: "completed" }, preview)
assert.equal(again.continue, false)
await runner.emit({ type: "session_shutdown", reason: "quit" })
assert.deepEqual(errors, [])
console.log(JSON.stringify({ status: "passed", host: "Pi 1.3.0 actual loader + ExtensionRunner", model_calls: 0,
  context: true, private_sentinel_excluded: true, reminder_continuation: true, bounded_continuation: true,
  editor_notice: true, structured_output_preserved: true, extension_errors: errors.length }))
