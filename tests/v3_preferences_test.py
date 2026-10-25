"""Offline preference and hook behavior contracts; synthetic vaults only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_preferences as prefs


class PreferencesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'Örnek Vault'; self.vault.mkdir()
        self.state = self.root / 'state'

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
            '--vault', str(self.vault), '--state', str(self.state), *args],
            capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def hook(self, event, harness='codex', extra=()):
        payload = {'hook_event_name': event, 'session_id': 'synthetic', 'event_id': event,
                   'conversationId': 'synthetic', 'invocationNum': 0, 'fullyIdle': True}
        r = subprocess.run([sys.executable, str(ROOT / 'template/.claude/scripts/beyin_v3_hook.py'),
            '--vault', str(self.vault), '--state', str(self.state), '--harness', harness, *extra],
            input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8',
            env=inherited_env(BEYIN_V3_NO_SPAWN='1'), timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_default_and_custom_roundtrip(self):
        self.assertEqual(prefs.read(self.vault), prefs.PROFILES['normal'])
        self.cli('preferences', '--profile', 'economical', '--interval-minutes', '30')
        result = self.cli('preferences')
        self.assertEqual(result['preferences']['interval_minutes'], 30)
        self.assertEqual(result['preferences']['context_chars'], 2000)
        self.assertFalse(result['model_calls'])
        self.assertFalse(result['timer_installed'])

    def test_secret_filter_is_explicit_and_survives_profile_changes(self):
        self.assertFalse(self.cli('preferences')['preferences']['secret_filter'])
        self.assertTrue(self.cli('preferences', '--secret-filter', 'on')['preferences']['secret_filter'])
        self.assertTrue(self.cli('preferences', '--profile', 'economical')['preferences']['secret_filter'])
        self.assertFalse(self.cli('preferences', '--secret-filter', 'off')['preferences']['secret_filter'])

    def test_daily_layout_is_vault_owned_independent_and_survives_profiles(self):
        self.assertEqual(self.cli('preferences')['daily_layout'], 'v3')
        self.assertFalse((self.vault / '.beyin-daily-layout.json').exists())
        self.cli('preferences', '--profile', 'normal')
        before = (self.vault / '.beyin-preferences.json').read_bytes()
        self.assertEqual(self.cli('preferences', '--daily-layout', 'flat')['daily_layout'], 'flat')
        self.assertEqual((self.vault / '.beyin-preferences.json').read_bytes(), before)
        self.assertEqual(self.cli('preferences', '--profile', 'manual')['daily_layout'], 'flat')
        self.assertEqual(prefs.read_daily_layout(self.vault), 'flat')
        stored = json.loads((self.vault / '.beyin-daily-layout.json').read_text())
        self.assertEqual(stored, {'schema': 1, 'daily_layout': 'flat'})
        self.assertEqual(self.cli('preferences', '--daily-layout', 'v3')['daily_layout'], 'v3')

    def test_invalid_daily_layout_does_not_save_any_settings(self):
        prefs.save_daily_layout(self.vault, 'flat')
        path = self.vault / '.beyin-daily-layout.json'
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            prefs.save_daily_layout(self.vault, 'unknown')
        self.assertEqual(path.read_bytes(), before)
        for raw in ('{broken', '{"schema": 1, "daily_layout": "unknown"}',
                    '{"schema": true, "daily_layout": "flat"}', '[]'):
            path.write_text(raw, encoding='utf-8')
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
                                     '--vault', str(self.vault), '--state', str(self.state),
                                     'preferences', '--daily-layout', 'v3', '--profile', 'manual'],
                                    capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_text(encoding='utf-8'), raw)
            self.assertFalse((self.vault / '.beyin-preferences.json').exists())

    def test_human_preferences_shows_active_daily_layout(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import beyin_entry
        for layout, expected in (('flat', 'daily/YYYY-MM-DD.md'), ('v3', 'daily/v3/YYYY-MM-DD.md')):
            result = self.cli('preferences', '--daily-layout', layout)
            self.assertIn('Daily duzeni: ' + layout + ' (' + expected + ')',
                          beyin_entry.human_result(result, 'preferences'))

    def test_invalid_settings_do_not_replace_user_preferences(self):
        prefs.save(self.vault, {}, 'economical')
        before = (self.vault / '.beyin-preferences.json').read_bytes()
        for changes in ({'interval_minutes': -1}, {'interval_minutes': True}, {'context_chars': 999},
                        {'context_chars': 12001}, {'context_mode': 'unknown'}, {'unknown': 1},
                        {'auto_sync': 'false'}, {'secret_filter': 'true'}):
            with self.assertRaises(ValueError):
                prefs.save(self.vault, changes)
        self.assertEqual(before, (self.vault / '.beyin-preferences.json').read_bytes())

    def test_larger_opening_budget_is_machine_local_and_rollback_safe(self):
        # #140: the opening may use up to 24000 characters, but the vault preference range
        # stays 1000..12000. An older release validates .beyin-preferences.json and would
        # reject a wider value in every hook, in doctor and in preferences itself.
        directory = self.vault / '🔮 850-Companion'
        directory.mkdir()
        (directory / 'Core.md').write_text('# Kimlik\nIDENTITY_CANARY\n', encoding='utf-8')
        threads = ('# Threads\n## Active Threads\n' +
                   ''.join(f'- Konu {i}: açık iş maddesi ve bir sonraki adım burada duruyor.\n' for i in range(220)) +
                   '- THREADS_TAIL_CANARY\n## Closed Threads\n')
        self.assertGreater(len(threads), 13000)
        (directory / 'Threads.md').write_text(threads, encoding='utf-8')
        self.cli('sync')
        self.cli('preferences', '--context-mode', 'session', '--context-chars', '12000')
        short = self.hook('SessionStart')['hookSpecificOutput']['additionalContext']
        self.assertLessEqual(len(short), 12000)
        self.assertNotIn('THREADS_TAIL_CANARY', short)
        saved = self.cli('preferences', '--companion-context-chars', '24000')
        self.assertEqual(saved['companion_context'], {'context_chars': 24000})
        self.assertEqual(json.loads((self.state / 'companion-context.json').read_text(encoding='utf-8')),
                         {'schema': 1, 'context_chars': 24000})
        stored = json.loads((self.vault / '.beyin-preferences.json').read_text(encoding='utf-8'))
        self.assertEqual(set(stored), set(prefs.PROFILES['normal']), 'no new vault preference field')
        self.assertEqual(stored['context_chars'], 12000, 'vault range unchanged for older releases')
        # A client without a known cut-off receives the larger opening; Codex stays below its own (#175).
        text = self.hook('SessionStart', harness='opencode')['hookSpecificOutput']['additionalContext']
        self.assertGreater(len(text), 12000)
        self.assertLessEqual(len(text), 24000)
        self.assertIn('THREADS_TAIL_CANARY', text)
        self.assertLessEqual(len(self.hook('SessionStart')['hookSpecificOutput']['additionalContext'].encode('utf-8')), 10000)
        before = (self.state / 'companion-context.json').read_bytes()
        for value in ('999', '24001'):
            r = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                '--state', str(self.state), 'preferences', '--context-chars', '3000',
                                '--companion-context-chars', value],
                               capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.state / 'companion-context.json').read_bytes(), before)
        self.assertEqual(json.loads((self.vault / '.beyin-preferences.json').read_text(encoding='utf-8'))['context_chars'],
                         12000, 'a rejected opening budget saves nothing else')
        # A damaged file falls back to context_chars; the opening is never lost.
        (self.state / 'companion-context.json').write_text('{broken', encoding='utf-8')
        fallback = self.hook('SessionStart')['hookSpecificOutput']['additionalContext']
        self.assertLessEqual(len(fallback), 12000)
        self.assertIn('IDENTITY_CANARY', fallback)
        self.assertIn('companion_context_notice', self.cli('preferences'))

    def test_claude_context_stays_below_its_file_cutoff(self):
        # #175: Claude Code files an additionalContext over 10,000 characters away and shows only
        # a ~2,000 character preview, so a 24000 opening hid Last-Session, Threads and Journal.
        directory = self.vault / '🔮 850-Companion'
        directory.mkdir()
        def fill(head, line, size):
            return head + line * ((size - len(head)) // len(line))
        bodies = {'Core.md': fill('# Kimlik\nMARK-CORE\n', 'Kısa ve somut konuşuruz.\n', 900),
                  'Kurallar.md': fill('# Kurallar\nMARK-KURALLAR\n', '- Önce sonucu söyle, sonra kaynağı.\n', 2700),
                  'Last-Session.md': fill('# Son oturum\n## 2026-09-30 18:30 · Deneme\nMARK-LAST\n', 'Etiket taslağı bitti; ambalaj ölçüsü açık.\n', 2600),
                  'Threads.md': fill('# Threads\n## Active Threads\nMARK-THREADS\n', 'Sonraki adım ölçüm; açık soru süre ve malzeme payı.\n', 6000),
                  'Journal.md': fill('# Journal\n## 2026-09-30 18:00 Gözlem\nMARK-JOURNAL\n', 'Küçük partide takip çizelgesi işe yaradı.\n', 6000)}
        for name, body in bodies.items():
            (directory / name).write_text(body, encoding='utf-8')
        (self.vault / 'Notlar').mkdir()
        (self.vault / 'Notlar/Ambalaj.md').write_text('# Ambalaj\n' + 'ambalaj ölçüsü etiket taslağı kutu. ' * 300, encoding='utf-8')
        self.cli('sync')
        self.cli('preferences', '--context-mode', 'turn', '--context-chars', '12000')
        saved = self.cli('preferences', '--companion-context-chars', '24000')
        self.assertIn('9500', saved['client_context_notice'])
        def context(harness, event, prompt=None):
            payload = {'hook_event_name': event, 'session_id': 'synthetic-' + harness, 'event_id': event + harness}
            if prompt:
                payload['prompt'] = prompt
            r = subprocess.run([sys.executable, str(ROOT / 'template/.claude/scripts/beyin_v3_hook.py'),
                                '--vault', str(self.vault), '--state', str(self.state), '--harness', harness],
                               input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8',
                               env=inherited_env(BEYIN_V3_NO_SPAWN='1'), timeout=20)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)['hookSpecificOutput']['additionalContext']
        for event, prompt in (('SessionStart', None), ('UserPromptSubmit', 'nerede kalmıştık')):
            # Claude Code measures JavaScript string length (the folder emoji counts twice);
            # Codex measures UTF-8 bytes (2,500 tokens of 4 bytes), where ş and ı take two.
            for harness, measure in (('claude', lambda t: len(t.encode('utf-16-le')) // 2),
                                     ('codex', lambda t: len(t.encode('utf-8')))):
                with self.subTest(event=event, prompt=prompt, harness=harness):
                    text = context(harness, event, prompt)
                    self.assertLessEqual(measure(text), 10000)
                    self.assertGreater(measure(text), 9000, 'the room under the cut-off is still used')
                    for mark in ('CORE', 'KURALLAR', 'LAST', 'THREADS', 'JOURNAL'):
                        self.assertIn('MARK-' + mark, text)
            # Other clients keep the budget the user chose.
            self.assertGreater(len(context('opencode', event, prompt)), 10000)
        # An ordinary turn uses the same ceiling (context_chars may be 12000).
        text = context('claude', 'UserPromptSubmit', 'ambalaj ölçüsü etiket taslağı')
        self.assertIn('Notlar/Ambalaj.md', text)
        self.assertLessEqual(len(text), 9500)
        import beyin_v3_companion as companion
        self.assertEqual(companion.client_budget('claude', 24000), 9500)
        self.assertEqual(companion.client_budget('codex', 24000), 9500)
        self.assertEqual(companion.client_budget('codex', 5000), 5000)
        for harness in ('antigravity', 'hermes', 'opencode', 'omp'):
            self.assertEqual(companion.client_budget(harness, 24000), 24000)
            self.assertEqual(companion.fit_client(harness, '🔮' * 9500), '🔮' * 9500)
        astral = companion.fit_client('claude', 'a' * 9000 + '🔮' * 600)
        self.assertIn(len(astral.encode('utf-16-le')) // 2, (9999, 10000))
        self.assertTrue(astral.startswith('a' * 9000))
        for tail in ('🔮' * 600, 'ş' * 1200, 'ı' * 501):
            fitted = companion.fit_client('codex', 'a' * 9000 + tail)
            self.assertIn(len(fitted.encode('utf-8')), range(9997, 10001))
            self.assertTrue(fitted.startswith('a' * 9000))
        self.assertEqual(companion.fit_client('codex', 'ş' * 5000), 'ş' * 5000)
        self.assertNotIn('client_context_notice', self.cli('preferences', '--context-chars', '5000',
                                                           '--companion-context-chars', '9000'))

    def test_manual_hooks_do_not_enqueue_or_inject_across_clients(self):
        prefs.save(self.vault, {}, 'manual')
        for harness in ('codex', 'claude', 'antigravity'):
            event = 'PreInvocation' if harness == 'antigravity' else 'SessionStart'
            result = self.hook(event, harness)
            self.assertNotIn('hookSpecificOutput', result)
            self.assertNotIn('injectSteps', result)
            self.hook('Stop', harness)
        self.assertEqual(list((self.state / 'hook-queue').glob('*.json')), [])

    def test_economical_injects_only_start_and_caps_entire_context(self):
        prefs.save(self.vault, {}, 'economical')
        self.assertEqual(self.hook('UserPromptSubmit'), {})
        result = self.hook('SessionStart')
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn('Receipt session=', text)
        self.assertLessEqual(len(text), 2000)

    def test_interval_atomic_and_new_session_or_failure_refreshes(self):
        settings = prefs.PROFILES['economical']
        self.assertTrue(prefs.claim_check(self.state, settings, 'Stop', now=1000))
        self.assertFalse(prefs.claim_check(self.state, settings, 'Stop', now=1899))
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed = list(pool.map(lambda _: prefs.claim_check(self.state, settings, 'Stop', now=1900), range(4)))
        self.assertEqual(sum(claimed), 1)
        self.assertTrue(prefs.claim_check(self.state, settings, 'SessionStart', now=1901))
        (self.state / 'hook-error.json').write_text('{}')
        self.assertTrue(prefs.claim_check(self.state, settings, 'Stop', now=1902))
        self.assertFalse(prefs.claim_check(self.state, prefs.PROFILES['manual'], 'SessionStart', now=2900))

    def test_manual_explicit_context_still_refreshes_current_source(self):
        prefs.save(self.vault, {}, 'manual')
        (self.vault / 'choice.md').write_text('Synthetic project format CSV.', encoding='utf-8')
        self.cli('sync')
        (self.vault / 'choice.md').write_text('Synthetic project format JSON.', encoding='utf-8')
        result = self.cli('context', 'Synthetic project format')
        self.assertIn('JSON', json.dumps(result))
        self.assertNotIn('CSV', json.dumps(result))

    def test_started_worker_respects_disable_but_explicit_drain_is_available(self):
        self.hook('Stop')
        prefs.save(self.vault, {}, 'manual')
        self.assertTrue(self.hook('Stop', extra=('--worker',))['paused'])
        self.assertEqual(len(list((self.state / 'hook-queue').glob('*.json'))), 1)
        self.assertEqual(self.hook('Stop', extra=('--drain-queue',))['pending'], 0)

    def test_install_preserves_preferences_and_ships_working_command(self):
        prefs.save(self.vault, {}, 'economical')
        prefs.save_daily_layout(self.vault, 'flat')
        layout_before = (self.vault / '.beyin-daily-layout.json').read_bytes()
        before = (self.vault / '.beyin-preferences.json').read_bytes()
        r = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault',
                            str(self.vault), '--state', str(self.state)], capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(before, (self.vault / '.beyin-preferences.json').read_bytes())
        self.assertEqual(layout_before, (self.vault / '.beyin-daily-layout.json').read_bytes())
        installed = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'preferences',
                                    '--daily-layout', 'v3', '--json'], capture_output=True,
                                   text=True, encoding='utf-8', timeout=20)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(json.loads(installed.stdout)['daily_layout'], 'v3')
        self.assertEqual(before, (self.vault / '.beyin-preferences.json').read_bytes())
        r = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'preferences', '--human'],
                           capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('2000 karakter', r.stdout)
        self.assertIn('preferences', self.cli('doctor'))

    def test_project_context_preference_is_machine_local_and_rollback_safe(self):
        self.assertEqual(self.cli('preferences')['project_context'], 'off')
        self.assertFalse((self.vault / '.beyin-preferences.json').exists())
        saved = self.cli('preferences', '--project-context', 'on')
        self.assertEqual(saved['status'], 'saved')
        self.assertEqual(saved['project_context'], 'on')
        self.assertFalse((self.vault / '.beyin-preferences.json').exists(), 'must not modify vault preference schema')
        self.assertTrue((self.state / 'project-context.json').exists())
        self.cli('preferences', '--project-context', 'off')
        self.assertEqual(self.cli('preferences')['project_context'], 'off')

    def test_context_dedupe_preference_is_machine_local_and_rollback_safe(self):
        self.assertEqual(self.cli('preferences')['context_dedupe'], 'off')
        self.assertFalse((self.vault / '.beyin-preferences.json').exists())
        saved = self.cli('preferences', '--context-dedupe', 'on')
        self.assertEqual(saved['status'], 'saved')
        self.assertEqual(saved['context_dedupe'], 'on')
        self.assertFalse((self.vault / '.beyin-preferences.json').exists(), 'must not modify vault preference schema')
        self.assertTrue((self.state / 'context-dedupe.json').exists())
        self.cli('preferences', '--context-dedupe', 'off')
        self.assertEqual(self.cli('preferences')['context_dedupe'], 'off')

    def test_hygiene_opt_ins_are_machine_local_and_rollback_safe(self):
        # V3.5.1 and older validate() reject any other .beyin-preferences.json key, so a
        # rollback after enabling hygiene must find only these keys (maintainer change).
        released_keys = {'auto_sync', 'interval_minutes', 'context_mode', 'context_chars', 'secret_filter'}
        current = self.cli('preferences')
        self.assertEqual(current['hygiene'], {'word_cap_warning': False, 'max_words': 500,
                                              'folder_questions': False, 'promotion': False})
        saved = self.cli('preferences', '--word-cap-warning', 'on', '--max-words', '800',
                         '--folder-questions', 'on', '--promotion', 'on')
        self.assertEqual(saved['status'], 'saved')
        self.assertEqual(saved['hygiene'], {'word_cap_warning': True, 'max_words': 800,
                                            'folder_questions': True, 'promotion': True})
        self.assertFalse((self.vault / '.beyin-preferences.json').exists(), 'must not modify vault preference schema')
        self.cli('preferences', '--profile', 'economical', '--context-chars', '3000')
        self.assertEqual(set(json.loads((self.vault / '.beyin-preferences.json').read_text(encoding='utf-8'))), released_keys)
        self.assertTrue(self.cli('preferences')['hygiene']['word_cap_warning'], 'a profile switch leaves hygiene alone')
        before = (self.state / 'hygiene.json').read_bytes()
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'preferences', '--max-words', '5', '--promotion', 'off'],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, (self.state / 'hygiene.json').read_bytes(), 'an invalid value changes nothing')
        (self.state / 'hygiene.json').write_text('{"word_cap_warning": "yes"}', encoding='utf-8')
        damaged = self.cli('preferences')
        self.assertFalse(damaged['hygiene']['word_cap_warning'])
        self.assertIn('hygiene_notice', damaged)
        self.assertNotIn('Soru sirasi', json.dumps(self.hook('SessionStart', 'claude')))


if __name__ == '__main__':
    unittest.main()
