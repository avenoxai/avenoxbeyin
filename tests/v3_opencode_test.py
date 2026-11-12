"""OpenCode harness contract: same adapter, same context, real plugin hook shapes."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env, without_hook_clock

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')

# Drives the generated plugin the way OpenCode does: import, call the factory with a
# client, then invoke hooks with OpenCode's (input, output) shapes. Prints one JSON line.
DRIVER = r"""
import { pathToFileURL } from "node:url"
const [plugin, parents, directory] = process.argv.slice(1)
const parentOf = JSON.parse(parents)
const client = { session: { get: async ({ path }) => ({ data: { id: path.id, parentID: parentOf[path.id], directory } }) } }
const mod = await import(pathToFileURL(plugin).href)
const hooks = await mod.BeyinV3({ client, directory })
const system = async (sessionID) => {
  const output = { system: ["base"] }
  await hooks["experimental.chat.system.transform"]?.({ sessionID, model: {} }, output)
  return output.system.slice(1)
}
const say = (sessionID, text) => hooks["chat.message"]?.({ sessionID }, {
  message: {}, parts: [{ type: "text", text }, { type: "text", text: "IGNORED", synthetic: true }],
})
const result = { keys: Object.keys(hooks).sort() }
if (result.keys.length) {
  await say("oc-1", "merhaba")
  result.first = await system("oc-1")
  await say("oc-1", "klima kargo DHL")
  result.second = await system("oc-1")
  await hooks["tool.execute.after"]({ tool: "edit", sessionID: "oc-1", callID: "c1", args: {} }, { title: "", output: "", metadata: {} })
  await hooks["tool.execute.after"]({ tool: "read", sessionID: "oc-1", callID: "c2", args: {} }, { title: "", output: "", metadata: {} })
  await hooks["experimental.session.compacting"]({ sessionID: "oc-1" }, { context: [] })
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "oc-1" } } })
  await say("sub-1", "alt ajan")
  result.child = await system("sub-1")
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "sub-1" } } })
  await hooks.event({ event: { type: "session.deleted", properties: { info: { id: "oc-1" } } } })
  result.afterDelete = await system("oc-1")
}
console.log(JSON.stringify(result))
"""

# Drives the same module the way OpenCode 2.x does: read the default definition, call
# setup(ctx) with hook registries and a controllable event stream, then use the cleanup.
DRIVER_V2 = r"""
import { pathToFileURL } from "node:url"
const [plugin, parents, directory] = process.argv.slice(1)
const parentOf = JSON.parse(parents)
const hooks = {}
const register = (domain) => async (name, callback) => {
  hooks[`${domain}.${name}`] = callback
  return { dispose: async () => {} }
}
const pending = []
let notify = null
let idle = null
let aborted = false
function subscribe({ signal } = {}) {
  signal?.addEventListener("abort", () => { aborted = true; notify?.() })
  return (async function* () {
    while (!aborted) {
      if (pending.length) { yield pending.shift(); continue }
      idle?.()
      await new Promise((resolve) => { notify = resolve })
      notify = null
    }
    idle?.()
  })()
}
// Resolves once the plugin has fully handled the event and asked for the next one.
const emit = (type, sessionID) => new Promise((resolve) => {
  idle = resolve
  pending.push({ id: `evt-${pending.length}`, type, data: { sessionID } })
  notify?.()
})
// OpenCode 2.0.18 and 2.2.6 keep the folder under location, on the session and on ctx;
// neither has a top-level directory, and the service runs in its own directory.
const ctx = {
  session: { hook: register("session"),
    get: async ({ sessionID }) => ({ id: sessionID, projectID: "global", parentID: parentOf[sessionID], location: { directory } }) },
  tool: { hook: register("tool") },
  event: { subscribe },
  location: { directory, workspaceID: undefined, project: { id: "global", directory } },
}
const mod = await import(pathToFileURL(plugin).href)
const definition = mod.default
const result = {
  id: definition?.id,
  setup: typeof definition?.setup,
  serverIsNamedExport: definition?.server === mod.BeyinV3 && typeof mod.BeyinV3 === "function",
  tui: definition && "tui" in definition,
}
const cleanup = await definition.setup(ctx)
result.cleanup = typeof cleanup
result.keys = Object.keys(hooks).sort()
if (result.keys.length) {
  const prompt = (sessionID, text) => hooks["session.prompt"]({ sessionID, messageID: "msg", prompt: { text, files: [] }, delivery: "steer" })
  const system = async (sessionID) => {
    const event = { sessionID, agent: "build", model: {}, system: [{ type: "text", text: "base" }], messages: [], options: {}, tools: {} }
    await hooks["session.context"](event)
    return event.system.slice(1)
  }
  const tool = (tool, status) => hooks["tool.execute.after"]({ tool, sessionID: "oc-1", agent: "build", messageID: "msg", id: tool, input: {}, status })
  await prompt("oc-1", "merhaba")
  result.first = await system("oc-1")
  await prompt("oc-1", "klima kargo DHL")
  result.second = await system("oc-1")
  await tool("edit", "completed")
  await tool("write", "error")
  await tool("read", "completed")
  await hooks["session.compaction"]({ sessionID: "oc-1", agent: "build", model: {}, system: [], messages: [], options: {}, tools: {} })
  await emit("session.execution.started", "oc-1")
  await emit("session.execution.succeeded", "oc-1")
  await emit("session.idle", "oc-1")
  await emit("session.execution.succeeded", "oc-1")
  await prompt("sub-1", "alt ajan")
  result.child = await system("sub-1")
  await emit("session.execution.succeeded", "sub-1")
  await emit("session.deleted", "sub-1")
  await emit("session.deleted", "oc-1")
  result.afterDelete = await system("oc-1")
  await emit("session.deleted", "oc-1")
  await prompt("oc-2", "ikinci oturum")
  await prompt("oc-3", "ucuncu oturum")
  // Service stop while a delete is still being reported: one SessionEnd each, never two.
  const deleting = emit("session.deleted", "oc-2")
  await new Promise((resolve) => setImmediate(resolve))
  await cleanup()
  await cleanup()
  await deleting
  result.afterCleanup = [...await system("oc-2"), ...await system("oc-3")]
}
console.log(JSON.stringify(result))
process.exit(0)
"""


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpenCodeHarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='beyin-opencode-')
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name) / 'home'
        home.mkdir()
        environment = patch.dict(os.environ, inherited_env(HOME=str(home), USERPROFILE=str(home),
                                              BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1'), clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.vault = Path(self.temp.name) / 'Beyin Ölçüm & Vault'
        self.state = Path(self.temp.name) / 'state'
        self.elsewhere = Path(self.temp.name) / 'elsewhere'
        self.elsewhere.mkdir()
        self.vault.mkdir()
        (self.vault / '🔮 850-Companion').mkdir()
        (self.vault / '🔮 850-Companion/Last-Session.md').write_text('# Son oturum\n\nOpenCode köprüsü kuruldu; makbuz bekleniyor.\n', encoding='utf-8')
        (self.vault / '🔮 850-Companion/Threads.md').write_text('# Threads\n\n## Active Threads\n- OpenCode köprüsü\n\n## Closed Threads\n', encoding='utf-8')
        (self.vault / '🔮 850-Companion/Kurallar.md').write_text('# Kurallar\n- Kısa yaz.\n', encoding='utf-8')
        (self.vault / '🔮 850-Companion/Journal.md').write_text('# Journal\n\n## 2026-09-18\nİlk giriş.\n', encoding='utf-8')
        (self.vault / 'notes').mkdir()
        (self.vault / 'notes/klima.md').write_text('Klima kargo DHL yasak listesinde.\n', encoding='utf-8')
        install = load(ROOT / 'scripts/install_v3.py', 'test_opencode_install')
        install.install(self.vault, self.state)
        self.plugin = self.vault / '.opencode/plugins/beyin-v3.js'
        self.cli('sync')  # like a real vault: sources indexed before the first client turn

    def cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *args], input=stdin,
                              capture_output=True, text=True, encoding='utf-8', cwd=self.vault)

    def hook(self, harness, payload, *extra):
        return subprocess.run([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'), '--vault', str(self.vault),
                               '--state', str(self.state), '--harness', harness, *extra],
                              input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8')

    def drive(self, parents=None, driver=DRIVER):
        # The service runs somewhere else; only the session carries the project directory.
        result = subprocess.run([NODE, '--input-type=module', '-e', driver, str(self.plugin), json.dumps(parents or {}),
                                 str(self.vault)],
                                capture_output=True, text=True, encoding='utf-8', cwd=self.elsewhere)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def done_events(self):
        drain = self.hook('opencode', {}, '--drain-queue')
        self.assertEqual(drain.returncode, 0, drain.stderr)
        return [json.loads(p.read_text(encoding='utf-8')) for p in (self.state / 'hook-done').glob('*.json')]

    def project_label(self):
        return ''.join(c for c in self.vault.name if c.isalnum() or c in ' ._-')

    def test_installer_plans_vault_local_plugin(self):
        self.assertTrue(self.plugin.is_file())
        text = self.plugin.read_text(encoding='utf-8')
        self.assertIn('--harness", "opencode"', text)
        self.assertIn(json.dumps(sys.executable), text, 'Interpreter is pinned like the hook commands')
        self.assertNotIn(str(self.state), text, 'State comes from .beyin-runtime.json, not the plugin')
        installed = json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))
        self.assertIn('.opencode/plugins/beyin-v3.js', installed['files'], 'plugin must roll back with the package')

    def test_uninstall_removes_plugin(self):
        install = load(ROOT / 'scripts/install_v3.py', 'test_opencode_uninstall')
        install.install(self.vault, self.state, uninstall=True)
        self.assertFalse(self.plugin.exists())

    def test_opencode_context_equals_claude_context_for_same_query(self):
        outputs = {}
        for harness in ('claude', 'opencode'):
            result = self.hook(harness, {'hook_event_name': 'UserPromptSubmit', 'session_id': 'same', 'prompt': 'klima kargo'})
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs[harness] = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
        self.assertEqual(without_hook_clock(outputs['claude']), without_hook_clock(outputs['opencode']), 'Harness selection must not change retrieval semantics')

    def test_receipt_and_doctor_accept_opencode_harness(self):
        receipt = json.dumps({'event_id': 'opencode-receipt-1', 'summary': 'OpenCode köprüsü doğrulandı.', 'refs': ['notes/klima.md']})
        result = self.cli('receipt', '--file', '-', '--harness', 'opencode', stdin=receipt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'succeeded')
        source = self.vault / json.loads(result.stdout)['source']
        self.assertIn('"harness": "opencode"', source.read_text(encoding='utf-8'))
        doctor = json.loads(self.cli('doctor').stdout)
        self.assertIn('opencode', doctor['lifecycle'])
        context = self.cli('context', 'klima', '--harness', 'opencode')
        self.assertEqual(context.returncode, 0, context.stderr)

    @unittest.skipUnless(NODE, 'node is required to execute the OpenCode plugin')
    def test_plugin_maps_opencode_lifecycle_to_adapter_events(self):
        result = self.drive({'sub-1': 'oc-1'})
        self.assertEqual(result['keys'], ['chat.message', 'event', 'experimental.chat.system.transform',
                                          'experimental.session.compacting', 'tool.execute.after'])
        self.assertEqual(len(result['first']), 1, 'First turn injects only the SessionStart context')
        self.assertIn('Receipt session=', result['first'][0])
        self.assertIn('OpenCode köprüsü kuruldu', result['first'][0])
        self.assertEqual(result['second'][0], result['first'][0], 'SessionStart context stays for the whole session')
        self.assertIn('notes/klima.md', result['second'][1])
        self.assertEqual(result['child'], [], 'Sub-agent sessions get no memory context')
        self.assertEqual(result['afterDelete'], [], 'Deleted sessions are forgotten')
        done = self.done_events()
        self.assertEqual({e['harness'] for e in done}, {'opencode'})
        self.assertEqual(sorted(e['event'] for e in done),
                         sorted(['SessionStart', 'UserPromptSubmit', 'PostToolUse', 'PreCompact', 'Stop', 'SessionEnd']),
                         'One event per mapped hook; read tools and the sub-agent session queue nothing')
        self.assertEqual({e['project'] for e in done}, {self.project_label()},
                         'The session directory names the project, not the service directory')
        for event in done:
            self.assertNotIn('prompt', event, 'Hook metadata must never persist transcript text')

    @unittest.skipUnless(NODE, 'node is required to execute the OpenCode plugin')
    def test_plugin_maps_opencode_2_lifecycle_to_adapter_events(self):
        result = self.drive({'sub-1': 'oc-1'}, DRIVER_V2)
        self.assertEqual(result['id'], 'beyin-v3')
        self.assertEqual(result['setup'], 'function')
        self.assertTrue(result['serverIsNamedExport'], 'OpenCode 1.x loads default.server, which must be the same factory')
        self.assertFalse(result['tui'], 'OpenCode 1.x rejects a definition with both server and tui')
        self.assertEqual(result['cleanup'], 'function')
        self.assertEqual(result['keys'], ['session.compaction', 'session.context', 'session.prompt', 'tool.execute.after'])
        self.assertEqual(len(result['first']), 1, 'First turn injects only the SessionStart context')
        self.assertEqual(set(result['first'][0]), {'type', 'text'}, 'OpenCode 2.x system entries are text parts')
        self.assertEqual(result['first'][0]['type'], 'text')
        self.assertIn('Receipt session=', result['first'][0]['text'])
        self.assertIn('OpenCode köprüsü kuruldu', result['first'][0]['text'])
        self.assertEqual(result['second'][0], result['first'][0], 'SessionStart context stays for the whole session')
        self.assertIn('notes/klima.md', result['second'][1]['text'])
        self.assertEqual(result['child'], [], 'Sub-agent sessions get no memory context')
        self.assertEqual(result['afterDelete'], [], 'Deleted sessions are forgotten')
        self.assertEqual(result['afterCleanup'], [], 'Cleanup forgets every session')
        done = self.done_events()
        self.assertEqual({e['harness'] for e in done}, {'opencode'})
        self.assertEqual(sorted(e['event'] for e in done),
                         sorted(['SessionStart', 'UserPromptSubmit', 'PostToolUse', 'PreCompact', 'Stop', 'SessionEnd',
                                 'SessionStart', 'SessionEnd', 'SessionStart', 'SessionEnd']),
                         'One Stop per prompted turn, one SessionEnd per session, nothing for read tools, '
                         'failed tools or the sub-agent session')
        self.assertEqual({e['project'] for e in done}, {self.project_label()},
                         'The session directory names the project, not the service directory')
        for event in done:
            self.assertNotIn('prompt', event, 'Hook metadata must never persist transcript text')

    @unittest.skipUnless(NODE, 'node is required to execute the OpenCode plugin')
    def test_plugin_fails_open(self):
        runtime = self.vault / '.beyin-runtime.json'
        original = runtime.read_text(encoding='utf-8')
        for broken in ('{not json', '[]', '{"state": ""}', '{"state": "relative/state"}'):
            runtime.write_text(broken, encoding='utf-8')
            self.assertEqual(self.drive()['keys'], [], broken)
            self.assertEqual(self.drive(driver=DRIVER_V2)['keys'], [], broken)
        runtime.unlink()
        self.assertEqual(self.drive()['keys'], [], 'An uninstalled vault registers no hooks')
        self.assertEqual(self.drive(driver=DRIVER_V2)['cleanup'], 'undefined')
        runtime.write_text(original, encoding='utf-8')
        with patch.dict(os.environ, {'BEYIN_PYTHON': str(Path(self.temp.name) / 'missing-python')}):
            result = self.drive()
        self.assertEqual(result['first'], [], 'A missing interpreter degrades to no context, never an error')
        self.assertEqual(result['second'], [])
        with patch.dict(os.environ, {'BEYIN_PYTHON': str(Path(self.temp.name) / 'missing-python')}):
            result = self.drive(driver=DRIVER_V2)
        self.assertEqual(result['first'], [])
        self.assertEqual(result['second'], [])


if __name__ == '__main__':
    unittest.main()
