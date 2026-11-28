"""A source whose opening share cannot hold an excerpt arrives as one omission pointer, and doctor
reports every companion file's share (#916). Class test against the 3.18.0 path, which is the
same code with the pointer turned off. Synthetic companion files only."""
import importlib.util
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
COMPANION = '🔮 850-Companion'
LABEL = re.compile(r'\n\[' + re.escape(COMPANION) + r'/([^\]\n]+)\]\n')
STUB = re.compile(r'\[omitted(?: \(\d+ chars\))?(?::)?(?: read (?:full )?source)?(?:; read source)?\]\n')


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


engine = load('beyin_v3_issue916_engine', 'template/.claude/scripts/beyin_v3.py')
companion = load('beyin_v3_issue916_companion', 'template/.claude/scripts/beyin_v3_companion.py')
entry = load('beyin_entry_issue916', 'scripts/beyin_entry.py')


def pad(head, line, size):
    text = head
    while len(text) < size:
        text += line
    return text[:size - 1] + '\n'


def topics(count, width):
    return '## Active Threads\n' + ''.join(
        f'### Konu {i:02d}\n- [ ] 2026-11-27: Durum özeti {i}. ' + 'x' * width + '\n' for i in range(count))


RULES = pad('# Kurallar\n\n', '- Kural metni burada durur, kısa ve tek satır.\n', 3769)
REASONED = pad('# Kurallar\n\n', '- **kural:** Kısa yaz. **neden:** uzun gerekçe metni burada durur ve sürer.\n', 6190)
LAST = pad('## 2026-11-27 10:00 · handoff · 12345678\n', 'Son oturum ayrıntısı ve açık adım.\n', 1976)
# Both field measurements from #916 (3.17.0, Claude Code), and variants around them.
SHAPES = {
    'field_one': {'Core.md': pad('# Düşünme ortağı\n\n', 'Kimlik tercihi ve çalışma bağlamı notu.\n', 4880),
                  'Kurallar.md': RULES, 'Last-Session.md': LAST, 'Threads.md': topics(11, 600),
                  'Journal.md': pad('## 2026-11-27 · Gözlem\n', 'Uzun gözlem ve öğrenim notu.\n', 6530)},
    'field_two': {'Core.md': pad('# Kimlik\n\n', 'Kimlik ve ton notu satırı burada.\n', 6726),
                  'Kurallar.md': REASONED, 'Threads.md': topics(11, 700),
                  'Journal.md': pad('## 2026-11-27\n', 'Kısa gözlem.\n', 288)},
    'short_identity': {'Core.md': '# Kimlik\nKısa kimlik notu, tek satır ve biraz daha.\n',
                       'Kurallar.md': RULES, 'Last-Session.md': LAST, 'Threads.md': topics(11, 600)},
    'soul_only': {'Soul.md': pad('# Soul\n\n', 'Ton ve üslup notu satırı.\n', 3000),
                  'Kurallar.md': RULES, 'Last-Session.md': LAST, 'Threads.md': topics(4, 600),
                  'Journal.md': pad('# Journal\n', 'Gözlem satırı.\n', 1200)},
    'roomy': {'Core.md': pad('# Kimlik\n\n', 'Kimlik satırı.\n', 600), 'Kurallar.md': RULES[:900] + '\n',
              'Last-Session.md': LAST[:500] + '\n', 'Threads.md': topics(2, 80),
              'Journal.md': pad('## 2026-11-27\n', 'Gözlem.\n', 300)},
}
BUDGETS = (1000, 2500, 5000, 8000, 9500)


def sections(text):
    parts = LABEL.split(text)
    return {Path(parts[i]).name: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


class OpeningReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='issue916-opening-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        (self.vault / COMPANION).mkdir(parents=True)
        self.store = engine.MemoryStore(root / 'runtime', self.vault)

    def write(self, files):
        for path in (self.vault / COMPANION).iterdir():
            path.unlink()
        with self.store._connect() as db:
            db.execute('DELETE FROM records')
        for name, body in files.items():
            source = f'{COMPANION}/{name}'
            (self.vault / source).write_text(body, encoding='utf-8', newline='\n')
            self.store.ingest({'id': name, 'kind': 'note', 'status': 'active', 'text': body,
                               'source': source, 'updated_at': '2026-10-16T10:00:00Z'})

    def render(self, budget):
        report = {}
        return companion.context(self.store, budget, '0' * 24, 'claude', report=report), report

    def test_pointer_replaces_only_shares_too_small_for_an_excerpt(self):
        # Invariants per shape and budget: inside the budget; with no file omitted the opening is
        # byte for byte the 3.18.0 one (the same code with the pointer off); an omitted file shows
        # only the pointer and every other source keeps at least its 3.18.0 text; and a file is
        # clipped exactly when its share is below its size.
        omitted_seen = set()
        for shape, files in SHAPES.items():
            self.write(files)
            for budget in BUDGETS:
                with self.subTest(shape=shape, budget=budget):
                    text, report = self.render(budget)
                    with mock.patch.object(companion, 'MIN_USABLE_EXCERPT', -1):
                        before, _ = self.render(budget)
                    self.assertLessEqual(len(text), budget)
                    entries = report['files']
                    self.assertEqual(set(entries), set(files))
                    omitted = {name for name, item in entries.items() if item['omitted']}
                    omitted_seen |= {(shape, name) for name in omitted}
                    if not omitted:
                        self.assertEqual(text, before)
                    now, then = sections(text), sections(before)
                    for name, item in entries.items():
                        self.assertEqual(item['chars'], len(files[name]))
                        self.assertEqual(item['clipped'], item['omitted'] or item['share'] < item['chars'], item)
                        if item['omitted']:
                            self.assertEqual(item['share'], 0)
                            self.assertNotIn(name, ('Kurallar.md', 'Last-Session.md', 'Threads.md'))
                            if budget >= 2500:
                                self.assertEqual(now[name], f'[omitted ({len(files[name])} chars); read source]\n')
                            else:
                                self.assertTrue(now[name] == '' or STUB.fullmatch(now[name])
                                                or '[omitted: read source]'.startswith(now[name]), now[name])
                        else:
                            self.assertGreaterEqual(len(now[name]), len(then[name]), name)
        # The field shapes starve the identity even when it is one short line: its own size
        # never earns it room beside the protected core, which is what doctor has to say.
        self.assertTrue({('field_one', 'Core.md'), ('field_two', 'Core.md'),
                         ('short_identity', 'Core.md'), ('soul_only', 'Soul.md')} <= omitted_seen, omitted_seen)
        self.assertNotIn(('roomy', 'Core.md'), omitted_seen)

    def test_doctor_status_names_what_it_measured(self):
        self.write(SHAPES['field_one'])
        _, opening = companion.opening_report(self.store, 8000)
        self.assertEqual((opening['status'], opening['identity']), ('warn', 'Core.md'))
        self.write(SHAPES['roomy'])
        _, opening = companion.opening_report(self.store, 9500)
        self.assertEqual(opening['status'], 'ok')
        # An empty companion folder measures nothing; "ok" would hide that.
        self.write({})
        _, opening = companion.opening_report(self.store, 8000)
        self.assertEqual((opening['status'], opening['files']), ('no_sources', {}))

    def test_human_warning_points_at_the_protected_core_not_the_identity(self):
        self.write(SHAPES['short_identity'])
        rules, opening = companion.opening_report(self.store, 8000)
        self.assertEqual(opening['status'], 'warn')
        human = entry.human_result({'status': 'ok', 'companion_rules_opening': dict(rules, status='ok'),
                                    'companion_opening': opening}, 'doctor', '3.18.0')
        line = next(text for text in human.splitlines() if text.startswith('Core.md açılışa sığmıyor (uyarı)'))
        self.assertIn('Açılış payını önce Kurallar.md, Last-Session.md ve Threads.md alıyor', line)
        self.assertNotIn('dosyayı kısalt', line)


if __name__ == '__main__':
    unittest.main()
