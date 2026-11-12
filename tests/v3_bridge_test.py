"""Global boundary adapters exercised in isolated homes, never user hook settings."""
import contextlib
import io
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from v3_package_helpers import inherited_env, install, snapshot

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_bridge as bridge
import beyin_v3_hook as hook
from beyin_v3_sync import SyncEngine

# Drives the generated global plugin the way OpenCode 2.x does: setup(ctx), prompt,
# request-time context, then the event stream. Prints one JSON line.
OPENCODE_DRIVER = r"""
import { pathToFileURL } from "node:url"
const [plugin, project, elsewhere] = process.argv.slice(1)
const sessions = { "bridge-1": project, "bridge-out": elsewhere, "bridge-sub": project }
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
const emit = (type, sessionID) => new Promise((resolve) => {
  idle = resolve
  pending.push({ id: `evt-${pending.length}`, type, data: { sessionID } })
  notify?.()
})
// OpenCode 2.0.18 and 2.2.6 keep the folder under location, on the session and on ctx.
const ctx = {
  session: { hook: register("session"),
    get: async ({ sessionID }) => ({ id: sessionID, projectID: "global", parentID: sessionID === "bridge-sub" ? "bridge-1" : undefined,
                                    location: { directory: sessions[sessionID] } }) },
  event: { subscribe },
  location: { directory: elsewhere, workspaceID: undefined, project: { id: "global", directory: elsewhere } },
}
const mod = await import(pathToFileURL(plugin).href)
const definition = mod.default
const cleanup = await definition.setup(ctx)
const prompt = (sessionID) => hooks["session.prompt"]({ sessionID, messageID: "msg", prompt: { text: "hi", files: [] }, delivery: "steer" })
const system = async (sessionID) => {
  const event = { sessionID, agent: "build", model: {}, system: [{ type: "text", text: "base" }], messages: [], options: {}, tools: {} }
  await hooks["session.context"](event)
  return event.system.slice(1)
}
const result = { id: definition?.id, keys: Object.keys(hooks).sort() }
await prompt("bridge-1")
result.allowed = await system("bridge-1")
await emit("session.execution.succeeded", "bridge-1")
await prompt("bridge-out")
result.blocked = await system("bridge-out")
await prompt("bridge-sub")
result.child = await system("bridge-sub")
await emit("session.deleted", "bridge-1")
await cleanup()
console.log(JSON.stringify(result))
process.exit(0)
"""

# A 2.x instance whose session folder cannot be read: no location on the session or ctx.
# The service directory (the Node cwd here) must never stand in for it.
OPENCODE_NO_FOLDER_DRIVER = r"""
import { pathToFileURL } from "node:url"
const [plugin] = process.argv.slice(1)
const hooks = {}
const register = (domain) => async (name, callback) => {
  hooks[`${domain}.${name}`] = callback
  return { dispose: async () => {} }
}
const events = [{ type: "session.execution.succeeded", data: { sessionID: "lost-1" } },
                { type: "session.execution.succeeded", data: { sessionID: "lost-2" } }]
let release
const ready = new Promise((resolve) => { release = resolve })
async function* subscribe() {
  await ready
  yield* events
}
const ctx = {
  session: { hook: register("session"),
    get: async ({ sessionID }) => {
      if (sessionID === "lost-2") throw new Error("session lookup failed")
      return { id: sessionID, projectID: "global" }
    } },
  event: { subscribe },
}
const definition = (await import(pathToFileURL(plugin).href)).default
const cleanup = await definition.setup(ctx)
const result = {}
for (const sessionID of ["lost-1", "lost-2"]) {
  await hooks["session.prompt"]({ sessionID, messageID: "msg", prompt: { text: "hi", files: [] }, delivery: "steer" })
  const event = { sessionID, agent: "build", model: {}, system: [], messages: [], options: {}, tools: {} }
  await hooks["session.context"](event)
  result[sessionID] = event.system
}
release()
await new Promise((resolve) => setTimeout(resolve, 200))
await cleanup()
console.log(JSON.stringify(result))
process.exit(0)
"""

# One imported module, as in a running OpenCode service that never re-imports a changed
# plugin file: a reinstall with other roots and an uninstall must still apply to it.
OPENCODE_RESCOPE_DRIVER = r"""
import { copyFileSync, unlinkSync } from "node:fs"
import { pathToFileURL } from "node:url"
const [plugin, settings, narrowed, project, other] = process.argv.slice(1)
const folders = { "scope-a": project, "scope-b": project, "scope-c": other, "scope-d": other }
const hooks = {}
const register = (domain) => async (name, callback) => {
  hooks[`${domain}.${name}`] = callback
  return { dispose: async () => {} }
}
async function* subscribe({ signal } = {}) {
  await new Promise((resolve) => signal?.addEventListener("abort", resolve))
}
const ctx = {
  session: { hook: register("session"),
    get: async ({ sessionID }) => ({ id: sessionID, projectID: "global", location: { directory: folders[sessionID] } }) },
  event: { subscribe },
  location: { directory: other },
}
const cleanup = await (await import(pathToFileURL(plugin).href)).default.setup(ctx)
const open = async (sessionID) => {
  await hooks["session.prompt"]({ sessionID, messageID: "msg", prompt: { text: "hi", files: [] }, delivery: "steer" })
  const event = { sessionID, agent: "build", model: {}, system: [], messages: [], options: {}, tools: {} }
  await hooks["session.context"](event)
  return event.system.length
}
const result = { a: await open("scope-a") }
copyFileSync(narrowed, settings)
result.b = await open("scope-b")
result.c = await open("scope-c")
unlinkSync(settings)
result.d = await open("scope-d")
await cleanup()
console.log(JSON.stringify(result))
process.exit(0)
"""


class BridgeTest(unittest.TestCase):
    def cleanup_tmp(self):
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def setUp(self):
        kwargs = {'ignore_cleanup_errors': True} if sys.version_info >= (3, 10) else {}
        self.tmp = tempfile.TemporaryDirectory(**kwargs)
        self.addCleanup(self.cleanup_tmp)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'Brain Space'; self.vault.mkdir()
        self.state = self.root / 'state'
        self.project = self.root / 'Projects' / 'Örnek Space'; self.project.mkdir(parents=True)
        self.home = self.root / 'home'; self.home.mkdir()
        self.env = inherited_env(HOME=str(self.home), USERPROFILE=str(self.home),
                        BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        self.payload = dict(hook_event_name='SessionStart', session_id='one-session', event_id='start',
                            cwd=str(self.project), prompt='TRANSCRIPT_CANARY')

    def invoke(self, payload=None, extra=(), harness='codex', script=None, cwd=None):
        command = [sys.executable, str(script or SCRIPTS / 'beyin_v3_bridge.py'),
                   '--vault', str(self.vault), '--state', str(self.state), '--harness', harness,
                   '--project-root', str(self.root / 'Projects'), *extra]
        result = subprocess.run(command, input=json.dumps(self.payload if payload is None else payload),
                                text=True, encoding='utf-8', capture_output=True, env=self.env,
                                cwd=cwd or self.project, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def queued(self):
        return [json.loads(p.read_text(encoding='utf-8')) for p in (self.state / 'hook-queue').glob('*.json')]

    def test_parser_returns_codes_to_embedders_and_preserves_real_cli_output(self):
        script = SCRIPTS / 'beyin_v3_bridge.py'
        cases = [
            ([], 2),
            (['--harness', 'codex', '--unknown-option'], 2),
            (['--harness', 'invalid-client'], 2),
            (['--harness', 'codex', '--context-chars', 'not-an-int'], 2),
            (['--harness', 'codex', '--event', 'invalid-event'], 2),
            (['--harness', 'codex'], 2),  # --vault required inside runtime try
            (['--harness', 'codex', '--vault', str(self.vault), '--install'], 2),
            (['--harness', 'codex', '--print-config', '--install'], 2),
            (['--harness', 'codex', '--install', '--uninstall'], 2),
            (['--help'], 0),
        ]
        before = snapshot(self.root)
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                process = subprocess.run([sys.executable, str(script), *arguments], env=self.env,
                                         capture_output=True, text=True, encoding='utf-8', timeout=20)
                self.assertEqual(process.returncode, expected, process.stderr)
                self.assertEqual(snapshot(self.root), before)
                out, err = io.StringIO(), io.StringIO()
                with patch.object(sys, 'argv', [str(script)]), patch.object(sys, 'stdin', io.StringIO()), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                        patch.object(bridge, 'install_bridge') as install_mock, \
                        patch.object(bridge, 'uninstall_bridge') as uninstall_mock, patch.object(hook, 'main') as hook_mock:
                    result = bridge.main(arguments)
                self.assertEqual(result, expected)
                self.assertEqual(out.getvalue(), process.stdout)
                self.assertEqual(err.getvalue(), process.stderr)
                install_mock.assert_not_called()
                uninstall_mock.assert_not_called()
                hook_mock.assert_not_called()
                self.assertEqual(snapshot(self.root), before)
                if expected == 2:
                    self.assertIn('usage:', err.getvalue())
                    self.assertEqual(out.getvalue(), '')
                else:
                    self.assertIn('usage:', out.getvalue())
                    self.assertEqual(err.getvalue(), '')

    def test_parser_only_calls_leave_host_stdin_untouched(self):
        cases = [(['--help'], 0), (['--harness', 'invalid-client'], 2),
                 (['--harness', 'codex'], 2),
                 (['--harness', 'codex', '--vault', str(self.vault), '--install'], 2),
                 (['--harness', 'codex', '--install', '--uninstall'], 2)]
        for already_read in (False, True):
            for arguments, expected in cases:
                with self.subTest(already_read=already_read, arguments=arguments):
                    with io.TextIOWrapper(io.BytesIO(b'host input\n'), encoding='ascii') as stream:
                        if already_read:
                            self.assertEqual(stream.read(1), 'h')
                        position = stream.tell()
                        with patch.object(sys, 'stdin', stream), \
                                contextlib.redirect_stdout(io.StringIO()), \
                                contextlib.redirect_stderr(io.StringIO()):
                            self.assertEqual(bridge.main(arguments), expected)
                        self.assertEqual(stream.encoding, 'ascii')
                        self.assertEqual(stream.tell(), position)
                        self.assertEqual(stream.read(), 'ost input\n' if already_read else 'host input\n')

    def test_default_argv_help_returns_without_exiting_host(self):
        with patch.object(sys, 'argv', ['beyin_v3_bridge.py', '--help']), \
                patch.object(sys, 'stdin', io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bridge.main(), 0)

    def test_runtime_system_exit_is_not_swallowed_by_parser_boundary(self):
        with patch.object(sys, 'stdin', io.StringIO()), \
                patch.object(bridge, 'uninstall_bridge', side_effect=SystemExit(19)):
            with self.assertRaises(SystemExit) as caught:
                bridge.main(['--harness', 'codex', '--uninstall'])
        self.assertEqual(caught.exception.code, 19)

    def test_runtime_exception_keeps_existing_management_error_contract(self):
        err = io.StringIO()
        with patch.object(sys, 'stdin', io.StringIO()), contextlib.redirect_stderr(err), \
                patch.object(bridge, 'uninstall_bridge', side_effect=ValueError('synthetic failure')):
            self.assertEqual(bridge.main(['--harness', 'codex', '--uninstall']), 1)
        self.assertIn('Bridge error: synthetic failure', err.getvalue())

    def test_external_boundary_context_and_project_metadata_for_both_clients(self):
        for harness in ('claude', 'codex'):
            result = self.invoke(harness=harness)
            text = result['hookSpecificOutput']['additionalContext']
            self.assertIn('receipt --harness ' + harness, text)
            self.assertIn('--event-id EVENT_ID --summary "Work result" --ref PATH', text)
            self.assertIn('note-create', text)
            self.assertIn('Beyin bridge active', text)
            self.assertNotIn('Optional Beyin bridge', text)
            self.assertIn(str(self.vault / 'beyin.py'), text)
            self.assertLessEqual(len(text), 1500)
        events = self.queued()
        self.assertEqual(len(events), 2)
        for event in events:
            self.assertEqual(event['project'], self.project.name)
            self.assertEqual(len(event['project_id']), 24)
        self.assertNotIn(str(self.project), json.dumps(events))
        self.assertNotIn('TRANSCRIPT_CANARY', json.dumps(events))

    def test_utf8_bom_stdin_payload_handled_cleanly(self):
        bom_input = b'\xef\xbb\xbf' + json.dumps(self.payload).encode('utf-8')
        command = [sys.executable, str(SCRIPTS / 'beyin_v3_bridge.py'),
                   '--vault', str(self.vault), '--state', str(self.state), '--harness', 'claude',
                   '--project-root', str(self.root / 'Projects')]
        result = subprocess.run(command, input=bom_input, capture_output=True, env=self.env,
                                cwd=self.project, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn('hookSpecificOutput', data)

    def test_stop_forwards_receipt_reminder_when_session_was_edited(self):
        # 1. Startup instructions contain note-create instruction
        start_res = self.invoke(harness='claude')
        self.assertIn('note-create', start_res['hookSpecificOutput']['additionalContext'])
        # A real CLI write records the same project namespace as the bridge.
        self.create_cli_knowledge()
        # 3. Invoke Stop via bridge: must forward block decision with reminder
        stop_payload = dict(self.payload, hook_event_name='Stop')
        stop_res = self.invoke(stop_payload, harness='claude')
        self.assertEqual(stop_res.get('decision'), 'block')
        self.assertIn('files were edited in this external project session but no receipt was written',
                      stop_res.get('reason', ''))

    def create_cli_knowledge(self):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='one-session')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
                                 '--vault', str(self.vault), '--state', str(self.state),
                                 'note-create', '--file', '-'],
                                input=json.dumps({'source': 'knowledge/concepts/finding.md',
                                                  'text': '# Durable finding\n'}),
                                text=True, capture_output=True, env=env, cwd=self.project, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_cli_write_does_not_mark_another_project(self):
        self.invoke(harness='claude')
        self.create_cli_knowledge()
        other = self.root / 'Projects/other'
        other.mkdir()
        stop = dict(self.payload, hook_event_name='Stop', cwd=str(other))
        self.assertEqual(self.invoke(stop, harness='claude'), {})
        self.assertEqual(self.invoke(dict(self.payload, hook_event_name='Stop'), harness='claude')['decision'], 'block')

    def test_real_cli_knowledge_and_receipt_satisfy_stop(self):
        self.invoke(harness='claude')
        self.create_cli_knowledge()
        session = hashlib.sha256(json.dumps([str(self.project), 'one-session']).encode()).hexdigest()[:24]
        SyncEngine(self.vault, self.state).receipt('finding', 'Learned: durable finding',
            ['knowledge/concepts/finding.md'], 'claude', session=session)
        self.assertEqual(self.invoke(dict(self.payload, hook_event_name='Stop'), harness='claude'), {})

    def test_long_vault_path_preserves_bootstrap(self):
        self.vault = self.root / ('Brain' + 'x' * 160)
        self.vault.mkdir()
        text = self.invoke(harness='claude')['hookSpecificOutput']['additionalContext']
        self.assertIn('note-create', text)
        self.assertIn('receipt --harness claude', text)
        self.assertIn(json.dumps(self.project.name), text)
        self.assertLessEqual(len(text), 1500)

    def installed_handlers(self, harness):
        """Bridge handler groups per event as the client reads them from its global settings."""
        data = json.loads(bridge.global_hook_path(harness, home=self.home).read_text(encoding='utf-8'))
        return {event: [group for group in groups
                        if any('beyin_v3_bridge.py' in bridge._decoded_command(h.get('command'))
                               for h in group.get('hooks', []))]
                for event, groups in data['hooks'].items()}

    def run_installed(self, groups, payload):
        """Run one installed handler the way the client does: its own shell, the project as cwd."""
        command = groups[0]['hooks'][0]['command']
        result = subprocess.run(command, shell=True, input=json.dumps(payload), capture_output=True,
                                text=True, encoding='utf-8', env=self.env, cwd=self.project, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def cli(self, *args, request=None, session='issue-500'):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), *args],
                                input=json.dumps(request) if request is not None else None,
                                text=True, encoding='utf-8', capture_output=True, timeout=30,
                                env=dict(self.env, CLAUDE_CODE_SESSION_ID=session), cwd=self.project)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_issue_500_four_event_bridge_reminds_a_session_that_never_wrote_a_note(self):
        # #500 as reported: claude, the four lifecycle events, four project roots, an external
        # project session that edits files and never writes a note or a receipt.
        roots = [self.root / 'Projects'] + [self.root / name for name in ('Work', 'Lab', 'Client')]
        for root in roots[1:]:
            root.mkdir()
        bridge.install_bridge(self.vault, self.state, 'claude', roots,
                              ['SessionStart', 'Stop', 'PreCompact', 'SessionEnd'], home=self.home)
        handlers = self.installed_handlers('claude')
        self.assertEqual(set(handlers), {'SessionStart', 'Stop', 'PreCompact', 'SessionEnd', 'PostToolUse'})
        self.assertEqual(handlers['PostToolUse'][0]['matcher'], bridge.EDIT_MATCHER)
        for event, groups in handlers.items():
            # Older bridges parse this command line too; they answer {} to the edit observer.
            self.assertNotIn('PostToolUse', bridge._decoded_command(groups[0]['hooks'][0]['command']))
        base = dict(session_id='issue-500', cwd=str(self.project), transcript_path=str(self.root / 'none.jsonl'))
        start = self.run_installed(handlers['SessionStart'], dict(base, hook_event_name='SessionStart', source='startup'))
        text = start['hookSpecificOutput']['additionalContext']
        self.assertIn('note-create', text)
        self.assertLessEqual(len(text), 1500)
        session = text.split('Receipt session=')[1].split(';')[0]
        # A session that only reads is never stopped.
        self.assertEqual(self.run_installed(handlers['Stop'], dict(base, hook_event_name='Stop', stop_hook_active=False)), {})
        queued = len(self.queued())
        edit = dict(base, hook_event_name='PostToolUse', tool_name='Edit',
                    tool_input={'file_path': str(self.project / 'app.py'), 'old_string': 'a', 'new_string': 'b'})
        self.assertEqual(self.run_installed(handlers['PostToolUse'], edit), {})
        self.assertEqual(len(self.queued()), queued)  # the observer queues nothing and starts no sync
        stop = self.run_installed(handlers['Stop'], dict(base, hook_event_name='Stop', stop_hook_active=False))
        self.assertEqual(stop['decision'], 'block')
        reason = stop['reason']
        self.assertIn('note-create', reason)
        self.assertLess(reason.index('note-create'), reason.index('receipt --harness claude --session ' + session))
        self.assertIn(str(self.vault / 'beyin.py'), reason)
        self.assertLess(len(reason), 10000)
        self.assertNotIn('app.py', reason)
        self.assertEqual(self.run_installed(handlers['Stop'], dict(base, hook_event_name='Stop', stop_hook_active=True)), {})
        # The agent follows the reminder from the project folder: note first, then a receipt citing it.
        created = self.cli('note-create', '--file', '-',
                           request={'source': 'knowledge/concepts/issue-500.md', 'text': '# Kalıcı öğrenim\n'})
        self.assertEqual(created['source'], 'knowledge/concepts/issue-500.md')
        self.cli('receipt', '--harness', 'claude', '--session', session, '--event-id', 'issue-500-work',
                 '--summary', 'Ürün düzeltildi.\nLearned: köprü notu', '--ref', 'knowledge/concepts/issue-500.md')
        for _ in range(2):  # once per session: no knowledge or receipt block follows
            self.assertEqual(self.run_installed(handlers['Stop'], dict(base, hook_event_name='Stop', stop_hook_active=False)), {})

    def test_edit_window_belongs_to_one_project_and_session(self):
        edit = dict(self.payload, hook_event_name='PostToolUse', tool_name='Write',
                    tool_input={'file_path': str(self.project / 'new.txt'), 'content': 'x'})
        self.assertEqual(self.invoke(edit, harness='claude'), {})
        other = self.root / 'Projects' / 'other'; other.mkdir()
        self.assertEqual(self.invoke(dict(self.payload, hook_event_name='Stop', cwd=str(other)), harness='claude'), {})
        self.assertEqual(self.invoke(dict(self.payload, hook_event_name='Stop', session_id='second'), harness='claude'), {})
        self.assertEqual(self.invoke(dict(self.payload, hook_event_name='Stop'), harness='claude')['decision'], 'block')
        self.assertEqual(self.invoke(dict(self.payload, hook_event_name='Stop'), harness='claude'), {})

    def test_codex_apply_patch_opens_the_window_with_its_own_harness(self):
        patch_text = '*** Begin Patch\n*** Update File: app.py\n@@\n-a\n+b\n*** End Patch\n'
        edit = dict(self.payload, hook_event_name='PostToolUse', tool_name='apply_patch',
                    tool_input={'command': patch_text})
        self.assertEqual(self.invoke(edit), {})
        reason = self.invoke(dict(self.payload, hook_event_name='Stop'))['reason']
        self.assertIn('receipt --harness codex', reason)
        self.assertIn('note-create', reason)

    def test_edit_observer_respects_scope_opt_outs_and_selected_events(self):
        edit = dict(self.payload, hook_event_name='PostToolUse', tool_name='Edit',
                    tool_input={'file_path': str(self.project / 'app.py')})
        markers = self.state / 'receipt-reminders'
        cases = [('no Stop selected', edit, ['--event', 'SessionStart']),
                 ('no memory', dict(edit, no_memory=True), []),
                 ('outside roots', dict(edit, cwd=str(self.root)), [])]
        for label, payload, extra in cases:
            with self.subTest(label):
                self.assertEqual(self.invoke(payload, extra=extra, harness='claude'), {})
                self.assertFalse(markers.exists() and any(markers.iterdir()))
        self.env['BEYIN_V3_NO_RECEIPT_REMINDER'] = '1'
        self.assertEqual(self.invoke(edit, harness='claude'), {})
        self.assertEqual(self.invoke(dict(self.payload, hook_event_name='Stop'), harness='claude'), {})
        del self.env['BEYIN_V3_NO_RECEIPT_REMINDER']
        (self.vault / '.beyin-preferences.json').write_text('{"auto_sync": false}')
        self.assertEqual(self.invoke(edit, harness='claude'), {})
        self.assertFalse(markers.exists() and any(markers.iterdir()))
        self.assertEqual([event['event'] for event in self.queued()], ['Stop'])  # never an edit event

    def test_reinstall_replaces_every_earlier_bridge_handler(self):
        target = bridge.global_hook_path('claude', home=self.home)
        target.parent.mkdir(parents=True)
        custom = {'matcher': 'Edit', 'hooks': [{'type': 'command', 'command': 'echo custom'}]}
        target.write_text(json.dumps({'hooks': {'PostToolUse': [custom]}}), encoding='utf-8')
        bridge.install_bridge(self.vault, self.state, 'claude', [self.project], home=self.home)
        self.assertEqual(set(self.installed_handlers('claude')),
                         {'SessionStart', 'Stop', 'PreCompact', 'SessionEnd', 'PostToolUse'})
        bridge.install_bridge(self.vault, self.state, 'claude', [self.project], ['SessionStart'], home=self.home)
        hooks = json.loads(target.read_text(encoding='utf-8'))['hooks']
        self.assertEqual(set(hooks), {'PostToolUse', 'SessionStart'})
        self.assertEqual(hooks['PostToolUse'], [custom])
        bridge.uninstall_bridge('claude', home=self.home)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'hooks': {'PostToolUse': [custom]}})

    def test_cli_print_config_keeps_the_selected_events(self):
        base = [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault), '--state', str(self.state),
                'bridge', '--harness', 'claude', '--project-root', str(self.project), '--print-config']
        for extra, expected in (([], set(bridge.EVENTS) | {'PostToolUse'}),
                                (['--event', 'SessionStart'], {'SessionStart'}),
                                (['--event', 'Stop'], {'Stop', 'PostToolUse'})):
            with self.subTest(extra=extra):
                result = subprocess.run(base + extra, capture_output=True, text=True, encoding='utf-8',
                                        env=self.env, cwd=self.project, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(set(json.loads(result.stdout)['hooks']), expected)

    def test_bridge_reminders_name_the_cli_and_stay_bounded(self):
        self.state.mkdir()
        for cli in ('python3 /v/beyin.py', 'x' * 20000):
            with self.subTest(length=len(cli)):
                payload = {'session_id': json.dumps([str(self.project), str(len(cli))]), 'cwd': str(self.project),
                           'hook_event_name': 'PostToolUse', 'tool_input': {'file_path': str(self.project / 'a.py')}}
                hook.receipt_reminder(payload, self.state, 'claude', 'PostToolUse', vault=self.vault, bridge_cli=cli)
                reason = hook.receipt_reminder(dict(payload, hook_event_name='Stop'), self.state, 'claude', 'Stop',
                                               vault=self.vault, bridge_cli=cli)['reason']
                self.assertLess(len(reason), 3000)
                self.assertEqual(reason.count(cli), 1 if len(cli) < 1000 else 0)

    def test_bridge_knowledge_reminder_points_at_note_create(self):
        (self.vault / 'notes').mkdir()
        (self.vault / 'notes/result.md').write_text('# Result\n', encoding='utf-8')
        edit = dict(self.payload, hook_event_name='PostToolUse', tool_name='Edit',
                    tool_input={'file_path': str(self.project / 'app.py')})
        self.invoke(edit, harness='claude')
        self.assertEqual(self.invoke(dict(self.payload, hook_event_name='Stop'), harness='claude')['decision'], 'block')
        session = hashlib.sha256(json.dumps([str(self.project), 'one-session']).encode()).hexdigest()[:24]
        SyncEngine(self.vault, self.state).receipt('result', 'Done.\nLearned: köprü dersi', ['notes/result.md'],
                                                   'claude', session=session)
        reason = self.invoke(dict(self.payload, hook_event_name='Stop'), harness='claude')['reason']
        self.assertIn('knowledge/concepts/', reason)
        self.assertIn(str(self.vault / 'beyin.py'), reason)
        self.assertIn('note-create', reason)
        self.assertEqual(self.invoke(dict(self.payload, hook_event_name='Stop'), harness='claude'), {})

    def test_sensitive_folder_task_cannot_enter_external_project_context(self):
        secret = self.vault / '🔐 Kasa/task.md'
        secret.parent.mkdir()
        secret.write_text('Task details from a private folder', encoding='utf-8')
        safe = self.vault / 'notes/task.md'
        safe.parent.mkdir()
        safe.write_text('Ordinary task', encoding='utf-8')
        record = {'source': secret.relative_to(self.vault).as_posix(),
                  'source_sha256': hashlib.sha256(secret.read_bytes()).hexdigest()}
        self.assertFalse(bridge._fresh(self.vault, record))
        record = {'source': safe.relative_to(self.vault).as_posix(),
                  'source_sha256': hashlib.sha256(safe.read_bytes()).hexdigest()}
        self.assertTrue(bridge._fresh(self.vault, record))

    def test_vault_subdirectory_and_other_installed_vault_skip_duplicates(self):
        for cwd in (self.vault, self.vault / 'nested', self.project):
            cwd.mkdir(exist_ok=True)
            if cwd == self.project: (cwd / '.beyin-runtime.json').write_text('{}')
            self.assertEqual(self.invoke(dict(self.payload, cwd=str(cwd)),
                                        extra=['--project-root', str(self.root)]), {})
        self.assertEqual(self.queued(), [])

    def test_allowlist_sibling_prefix_and_symlink_escape_are_not_authorized(self):
        sibling = self.root / 'Projects-elsewhere'; sibling.mkdir()
        self.assertEqual(self.invoke(dict(self.payload, cwd=str(sibling))), {})
        link = self.project / 'outside'
        try: link.symlink_to(sibling, target_is_directory=True)
        except OSError: pass  # Native Windows without symlink permission still tests prefix boundary.
        else: self.assertEqual(self.invoke(dict(self.payload, cwd=str(link))), {})
        self.assertEqual(self.queued(), [])

    def test_unsupported_events_no_memory_internal_and_manual_are_inert(self):
        for event in ('UserPromptSubmit', 'PostToolUse', 'Unexpected'):
            self.assertEqual(self.invoke(dict(self.payload, hook_event_name=event)), {})
        self.assertEqual(self.invoke(dict(self.payload, no_memory=True)), {})
        self.env['BEYIN_V3_INTERNAL'] = '1'
        self.assertEqual(self.invoke(), {})
        del self.env['BEYIN_V3_INTERNAL']
        (self.vault / '.beyin-preferences.json').write_text('{"auto_sync": false}')
        self.assertEqual(self.invoke(), {})
        self.assertEqual(self.queued(), [])

    def test_public_skip_guard_is_inert(self):
        self.env['BEYIN_V3_SKIP'] = '1'
        self.assertEqual(self.invoke(), {})
        self.assertEqual(self.queued(), [])

    def test_public_skip_guard_honours_only_exact_one(self):
        for count, value in enumerate(('0', 'false', ''), start=1):
            with self.subTest(value=value):
                self.env['BEYIN_V3_SKIP'] = value
                result = self.invoke(dict(self.payload, event_id='start-%d' % count))
                self.assertIn('receipt --harness codex', result['hookSpecificOutput']['additionalContext'])
                self.assertEqual(len(self.queued()), count)

    def test_independent_budget_events_and_local_context_off(self):
        for budget in ('0', '20'):
            self.assertEqual(self.invoke(extra=['--context-chars', budget]), {})
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.invoke(extra=['--event', 'SessionEnd']), {})
        (self.vault / '.beyin-preferences.json').write_text('{"context_mode": "off"}')
        self.assertEqual(self.invoke(), {})

    def test_same_event_and_session_ids_in_different_projects_do_not_collide(self):
        self.invoke(); self.invoke()
        other = self.root / 'Projects' / 'second' / self.project.name; other.mkdir(parents=True)
        self.invoke(dict(self.payload, cwd=str(other)))
        events = self.queued()
        self.assertEqual(len(events), 2)
        self.assertEqual(len({e['session'] for e in events}), 2)
        self.assertEqual(len({e['project_id'] for e in events}), 2)

    def test_global_start_never_injects_companion_or_receipts(self):
        self.state.mkdir()
        (self.state / 'jev.json').write_text('{"mode":"on","features":["auto_context"]}')
        (self.vault / 'receipts').mkdir()
        (self.vault / 'receipts/private.md').write_text('PRIVATE_VAULT_CANARY')
        result = self.invoke()
        self.assertNotIn('PRIVATE_VAULT_CANARY', json.dumps(result))
        self.assertFalse((self.state / 'jev-calls.jsonl').exists())

    def test_unknown_session_cannot_mix_unrelated_receipt_checkpoints(self):
        for session in ('', 'unknown', None, 1):
            self.assertEqual(self.invoke(dict(self.payload, session_id=session)), {})
        self.assertEqual(self.queued(), [])

    def test_origin_ignores_invalid_cwd(self):
        for cwd in (123, '', 'relative', str(self.project) + '\0bad', str(self.root / 'deleted')):
            with self.subTest(cwd=cwd):
                self.assertEqual(bridge.origin({'cwd': cwd}, 'codex', self.vault), {})

    def test_origin_ignores_deleted_process_directory(self):
        from unittest.mock import patch
        with patch.object(bridge.os, 'getcwd', side_effect=FileNotFoundError):
            self.assertEqual(bridge.origin({}, 'codex', self.vault), {})

    def test_origin_ignores_symlink_loop(self):
        left, right = self.root / 'loop-a', self.root / 'loop-b'
        try:
            left.symlink_to(right, target_is_directory=True)
            right.symlink_to(left, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable on this platform')
        self.assertEqual(bridge.origin({'cwd': str(left)}, 'codex', self.vault), {})
        self.assertEqual(self.invoke(dict(self.payload, cwd=str(left))), {})

    def test_cwd_environment_and_process_fallback(self):
        payload = {k: v for k, v in self.payload.items() if k != 'cwd'}
        self.env['CLAUDE_PROJECT_DIR'] = str(self.project)
        self.assertIn('hookSpecificOutput', self.invoke(payload, harness='claude', cwd=self.home))
        self.assertIn('hookSpecificOutput', self.invoke(payload, harness='codex'))
        self.assertEqual(self.invoke(dict(self.payload, cwd=123)), {})

    def test_opencode_never_uses_the_process_directory(self):
        # The OpenCode service runs in its own directory for every session (home on 2.x); a
        # payload without the session folder must not be attributed to it, even inside a root.
        payload = {k: v for k, v in self.payload.items() if k != 'cwd'}
        self.assertEqual(self.invoke(payload, harness='opencode', cwd=self.project), {})
        self.assertEqual(self.invoke(dict(payload, cwd=None), harness='opencode', cwd=self.project), {})
        self.assertEqual(self.queued(), [])
        self.assertIn('hookSpecificOutput', self.invoke(dict(payload, cwd=str(self.project)), harness='opencode',
                                                        cwd=self.home))
        self.assertEqual([event['project'] for event in self.queued()], [self.project.name])

    def agy(self, native, workspaces, cwd=None, **fields):
        # Antigravity sends camelCase fields, no cwd, and runs the hook in the hooks.json folder.
        payload = dict(conversationId='agy-conversation', workspacePaths=[str(p) for p in workspaces], **fields)
        return self.invoke(payload, extra=['--native-event', native], harness='antigravity', cwd=cwd or self.home)

    def test_antigravity_first_workspace_is_the_project_and_only_boundaries_count(self):
        result = self.agy('PreInvocation', [self.project], invocationNum=0)
        text = result['injectSteps'][0]['ephemeralMessage']
        self.assertIn('receipt --harness antigravity', text)
        self.assertEqual(self.agy('PreInvocation', [self.project], invocationNum=1), {})
        self.assertEqual(self.agy('Stop', [self.project], fullyIdle=False), {'decision': 'stop'})
        self.assertEqual(self.agy('Stop', [self.project], fullyIdle=True), {'decision': 'stop'})
        events = self.queued()
        self.assertEqual(sorted(e['event'] for e in events), ['SessionStart', 'Stop'])
        self.assertEqual({e['harness'] for e in events}, {'antigravity'})
        self.assertEqual({e['project'] for e in events}, {self.project.name})
        # Same conversation id in another project is a different receipt session.
        other = self.root / 'Projects' / 'other'; other.mkdir()
        self.agy('PreInvocation', [other], invocationNum=0)
        self.assertEqual(len({e['session'] for e in self.queued()}), 2)

    def test_antigravity_needs_an_allowed_workspace_and_none_owned_by_a_vault(self):
        # The hook process runs in the hooks.json folder; it never stands in for the project.
        for workspaces in ([], [self.vault], [self.project, self.vault]):
            self.assertEqual(self.agy('PreInvocation', workspaces, cwd=self.project, invocationNum=0), {})
        self.assertEqual(self.invoke(dict(conversationId='agy-conversation', invocationNum=0),
                                     extra=['--native-event', 'PreInvocation'], harness='antigravity'), {})
        self.assertEqual(self.queued(), [])

    def test_antigravity_relative_workspace_is_never_resolved_against_the_hook_folder(self):
        # A relative entry would resolve against the hooks.json folder, so its vault check is meaningless.
        other = self.root / 'Projects' / 'other'; other.mkdir()
        for workspaces in (['Projects/other'], [str(self.project), 'Projects/other']):
            self.assertEqual(self.invoke(dict(conversationId='agy-relative', invocationNum=0, workspacePaths=workspaces),
                                         extra=['--native-event', 'PreInvocation'], harness='antigravity',
                                         cwd=self.root), {})
        self.assertEqual(self.queued(), [])

    def test_antigravity_config_is_a_named_hook_whose_command_runs(self):
        config = self.invoke(extra=['--print-config'], harness='antigravity')
        self.assertEqual(list(config), ['beyin-v3-bridge'])
        self.assertEqual(set(config['beyin-v3-bridge']), {'PreInvocation', 'Stop'})
        command = config['beyin-v3-bridge']['PreInvocation'][0]['command']
        payload = dict(conversationId='agy-config', invocationNum=0, workspacePaths=[str(self.project)])
        result = subprocess.run(command, shell=True, input=json.dumps(payload), capture_output=True,
                                text=True, encoding='utf-8', env=self.env, cwd=self.home, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Receipt session=', json.loads(result.stdout)['injectSteps'][0]['ephemeralMessage'])
        narrowed = self.invoke(extra=['--print-config', '--event', 'SessionStart'], harness='antigravity')
        self.assertEqual(set(narrowed['beyin-v3-bridge']), {'PreInvocation'})
        with self.assertRaises(AssertionError):
            self.invoke(extra=['--print-config', '--event', 'SessionEnd'], harness='antigravity')

    def test_folder_below_another_installed_vault_is_skipped_for_every_client(self):
        # The owning vault is found by walking up, so a subfolder is as silent as the vault root.
        below = self.root / 'Projects' / 'second-vault' / 'notes' / 'deep'; below.mkdir(parents=True)
        (self.root / 'Projects' / 'second-vault' / '.beyin-runtime.json').write_text('{}')
        for harness in ('codex', 'claude'):
            self.assertEqual(self.invoke(dict(self.payload, cwd=str(below)), harness=harness), {})
        for workspaces in ([below], [self.project, below]):
            self.assertEqual(self.agy('PreInvocation', workspaces, invocationNum=0), {})
        self.assertEqual(self.queued(), [])

    def test_antigravity_vault_adapter_labels_the_first_workspace_never_the_hook_folder(self):
        # A real install: the vault's own .agents/hooks.json shares the bridge's project rule (origin()).
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        local = json.loads((self.vault / '.agents/hooks.json').read_text(encoding='utf-8'))['beyin-v3']
        config = self.invoke(extra=['--print-config'], harness='antigravity',
                             script=self.vault / '.claude/scripts/beyin_v3_bridge.py')['beyin-v3-bridge']
        # The global hook keeps the timeout the installer chose for the vault's Antigravity hooks.
        self.assertEqual({native: handlers[0]['timeout'] for native, handlers in config.items()},
                         {native: handlers[0]['timeout'] for native, handlers in local.items()})
        hook_folder = self.vault / '.agents'  # Antigravity runs a hook where its hooks.json lives.
        cases = {'agy-vault': (dict(workspacePaths=[str(self.vault)]), self.vault.name),
                 'agy-first-of-two': (dict(workspacePaths=[str(self.project), str(self.vault)]), self.project.name),
                 'agy-cwd-field': (dict(workspacePaths=[str(self.vault)], cwd=str(hook_folder)), self.vault.name),
                 'agy-no-workspaces': ({}, None),
                 'agy-null-workspace': (dict(workspacePaths=[None]), None),
                 'agy-relative-workspace': (dict(workspacePaths=['.']), None),
                 'agy-cwd-field-only': (dict(cwd=str(hook_folder)), None)}
        for conversation, (fields, _) in cases.items():
            result = subprocess.run(local['PreInvocation'][0]['command'], shell=True, capture_output=True, text=True,
                                    input=json.dumps(dict(fields, conversationId=conversation, invocationNum=0)),
                                    encoding='utf-8', env=self.env, cwd=hook_folder, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
        events = {event['session']: event for event in self.queued()}
        self.assertEqual(len(events), len(cases))
        for conversation, (_, label) in cases.items():
            with self.subTest(conversation=conversation):
                # The vault adapter keeps the plain conversation id as the session.
                event = events[hashlib.sha256(conversation.encode()).hexdigest()[:24]]
                self.assertEqual(event['harness'], 'antigravity')
                self.assertEqual(event.get('project'), label)
                self.assertEqual('project_id' in event, label is not None)
        # The global hook sees the same in-vault conversation and leaves it to the vault.
        self.assertEqual(self.agy('PreInvocation', [self.vault], invocationNum=0), {})
        self.assertEqual(len(self.queued()), len(cases))

    def test_gap_projection_preserves_project_and_receipt_clears_gap(self):
        self.invoke()
        self.invoke(dict(self.payload, hook_event_name='Stop', event_id='stop'))
        self.assertEqual(hook.drain_queue(self.vault, self.state)['processed'], 2)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints']
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]['project'], self.project.name)
        engine = SyncEngine(self.vault, self.state)
        engine.note_create('notes/result.md', 'Synthetic result.', {'id': 'result', 'project': 'demo'})
        engine.receipt('finished', 'Synthetic result.', ['notes/result.md'], 'codex', session=gaps[0]['session'])
        engine.sync()
        self.assertEqual(json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['potential_missing_receipts'], 0)

    def test_legacy_checkpoint_schema_migrates_without_losing_rows(self):
        engine = SyncEngine(self.vault, self.state)
        with engine.store._connect() as db:
            db.execute('DROP TABLE IF EXISTS receipt_checkpoints')
            db.execute('CREATE TABLE receipt_checkpoints(harness TEXT,session TEXT,at REAL,PRIMARY KEY(harness,session))')
            db.execute("INSERT INTO receipt_checkpoints VALUES ('codex','old',123)")
        hook.drain_queue(self.vault, self.state)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints']
        self.assertEqual(gaps, [], 'legacy rows without prompt evidence do not imply missing work')
        with engine.store._connect() as db:
            self.assertEqual(db.execute("SELECT harness,session,at FROM receipt_checkpoints WHERE session='old'").fetchone(),
                             ('codex', 'old', 123.0), 'migration preserves the source row')

    def test_installed_zip_config_executes_from_external_project_without_global_writes(self):
        package = self.root / 'candidate.zip'
        def run(*args):
            result = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True,
                                    encoding='utf-8', env=self.env, cwd=self.project, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result
        run(ROOT / 'scripts/build_v3_release.py', '--version', '3.1.1', '--output', package)
        with zipfile.ZipFile(package) as archive: archive.extractall(self.root / 'package')
        run(self.root / 'package/scripts/install_v3.py', '--vault', self.vault, '--state', self.state)
        installed = self.vault / '.claude/scripts/beyin_v3_bridge.py'
        self.assertTrue(installed.is_file())
        for harness in ('codex', 'claude'):
            config = self.invoke(extra=['--print-config'], harness=harness, script=installed)
            self.assertEqual(set(config['hooks']), set(bridge.EVENTS) | {'PostToolUse'})
            self.assertEqual(config['hooks']['PostToolUse'][0]['matcher'], bridge.EDIT_MATCHER)
            command = config['hooks']['SessionStart'][0]['hooks'][0]['command']
            result = subprocess.run(command, shell=True, input=json.dumps(self.payload), capture_output=True,
                                    text=True, encoding='utf-8', env=self.env, cwd=self.project, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Receipt session=', json.loads(result.stdout)['hookSpecificOutput']['additionalContext'])
        self.assertFalse((self.home / '.codex/hooks.json').exists())
        self.assertFalse((self.home / '.claude/settings.json').exists())

    def test_project_context_off_by_default_leaves_output_byte_for_byte_identical(self):
        engine = SyncEngine(self.vault, self.state)
        engine.task_create('tasks/test.md', 'Task', {'id': 'task-a', 'title': 'Task A', 'status': 'active', 'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user'})
        engine.sync()
        result = self.invoke()
        text = result['hookSpecificOutput']['additionalContext']
        self.assertNotIn('Project context', text)
        self.assertNotIn('Task A', text)

    def test_project_context_on_injects_scoped_receipt_and_matching_due_tasks(self):
        bridge.save_project_context(self.state, True)
        self.invoke(dict(self.payload, event_id='init-start'))
        self.invoke(dict(self.payload, hook_event_name='Stop', event_id='init-stop'))
        hook.drain_queue(self.vault, self.state)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints']
        session = gaps[0]['session']
        engine = SyncEngine(self.vault, self.state)
        engine.note_create('notes/out.md', 'Outcome', {'id': 'out', 'project': self.project.name})
        engine.receipt('rec-1', 'Sprint 12 completed with zero bugs.', ['notes/out.md'], 'codex', session=session)
        engine.task_create('tasks/deploy.md', 'Deploy details', {'id': 'deploy', 'title': 'Deploy v2', 'next_action': 'Run ansible', 'status': 'active', 'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user'})
        engine.sync()
        result = self.invoke(dict(self.payload, event_id='second-start'))
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn('Project context (' + self.project.name + '):', text)
        self.assertIn('Son kayit: Sprint 12 completed with zero bugs.', text)
        self.assertIn('Tarihi gelen gorevler:', text)
        self.assertIn('- Deploy v2: Run ansible', text)

    def test_different_cwd_receipts_do_not_leak_across_projects(self):
        bridge.save_project_context(self.state, True)
        self.invoke(dict(self.payload, event_id='start-a'))
        self.invoke(dict(self.payload, hook_event_name='Stop', event_id='stop-a'))
        hook.drain_queue(self.vault, self.state)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints']
        session_a = gaps[0]['session']

        other = self.root / 'Projects' / 'Other Project'; other.mkdir()
        self.invoke(dict(self.payload, cwd=str(other), event_id='start-b'), cwd=other)
        self.invoke(dict(self.payload, cwd=str(other), hook_event_name='Stop', event_id='stop-b'), cwd=other)
        hook.drain_queue(self.vault, self.state)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints']
        session_b = [g['session'] for g in gaps if g['project'] == 'Other Project'][0]

        engine = SyncEngine(self.vault, self.state)
        engine.note_create('notes/out_a.md', 'Outcome A', {'id': 'out_a', 'project': self.project.name})
        engine.note_create('notes/out_b.md', 'Outcome B', {'id': 'out_b', 'project': 'Other Project'})
        engine.receipt('rec-a', 'ALPHA_PROJECT_CANARY: Completed project A work.', ['notes/out_a.md'], 'codex', session=session_a)
        engine.receipt('rec-b', 'BETA_PROJECT_CANARY: Completed project B work.', ['notes/out_b.md'], 'codex', session=session_b)
        engine.sync()

        text_a = self.invoke(dict(self.payload, event_id='read-a'))['hookSpecificOutput']['additionalContext']
        self.assertIn('ALPHA_PROJECT_CANARY', text_a)
        self.assertNotIn('BETA_PROJECT_CANARY', text_a)

        text_b = self.invoke(dict(self.payload, cwd=str(other), event_id='read-b'), cwd=other)['hookSpecificOutput']['additionalContext']
        self.assertIn('BETA_PROJECT_CANARY', text_b)
        self.assertNotIn('ALPHA_PROJECT_CANARY', text_b)

    def test_other_projects_due_tasks_show_count_only_never_titles(self):
        bridge.save_project_context(self.state, True)
        engine = SyncEngine(self.vault, self.state)
        engine.task_create('tasks/my.md', 'My task', {'id': 'my-task', 'title': 'Visible Local Task', 'next_action': 'Fix it', 'status': 'active', 'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user'})
        engine.task_create('tasks/other1.md', 'Other 1', {'id': 'other-1', 'title': 'SECRET_TITLE_ONE', 'next_action': 'Secret action 1', 'status': 'active', 'due_at': '2026-09-01', 'project': 'Unrelated Project', 'owner': 'user'})
        engine.task_create('tasks/other2.md', 'Other 2', {'id': 'other-2', 'title': 'SECRET_TITLE_TWO', 'next_action': 'Secret action 2', 'status': 'waiting', 'due_at': '2026-09-02', 'project': 'Different Project', 'owner': 'user'})
        engine.sync()
        text = self.invoke()['hookSpecificOutput']['additionalContext']
        self.assertIn('- Visible Local Task: Fix it', text)
        self.assertIn('(bu proje disinda 2 tarihi gelmis gorev)', text)
        self.assertNotIn('SECRET_TITLE_ONE', text)
        self.assertNotIn('SECRET_TITLE_TWO', text)
        self.assertNotIn('Secret action', text)

    def test_private_records_never_enter_project_context(self):
        bridge.save_project_context(self.state, True)
        self.invoke(dict(self.payload, event_id='start-priv'))
        self.invoke(dict(self.payload, hook_event_name='Stop', event_id='stop-priv'))
        hook.drain_queue(self.vault, self.state)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints']
        session = gaps[0]['session']
        engine = SyncEngine(self.vault, self.state)
        engine.note_create('notes/public.md', 'Outcome', {'id': 'public-out', 'project': self.project.name})
        engine.note_create('notes/secret.md', 'Private outcome', {'id': 'secret-out', 'project': self.project.name, 'visibility': 'private'})
        engine.receipt('rec-old', 'OLDER_PUBLIC_RECEIPT', ['notes/public.md'], 'codex', session=session)
        engine.receipt('rec-priv', 'PRIVATE_RECEIPT_CANARY', ['notes/secret.md'], 'codex', session=session)
        engine.task_create('tasks/priv_task.md', 'Private task', {'id': 'priv-task', 'title': 'PRIVATE_TASK_CANARY', 'next_action': 'Secret', 'status': 'active', 'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user', 'visibility': 'private'})
        engine.task_create('tasks/other_priv.md', 'Other private', {'id': 'other-priv', 'title': 'Other', 'status': 'active', 'due_at': '2026-09-01', 'project': 'Elsewhere', 'owner': 'user', 'visibility': 'private'})
        engine.sync()
        text = self.invoke(dict(self.payload, event_id='read-priv'))['hookSpecificOutput']['additionalContext']
        self.assertNotIn('PRIVATE_RECEIPT_CANARY', text)
        self.assertNotIn('PRIVATE_TASK_CANARY', text)
        self.assertNotIn('bu proje disinda', text)
        # A receipt that cites a private source is skipped, not the whole block.
        self.assertIn('Son kayit: OLDER_PUBLIC_RECEIPT', text)

    def test_stale_superseded_and_untrusted_tasks_stay_out(self):
        bridge.save_project_context(self.state, True)
        engine = SyncEngine(self.vault, self.state)
        base = {'status': 'active', 'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user'}
        engine.task_create('tasks/stale.md', 'Body', dict(base, id='stale', title='STALE_TASK'))
        engine.task_create('tasks/old.md', 'Body', dict(base, id='old', title='OLD_TASK'))
        engine.task_create('tasks/new.md', 'Body', dict(base, id='new', title='NEW_TASK'))
        engine.note_create('notes/decision.md', 'Old task replaced', {'id': 'decision', 'project': self.project.name, 'supersedes': ['old']})
        engine.task_create('tasks/web.md', 'Body', dict(base, id='web', title='UNTRUSTED_TASK'))
        engine.sync()
        with engine.store._connect() as db:
            row = json.loads(db.execute("SELECT payload FROM records WHERE id='web'").fetchone()[0])
            db.execute("UPDATE records SET payload=? WHERE id='web'", (json.dumps(dict(row, trust='untrusted')),))
        with (self.vault / 'tasks/stale.md').open('a', encoding='utf-8') as handle:
            handle.write('edited after indexing\n')
        text = self.invoke()['hookSpecificOutput']['additionalContext']
        self.assertIn('- NEW_TASK', text)
        for canary in ('STALE_TASK', 'OLD_TASK', 'UNTRUSTED_TASK'):
            self.assertNotIn(canary, text)

    def test_project_context_failure_keeps_the_startup_instructions(self):
        bridge.save_project_context(self.state, True)
        baseline = self.invoke(dict(self.payload, event_id='before'))['hookSpecificOutput']['additionalContext']
        (self.state / 'memory.sqlite3').write_bytes(b'not a database')
        text = self.invoke(dict(self.payload, event_id='broken'))['hookSpecificOutput']['additionalContext']
        self.assertIn('receipt --harness codex', text)
        self.assertNotIn('Project context', text)
        self.assertEqual(text, baseline)
        (self.state / 'project-context.json').write_text('[true]', encoding='utf-8')
        self.assertFalse(bridge.read_project_context(self.state))

    def test_default_budget_keeps_due_tasks_next_to_a_long_receipt(self):
        bridge.save_project_context(self.state, True)
        self.invoke(dict(self.payload, event_id='start-long'))
        self.invoke(dict(self.payload, hook_event_name='Stop', event_id='stop-long'))
        hook.drain_queue(self.vault, self.state)
        session = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))['checkpoints'][0]['session']
        engine = SyncEngine(self.vault, self.state)
        engine.note_create('notes/long.md', 'Outcome', {'id': 'long-out', 'project': self.project.name})
        engine.receipt('rec-long', 'LONG_SUMMARY ' + 'word ' * 200, ['notes/long.md'], 'codex', session=session)
        engine.task_create('tasks/due.md', 'Body', {'id': 'due', 'title': 'DUE_NEXT_TO_LONG', 'status': 'active', 'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user'})
        engine.sync()
        text = self.invoke(dict(self.payload, event_id='read-long'))['hookSpecificOutput']['additionalContext']
        self.assertLessEqual(len(text), 1500)
        self.assertIn('Son kayit: LONG_SUMMARY', text)
        self.assertIn('...', text)
        self.assertIn('- DUE_NEXT_TO_LONG', text)

    def test_budget_cap_drops_entries_cleanly_without_partial_cut(self):
        engine = SyncEngine(self.vault, self.state)
        for i in range(15):
            engine.task_create(f'tasks/t_{i:02d}.md', 'Body', {
                'id': f'task-{i:02d}',
                'title': f'TASK_{i:02d}_TITLE_' + ('x' * 40),
                'next_action': f'Action {i}',
                'status': 'active', 'due_at': '2026-09-01',
                'project': self.project.name, 'owner': 'user'
            })
        engine.sync()
        ctx = bridge.project_context(self.vault, self.state, 'test-id', self.project.name, budget=150)
        self.assertLessEqual(len(ctx), 150)
        lines = ctx.splitlines()
        for line in lines[2:]:
            self.assertTrue(line.startswith('- TASK_'))
            self.assertIn(': Action', line)

    def test_project_context_handles_legacy_schema_without_project_id_column(self):
        engine = SyncEngine(self.vault, self.state)
        engine.task_create('tasks/legacy_due.md', 'Body', {
            'id': 'legacy_due', 'title': 'LEGACY_DUE_TASK', 'status': 'active',
            'due_at': '2026-09-01', 'project': self.project.name, 'owner': 'user'
        })
        engine.sync()
        import sqlite3
        db = sqlite3.connect(self.state / 'memory.sqlite3')
        try:
            db.execute('DROP TABLE IF EXISTS receipt_checkpoints')
            db.execute('CREATE TABLE receipt_checkpoints(harness TEXT, session TEXT, at REAL, turn_at REAL DEFAULT 0, PRIMARY KEY(harness,session))')
        finally:
            db.close()
        ctx = bridge.project_context(self.vault, self.state, 'test-id', self.project.name, budget=1200)
        self.assertIn('Project context (' + self.project.name + '):', ctx)
        self.assertIn('- LEGACY_DUE_TASK', ctx)

    def raw(self, data):
        command = [sys.executable, str(SCRIPTS / 'beyin_v3_bridge.py'), '--vault', str(self.vault), '--state',
                   str(self.state), '--harness', 'claude', '--project-root', str(self.root / 'Projects')]
        result = subprocess.run(command, input=data, capture_output=True, env=self.env, cwd=self.project, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_undecodable_prompt_byte_keeps_the_event_and_a_cut_payload_stays_inert(self):
        # A stray byte in the prompt no longer costs the whole event: it is read as U+FFFD.
        data = json.dumps(self.payload).encode('utf-8').replace(b'TRANSCRIPT_CANARY', b'TRANSCRIPT\xffCANARY')
        self.assertIn('receipt --harness claude', self.raw(data)['hookSpecificOutput']['additionalContext'])
        self.assertEqual(len(self.queued()), 1)
        # A payload cut at the 1 MB read is not JSON: no context, no event, exit 0.
        cut = json.dumps(dict(self.payload, event_id='cut', prompt='x' * 1_100_000)).encode('utf-8')
        self.assertEqual(self.raw(cut), {})
        self.assertEqual(len(self.queued()), 1)

    def test_bridge_install_and_uninstall_antigravity(self):
        gemini_dir = self.home / '.gemini/config'
        gemini_dir.mkdir(parents=True, exist_ok=True)
        hooks_json = gemini_dir / 'hooks.json'
        hooks_json.write_text(json.dumps({'custom-hook': {'PreInvocation': [{'type': 'command', 'command': 'echo custom'}]}}), encoding='utf-8')
        gemini_md = gemini_dir / 'GEMINI.md'
        gemini_md.write_text("# Existing Instructions\nKeep this rule safe.\n", encoding='utf-8')

        # Install bridge
        res = bridge.install_bridge(self.vault, self.state, 'antigravity', [self.project], home=self.home)
        self.assertEqual(res['status'], 'installed')
        self.assertEqual(res['harness'], 'antigravity')

        # Verify hooks.json has beyin-v3-bridge AND preserved custom-hook
        data = json.loads(hooks_json.read_text(encoding='utf-8'))
        self.assertIn('beyin-v3-bridge', data)
        self.assertIn('custom-hook', data)
        self.assertTrue((gemini_dir / 'hooks.json.bak').is_file())

        # Verify GEMINI.md has pointer and preserved custom rule
        md_text = gemini_md.read_text(encoding='utf-8')
        self.assertIn('<!-- beyin-v3-bridge:start -->', md_text)
        self.assertIn('"' + str(self.vault) + '"', md_text)
        self.assertIn('Keep this rule safe.', md_text)
        self.assertTrue((gemini_dir / 'GEMINI.md.bak').is_file())

        # Idempotent install: run again, verify content is preserved and no extra backups
        res2 = bridge.install_bridge(self.vault, self.state, 'antigravity', [self.project], home=self.home)
        self.assertEqual(res2['status'], 'installed')
        self.assertFalse((gemini_dir / 'hooks.json.bak.1').exists())

        # Uninstall bridge
        un_res = bridge.uninstall_bridge('antigravity', home=self.home)
        self.assertEqual(un_res['status'], 'uninstalled')
        self.assertGreaterEqual(un_res['removed_count'], 1)

        # Verify hooks.json no longer has beyin-v3-bridge but still has custom-hook
        data_after = json.loads(hooks_json.read_text(encoding='utf-8'))
        self.assertNotIn('beyin-v3-bridge', data_after)
        self.assertIn('custom-hook', data_after)

        # Verify GEMINI.md no longer has pointer block but still has custom rule
        md_after = gemini_md.read_text(encoding='utf-8')
        self.assertNotIn('<!-- beyin-v3-bridge:start -->', md_after)
        self.assertIn('Keep this rule safe.', md_after)

    def test_install_rejects_invalid_global_settings_without_replacing_them(self):
        for harness in ('claude', 'codex', 'antigravity'):
            target = bridge.global_hook_path(harness, home=self.home)
            target.parent.mkdir(parents=True, exist_ok=True)
            for raw in (b'{invalid', b'[]', b'{"hooks": []}', b'{"hooks": {"SessionStart": {}}}'):
                if harness == 'antigravity' and b'hooks' in raw:
                    continue
                with self.subTest(harness=harness, raw=raw):
                    target.write_bytes(raw)
                    with self.assertRaises(ValueError):
                        bridge.install_bridge(self.vault, self.state, harness, [self.project], home=self.home)
                    self.assertEqual(target.read_bytes(), raw)
                    self.assertFalse(target.with_name(target.name + '.bak').exists())

    def test_install_preserves_unknown_handlers_and_replaces_windows_only_bridge(self):
        target = bridge.global_hook_path('codex', home=self.home)
        target.parent.mkdir(parents=True)
        foreign = {'matcher': 'custom', 'hooks': ['opaque-handler', {'type': 'future', 'command': None}]}
        windows = {'hooks': [{'type': 'command', 'commandWindows': 'python beyin_v3_bridge.py'}]}
        target.write_text(json.dumps({'hooks': {'SessionStart': [foreign, windows], 'Stop': []}}), encoding='utf-8')
        bridge.install_bridge(self.vault, self.state, 'codex', [self.project], ['SessionStart'], home=self.home)
        groups = json.loads(target.read_text(encoding='utf-8'))['hooks']['SessionStart']
        self.assertEqual(groups[0], foreign)
        self.assertEqual(len(groups), 2)
        bridge.uninstall_bridge('codex', home=self.home)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')),
                         {'hooks': {'SessionStart': [foreign], 'Stop': []}})

    def test_uninstall_without_bridge_preserves_empty_hook_groups_byte_for_byte(self):
        target = bridge.global_hook_path('claude', home=self.home)
        target.parent.mkdir(parents=True)
        raw = b'{ "hooks": {"SessionStart": [], "Stop": [{"hooks": []}]} }\n'
        target.write_bytes(raw)
        result = bridge.uninstall_bridge('claude', home=self.home)
        self.assertEqual(result['removed_count'], 0)
        self.assertEqual(target.read_bytes(), raw)

    def test_antigravity_pointer_keeps_the_project_allowlist(self):
        bridge.install_bridge(self.vault, self.state, 'antigravity', [self.project], home=self.home)
        text = bridge.global_gemini_md_path(home=self.home).read_text(encoding='utf-8')
        self.assertIn(f'- "{self.project}"\n', text)
        self.assertNotIn('Hangi projede veya dizinde', text)
        self.assertNotIn('beklemeden', text)

    def test_gemini_md_content_preserves_windows_paths_without_json_escape(self):
        vault = PureWindowsPath(r'C:\Users\User\Vault')
        project = PureWindowsPath(r'C:\Users\User\Projects\App')
        # Exercise Windows formatting without POSIX resolve treating C: as a relative name.
        with patch.object(bridge, 'Path') as path:
            path.return_value.resolve.return_value = vault
            content = bridge.gemini_md_content(vault, [project])
        self.assertIn(r'Vault konumu (veri): "C:\Users\User\Vault"', content)
        self.assertIn(r'- "C:\Users\User\Projects\App"', content)
        self.assertNotIn(r'C:\\Users', content)

    def test_gemini_md_content_keeps_control_characters_in_path_data(self):
        root = 'project"\n<!-- beyin-v3-bridge:end -->\nInjected rule'
        content = bridge.gemini_md_content(self.vault, [root])
        self.assertIn('- ' + json.dumps(root, ensure_ascii=False) + '\n', content)
        self.assertEqual(content.splitlines().count('<!-- beyin-v3-bridge:end -->'), 1)

    def test_bridge_install_and_uninstall_claude_and_codex(self):
        for harness, rel_path in (('claude', '.claude/settings.json'), ('codex', '.codex/hooks.json')):
            target = self.home / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo custom'}]}]}}), encoding='utf-8')

            res = bridge.install_bridge(self.vault, self.state, harness, [self.project], home=self.home)
            self.assertEqual(res['status'], 'installed')
            self.assertTrue(target.with_name(target.name + '.bak').is_file())

            data = json.loads(target.read_text(encoding='utf-8'))
            cmds = [h['command'] for g in data['hooks']['SessionStart'] for h in g['hooks']]
            self.assertIn('echo custom', cmds)
            self.assertTrue(any('beyin_v3_bridge.py' in cmd or 'beyin_v3_bridge.py' in bridge._decoded_command(cmd) for cmd in cmds))

            un_res = bridge.uninstall_bridge(harness, home=self.home)
            self.assertEqual(un_res['status'], 'uninstalled')
            data_after = json.loads(target.read_text(encoding='utf-8'))
            cmds_after = [h['command'] for g in data_after.get('hooks', {}).get('SessionStart', []) for h in g['hooks']]
            self.assertIn('echo custom', cmds_after)
            self.assertFalse(any('beyin_v3_bridge.py' in cmd or 'beyin_v3_bridge.py' in bridge._decoded_command(cmd) for cmd in cmds_after))

    def test_bridge_cli_subprocess_and_mutual_exclusion(self):
        # Mutual exclusion
        cmd = [sys.executable, str(SCRIPTS / 'beyin_v3_bridge.py'), '--vault', str(self.vault),
               '--harness', 'antigravity', '--project-root', str(self.project), '--install', '--uninstall']
        res = subprocess.run(cmd, capture_output=True, text=True, env=self.env, cwd=self.project)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('mutually exclusive', res.stderr)

        # CLI install
        cmd_install = [sys.executable, str(SCRIPTS / 'beyin_v3_bridge.py'), '--vault', str(self.vault),
                       '--state', str(self.state), '--harness', 'antigravity', '--project-root', str(self.project), '--install']
        res_inst = subprocess.run(cmd_install, capture_output=True, text=True, env=self.env, cwd=self.project)
        self.assertEqual(res_inst.returncode, 0, res_inst.stderr)
        data = json.loads(res_inst.stdout)
        self.assertEqual(data['status'], 'installed')

        # CLI uninstall
        cmd_uninst = [sys.executable, str(SCRIPTS / 'beyin_v3_bridge.py'), '--harness', 'antigravity', '--uninstall']
        res_uninst = subprocess.run(cmd_uninst, capture_output=True, text=True, env=self.env, cwd=self.project)
        self.assertEqual(res_uninst.returncode, 0, res_uninst.stderr)
        data_un = json.loads(res_uninst.stdout)
        self.assertEqual(data_un['status'], 'uninstalled')

    def test_bridge_print_config_cli_and_human_mode(self):
        import shutil
        vault_entry = self.vault / 'beyin.py'
        shutil.copy(str(ROOT / 'scripts/beyin_entry.py'), str(vault_entry))
        (self.vault / '.beyin-version').write_text('3.9.1', encoding='utf-8')
        (self.vault / '.beyin-runtime.json').write_text(json.dumps({'state': str(self.state)}), encoding='utf-8')
        vault_scripts = self.vault / '.claude/scripts'
        vault_scripts.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(ROOT / 'scripts/beyin_v3.py'), str(vault_scripts / 'beyin_v3_cli.py'))
        shutil.copy(str(SCRIPTS / 'beyin_v3_bridge.py'), str(vault_scripts / 'beyin_v3_bridge.py'))

        # Run with --human to test interactive terminal code path
        res_human = subprocess.run(
            [sys.executable, str(vault_entry), '--human', 'bridge', '--harness', 'claude',
             '--project-root', str(self.project), '--print-config'],
            capture_output=True, text=True, env=self.env, cwd=self.project, timeout=20,
        )
        self.assertEqual(res_human.returncode, 0, res_human.stderr)
        self.assertNotIn('cannot unpack non-iterable', res_human.stderr)
        config_data = json.loads(res_human.stdout)
        self.assertIn('hooks', config_data)
        self.assertIn('SessionStart', config_data['hooks'])

        # Antigravity print-config
        res_ag = subprocess.run(
            [sys.executable, str(vault_entry), 'bridge', '--harness', 'antigravity',
             '--project-root', str(self.project), '--print-config'],
            capture_output=True, text=True, env=self.env, cwd=self.project, timeout=20,
        )
        self.assertEqual(res_ag.returncode, 0, res_ag.stderr)
        ag_data = json.loads(res_ag.stdout)
        self.assertIn('beyin-v3-bridge', ag_data)
        self.assertIn('PreInvocation', ag_data['beyin-v3-bridge'])


    def test_bridge_install_and_uninstall_opencode_global_plugin(self):
        target = bridge.global_opencode_plugin_path(home=self.home)
        settings = bridge.global_opencode_settings_path(home=self.home)
        self.assertEqual(target, self.home / '.config/opencode/plugins/beyin-v3-bridge.js')
        self.assertEqual(settings, self.home / '.config/opencode/beyin-v3-bridge.json')
        self.assertFalse(target.exists())

        res = bridge.install_bridge(self.vault, self.state, 'opencode', [self.project], home=self.home)
        self.assertEqual(res['status'], 'installed')
        self.assertEqual(res['hook_file'], str(target))
        text = target.read_text(encoding='utf-8')
        self.assertIn(bridge.OPENCODE_MARKER, text)
        # The module is the same for every install; paths and roots live in the settings file.
        self.assertEqual(text, bridge.opencode_plugin_content())
        self.assertNotIn(str(self.vault), text)
        data = json.loads(settings.read_text(encoding='utf-8'))
        self.assertEqual(data['managed_by'], 'beyin-v3-bridge')
        self.assertEqual(data['python'], sys.executable)
        command = data['command']
        self.assertEqual(Path(command[0]).name, 'beyin_v3_bridge.py')
        self.assertEqual(command[command.index('--vault') + 1], str(self.vault.resolve()))
        self.assertEqual(command[command.index('--state') + 1], str(self.state.resolve()))
        self.assertEqual(command[command.index('--harness') + 1], 'opencode')
        self.assertEqual(command[command.index('--project-root') + 1], str(self.project.resolve()))

        # Idempotent install: same bytes, no extra backup.
        bridge.install_bridge(self.vault, self.state, 'opencode', [self.project], home=self.home)
        self.assertFalse(target.with_name(target.name + '.bak').exists())
        self.assertFalse(settings.with_name(settings.name + '.bak').exists())

        # Reinstall after an edit backs the file up and rewrites the managed plugin.
        target.write_text(text + '\n// local edit\n', encoding='utf-8')
        bridge.install_bridge(self.vault, self.state, 'opencode', [self.project], home=self.home)
        self.assertTrue(target.with_name(target.name + '.bak').is_file())
        self.assertNotIn('// local edit', target.read_text(encoding='utf-8'))

        # Other roots change only the settings, with a backup of the previous ones.
        other = self.root / 'Projects' / 'Other'; other.mkdir()
        bridge.install_bridge(self.vault, self.state, 'opencode', [other], home=self.home)
        self.assertEqual(target.read_text(encoding='utf-8'), text)
        self.assertIn(str(other.resolve()), json.loads(settings.read_text(encoding='utf-8'))['command'])
        self.assertTrue(settings.with_name(settings.name + '.bak').is_file())

        # A user file at either managed path is never replaced or deleted; nothing is written.
        target.unlink()
        target.write_text('export default {}\n', encoding='utf-8')
        before = settings.read_bytes()
        with self.assertRaises(ValueError):
            bridge.install_bridge(self.vault, self.state, 'opencode', [self.project], home=self.home)
        self.assertEqual(target.read_text(encoding='utf-8'), 'export default {}\n')
        self.assertEqual(settings.read_bytes(), before)
        # Uninstall removes only the managed settings file and keeps the user's plugin.
        self.assertEqual(bridge.uninstall_bridge('opencode', home=self.home)['removed_count'], 1)
        self.assertTrue(target.is_file())
        self.assertFalse(settings.exists())
        target.unlink()
        settings.write_text('{"user": true}\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            bridge.install_bridge(self.vault, self.state, 'opencode', [self.project], home=self.home)
        self.assertFalse(target.exists())
        self.assertEqual(bridge.uninstall_bridge('opencode', home=self.home)['removed_count'], 0)
        self.assertEqual(settings.read_text(encoding='utf-8'), '{"user": true}\n')

        # A managed plugin and its settings are removed by uninstall.
        settings.unlink()
        bridge.install_bridge(self.vault, self.state, 'opencode', [self.project], home=self.home)
        self.assertEqual(bridge.uninstall_bridge('opencode', home=self.home)['removed_count'], 2)
        self.assertFalse(target.exists())
        self.assertFalse(settings.exists())

    @unittest.skipUnless(shutil.which('node'), 'node is required to execute the OpenCode plugin')
    def test_opencode_bridge_plugin_injects_only_for_allowed_projects(self):
        target = bridge.global_opencode_plugin_path(home=self.home)
        bridge.install_bridge(self.vault, self.state, 'opencode', [self.root / 'Projects'], home=self.home)
        elsewhere = self.root / 'Elsewhere'
        elsewhere.mkdir()
        result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', OPENCODE_DRIVER,
                                 str(target), str(self.project), str(elsewhere)],
                                capture_output=True, text=True, encoding='utf-8', env=self.env,
                                cwd=self.root, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data['id'], 'beyin-v3-bridge')
        self.assertEqual(data['keys'], ['session.compaction', 'session.context', 'session.prompt'])
        self.assertIn('Beyin bridge active.', data['allowed'][0]['text'])  # bootstrap text of #500 (PR #505)
        self.assertIn('--harness opencode', data['allowed'][0]['text'])
        self.assertEqual(data['blocked'], [], 'Sessions outside the project roots get no bridge context')
        self.assertEqual(data['child'], [], 'Sub-agent sessions get no bridge context')

        hook.drain_queue(self.vault, self.state)
        done = [json.loads(p.read_text(encoding='utf-8')) for p in (self.state / 'hook-done').glob('*.json')]
        self.assertEqual({e['harness'] for e in done}, {'opencode'})
        self.assertEqual({e['project'] for e in done}, {self.project.name},
                         'The session directory names the project, not the service directory')
        self.assertEqual(sorted(e['event'] for e in done), ['SessionEnd', 'SessionStart', 'Stop'])

    @unittest.skipUnless(shutil.which('node'), 'node is required to execute the OpenCode plugin')
    def test_opencode_bridge_plugin_without_a_session_folder_sends_nothing(self):
        target = bridge.global_opencode_plugin_path(home=self.home)
        bridge.install_bridge(self.vault, self.state, 'opencode', [self.root / 'Projects'], home=self.home)
        # The service directory is inside a project root: falling back to it would bridge the session.
        result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', OPENCODE_NO_FOLDER_DRIVER,
                                 str(target)], capture_output=True, text=True, encoding='utf-8', env=self.env,
                                cwd=self.project, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]), {'lost-1': [], 'lost-2': []})
        self.assertEqual(self.queued(), [])
        self.assertFalse((self.state / 'hook-done').exists() and any((self.state / 'hook-done').iterdir()))

    @unittest.skipUnless(shutil.which('node'), 'node is required to execute the OpenCode plugin')
    def test_opencode_bridge_scope_changes_apply_to_an_imported_plugin(self):
        target = bridge.global_opencode_plugin_path(home=self.home)
        settings = bridge.global_opencode_settings_path(home=self.home)
        other = self.root / 'Other Projects' / 'İkinci'; other.mkdir(parents=True)
        bridge.install_bridge(self.vault, self.state, 'opencode', [self.root / 'Projects'], home=self.home)
        narrowed = self.root / 'narrowed.json'
        narrowed.write_text(bridge.opencode_settings_content(self.vault.resolve(), self.state.resolve(),
                                                             [other.resolve()], bridge.EVENTS, 1500), encoding='utf-8')
        result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', OPENCODE_RESCOPE_DRIVER,
                                 str(target), str(settings), str(narrowed), str(self.project), str(other)],
                                capture_output=True, text=True, encoding='utf-8', env=self.env,
                                cwd=self.root, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        # a: installed root; b: same root after narrowing; c: the new root; d: after the settings are removed.
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]), {'a': 1, 'b': 0, 'c': 1, 'd': 0})
        self.assertEqual(sorted(event['project'] for event in self.queued()), sorted([self.project.name, other.name]))


if __name__ == '__main__':
    unittest.main()
