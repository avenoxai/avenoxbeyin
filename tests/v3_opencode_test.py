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

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')

# Drives the generated plugin the way OpenCode 1.x does: import, call default.server with a
# client, then invoke hooks with OpenCode's (input, output) shapes. Prints one JSON line.
DRIVER = r"""
import { pathToFileURL } from "node:url"
const [plugin, parents] = process.argv.slice(1)
const parentOf = JSON.parse(parents)
const client = { session: { get: async ({ path }) => ({ data: { id: path.id, parentID: parentOf[path.id] } }) } }
const mod = await import(pathToFileURL(plugin).href)
const hooks = await mod.default.server({ client, directory: process.cwd() })
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

# Drives the same file the way OpenCode 2.x does: call default.setup(ctx), which registers hooks on
# ctx and subscribes to the event stream; events carry data.sessionID and there is no session.idle.
DRIVER_V2 = r"""
import { pathToFileURL } from "node:url"
const [plugin, parents] = process.argv.slice(1)
const parentOf = JSON.parse(parents)
const hooks = {}
const queue = []
let wake
const push = (event) => { queue.push(event); wake?.() }
let pending = 0
const settle = async () => {
  // Events are consumed serially and each mapped one runs the adapter; wait until the stream is idle.
  for (let i = 0; i < 400 && (queue.length || pending); i++) await new Promise((resolve) => setTimeout(resolve, 25))
}
const ctx = {
  session: {
    hook: async (name, fn) => { hooks["session." + name] = fn },
    get: async ({ sessionID }) => ({ id: sessionID, parentID: parentOf[sessionID] }),
  },
  tool: { hook: async (name, fn) => { hooks["tool." + name] = fn } },
  event: {
    subscribe: ({ signal } = {}) => ({
      async *[Symbol.asyncIterator]() {
        while (!signal?.aborted) {
          if (queue.length) { pending++; yield queue.shift(); pending--; continue }
          await new Promise((resolve) => { wake = resolve; signal?.addEventListener("abort", resolve, { once: true }) })
        }
      },
    }),
  },
}
const mod = await import(pathToFileURL(plugin).href)
const dispose = await mod.default.setup(ctx)
const system = async (sessionID) => {
  const event = { sessionID, system: [] }
  await hooks["session.context"]?.(event)
  return event.system
}
const say = (sessionID, text) => hooks["session.prompt"]?.({ sessionID, prompt: { text } })
const result = { id: mod.default.id, hooks: Object.keys(hooks).sort(), dispose: typeof dispose }
if (result.hooks.length) {
  await say("oc-1", "merhaba")
  result.first = await system("oc-1")
  await say("oc-1", "klima kargo DHL")
  result.second = await system("oc-1")
  await hooks["tool.execute.after"]({ tool: "edit", sessionID: "oc-1" })
  await hooks["tool.execute.after"]({ tool: "read", sessionID: "oc-1" })
  await hooks["session.compaction"]({ sessionID: "oc-1" })
  push({ type: "session.execution.succeeded", data: { sessionID: "oc-1" } })
  await say("sub-1", "alt ajan")
  result.child = await system("sub-1")
  push({ type: "session.execution.succeeded", data: { sessionID: "sub-1" } })
  push({ type: "session.deleted", data: { sessionID: "oc-1" } })
  await settle()
  result.afterDelete = await system("oc-1")
  await say("oc-2", "ikinci oturum")
  await dispose()
}
console.log(JSON.stringify(result))
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
        environment = patch.dict(os.environ, {'HOME': str(home), 'USERPROFILE': str(home),
                                              'BEYIN_V3_NO_SPAWN': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
        environment.start()
        self.addCleanup(environment.stop)
        self.vault = Path(self.temp.name) / 'Beyin Ölçüm & Vault'
        self.state = Path(self.temp.name) / 'state'
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
        result = subprocess.run([NODE, '--input-type=module', '-e', driver, str(self.plugin), json.dumps(parents or {})],
                                capture_output=True, text=True, encoding='utf-8', cwd=self.vault)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def done_events(self):
        drain = self.hook('opencode', {}, '--drain-queue')
        self.assertEqual(drain.returncode, 0, drain.stderr)
        return [json.loads(p.read_text(encoding='utf-8')) for p in (self.state / 'hook-done').glob('*.json')]

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
        self.assertEqual(outputs['claude'], outputs['opencode'], 'Harness selection must not change retrieval semantics')

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
        for event in done:
            self.assertNotIn('prompt', event, 'Hook metadata must never persist transcript text')

    @unittest.skipUnless(NODE, 'node is required to execute the OpenCode plugin')
    def test_plugin_maps_opencode_v2_lifecycle_to_adapter_events(self):
        result = self.drive({'sub-1': 'oc-1'}, DRIVER_V2)
        self.assertEqual(result['id'], 'beyin-v3')
        self.assertEqual(result['hooks'], ['session.compaction', 'session.context', 'session.prompt', 'tool.execute.after'])
        self.assertEqual(result['dispose'], 'function', 'setup returns a cleanup that ends open sessions')
        self.assertEqual(len(result['first']), 1, 'First turn injects only the SessionStart context')
        self.assertEqual(result['first'][0]['type'], 'text', 'OpenCode 2.x system parts are typed objects')
        self.assertIn('Receipt session=', result['first'][0]['text'])
        self.assertEqual(result['second'][0], result['first'][0], 'SessionStart context stays for the whole session')
        self.assertIn('notes/klima.md', result['second'][1]['text'])
        self.assertEqual(result['child'], [], 'Sub-agent sessions get no memory context')
        self.assertEqual(result['afterDelete'], [], 'Deleted sessions are forgotten')
        done = self.done_events()
        self.assertEqual({e['harness'] for e in done}, {'opencode'})
        self.assertEqual(sorted(e['event'] for e in done),
                         sorted(['SessionStart', 'UserPromptSubmit', 'PostToolUse', 'PreCompact', 'Stop', 'SessionEnd',
                                 'SessionStart', 'SessionEnd']),
                         'session.execution.* is Stop; delete and dispose each end a session once; the sub-agent queues nothing')
        for event in done:
            self.assertNotIn('prompt', event, 'Hook metadata must never persist transcript text')

    @unittest.skipUnless(NODE, 'node is required to execute the OpenCode plugin')
    def test_plugin_fails_open(self):
        runtime = self.vault / '.beyin-runtime.json'
        original = runtime.read_text(encoding='utf-8')
        for broken in ('{not json', '[]', '{"state": ""}', '{"state": "relative/state"}'):
            runtime.write_text(broken, encoding='utf-8')
            self.assertEqual(self.drive()['keys'], [], broken)
        runtime.unlink()
        self.assertEqual(self.drive()['keys'], [], 'An uninstalled vault registers no hooks')
        runtime.write_text(original, encoding='utf-8')
        with patch.dict(os.environ, {'BEYIN_PYTHON': str(Path(self.temp.name) / 'missing-python')}):
            result = self.drive()
        self.assertEqual(result['first'], [], 'A missing interpreter degrades to no context, never an error')
        self.assertEqual(result['second'], [])


if __name__ == '__main__':
    unittest.main()
