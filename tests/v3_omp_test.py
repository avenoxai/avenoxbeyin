"""OMP harness contract: same adapter, same context, real hook handler shapes."""
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

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
BUN = shutil.which('bun')

# Drives the generated hook the way OMP does: import the TS module, register its
# pi.on handlers, then invoke them with OMP's (event, ctx) shapes. Prints one JSON line.
DRIVER = """
import { pathToFileURL } from "node:url"
const handlers = {}
const pi = { on: (name, fn) => { handlers[name] = fn } }
const mod = await import(pathToFileURL(process.env.PLUGIN_PATH).href)
mod.default(pi)
const result = { keys: Object.keys(handlers).sort() }
if (result.keys.length) {
  const ctx = { cwd: process.env.OMP_CWD,
    sessionManager: { getSessionId: () => "omp-1", getEntries: () => [{ type: "message" }] } }
  await handlers.session_start({}, ctx)
  const first = await handlers.before_agent_start({ prompt: "merhaba" }, ctx)
  result.first = first?.message?.content ?? null
  const second = await handlers.before_agent_start({ prompt: "klima kargo DHL" }, ctx)
  result.second = second?.message?.content ?? null
  await handlers.tool_result({ toolName: "edit" }, ctx)
  await handlers.tool_result({ toolName: "read" }, ctx)
  await handlers.session_before_compact({}, ctx)
  await handlers.session_stop({}, ctx)
  const outside = { cwd: process.env.OMP_OUTSIDE, sessionManager: { getSessionId: () => "omp-2" } }
  const out = await handlers.before_agent_start({ prompt: "x" }, outside)
  result.outside = out?.message?.content ?? null
  // OMP task sub-agents reuse the parent's hooks; the executor records session_init first.
  const sub = { cwd: process.env.OMP_CWD,
    sessionManager: { getSessionId: () => "omp-sub", getEntries: () => [{ type: "session_init", task: "t" }] } }
  await handlers.session_start({}, sub)
  const subPrompt = await handlers.before_agent_start({ prompt: "klima kargo DHL" }, sub)
  result.subagent = subPrompt?.message?.content ?? null
  await handlers.tool_result({ toolName: "edit" }, sub)
  await handlers.session_stop({}, sub)
  await handlers.session_shutdown({}, sub)
  await handlers.session_shutdown({}, ctx)
}
console.log(JSON.stringify(result))
"""


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OMPHarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='beyin-omp-')
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name) / 'home'
        home.mkdir()
        environment = patch.dict(os.environ, inherited_env(HOME=str(home), USERPROFILE=str(home),
                                              BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1'), clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.vault = Path(self.temp.name) / 'Beyin Ölçüm & Vault'
        self.state = Path(self.temp.name) / 'state'
        self.vault.mkdir()
        (self.vault / '🔮 850-Companion').mkdir()
        (self.vault / '🔮 850-Companion/Last-Session.md').write_text('# Son oturum\n\nOMP köprüsü kuruldu; makbuz bekleniyor.\n', encoding='utf-8')
        (self.vault / '🔮 850-Companion/Threads.md').write_text('# Threads\n\n## Active Threads\n- OMP köprüsü\n\n## Closed Threads\n', encoding='utf-8')
        (self.vault / '🔮 850-Companion/Kurallar.md').write_text('# Kurallar\n- Kısa yaz.\n', encoding='utf-8')
        (self.vault / '🔮 850-Companion/Journal.md').write_text('# Journal\n\n## 2026-09-18\nİlk giriş.\n', encoding='utf-8')
        (self.vault / 'notes').mkdir()
        (self.vault / 'notes/klima.md').write_text('Klima kargo DHL yasak listesinde.\n', encoding='utf-8')
        install = load(ROOT / 'scripts/install_v3.py', 'test_omp_install')
        install.install(self.vault, self.state)
        self.hook_file = self.vault / '.omp/hooks/pre/beyin-v3.ts'
        self.driver = Path(self.temp.name) / 'driver.ts'
        self.driver.write_text(DRIVER, encoding='utf-8')
        self.cli('sync')  # like a real vault: sources indexed before the first client turn

    def cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *args], input=stdin,
                              capture_output=True, text=True, encoding='utf-8', cwd=self.vault)

    def hook(self, harness, payload, *extra):
        return subprocess.run([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'), '--vault', str(self.vault),
                               '--state', str(self.state), '--harness', harness, *extra],
                              input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8')

    def drive(self, cwd=None):
        env = inherited_env(PLUGIN_PATH=str(self.hook_file), OMP_CWD=str(cwd or self.vault),
                   OMP_OUTSIDE=str(Path(self.temp.name) / 'elsewhere'))
        result = subprocess.run([BUN, 'run', str(self.driver)], env=env,
                                capture_output=True, text=True, encoding='utf-8', cwd=self.vault)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def done_events(self):
        drain = self.hook('omp', {}, '--drain-queue')
        self.assertEqual(drain.returncode, 0, drain.stderr)
        return [json.loads(p.read_text(encoding='utf-8')) for p in (self.state / 'hook-done').glob('*.json')]

    def test_installer_plans_vault_local_hook(self):
        self.assertTrue(self.hook_file.is_file())
        text = self.hook_file.read_text(encoding='utf-8')
        self.assertIn('--harness", "omp"', text)
        self.assertIn(json.dumps(str(self.vault.resolve())), text, 'Vault is pinned at install time')
        self.assertIn(json.dumps(sys.executable), text, 'Interpreter is pinned like the hook commands')
        self.assertNotIn(str(self.state), text, 'State comes from .beyin-runtime.json, not the hook')
        installed = json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))
        self.assertIn('.omp/hooks/pre/beyin-v3.ts', installed['files'], 'hook must roll back with the package')

    def test_uninstall_removes_hook(self):
        install = load(ROOT / 'scripts/install_v3.py', 'test_omp_uninstall')
        install.install(self.vault, self.state, uninstall=True)
        self.assertFalse(self.hook_file.exists())

    def test_omp_context_equals_claude_context_for_same_query(self):
        outputs = {}
        for harness in ('claude', 'omp'):
            result = self.hook(harness, {'hook_event_name': 'UserPromptSubmit', 'session_id': 'same', 'prompt': 'klima kargo'})
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs[harness] = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
        self.assertEqual(outputs['claude'], outputs['omp'], 'Harness selection must not change retrieval semantics')

    def test_receipt_and_doctor_accept_omp_harness(self):
        receipt = json.dumps({'event_id': 'omp-receipt-1', 'summary': 'OMP köprüsü doğrulandı.', 'refs': ['notes/klima.md']})
        result = self.cli('receipt', '--file', '-', '--harness', 'omp', stdin=receipt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'succeeded')
        source = self.vault / json.loads(result.stdout)['source']
        self.assertIn('"harness": "omp"', source.read_text(encoding='utf-8'))
        doctor = json.loads(self.cli('doctor').stdout)
        self.assertIn('omp', doctor['lifecycle'])
        context = self.cli('context', 'klima', '--harness', 'omp')
        self.assertEqual(context.returncode, 0, context.stderr)

    @unittest.skipUnless(BUN, 'bun is required to execute the OMP hook')
    def test_omp_hook_maps_lifecycle_to_adapter_events(self):
        result = self.drive()
        self.assertEqual(result['keys'], ['before_agent_start', 'session_before_compact', 'session_shutdown',
                                          'session_start', 'session_stop', 'tool_result'])
        self.assertIn('Receipt session=', result['first'], 'First prompt injects the pinned SessionStart context')
        self.assertIn('OMP köprüsü kuruldu', result['first'])
        self.assertIn('notes/klima.md', result['second'], 'Later prompts inject turn context')
        self.assertEqual(result['outside'], None, 'Sessions outside the vault get no memory context')
        self.assertEqual(result['subagent'], None, 'Task sub-agents get no memory context')
        done = self.done_events()
        self.assertEqual({e['harness'] for e in done}, {'omp'})
        self.assertEqual(sorted(e['event'] for e in done),
                         sorted(['SessionStart', 'UserPromptSubmit', 'UserPromptSubmit', 'PostToolUse',
                                 'PreCompact', 'Stop', 'SessionEnd']),
                         'One event per mapped hook; the first prompt is both SessionStart and a real turn, '
                         'read tools, the outside session and sub-agents queue nothing')
        for event in done:
            self.assertNotIn('prompt', event, 'Hook metadata must never persist transcript text')

    @unittest.skipUnless(BUN, 'bun is required to execute the OMP hook')
    def test_omp_hook_matches_vault_through_path_alias(self):
        alias = Path(self.temp.name) / 'vault-link'
        try:
            alias.symlink_to(self.vault, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks are unavailable')
        result = self.drive(alias)
        self.assertIn('OMP köprüsü kuruldu', result['first'] or '',
                      'OMP reports cwd without resolving symlinks or /private; the hook must still match the vault')

    @unittest.skipUnless(BUN, 'bun is required to execute the OMP hook')
    def test_omp_hook_fails_open(self):
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
        self.assertEqual(result['first'], None, 'A missing interpreter degrades to no context, never an error')
        self.assertEqual(result['second'], None)

    def test_doctor_reports_stale_global_hook_copy(self):
        # The installer never updates or removes a global copy; a user-managed copy can fall
        # behind after an engine update. Doctor must report the digest mismatch informationally.
        global_dir = Path.home() / '.omp/agent/hooks/pre'
        global_dir.mkdir(parents=True, exist_ok=True)
        global_copy = global_dir / 'beyin-v3.ts'
        global_copy.write_text('// stale copy of an older adapter\n', encoding='utf-8')
        self.addCleanup(lambda: (global_copy.unlink(missing_ok=True), global_dir.rmdir()))
        doctor = json.loads(self.cli('doctor').stdout)
        self.assertTrue(doctor['omp_global_hook']['stale'])
        self.assertIn('not updated by install/rollback', doctor['omp_global_hook']['note'])

    @unittest.skipUnless(BUN, 'bun is required to execute the OMP hook')
    def test_doctor_treats_global_symlink_as_absent(self):
        # A user-managed symlink to the vault hook is always fresh by construction: doctor
        # must not report it as a stale file, because its target follows engine updates.
        global_dir = Path.home() / '.omp/agent/hooks/pre'
        global_dir.mkdir(parents=True, exist_ok=True)
        global_link = global_dir / 'beyin-v3.ts'
        global_link.symlink_to(self.hook_file)
        self.addCleanup(lambda: (global_link.unlink(missing_ok=True), global_dir.rmdir()))
        doctor = json.loads(self.cli('doctor').stdout)
        self.assertNotIn('omp_global_hook', doctor)

    @unittest.skipUnless(BUN, 'bun is required to execute the OMP hook')
    def test_session_shutdown_removes_from_subagents_map(self):
        result = self.drive()
        self.assertIn('session_shutdown', result['keys'])


if __name__ == '__main__':
    unittest.main()
