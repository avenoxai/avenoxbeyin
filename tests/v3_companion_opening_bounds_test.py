"""The priority opening (#528) stays inside every client cap (#175) at large source sizes.

Real SessionStart hook runs over synthetic vaults: huge rule files, hundreds of open topics
and astral characters. The client guard must never be the thing that cuts, every cut must
carry an explicit marker, and two runs over the same sources must be identical.
"""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_companion as companion
import beyin_v3_hook as hook

DIRECTORY = '🔮 850-Companion'


class OpeningBoundsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='opening-bounds-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        (self.vault / DIRECTORY).mkdir(parents=True)
        self.store = runtime.MemoryStore(root / 'state', self.vault)
        companion.save_context(self.store.state_dir, 9500)

    def write(self, name, text):
        source = f'{DIRECTORY}/{name}'
        (self.vault / source).write_text(text, encoding='utf-8')
        self.store.ingest(dict(id=source, source=source, text=text, kind='note', status='active',
                               updated_at='2026-11-01T00:00:00Z'))

    def seed(self, rules=30, topics=12, astral=False, history=30):
        mark = '😀' if astral else 'ş'
        self.write('Kurallar.md', '# Kurallar\n' + '\n'.join(
            f'- **kural:** KURAL_{i:04d} Şifre, Müşteri ve Arşiv ayrıntısını koru {mark * 3}. '
            f'**neden:** ' + 'Gerekçe ayrıntısı. ' * 4 for i in range(rules)) + '\n')
        self.write('Core.md', '# Kimlik\n' + f'Kimlik ayrıntısı {mark}. ' * 200)
        self.write('Threads.md', '## Açık konular\n' + '\n\n'.join(
            f'### Konu {i:03d} {mark}\n**Durum:** DURUM_{i:03d} hazır {mark}.\n'
            f'**Sonraki adım:** ADIM_{i:03d} çalıştır.\n\n' + f'Geçmiş ayrıntı {mark}. ' * history
            for i in range(topics)) + '\n## Kapanan konular\nKAPALI_CANARY\n')
        self.write('Last-Session.md', '# Son oturum\n## 2026-11-01 10:00 · a · 11111111\nKART_A '
                   + f'devir {mark}. ' * 120 + '\n## 2026-10-31 09:00 · b · 22222222\nKART_B ' + 'devir. ' * 300 + '\n')
        self.write('Journal.md', '# Journal\n## 2026-11-01\n' + f'Gözlem {mark}. ' * 200)

    def run_hook(self, harness):
        renders = []
        original_context, original_guard = companion.context, companion.fit_client

        def counted(*args, **kwargs):
            renders.append(args[1])
            return original_context(*args, **kwargs)

        def guard(client, text):
            self.assertEqual(original_guard(client, text), text, 'the client guard must not cut the opening')
            return text

        payload = dict(hook_event_name='SessionStart', session_id='bounds-' + harness, cwd=str(self.vault))
        output = io.StringIO()
        argv = ['hook', '--vault', str(self.vault), '--state', str(self.store.state_dir), '--harness', harness]
        with patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                patch.object(sys, 'stdout', output), patch.object(companion, 'context', side_effect=counted), \
                patch.object(companion, 'fit_client', side_effect=guard), \
                patch.dict(os.environ, inherited_env(BEYIN_V3_NO_SPAWN='1', BEYIN_UPDATES_OFF='1'), clear=True):
            hook.main()
        self.assertLessEqual(len(renders), 2 + companion.client_budget(harness, 9500).bit_length())
        text = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
        cap = companion.CLIENT_TEXT_LIMITS[harness][0]
        self.assertLessEqual(companion.client_size(harness, text), cap)
        self.assertLessEqual(len(text), 9500)
        return text

    def check(self, rules, topics, astral=False, history=30):
        """(harness, text, rules missing, topics missing) for Claude Code and Codex."""
        self.seed(rules=rules, topics=topics, astral=astral, history=history)
        results = []
        for harness in ('claude', 'codex'):
            text = self.run_hook(harness)
            self.assertEqual(text, self.run_hook(harness), 'the same sources render the same opening')
            self.assertIn('KURAL_0000', text)
            self.assertIn('KART_A', text)
            self.assertNotIn('KAPALI_CANARY', text)
            self.assertIn('ADIM_000', text)
            missing_rules = [i for i in range(rules) if f'KURAL_{i:04d}' not in text]
            if missing_rules:
                self.assertRegex(text, r'\[truncated: (rules \d+-\d+ \(\d+ of \d+\)|[^\]]*) omitted here; read source\]')
            missing_topics = [i for i in range(topics) if f'ADIM_{i:03d}' not in text]
            if missing_topics:
                self.assertIn('read source', text[text.index(f'[{DIRECTORY}/Threads.md]'):])
            results.append((harness, text, missing_rules, missing_topics))
        return results

    def test_typical_vault_keeps_every_rule_and_topic(self):
        for _, _, missing_rules, missing_topics in self.check(rules=30, topics=12):
            self.assertEqual(missing_rules, [])
            self.assertEqual(missing_topics, [])

    def test_huge_rule_file_is_cut_at_rule_boundaries_with_a_marker(self):
        for _, text, missing_rules, _ in self.check(rules=1500, topics=12):
            self.assertTrue(missing_rules)
            self.assertIn('KURAL_1499', text)  # both ends of the rule file stay visible
            self.assertIn('Memory hygiene: Kurallar.md opens clipped', text)

    def test_hundreds_of_topics_share_room_and_say_so(self):
        for _, text, missing_rules, missing_topics in self.check(rules=30, topics=300, history=6):
            self.assertEqual(missing_rules, [])
            self.assertTrue(missing_topics)
            # 300 headings would leave room for a handful of status lines: whole topics in
            # source order come first instead, far more than the plain head clip showed.
            shown = [i for i in range(300) if f'ADIM_{i:03d}' in text]
            self.assertEqual(shown, list(range(len(shown))))
            self.assertGreaterEqual(len(shown), 40)
            for i in shown:
                self.assertIn(f'**Durum:** DURUM_{i:03d} hazır ş.', text)
            # Shared room is never a one-character fragment per topic.
            self.assertNotRegex(text, r'\n\*\n###')

    def opening(self, rules, budget):
        with tempfile.TemporaryDirectory(prefix='opening-band-') as tmp:
            vault = Path(tmp) / 'vault'
            (vault / DIRECTORY).mkdir(parents=True)
            store = runtime.MemoryStore(Path(tmp) / 'state', vault)
            bodies = {
                'Kurallar.md': '# Kurallar\n' + '\n'.join(
                    f'- **kural:** KURAL_{i:03d} Şifre ve Müşteri ayrıntısını koru. **neden:** ' + 'Gerekçe. ' * 4
                    for i in range(rules)) + '\n',
                'Core.md': '# Kimlik\n' + 'Kimlik ayrıntısı. ' * 50,
                'Threads.md': '\n\n'.join(
                    f'### Konu {i}\n**Durum:** DURUM_{i}.\n**Sonraki adım:** ADIM_{i}.' for i in range(12)) + '\n',
                'Last-Session.md': '# Son oturum\n## 2026-11-01 10:00 · a\nSON_DEVIR_KARTI kaldığımız yer: '
                                   + 'ayrıntı. ' * 60 + 'KART_SONU\n',
            }
            for name, text in bodies.items():
                source = f'{DIRECTORY}/{name}'
                (vault / source).write_text(text, encoding='utf-8')
                store.ingest(dict(id=source, source=source, text=text, kind='note', status='active',
                                  updated_at='2026-11-01T00:00:00Z'))
            return companion.context(store, budget, 's' * 24, 'claude')

    def test_rules_that_fit_alone_do_not_cost_the_handoff_or_next_steps(self):
        # Review repro (7 Nov): where the complete rules fit but rules, topic titles and the
        # newest card do not, the card and most next steps vanished although the
        # proportional split before #528 showed them all.
        for budget, counts in ((5000, (40, 50, 55, 60, 64, 67, 70, 80, 120)), (2000, (5, 10, 15, 30))):
            for rules in counts:
                with self.subTest(budget=budget, rules=rules):
                    text = self.opening(rules, budget)
                    self.assertLessEqual(len(text), budget)
                    self.assertIn('## 2026-11-01 10:00 · a\nSON_DEVIR_KARTI', text)
                    if budget == 5000:
                        self.assertIn('KART_SONU', text)
                        self.assertEqual([i for i in range(12) if f'ADIM_{i}.' not in text], [])
                        self.assertGreaterEqual(sum(f'KURAL_{i:03d}' in text for i in range(rules)), min(rules, 40))
                    else:
                        self.assertGreaterEqual(sum(f'ADIM_{i}.' in text for i in range(12)), 3)

    def test_clip_threads_never_starves_status_for_headings(self):
        overview = companion.thread_overview('## Açık konular\n' + '\n\n'.join(
            f'### Konu {i:03d}\n**Durum:** DURUM_{i:03d}.\n**Sonraki adım:** ADIM_{i:03d}.' for i in range(300)))
        previous = 0
        for budget in range(3000, 12001, 250):
            text = companion.clip_threads(overview, budget)
            self.assertLessEqual(len(text), budget)
            self.assertIn('read source', text)
            steps = sum(f'ADIM_{i:03d}.' in text for i in range(300))
            # More room never shows fewer next steps (the headings-only cliff, review 7 Nov).
            self.assertGreaterEqual(steps, previous, budget)
            previous = steps
        self.assertGreater(previous, 100)

    def test_huge_rules_many_topics_and_astral_characters_together(self):
        for _, text, missing_rules, _ in self.check(rules=1500, topics=300, astral=True, history=6):
            self.assertTrue(missing_rules)

    def test_budget_search_stops_at_half_the_hook_budget(self):
        self.seed()
        calls = []

        def renderer(store, budget, *args, **kwargs):
            calls.append(budget)
            return 'PRIORITY_CANARY\n' + 'ç' * budget  # every render overflows the byte cap

        payload = dict(hook_event_name='SessionStart', session_id='deadline-codex', cwd=str(self.vault))
        output = io.StringIO()
        argv = ['hook', '--vault', str(self.vault), '--state', str(self.store.state_dir), '--harness', 'codex']
        with patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                patch.object(sys, 'stdout', output), patch.object(hook, 'HOOK_BUDGET', 0.0), \
                patch.object(companion, 'context', side_effect=renderer), \
                patch.dict(os.environ, inherited_env(BEYIN_V3_NO_SPAWN='1', BEYIN_UPDATES_OFF='1'), clear=True):
            hook.main()
        # The first render and the ratio retry only: no bisection once the budget is spent.
        self.assertEqual(len(calls), 2)
        text = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
        self.assertIn('PRIORITY_CANARY', text)
        self.assertLessEqual(companion.client_size('codex', text), companion.CLIENT_TEXT_LIMITS['codex'][0])

    def test_notice_over_the_cap_still_delivers_a_trimmed_opening(self):
        self.seed()
        for harness, notice in (('codex', 'ç' * 6000), ('claude', '😀' * 5200)):
            with self.subTest(harness=harness), \
                    patch('beyin_v3_releases.session_start', return_value=notice + '\n'):
                payload = dict(hook_event_name='SessionStart', session_id='notice-' + harness, cwd=str(self.vault))
                output = io.StringIO()
                argv = ['hook', '--vault', str(self.vault), '--state', str(self.store.state_dir),
                        '--harness', harness]
                with patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                        patch.object(sys, 'stdout', output), \
                        patch.dict(os.environ, inherited_env(BEYIN_V3_NO_SPAWN='1', BEYIN_UPDATES_OFF='1'), clear=True):
                    hook.main()
                text = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
                self.assertTrue(text.startswith(notice[:100]), 'the notice must not vanish with the opening')
                self.assertLessEqual(companion.client_size(harness, text), companion.CLIENT_TEXT_LIMITS[harness][0])


if __name__ == '__main__':
    unittest.main()
