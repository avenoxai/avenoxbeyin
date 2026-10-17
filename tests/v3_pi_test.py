"""Pi contract tests use synthetic vaults and the generated extension, never account state."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import ROOT, build_package, install, isolated_env

BUN = shutil.which('bun')

DRIVER = r'''
import { pathToFileURL } from "node:url"
const handlers = {}
const flags = JSON.parse(process.env.PI_FLAGS || "{}")
const registered = []
const pi = {
  on: (name, handler) => { handlers[name] = handler },
  registerFlag: (name) => registered.push(name),
  getFlag: (name) => flags[name],
}
const mod = await import(pathToFileURL(process.env.PLUGIN_PATH).href)
mod.default(pi)
const ctx = { cwd: process.env.PI_CWD, sessionManager: { getSessionId: () => process.env.PI_SESSION || "pi-1" } }
const result = { keys: Object.keys(handlers).sort(), flags: registered }
const steps = JSON.parse(process.env.PI_STEPS || "[]")
result.outputs = []
for (const step of steps) {
  const current = step.cwd ? { ...ctx, cwd: step.cwd } : ctx
  const value = await handlers[step.event]?.(step.payload || {}, current)
  result.outputs.push(value ?? null)
}
console.log(JSON.stringify(result))
'''

# Pi 1.3 loads every extension of one process against a shared event bus, gives a flag to the first
# path that registers it, answers getFlag only for the registering extension and exits at startup
# when a second extension path registers the same flag (resource-loader detectExtensionConflicts).
MULTI_DRIVER = r'''
import { EventEmitter } from "node:events"
import { pathToFileURL } from "node:url"
const bus = new EventEmitter()
const events = { emit: (channel, data) => { bus.emit(channel, data) },
  on: (channel, handler) => { bus.on(channel, handler); return () => bus.off(channel, handler) } }
const flags = JSON.parse(process.env.PI_FLAGS || "{}")
const owners = new Map()
const conflicts = []
const loaded = []
for (const path of JSON.parse(process.env.PLUGIN_PATHS)) {
  const handlers = {}
  const own = new Set()
  const pi = {
    on: (name, handler) => { handlers[name] = handler },
    registerFlag: (name) => {
      if (owners.has(name) && owners.get(name) !== path) conflicts.push(name)
      else owners.set(name, path)
      own.add(name)
    },
    getFlag: (name) => own.has(name) ? flags[name] : undefined,
    events,
  }
  const mod = await import(pathToFileURL(path).href)
  mod.default(pi)
  loaded.push(handlers)
}
const ctx = { cwd: process.env.PI_CWD, sessionManager: { getSessionId: () => "pi-multi" } }
const outputs = []
for (const step of JSON.parse(process.env.PI_STEPS || "[]")) {
  const values = []
  for (const handlers of loaded) values.push((await handlers[step.event]?.(step.payload || {}, ctx)) ?? null)
  outputs.push(values)
}
console.log(JSON.stringify({ conflicts, keys: loaded.map((handlers) => Object.keys(handlers).length), outputs }))
'''


class PiHarnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-pi-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.vault = self.base / 'Beyin Ölçüm & Vault'
        self.vault.mkdir()
        self.state = self.base / 'state'
        self.env = isolated_env(self.base / 'home')
        (self.vault / 'notes').mkdir()
        (self.vault / 'notes/klima.md').write_text('Klima kargo DHL yasak listesinde.\n', encoding='utf-8')
        (self.vault / 'notes/secret.md').write_text('---\nvisibility: private\n---\nKlima kargo DHL PRIVATE_PI_SENTINEL\n', encoding='utf-8')
        (self.vault / 'notes/untrusted.md').write_text('---\ntrust: untrusted\n---\nKlima kargo DHL UNTRUSTED_PI_SENTINEL\n', encoding='utf-8')
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.plugin = self.vault / '.pi/extensions/beyin-v3.ts'
        self.driver = self.base / 'driver.ts'
        self.driver.write_text(DRIVER, encoding='utf-8')
        self.assertEqual(self.cli('sync').returncode, 0)

    def cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *map(str, args)],
                              input=stdin, capture_output=True, text=True, encoding='utf-8',
                              cwd=self.vault, env=self.env, timeout=60)

    def hook(self, event, harness='pi', **extra):
        return subprocess.run([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'),
                               '--vault', str(self.vault), '--state', str(self.state), '--harness', harness],
                              input=json.dumps(dict(hook_event_name=event, session_id='pi-1', **extra)),
                              capture_output=True, text=True, encoding='utf-8', env=self.env, timeout=30)

    def drive(self, steps, cwd=None, flags=None, **env):
        result = subprocess.run([BUN, 'run', str(self.driver)],
                                env=dict(self.env, PLUGIN_PATH=str(self.plugin), PI_CWD=str(cwd or self.vault),
                                         PI_FLAGS=json.dumps(flags or {}), PI_STEPS=json.dumps(steps), **env),
                                capture_output=True, text=True, encoding='utf-8', timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def queued(self):
        return [json.loads(p.read_text(encoding='utf-8')) for p in (self.state / 'hook-queue').glob('*.json')]

    def test_installer_owns_extension_and_uninstall_removes_it(self):
        self.assertTrue(self.plugin.is_file())
        text = self.plugin.read_text(encoding='utf-8')
        self.assertIn(json.dumps(str(self.vault)), text)
        self.assertIn(json.dumps(sys.executable), text)
        self.assertNotIn(str(self.state), text)
        manifest = json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))
        self.assertIn('.pi/extensions/beyin-v3.ts', manifest['files'])
        self.assertIn('.claude/scripts/beyin_v3_pi.py', manifest['files'])
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), '--uninstall'], env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.plugin.exists())

    def test_pi_context_equals_claude_and_private_is_excluded(self):
        outputs = []
        for harness in ('claude', 'pi'):
            result = self.hook('UserPromptSubmit', harness=harness, prompt='klima kargo')
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(json.loads(result.stdout)['hookSpecificOutput']['additionalContext'])
        self.assertEqual(*outputs)
        self.assertIn('notes/klima.md', outputs[0])
        self.assertNotIn('PRIVATE_PI_SENTINEL', outputs[0])
        self.assertNotIn('UNTRUSTED_PI_SENTINEL', outputs[0])

    def test_no_memory_payload_is_inert_for_all_reference_clients(self):
        for harness in ('claude', 'codex', 'pi'):
            for event in ('SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop', 'PreCompact', 'SessionEnd'):
                with self.subTest(harness=harness, event=event):
                    result = self.hook(event, harness=harness, prompt='klima kargo', no_memory=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), {})
        self.assertEqual(self.queued(), [])
        self.assertFalse((self.state / 'receipt-reminders').exists())

    def test_companion_and_preferences_match_reference_hooks(self):
        companion = self.vault / '🔮 850-Companion'
        (companion / 'Core.md').write_text('# Core\nSynthetic public-user identity.\n', encoding='utf-8')
        self.assertEqual(self.cli('sync').returncode, 0)
        for profile in ('normal', 'economical', 'manual'):
            for mode in ('turn', 'session', 'off'):
                with self.subTest(profile=profile, mode=mode):
                    self.assertEqual(self.cli('preferences', '--profile', profile, '--context-mode', mode).returncode, 0)
                    for event in ('SessionStart', 'UserPromptSubmit'):
                        outputs = [json.loads(self.hook(event, harness=h, prompt='klima kargo').stdout)
                                   for h in ('claude', 'codex', 'pi')]
                        self.assertEqual(outputs[0], outputs[1])
                        self.assertEqual(outputs[0], outputs[2])

    def test_external_bridge_matches_reference_clients(self):
        project = self.base / 'Projects' / 'Public Project'
        project.mkdir(parents=True)
        outputs = []
        for harness in ('claude', 'codex', 'pi'):
            command = [sys.executable, str(self.vault / '.claude/scripts/beyin_v3_bridge.py'),
                       '--vault', str(self.vault), '--state', str(self.state), '--harness', harness,
                       '--project-root', str(project.parent)]
            result = subprocess.run(command, input=json.dumps(dict(hook_event_name='SessionStart',
                                    session_id='same-real-session', cwd=str(project))), env=self.env,
                                    capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            text = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
            outputs.append(text.replace('harness=' + harness, 'harness=CLIENT')
                           .replace('--harness ' + harness, '--harness CLIENT'))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])
        events = self.queued()
        self.assertEqual({e['harness'] for e in events}, {'claude', 'codex', 'pi'})
        self.assertEqual(len({e['session'] for e in events}), 1)
        self.assertEqual(len({e['project_id'] for e in events}), 1)

    def test_receipt_context_and_doctor_accept_pi(self):
        receipt = self.cli('receipt', '--harness', 'pi', '--event-id', 'pi-receipt', '--session', 'real-checkpoint',
                           '--summary', 'Pi contract checked.\nÖğrenilen: yok', '--ref', 'notes/klima.md')
        self.assertEqual(receipt.returncode, 0, receipt.stderr)
        source = self.vault / json.loads(receipt.stdout)['source']
        self.assertIn('"harness": "pi"', source.read_text(encoding='utf-8'))
        self.assertEqual(self.cli('context', 'klima', '--harness', 'pi').returncode, 0)
        self.assertIn('pi', json.loads(self.cli('doctor').stdout)['lifecycle'])

    def test_receipt_and_learning_reminders_use_shared_once_only_logic(self):
        self.hook('PostToolUse', tool_name='edit')
        first = json.loads(self.hook('Stop').stdout)
        session = hashlib.sha256(b'pi-1').hexdigest()[:24]
        self.assertEqual(first['decision'], 'block')
        self.assertIn('--harness pi --session ' + session, first['reason'])
        self.assertEqual(json.loads(self.hook('Stop').stdout), {})
        receipt = self.cli('receipt', '--harness', 'pi', '--session', session, '--event-id', 'pi-learning',
                           '--summary', 'Öğrenilen: sentetik test sonucu', '--ref', 'notes/klima.md')
        self.assertEqual(receipt.returncode, 0, receipt.stderr)
        learning = json.loads(self.hook('Stop').stdout)
        self.assertEqual(learning['decision'], 'block')
        self.assertIn('knowledge/', learning['reason'])
        self.assertEqual(json.loads(self.hook('Stop').stdout), {})

    def test_shell_cli_write_uses_real_pi_session_without_editor_event(self):
        self.env['PI_SESSION_ID'] = 'pi-1'
        payload = json.dumps({'source': 'notes/from-shell.md', 'text': 'Synthetic shell write.',
                              'metadata': {'kind': 'fact'}})
        created = self.cli('note-create', '--file', '-', stdin=payload)
        self.assertEqual(created.returncode, 0, created.stderr)
        reminder = json.loads(self.hook('Stop').stdout)
        self.assertEqual(reminder['decision'], 'block')
        self.assertIn('--harness pi', reminder['reason'])
        self.assertIn(hashlib.sha256(b'pi-1').hexdigest()[:24], reminder['reason'])

    def test_exclusions_reinclude_and_customized_extension(self):
        for excluded in ('adapters/pi', 'adapters'):
            result = self.cli('preferences', '--exclude-component', excluded)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = install(self.vault, self.state, self.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(self.plugin.exists())
            self.assertEqual(self.cli('preferences', '--include-component', excluded).returncode, 0)
            self.assertEqual(install(self.vault, self.state, self.env).returncode, 0)
            self.assertTrue(self.plugin.exists())
        custom = self.plugin.read_bytes() + b'// user change\n'
        self.plugin.write_bytes(custom)
        self.assertNotEqual(install(self.vault, self.state, self.env).returncode, 0)
        self.assertEqual(self.cli('preferences', '--exclude-component', 'adapters/pi').returncode, 0)
        self.assertEqual(install(self.vault, self.state, self.env).returncode, 0)
        self.assertEqual(self.plugin.read_bytes(), custom)

    def test_package_upgrade_and_rollback_preserve_extension(self):
        before = self.plugin.read_bytes()
        package = build_package(self.base / 'candidate.zip', '9.9.9', self.env)
        result = self.cli('update', '--package', package)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'updated')
        self.assertEqual(self.plugin.read_bytes(), before)
        self.assertEqual(self.cli('rollback').returncode, 0)
        self.assertEqual(self.plugin.read_bytes(), before)

    @unittest.skipUnless(BUN, 'bun is required to execute the generated Pi extension')
    def test_generated_extension_maps_lifecycle_and_reminder_continuation(self):
        steps = [dict(event='session_start'),
                 dict(event='before_agent_start', payload=dict(messageType='user', prompt='merhaba')),
                 dict(event='before_agent_start', payload=dict(messageType='user', prompt='klima kargo DHL')),
                 dict(event='tool_result', payload=dict(toolName='edit', input={'path': 'notes/klima.md'})),
                 dict(event='tool_result', payload=dict(toolName='read', input={})),
                 dict(event='tool_result', payload=dict(toolName='write', isError=True, input={})),
                 dict(event='session_before_compact'),
                 dict(event='agent_before_settle', payload=dict(outcome='completed', entries=[{'type': 'custom', 'customType': 'other'}],
                                                                context={'canContinue': True})),
                 dict(event='agent_before_settle', payload=dict(outcome='completed', entries=[], context={'canContinue': True})),
                 dict(event='session_shutdown')]
        result = self.drive(steps)
        self.assertEqual(result['keys'], sorted(['session_start', 'before_agent_start', 'tool_result',
                                                'agent_before_settle', 'session_before_compact', 'session_shutdown']))
        outputs = result['outputs']
        self.assertIn('Receipt session=', outputs[1]['message']['content'])
        self.assertIn('notes/klima.md', outputs[2]['message']['content'])
        self.assertFalse(outputs[1]['message']['display'])
        self.assertEqual(outputs[7]['entries'][0]['customType'], 'other')
        self.assertTrue(outputs[7]['continue'])
        self.assertIn('--harness pi', outputs[7]['entries'][-1]['content'])
        self.assertIsNone(outputs[8], 'The reminder must not create an infinite continuation loop')
        events = self.queued()
        self.assertEqual(sorted(e['event'] for e in events), sorted(['SessionStart', 'UserPromptSubmit', 'UserPromptSubmit',
                                                                  'PostToolUse', 'PreCompact', 'Stop', 'Stop', 'SessionEnd']))
        self.assertEqual({e['harness'] for e in events}, {'pi'})
        self.assertNotIn('klima kargo DHL', json.dumps(events))
        self.assertNotIn('PRIVATE_PI_SENTINEL', json.dumps(result))

    @unittest.skipUnless(BUN, 'bun is required to execute the generated Pi extension')
    def test_editor_hygiene_notice_reaches_model_without_losing_tool_output(self):
        note = self.vault / 'notes/long.md'
        note.write_text('kelime ' * 600, encoding='utf-8')
        (self.state / 'hygiene.json').write_text(json.dumps(dict(schema=1, word_cap_warning=True)), encoding='utf-8')
        content = [{'type': 'text', 'text': 'Original editor result'}]
        structured = {'changed': True}
        result = self.drive([dict(event='tool_result', payload=dict(toolName='write',
                                  input={'path': str(note)}, content=content, structuredContent=structured))])
        output = result['outputs'][0]
        self.assertIsNotNone(output, 'PostToolUse additionalContext must reach Pi, not be discarded')
        self.assertEqual(output['content'][0], content[0])
        self.assertIn('Bolum SINYALI', output['content'][-1]['text'])
        self.assertEqual(output['structuredContent'], structured)

    @unittest.skipUnless(BUN, 'bun is required to execute the generated Pi extension')
    def test_unrelated_second_user_install_is_self_contained(self):
        other = self.base / 'Another user vault'
        other.mkdir()
        state = self.base / 'another-state'
        env = isolated_env(self.base / 'another-home')
        installed = install(other, state, env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        (other / 'notes').mkdir()
        (other / 'notes/train.md').write_text('Tren takvimi Ankara pazartesi.\n', encoding='utf-8')
        sync = subprocess.run([sys.executable, str(other / 'beyin.py'), 'sync'], env=env, capture_output=True)
        self.assertEqual(sync.returncode, 0, sync.stderr)
        plugin = other / '.pi/extensions/beyin-v3.ts'
        source = plugin.read_text(encoding='utf-8')
        self.assertIn(json.dumps(str(other)), source)
        self.assertNotIn(str(self.vault), source)
        self.assertNotIn(str(self.state), source)
        result = subprocess.run([BUN, 'run', str(self.driver)], env=dict(env, PLUGIN_PATH=str(plugin),
                                PI_CWD=str(other), PI_STEPS=json.dumps([dict(event='session_start'),
                                dict(event='before_agent_start', payload=dict(messageType='user', prompt='tren takvimi Ankara'))])),
                                capture_output=True, text=True, encoding='utf-8', timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = json.loads(result.stdout)['outputs'][1]['message']['content']
        self.assertIn('notes/train.md', text)
        self.assertNotIn('notes/klima.md', text)

    @unittest.skipUnless(BUN, 'bun is required to execute the generated Pi extension')
    def test_scope_custom_runs_missing_session_and_opt_out_are_inert(self):
        steps = [dict(event='session_start'), dict(event='before_agent_start', payload=dict(messageType='user', prompt='klima')),
                 dict(event='tool_result', payload=dict(toolName='write', input={})), dict(event='session_shutdown')]
        outside = self.base / 'outside'; outside.mkdir()
        for cwd, flags, extra in [(outside, {}, {}), (self.vault, {'beyin-no-memory': True}, {}),
                                   (self.vault, {}, {'PI_SESSION': 'unknown'})]:
            result = self.drive(steps, cwd=cwd, flags=flags, **extra)
            self.assertTrue(all(o is None for o in result['outputs']))
        result = self.drive([dict(event='before_agent_start', payload=dict(messageType='custom', message={'content': 'klima'}))])
        self.assertEqual(result['outputs'], [None])
        self.assertEqual(self.queued(), [])

    def drive_many(self, plugins, steps, cwd, flags=None):
        driver = self.base / 'multi-driver.ts'
        driver.write_text(MULTI_DRIVER, encoding='utf-8')
        result = subprocess.run([BUN, 'run', str(driver)],
                                env=dict(self.env, PLUGIN_PATHS=json.dumps([str(p) for p in plugins]),
                                         PI_CWD=str(cwd), PI_FLAGS=json.dumps(flags or {}), PI_STEPS=json.dumps(steps)),
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    @unittest.skipUnless(BUN, 'bun is required to execute the generated Pi extension')
    def test_two_installed_vaults_in_one_pi_process_do_not_conflict(self):
        # A personal-settings or -e path to one vault plus the trusted vault in cwd: Pi must start,
        # and only the vault that owns cwd injects and queues.
        other = self.base / 'Diğer Kasa'
        other.mkdir()
        other_state = self.base / 'other-state'
        self.assertEqual(install(other, other_state, isolated_env(self.base / 'other-home')).returncode, 0)
        other_plugin = other / '.pi/extensions/beyin-v3.ts'
        steps = [dict(event='session_start'),
                 dict(event='before_agent_start', payload=dict(messageType='user', prompt='klima kargo DHL'))]
        for order in ([self.plugin, other_plugin], [other_plugin, self.plugin]):
            with self.subTest(first=order[0].parent.parent.parent.name):
                result = self.drive_many(order, steps, self.vault)
                self.assertEqual(result['conflicts'], [], 'Pi exits at startup on a duplicate flag')
                mine = order.index(self.plugin)
                prompt = result['outputs'][1]
                self.assertIn('notes/klima.md', prompt[mine]['message']['content'])
                self.assertIsNone(prompt[1 - mine], 'The other vault must stay inert in this vault')
        self.assertEqual({e['harness'] for e in self.queued()}, {'pi'})
        self.assertFalse((other_state / 'hook-queue').exists() and any((other_state / 'hook-queue').iterdir()))
        # The process-wide opt-out reaches the instance that did not register the flag.
        before = len(self.queued())
        for order in ([self.plugin, other_plugin], [other_plugin, self.plugin]):
            result = self.drive_many(order, steps, self.vault, flags={'beyin-no-memory': True})
            self.assertEqual(result['conflicts'], [])
            self.assertTrue(all(value is None for values in result['outputs'] for value in values))
        self.assertEqual(len(self.queued()), before)

    @unittest.skipUnless(BUN, 'bun is required to execute the generated Pi extension')
    def test_same_vault_loaded_through_two_paths_acts_once(self):
        alias = self.base / 'alias'
        try:
            alias.symlink_to(self.vault, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        steps = [dict(event='session_start'),
                 dict(event='before_agent_start', payload=dict(messageType='user', prompt='klima kargo DHL'))]
        result = self.drive_many([self.plugin, alias / '.pi/extensions/beyin-v3.ts'], steps, self.vault)
        self.assertEqual(result['conflicts'], [])
        self.assertEqual(sum(value is not None for value in result['outputs'][1]), 1, 'Context must not be injected twice')
        self.assertEqual(sorted(e['event'] for e in self.queued()), ['SessionStart', 'UserPromptSubmit'])

    @unittest.skipUnless(BUN, 'bun is required to execute the generated Pi extension')
    def test_failed_stop_preserves_reminder_until_completed_assistant_boundary(self):
        steps = [dict(event='tool_result', payload=dict(toolName='write', input={})),
                 dict(event='agent_before_settle', payload=dict(outcome='aborted', entries=[], context={'canContinue': True})),
                 dict(event='agent_before_settle', payload=dict(outcome='error', entries=[], context={'canContinue': True})),
                 dict(event='agent_before_settle', payload=dict(outcome='completed', entries=[], context={'canContinue': False}))]
        result = self.drive(steps)
        self.assertEqual(result['outputs'][:3], [None, None, None])
        self.assertTrue(result['outputs'][3]['continue'])
        self.assertEqual([e['event'] for e in self.queued()].count('Stop'), 3)

    @unittest.skipUnless(BUN, 'bun is required to execute the generated Pi extension')
    def test_external_bridge_requires_explicit_root_and_never_injects_companion(self):
        project = self.base / 'Projects' / 'Example'; project.mkdir(parents=True)
        steps = [dict(event='session_start'), dict(event='before_agent_start', payload=dict(messageType='user', prompt='klima')),
                 dict(event='tool_result', payload=dict(toolName='write', input={})), dict(event='session_shutdown')]
        flags = {'beyin-project-root': str(project.parent)}
        result = self.drive(steps, cwd=project, flags=flags)
        text = result['outputs'][1]['message']['content']
        self.assertIn('Optional Beyin bridge.', text)
        self.assertIn('harness=pi', text)
        self.assertNotIn('notes/klima.md', text)
        self.assertNotIn('PRIVATE_PI_SENTINEL', text)
        events = self.queued()
        self.assertEqual(sorted(e['event'] for e in events), ['SessionEnd', 'SessionStart'])
        self.assertEqual({e['project'] for e in events}, {'Example'})
        expected = hashlib.sha256(json.dumps([str(project), 'pi-1']).encode()).hexdigest()[:24]
        self.assertEqual({e['session'] for e in events}, {expected})
        self.assertIn('Receipt session=' + expected, text)
        other_vault = project / 'other'; other_vault.mkdir()
        (other_vault / '.beyin-runtime.json').write_text('{}')
        result = self.drive(steps, cwd=other_vault, flags=flags)
        self.assertTrue(all(o is None for o in result['outputs']))
        self.assertEqual(len(self.queued()), 2, 'Another installed vault owns its events')

    @unittest.skipUnless(BUN, 'bun is required to execute the generated Pi extension')
    def test_external_bridge_supports_reference_roots_budget_and_event_controls(self):
        roots = [self.base / 'First Projects', self.base / 'Second Projects']
        for root in roots:
            root.mkdir()
        project = roots[1] / 'Example'
        project.mkdir()
        steps = [dict(event='session_start'),
                 dict(event='before_agent_start', payload=dict(messageType='user', prompt='klima')),
                 dict(event='session_before_compact'),
                 dict(event='agent_before_settle', payload=dict(outcome='completed', entries=[], context={'canContinue': True})),
                 dict(event='session_shutdown')]
        flags = {'beyin-project-roots': json.dumps(list(map(str, roots))),
                 'beyin-bridge-context-chars': '0', 'beyin-bridge-events': 'SessionStart,SessionEnd'}
        result = self.drive(steps, cwd=project, flags=flags)
        self.assertTrue(all(o is None for o in result['outputs']))
        self.assertEqual(sorted(e['event'] for e in self.queued()), ['SessionEnd', 'SessionStart'])
        flags['beyin-bridge-events'] = 'SessionEnd'
        self.drive(steps, cwd=project, flags=flags)
        self.assertEqual(len(self.queued()), 3)
        for invalid in ({'beyin-project-roots': '["relative"]'}, {'beyin-project-roots': 'not-json'},
                        {'beyin-bridge-context-chars': '4001'}, {'beyin-bridge-context-chars': '-1'},
                        {'beyin-bridge-events': 'UserPromptSubmit'}, {'beyin-bridge-events': ''}):
            result = self.drive(steps, cwd=project, flags={**flags, **invalid})
            self.assertTrue(all(o is None for o in result['outputs']))
        self.assertEqual(len(self.queued()), 3, 'Invalid bridge configuration must not broaden access')

    @unittest.skipUnless(BUN and os.environ.get('PI_TEST_PACKAGE'), 'set PI_TEST_PACKAGE to an installed Pi 1.3 package for the offline host check')
    def test_actual_pi_loader_and_boundary_runner(self):
        result = subprocess.run([BUN, 'run', str(ROOT / 'tests/fixtures/v3/pi-runner.ts')],
                                env=dict(self.env, PI_PACKAGE=os.environ['PI_TEST_PACKAGE'], TEST_VAULT=str(self.vault)),
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['extension_errors'], 0)

    @unittest.skipUnless(BUN, 'bun is required to execute the generated Pi extension')
    def test_transport_rejects_malformed_output_nonzero_exit_and_timeout(self):
        steps = [dict(event='before_agent_start', payload=dict(messageType='user', prompt='klima'))]
        hook = self.vault / '.claude/scripts/beyin_v3_hook.py'
        for stub in ("print('not-json')", 'raise SystemExit(1)', 'import time; time.sleep(60)'):
            hook.write_text(stub + '\n', encoding='utf-8')
            self.assertEqual(self.drive(steps)['outputs'], [None])

    @unittest.skipUnless(BUN, 'bun is required to execute the generated Pi extension')
    def test_alias_and_broken_transport(self):
        steps = [dict(event='session_start'), dict(event='before_agent_start', payload=dict(messageType='user', prompt='klima'))]
        alias = self.base / 'alias'
        try:
            alias.symlink_to(self.vault, target_is_directory=True)
        except OSError:
            pass  # Windows without symlink permission still exercises transport failures.
        else:
            result = self.drive(steps, cwd=alias)
            self.assertIn('Receipt session=', result['outputs'][1]['message']['content'])
        result = self.drive(steps, BEYIN_PYTHON=str(self.base / 'missing-python'))
        self.assertEqual(result['outputs'], [None, None])
        locator = self.vault / '.beyin-runtime.json'
        for broken in ('not json', '[]', '{"state":"relative"}'):
            locator.write_text(broken, encoding='utf-8')
            self.assertEqual(self.drive(steps)['keys'], [])


if __name__ == '__main__':
    unittest.main()
