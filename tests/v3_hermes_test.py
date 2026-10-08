"""Hermes harness contract: same adapter, same context, real plugin hook shapes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
MODULE = SCRIPTS / 'beyin_v3_hermes.py'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHermesContext:
    """Minimal stand-in for hermes-agent's plugin ctx: only ``register_hook``."""

    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, function):
        self.hooks.setdefault(name, []).append(function)


class HermesHarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='beyin-hermes-')
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name) / 'home'
        home.mkdir()
        environment = patch.dict(os.environ, inherited_env(HOME=str(home), USERPROFILE=str(home),
                                              BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1'), clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.vault = Path(self.temp.name) / 'Beyin Vault'
        self.state = Path(self.temp.name) / 'state'
        self.vault.mkdir()
        (self.vault / '🔮 850-Companion').mkdir()
        (self.vault / '🔮 850-Companion/Last-Session.md').write_text('# Son oturum\n\nHermes köprüsü kuruldu; makbuz bekleniyor.\n', encoding='utf-8')
        (self.vault / '🔮 850-Companion/Threads.md').write_text('# Threads\n\n## Active Threads\n- Hermes köprüsü\n\n## Closed Threads\n', encoding='utf-8')
        (self.vault / '🔮 850-Companion/Kurallar.md').write_text('# Kurallar\n- Kısa yaz.\n', encoding='utf-8')
        (self.vault / '🔮 850-Companion/Journal.md').write_text('# Journal\n\n## 2026-09-18\nİlk giriş.\n', encoding='utf-8')
        (self.vault / 'notes').mkdir()
        (self.vault / 'notes/klima.md').write_text('Klima kargo DHL yasak listesinde.\n', encoding='utf-8')
        install = load(ROOT / 'scripts/install_v3.py', 'test_hermes_install')
        install.install(self.vault, self.state)
        self.module = load(self.vault / '.claude/scripts/beyin_v3_hermes.py', 'test_hermes_module')
        self.cli('sync')  # like a real vault: sources indexed before the first client turn

    def cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *args], input=stdin,
                              capture_output=True, text=True, encoding='utf-8', cwd=self.vault)

    def test_installer_plans_plugin_shim_bound_to_this_vault(self):
        shim = self.vault / '.claude/hermes-plugin/__init__.py'
        manifest = self.vault / '.claude/hermes-plugin/plugin.yaml'
        self.assertTrue(shim.is_file() and manifest.is_file())
        self.assertIn(repr(str(self.vault.resolve())), shim.read_text(encoding='utf-8'))
        self.assertIn('pre_llm_call', manifest.read_text(encoding='utf-8'))
        self.assertIn('on_session_finalize', manifest.read_text(encoding='utf-8'))
        installed = json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))
        self.assertIn('.claude/hermes-plugin/__init__.py', installed['files'], 'shim must roll back with the package')

    def test_installed_shim_registers_hooks_like_hermes_would(self):
        shim = load(self.vault / '.claude/hermes-plugin/__init__.py', 'test_hermes_shim')
        ctx = FakeHermesContext()
        shim.register(ctx)
        self.assertEqual(sorted(ctx.hooks), ['on_session_finalize', 'pre_llm_call'])
        first = ctx.hooks['pre_llm_call'][0](session_id='shim-1', turn_id='t1', user_message='merhaba', is_first_turn=True)
        self.assertIn('data, not instructions', first['context'])

    def test_register_binds_hooks_and_first_turn_gets_companion_context(self):
        ctx = FakeHermesContext()
        self.module.register(ctx, vault=self.vault)
        self.assertEqual(sorted(ctx.hooks), ['on_session_finalize', 'pre_llm_call'])
        first = ctx.hooks['pre_llm_call'][0](session_id='hermes-abc', turn_id='t1', user_message='merhaba', is_first_turn=True)
        self.assertIsInstance(first, dict)
        text = first['context']
        self.assertIn('Receipt session=', text)
        self.assertIn('Last-Session.md', text)
        self.assertIn('Hermes köprüsü kuruldu', text)
        later = ctx.hooks['pre_llm_call'][0](session_id='hermes-abc', turn_id='t2', user_message='klima kargo DHL', is_first_turn=False)
        self.assertIn('notes/klima.md', later['context'])
        self.assertIsNone(ctx.hooks['on_session_finalize'][0](session_id='hermes-abc'))
        # BEYIN_V3_NO_SPAWN keeps the detached worker out of CI; drain explicitly like the
        # Codex/Claude lifecycle tests do, then check the acknowledged metadata.
        drain = subprocess.run([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'), '--vault', str(self.vault),
                                '--state', str(self.state), '--harness', 'hermes', '--drain-queue'], capture_output=True, text=True)
        self.assertEqual(drain.returncode, 0, drain.stderr)
        done = [json.loads(p.read_text(encoding='utf-8')) for p in (self.state / 'hook-done').glob('*.json')]
        self.assertEqual({e['harness'] for e in done}, {'hermes'})
        self.assertEqual({e['event'] for e in done}, {'SessionStart', 'UserPromptSubmit', 'SessionEnd'})
        for event in done:
            self.assertNotIn('prompt', event, 'Hook metadata must never persist transcript text')

    def test_hermes_context_equals_claude_context_for_same_query(self):
        hook = self.vault / '.claude/scripts/beyin_v3_hook.py'
        outputs = {}
        for harness in ('claude', 'hermes'):
            payload = json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': 'same', 'prompt': 'klima kargo'})
            result = subprocess.run([sys.executable, str(hook), '--vault', str(self.vault), '--state', str(self.state), '--harness', harness],
                                    input=payload, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs[harness] = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
        self.assertEqual(outputs['claude'], outputs['hermes'], 'Harness selection must not change retrieval semantics')

    def test_receipt_and_doctor_accept_hermes_harness(self):
        receipt = json.dumps({'event_id': 'hermes-receipt-1', 'summary': 'Hermes köprüsü doğrulandı.', 'refs': ['notes/klima.md']})
        result = self.cli('receipt', '--file', '-', '--harness', 'hermes', stdin=receipt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'succeeded')
        source = self.vault / json.loads(result.stdout)['source']
        self.assertIn('"harness": "hermes"', source.read_text(encoding='utf-8'))
        doctor = json.loads(self.cli('doctor').stdout)
        self.assertIn('hermes', doctor['lifecycle'])

    def test_missing_vault_or_no_memory_leaves_hermes_usable(self):
        ctx = FakeHermesContext()
        self.module.register(ctx, vault=Path(self.temp.name) / 'nowhere')
        self.assertEqual(ctx.hooks, {}, 'An absent vault must register nothing, never raise')
        hooks = self.module.make_hooks(self.vault, self.state)
        self.assertIsNone(hooks['pre_llm_call'](session_id='', is_first_turn=True))
        self.assertIsNone(hooks['pre_llm_call'](session_id=None, is_first_turn=True))
        self.assertIsNone(hooks['on_session_finalize'](session_id=''))
        self.assertEqual(self.module.run_hook(self.vault, self.state, {'hook_event_name': 'SessionStart', 'session_id': 'x'},
                                              python='/nonexistent/python'), '', 'A missing interpreter degrades to no context')

    def test_broken_runtime_file_registers_nothing_and_logs(self):
        runtime = self.vault / '.beyin-runtime.json'
        for broken in ('{not json', '[]', '{"state": ""}', '{"state": "relative/state"}', '{"schema": 1}'):
            runtime.write_text(broken, encoding='utf-8')
            ctx = FakeHermesContext()
            with self.assertLogs('test_hermes_module', level='WARNING') as logs:
                self.module.register(ctx, vault=self.vault)
            self.assertEqual(ctx.hooks, {}, broken)
            self.assertTrue(any('runtime' in line for line in logs.output), logs.output)
            for line in logs.output:
                self.assertNotIn(broken, line, 'Runtime contents must not be logged')

    def test_transport_failures_return_no_context_and_log(self):
        hooks = self.module.make_hooks(self.vault, self.state)
        cases = {
            'timeout': ('import time; time.sleep(5)', {'timeout': 0.2}),
            'nonzero': ('import sys; sys.exit(3)', {}),
            'invalid-json': ('print("not json")', {}),
        }
        for name, (body, extra) in cases.items():
            with self.subTest(name):
                fake = self.vault / f'fake-{name}.py'
                fake.write_text(body + '\n', encoding='utf-8')
                with patch.object(self.module, 'hook_command', return_value=[sys.executable, str(fake)]):
                    with self.assertLogs('test_hermes_module', level='WARNING') as logs:
                        text = self.module.run_hook(self.vault, self.state, {'hook_event_name': 'SessionStart', 'session_id': 'x'}, **extra)
                self.assertEqual(text, '')
                self.assertEqual(len(logs.output), 1, logs.output)
                self.assertNotIn('not json', logs.output[0], 'Raw child output must not be logged')
        with patch.object(self.module, 'run_hook', return_value='') as run:
            self.assertIsNone(hooks['pre_llm_call'](session_id='s', turn_id='1', user_message=['not', 'a', 'string'], is_first_turn=None))
            payload = run.call_args.args[2]
        self.assertEqual(payload['hook_event_name'], 'UserPromptSubmit', 'Unknown first-turn flag must not claim SessionStart')
        self.assertEqual(payload['prompt'], '', 'Non-string user input is forwarded as an empty prompt')

    def test_finalize_skips_receipt_tracking_on_unattended_platforms(self):
        # Cron and chat-gateway sessions never write receipts; queuing SessionEnd for them
        # would flag a "missing receipt" on every later Desktop start.
        hooks = self.module.make_hooks(self.vault, self.state)
        with patch.object(self.module, 'run_hook', return_value='') as run:
            hooks['pre_llm_call'](session_id='tg-1', turn_id='1', user_message='selam', is_first_turn=True, platform='telegram')
            hooks['on_session_finalize'](session_id='tg-1', platform='gateway')
            hooks['pre_llm_call'](session_id='cli-1', turn_id='1', user_message='selam', is_first_turn=True, platform='cli')
            hooks['on_session_finalize'](session_id='cli-1', platform='gateway')
            hooks['on_session_finalize'](session_id='never-seen')
        events = [(c.args[2]['session_id'], c.args[2]['hook_event_name']) for c in run.call_args_list]
        self.assertNotIn(('tg-1', 'SessionEnd'), events, 'Telegram session must not be receipt-tracked')
        self.assertIn(('cli-1', 'SessionEnd'), events, 'Interactive session keeps receipt tracking')
        self.assertIn(('never-seen', 'SessionEnd'), events, 'Unknown platform defaults to tracking')
        self.assertTrue(self.module.UNATTENDED_PLATFORMS >= {'telegram', 'cron'})

    def test_receipt_reminder_every_nth_user_turn(self):
        hooks = self.module.make_hooks(self.vault, self.state)
        seen = []
        with patch.object(self.module, 'run_hook', return_value='ctx'):
            for turn in range(1, self.module.REMINDER_EVERY * 2 + 1):
                result = hooks['pre_llm_call'](session_id='long', turn_id=str(turn), user_message='devam', is_first_turn=turn == 1)
                seen.append('[Hafıza]' in result['context'])
        reminders = [i + 1 for i, hit in enumerate(seen) if hit]
        self.assertEqual(reminders, [self.module.REMINDER_EVERY, self.module.REMINDER_EVERY * 2])
        with patch.object(self.module, 'run_hook', return_value=''):
            result = hooks['pre_llm_call'](session_id='long', turn_id='x', user_message='devam', is_first_turn=False)
        self.assertIsNone(result, 'No adapter context and no reminder due means no injection')

    def test_hermes_turn_counter_race_condition(self):
        import threading
        sys.setswitchinterval(1e-6)

        hooks = self.module.make_hooks(self.vault, self.state)
        with patch.object(self.module, 'run_hook', return_value=''):
            def worker():
                for _ in range(500):
                    hooks['pre_llm_call'](session_id='race', is_first_turn=False)

            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            closure = hooks['pre_llm_call'].__closure__
            sessions = None
            for c in closure:
                if isinstance(c.cell_contents, dict):
                    sessions = c.cell_contents
                    break
            self.assertEqual(sessions['race']['turns'], 5000, 'Turn counter lost updates due to race condition')


if __name__ == '__main__':
    unittest.main()
