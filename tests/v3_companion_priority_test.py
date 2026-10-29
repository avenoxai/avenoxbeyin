"""Opening priorities use synthetic sources only; the source notes are never rewritten."""
from pathlib import Path
import io
import json
import os
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

DIRECTORY = 'Companion'


class OpeningPriorityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='opening-priority-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        (self.vault / DIRECTORY).mkdir(parents=True)
        self.store = runtime.MemoryStore(root / 'state', self.vault)

    def write(self, source, text):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        self.store.ingest(dict(id=source, source=source, text=text, kind='note', status='active',
                               updated_at='2026-10-01T00:00:00Z'))

    def seed(self, unicode_rules=False):
        self.rules = [f'- **kural:** Rule {i}: preserve this complete synthetic instruction. '
                      + ('çığş' * 8 + ' ' if unicode_rules else '')
                      for i in range(22)]
        self.statuses = [f'**Status:** STATE_{i}: prototype ready.\n  Wrapped status {i}.' for i in range(9)]
        self.steps = [f'**Next step:** NEXT_{i}: run the synthetic check.\n  Wrapped step {i}.' for i in range(9)]
        self.card = '## 2026-10-01 10:00 · synthetic · 12345678\n' + 'Latest handoff.\n' * 35
        bodies = {
            'Core.md': '# Identity\n' + 'Identity detail. ' * 300,
            'Soul.md': '# Style\n' + 'Style detail. ' * 150,
            'Kurallar.md': '# Rules\n' + '\n'.join(r + '**neden:** ' + 'Reason detail. ' * 30 for r in self.rules),
            'Last-Session.md': '# Handoff\n' + self.card + '\n## 2026-09-30 10:00\nOLDER_CARD\n' + 'Old history. ' * 100,
            'Threads.md': '## Active Threads\n' + '\n\n'.join(
                f'### Thread: Topic {i}\n{self.statuses[i]}\n{self.steps[i]}\n\n' + 'History detail. ' * 100
                for i in range(9)) + '\n## Closed Threads\nCLOSED_CANARY\n',
            'Journal.md': '# Journal\n## 2026-10-01\n' + 'Journal detail. ' * 100,
        }
        for name, text in bodies.items():
            self.write(f'{DIRECTORY}/{name}', text)
        self.project_rules = '- Project rule one.\n- Project rule two.\n'
        self.write('projects/Synthetic/rules.md', self.project_rules)

    def test_all_rules_thread_fields_and_newest_handoff_precede_detail(self):
        self.seed()
        originals = {p: p.read_bytes() for p in self.vault.rglob('*.md')}
        for harness in ('claude', 'codex', 'hermes'):
            with self.subTest(harness=harness):
                report = {}
                text = companion.context(self.store, 9500, 'synthetic', harness,
                                         rules_project='Synthetic', report=report)
                self.assertLessEqual(len(text), 9500)
                for rule in self.rules:
                    self.assertIn(rule.rstrip(), text)
                self.assertIn(self.project_rules, text)
                for i in range(9):
                    self.assertIn(f'### Thread: Topic {i}', text)
                    self.assertIn(self.statuses[i], text)
                    self.assertIn(self.steps[i], text)
                self.assertIn(self.card, text)
                self.assertNotIn('OLDER_CARD', text)
                self.assertNotIn('CLOSED_CANARY', text)
                self.assertFalse(report['Kurallar.md']['clipped'])
                self.assertTrue(report['Kurallar.md']['reasons_omitted'])
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)

    def test_impossible_core_still_respects_every_budget(self):
        self.seed()
        for budget in (0, 1, 200, 1000, 2000, 4000, 9500, 24000):
            with self.subTest(budget=budget):
                text = companion.context(self.store, budget, 'synthetic', 'hermes',
                                         rules_project='Synthetic')
                self.assertLessEqual(len(text), budget)

    def test_session_start_delivers_priorities_without_the_final_guard_cutting(self):
        # A fixed UTF-8 surcharge in protected rules means proportional rebudgeting
        # needs more than one pass; the former single retry still cut the tail.
        self.seed(unicode_rules=True)
        companion.save_context(self.store.state_dir, 9500)
        original_guard = companion.fit_client

        def guard(harness, text):
            self.assertEqual(original_guard(harness, text), text)
            return text

        for harness in ('claude', 'codex', 'hermes'):
            with self.subTest(harness=harness):
                payload = dict(hook_event_name='SessionStart', session_id='synthetic-' + harness,
                               project='Synthetic')
                output = io.StringIO()
                argv = ['hook', '--vault', str(self.vault), '--state', str(self.store.state_dir),
                        '--harness', harness]
                with patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                        patch.object(sys, 'stdout', output), patch.object(companion, 'fit_client', side_effect=guard), \
                        patch.object(companion, 'client_rebudget', wraps=companion.client_rebudget) as rebudget, \
                        patch.dict(os.environ, inherited_env(BEYIN_V3_NO_SPAWN='1'), clear=True):
                    hook.main()
                if harness == 'codex':
                    self.assertGreaterEqual(rebudget.call_count, 3)
                text = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
                self.assertLessEqual(len(text), 9500)
                for rule in self.rules:
                    self.assertIn(rule.rstrip(), text)
                for status, step in zip(self.statuses, self.steps):
                    self.assertIn(status, text)
                    self.assertIn(step, text)
                self.assertIn(self.project_rules, text)
                self.assertIn(self.card, text)

    def test_latest_card_ignores_fenced_headings(self):
        card = '# Handoff\n## 2026-10-01\nLatest.\n```md\n## Example\n```\n'
        self.assertEqual(companion.latest_handoff(card + '\n## 2026-09-30\nOld.'), card)

    def test_legacy_overview_keeps_pending_sentence_without_inventing_fields(self):
        text = ('## Açık konular\n### Bir\n**Durum (2026-10-01):** Deneme bitti. '
                'Eski ayrıntı. Açık: onay bekliyor.\n\n'
                '### Two\nPrototype ready. History detail. Next: run the check.\n')
        result = companion.thread_overview(text)
        self.assertIn('**Durum (2026-10-01):** Deneme bitti.', result)
        self.assertIn('Açık: onay bekliyor.', result)
        self.assertIn('Next: run the check.', result)
        self.assertNotIn('Eski ayrıntı.', result)
        self.assertNotIn('History detail.', result)
        self.assertIn('read source', result)

    def test_unstructured_notes_and_fenced_examples_remain_source_verbatim(self):
        text = '## Active Threads\nNo headings.\n```md\n### Example\n```\n'
        self.assertEqual(companion.thread_overview(text), text)

    def test_explicit_current_fields_take_precedence_over_dated_history(self):
        text = ('## Active Threads\n### Topic\nStatus: ready\nNext: verify\n\n'
                '**Durum (2026-09-01):** old status\nOld history.\n')
        result = companion.thread_overview(text)
        self.assertIn('Status: ready\nNext: verify', result)
        self.assertNotIn('old status', result)

    def test_affordable_rules_stay_whole_when_the_thread_overview_cannot_fit(self):
        self.seed()
        # The explicit fields are themselves large: they must yield room to rules,
        # while every topic heading remains visible instead of only the first ones.
        with patch.object(companion, 'thread_overview', side_effect=lambda text, **kw: text):
            text = companion.context(self.store, 9500, 'synthetic', 'hermes', rules_project='Synthetic')
        for rule in self.rules:
            self.assertIn(rule.rstrip(), text)
        self.assertIn(self.project_rules, text)
        for i in range(9):
            self.assertIn(f'### Thread: Topic {i}', text)
        self.assertIn(self.card, text)
        self.assertLessEqual(len(text), 9500)

    def test_section_clipping_is_bounded_and_spreads_room_across_topics(self):
        text = '## Active Threads\n' + '\n'.join(
            f'### Topic {i}\nStatus: ' + 'state ' * 80 + '\nNext: ' + 'step ' * 80 for i in range(9))
        for budget in (0, 1, 80, 500, 1000, 3000, 20000):
            with self.subTest(budget=budget):
                result = companion.clip_threads(text, budget)
                self.assertLessEqual(len(result), budget)
                if 500 <= budget < len(text):
                    for i in range(9):
                        self.assertIn(f'### Topic {i}', result)
                    self.assertIn('read source', result)

    def test_a_shorter_overview_marker_can_preserve_all_fields_at_the_boundary(self):
        source = '## Active Threads\n' + '\n\n'.join(
            f'### Topic {i}\nStatus: ready {i}\nNext: verify {i}' for i in range(9))
        overview = companion.thread_overview(source)
        text = companion.clip_threads(overview, len(overview) - 1)
        self.assertLessEqual(len(text), len(overview) - 1)
        for i in range(9):
            self.assertIn(f'Status: ready {i}', text)
            self.assertIn(f'Next: verify {i}', text)

    def test_optional_identity_yields_at_the_exact_complete_core_boundary(self):
        self.seed()
        large = companion.context(self.store, 24000, 'synthetic', 'hermes', rules_project='Synthetic')
        header = large[:large.index(f'\n[{DIRECTORY}/Core.md]')]
        required = 0
        for path in self.vault.rglob('*.md'):
            source = path.relative_to(self.vault).as_posix()
            required += len(f'\n[{source}]\n')
            body = companion.excerpt(path.name, path.read_text(encoding='utf-8'), project='Synthetic')
            if path.name in ('Kurallar.md', 'rules.md'):
                required += len(companion.without_reasons(body))
            elif path.name == 'Threads.md':
                required += len(companion.thread_overview(body))
            elif path.name == 'Last-Session.md':
                required += len(companion.latest_handoff(body))
            else:
                required += min(40, len(body))
        budget = len(header) + required
        text = companion.context(self.store, budget, 'synthetic', 'hermes', rules_project='Synthetic')
        self.assertLessEqual(len(text), budget)
        for rule in self.rules:
            self.assertIn(rule.rstrip(), text)
        for status, step in zip(self.statuses, self.steps):
            self.assertIn(status, text)
            self.assertIn(step, text)
        self.assertIn(self.card, text)


if __name__ == '__main__':
    unittest.main()
