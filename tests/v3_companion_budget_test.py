"""Companion budget: a long rule set keeps both ends, and no budget is thrown away."""
import importlib.util
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evaluator = load('beyin_v3_budget_evaluator', 'scripts/evaluate_v3.py')
companion = load('beyin_v3_budget_companion', 'template/.claude/scripts/beyin_v3_companion.py')
COMPANION = '🔮 850-Companion'
RULES = ('# Kurallar\n- FIRST_RULE_CANARY: her oturumda geçerli temel kural.\n' +
         ''.join(f'- kural {i}: ayrıntılı bir çalışma kuralının tam metni burada durur.\n' for i in range(2, 220)) +
         '- CORRECTION_CANARY: gereksiz övgü kullanma.\n')
BODIES = {
    'Core.md': '# Kimlik\nIDENTITY_CANARY: kullanıcının düşünme ortağıyım.\n',
    'Soul.md': '# Üslup\nSTYLE_CANARY: kısa cümlelerle konuş.\n',
    'Kurallar.md': RULES,
    'Last-Session.md': '# Son oturum\nHANDOFF_CANARY: prototipi denedik, video kaydı bekliyor.\n',
    'Threads.md': '# Konular\n## Active Threads\nTHREAD_BODY_CANARY: ses denemesi sürüyor.\n## Closed Threads\nNOISE\n',
    'Journal.md': '# Journal\n## 2026-09-17\nJOURNAL_CANARY: örnek üzerinden ilerlemek yararlı oldu.\n',
}


class CompanionBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='companion-budget-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        (self.vault / COMPANION).mkdir(parents=True)
        module = evaluator.load_runtime()
        self.store = module.MemoryStore(root / 'runtime', self.vault)
        self.addCleanup(lambda: evaluator.close_store(self.store))
        for name, body in BODIES.items():
            source = f'{COMPANION}/{name}'
            (self.vault / source).write_text(body, encoding='utf-8')
            self.store.ingest({'id': name, 'kind': 'note', 'status': 'active', 'text': body,
                               'source': source, 'updated_at': '2026-09-18T10:00:00Z'})

    def context(self, budget):
        return companion.context(self.store, budget, 'synthetic-budget', 'codex')

    def test_long_rule_set_keeps_first_and_last_rule_with_an_omission_notice(self):
        for budget in (1000, 2000, 5000, 12000):
            with self.subTest(budget=budget):
                text = self.context(budget)
                self.assertLessEqual(len(text), budget)
                self.assertIn('FIRST_RULE_CANARY', text)
                self.assertIn('CORRECTION_CANARY', text)
                self.assertIn('HANDOFF_CANARY', text)
                self.assertRegex(text, r'\[truncated: rules \d+-\d+ \(\d+ of \d+\) omitted')

    def test_budget_left_over_by_retrieval_goes_back_to_the_clipped_sources(self):
        for budget in (1000, 2000, 5000, 12000):
            with self.subTest(budget=budget):
                text = self.context(budget)
                self.assertGreaterEqual(len(text), .95 * budget)

    def test_receipt_block_keeps_its_whole_header_or_stays_out(self):
        # #147/#174: the header names the source and labels the receipt a historical claim.
        # A clipped receipt must never show half of that header with no body under it.
        name = 'receipts/' + '3f' * 32 + '.md'
        receipt = (f'\nLatest receipt ({name}; historical agent claim, not independently verified):\n'
                   + 'R' * 1174 + '\n[truncated: read source]\n')
        header = receipt[:receipt.index('):\n') + 3]
        shown = 0
        for budget in range(1000, 6000, 7):
            text = companion.context(self.store, budget, 'synthetic-budget', 'codex', '', receipt)
            self.assertLessEqual(len(text), budget)
            if '\nLatest receipt' in text:
                shown += 1
                self.assertIn(header, text, budget)
        self.assertGreater(shown, 0)

    def test_session_start_snapshot_does_not_starve_clipped_companion_sources(self):
        # When an unqueried snapshot note exists in the vault, SessionStart must not
        # allocate budget to it while companion files are clipped (#140).
        plan_source = 'Notlar/Plan.md'
        plan_body = '# Proje Plani\n' + ('Ayrintili donem yol haritasi metni. ' * 100)
        (self.vault / 'Notlar').mkdir(exist_ok=True)
        (self.vault / plan_source).write_text(plan_body, encoding='utf-8')
        self.store.ingest({'id': 'plan-note', 'kind': 'note', 'status': 'active',
                           'text': plan_body, 'source': plan_source,
                           'updated_at': '2026-09-18T10:00:00Z'})

        for budget in (2000, 5000, 12000):
            with self.subTest(budget=budget):
                text = self.context(budget)
                self.assertLessEqual(len(text), budget)
                # Unqueried snapshot must yield to clipped companion files
                self.assertNotIn('[Related source: Notlar/Plan.md]', text)
                self.assertIn('FIRST_RULE_CANARY', text)
                self.assertIn('CORRECTION_CANARY', text)
                self.assertIn('HANDOFF_CANARY', text)
                # Leftover retrieval share goes back to companion
                self.assertGreaterEqual(len(text), .95 * budget)

    def test_session_start_snapshot_is_included_when_companion_sources_fit(self):
        # When companion sources are small and fit within their floor, spare budget
        # carries an ambient snapshot as intended.
        tmp = tempfile.TemporaryDirectory(prefix='companion-small-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        vault = root / 'vault'
        (vault / COMPANION).mkdir(parents=True)
        (vault / 'Notlar').mkdir(parents=True)
        store = evaluator.load_runtime().MemoryStore(root / 'runtime', vault)
        self.addCleanup(lambda: evaluator.close_store(store))

        small_bodies = {
            'Core.md': '# Kimlik\nKisa kimlik.\n',
            'Kurallar.md': '# Kurallar\n- Temel kural.\n',
            'Last-Session.md': '# Son oturum\nKisa oturum.\n',
            'Threads.md': '# Konular\n## Active Threads\nKisa konu.\n## Closed Threads\n',
        }
        for name, body in small_bodies.items():
            source = f'{COMPANION}/{name}'
            (vault / source).write_text(body, encoding='utf-8')
            store.ingest({'id': name, 'kind': 'note', 'status': 'active', 'text': body,
                          'source': source, 'updated_at': '2026-09-18T10:00:00Z'})

        note_source = 'Notlar/Ambient.md'
        note_body = '# Ambient Not\nFaydali ortam notu metni.\n'
        (vault / note_source).write_text(note_body, encoding='utf-8')
        store.ingest({'id': 'ambient', 'kind': 'note', 'status': 'active', 'text': note_body,
                      'source': note_source, 'updated_at': '2026-09-18T10:00:00Z'})

        text = companion.context(store, 5000, 'synthetic-session', 'codex')
        self.assertIn('[Related source: Notlar/Ambient.md]', text)
        self.assertNotIn('[truncated: read source]', text)

    def test_query_retrieval_does_not_starve_companion_floor_when_clipped(self):
        # When an explicit query is provided, retrieval takes its bounded share
        # without starving companion sources, and unused characters return to companion.
        note_source = 'Notlar/Plan.md'
        note_body = '# Proje Plani\n' + ('Ayrintili donem yol haritasi metni. ' * 100)
        (self.vault / 'Notlar').mkdir(exist_ok=True)
        (self.vault / note_source).write_text(note_body, encoding='utf-8')
        self.store.ingest({'id': 'plan-note', 'kind': 'note', 'status': 'active',
                           'text': note_body, 'source': note_source,
                           'updated_at': '2026-09-18T10:00:00Z'})

        text = companion.context(self.store, 5000, 'synthetic-session', 'codex', query='plani')
        self.assertLessEqual(len(text), 5000)
        self.assertIn('[Related source: Notlar/Plan.md]', text)
        self.assertIn('FIRST_RULE_CANARY', text)
        self.assertIn('HANDOFF_CANARY', text)
        self.assertGreaterEqual(len(text), .95 * 5000)

    def test_knowledge_index_tight_budget_bounds_with_full_companion(self):
        # Issue #146: knowledge/index.md under tight budgets (1000, 2000, 3000)
        # with full companion files must strictly stay within budget (len <= budget).
        index_source = 'knowledge/index.md'
        index_body = '# Knowledge Map\n' + ''.join(
            f'- [[concept-{i:03d}]]: summary of concept {i} with cross links.\n' for i in range(1, 150)
        )
        (self.vault / 'knowledge').mkdir(exist_ok=True)
        (self.vault / index_source).write_text(index_body, encoding='utf-8')
        self.store.ingest({'id': 'knowledge-index', 'kind': 'note', 'status': 'active',
                           'text': index_body, 'source': index_source,
                           'updated_at': '2026-09-18T10:00:00Z'})

        for budget in (1000, 2000, 3000, 5000, 12000, 24000):
            with self.subTest(budget=budget):
                text = self.context(budget)
                self.assertLessEqual(len(text), budget)
                self.assertIn('FIRST_RULE_CANARY', text)
                self.assertIn('CORRECTION_CANARY', text)
                self.assertIn('HANDOFF_CANARY', text)
                if budget <= 12000:
                    self.assertGreaterEqual(len(text), .90 * budget)

        # On large budget (24000), knowledge map reaches max cap (1500 chars)
        text_24k = self.context(24000)
        self.assertIn('[Knowledge map: knowledge/index.md]', text_24k)
        map_section = text_24k.split('[Knowledge map: knowledge/index.md]\n')[1]
        map_body = map_section.split('\n[')[0] if '\n[' in map_section else map_section
        self.assertGreaterEqual(len(map_body), 1400)

    def test_knowledge_index_scales_with_room_on_larger_budgets(self):
        # Issue #146: with standard companion sizes, allowance expands beyond
        # the legacy 600 cap proportionally with room (up to 1500 chars).
        tmp = tempfile.TemporaryDirectory(prefix='companion-index-scale-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        vault = root / 'vault'
        (vault / COMPANION).mkdir(parents=True)
        (vault / 'knowledge').mkdir(parents=True)
        store = evaluator.load_runtime().MemoryStore(root / 'runtime', vault)
        self.addCleanup(lambda: evaluator.close_store(store))

        small_bodies = {
            'Core.md': '# Kimlik\nKullanicinin dusunme ortagiyim.\n',
            'Soul.md': '# Uslup\nKisa cumleler.\n',
            'Kurallar.md': '# Kurallar\n- Temel calisma kurali.\n- Ikinci kural.\n',
            'Last-Session.md': '# Son oturum\nOturum ozeti burada.\n',
            'Threads.md': '# Konular\n## Active Threads\nAktif baslik.\n## Closed Threads\n',
            'Journal.md': '# Journal\n## 2026-09-17\nGunluk notu.\n',
        }
        for name, body in small_bodies.items():
            source = f'{COMPANION}/{name}'
            (vault / source).write_text(body, encoding='utf-8')
            store.ingest({'id': name, 'kind': 'note', 'status': 'active', 'text': body,
                          'source': source, 'updated_at': '2026-09-18T10:00:00Z'})

        index_source = 'knowledge/index.md'
        index_body = '# Knowledge Map\n' + ''.join(
            f'- [[concept-{i:03d}]]: summary of concept {i} with cross links.\n' for i in range(1, 150)
        )
        (vault / index_source).write_text(index_body, encoding='utf-8')
        store.ingest({'id': 'knowledge-index', 'kind': 'note', 'status': 'active',
                      'text': index_body, 'source': index_source,
                      'updated_at': '2026-09-18T10:00:00Z'})

        for budget in (1000, 2000, 3000, 5000, 12000, 24000):
            with self.subTest(budget=budget):
                text = companion.context(store, budget, 'synthetic-session', 'codex')
                self.assertLessEqual(len(text), budget)
                self.assertIn('[Knowledge map: knowledge/index.md]', text)
                map_section = text.split('[Knowledge map: knowledge/index.md]\n')[1]
                map_body = map_section.split('\n[')[0] if '\n[' in map_section else map_section

                # Room-based scaling checks
                if budget == 5000:
                    # Expands beyond legacy 600 limit
                    self.assertGreater(len(map_body), 600)
                elif budget >= 12000:
                    # Reaches full 1500 allowance ceiling
                    self.assertGreaterEqual(len(map_body), 1400)

    def test_knowledge_index_does_not_grow_while_companion_is_clipped(self):
        # #146 kept the map at its old share while companion sources are clipped; only the
        # separate opening budget (up to 24000) has room//4 above 600 in that state.
        tmp = tempfile.TemporaryDirectory(prefix='companion-index-clipped-')
        self.addCleanup(tmp.cleanup)
        vault = Path(tmp.name) / 'vault'
        (vault / COMPANION).mkdir(parents=True)
        (vault / 'knowledge').mkdir()
        store = evaluator.load_runtime().MemoryStore(Path(tmp.name) / 'runtime', vault)
        self.addCleanup(lambda: evaluator.close_store(store))
        rules = '# Kurallar\n' + ''.join(f'- kural {i}: uzun bir calisma kuralinin tam metni burada durur.\n'
                                         for i in range(600))
        index_body = '# Knowledge Map\n' + ''.join(f'- [[concept-{i:03d}]]: summary {i}.\n' for i in range(1, 150))
        for source, body in [(f'{COMPANION}/{name}', rules if name == 'Kurallar.md' else body)
                             for name, body in BODIES.items()] + [('knowledge/index.md', index_body)]:
            (vault / source).write_text(body, encoding='utf-8')
            store.ingest({'id': source, 'kind': 'note', 'status': 'active', 'text': body,
                          'source': source, 'updated_at': '2026-09-18T10:00:00Z'})
        for budget in (16000, 20000, 24000):
            with self.subTest(budget=budget):
                text = companion.context(store, budget, 'synthetic-budget', 'codex')
                self.assertLessEqual(len(text), budget)
                self.assertGreaterEqual(len(text), .95 * budget)
                self.assertRegex(text, r'\[truncated: rules \d+-\d+ \(\d+ of \d+\) omitted')
                map_body = text.split('[Knowledge map: knowledge/index.md]\n')[1].split('\n[')[0]
                self.assertLessEqual(len(map_body), 600)


if __name__ == '__main__':
    unittest.main()
