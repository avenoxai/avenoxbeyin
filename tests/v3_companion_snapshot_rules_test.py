"""A long Kurallar.md keeps its head before the companion layer sees it, and doctor names
how much of it the opening shows whole (#374). Synthetic rules only."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from v3_package_helpers import inherited_env

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
COMPANION = '🔮 850-Companion'
HEAD = '# Kullanıcının düzeltmeleri\n\n## Özet\nSUMMARY_CANARY: kısa yaz, kaynak göster.\n\n## Kurallar\n'
MARKER = re.compile(r'\[truncated: rules (\d+)-(\d+) \((\d+) of (\d+)\) omitted here; read source\]')


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


engine = load('beyin_v3_issue374_engine', 'template/.claude/scripts/beyin_v3.py')
companion = load('beyin_v3_issue374_companion', 'template/.claude/scripts/beyin_v3_companion.py')


def rules_file(size, exact=False):
    """Numbered one-line rules under a summary; exact pads the last rule to size characters."""
    text, count = HEAD, 0
    while len(text) < size:
        count += 1
        text += f'- R{count:04d} kural metni burada durur, kısa ve tek satır.\n'
    if exact:
        text = text[:size - 1] + '\n' if len(text) > size else text
        count = len(re.findall(r'(?m)^- R\d{4} ', text))
    return text, count


def section(context, name):
    start = context.index(f'[{COMPANION}/{name}]\n') + len(f'[{COMPANION}/{name}]\n')
    end = context.find(f'\n[{COMPANION}/', start)
    return context[start:end if end >= 0 else len(context)]


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='issue374-snapshot-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        (self.vault / COMPANION).mkdir(parents=True)
        self.store = engine.MemoryStore(root / 'runtime', self.vault)
        self.bodies = {
            'Core.md': '# Kimlik\nIDENTITY_CANARY\n',
            'Last-Session.md': '# Son oturum\n' + 'Devir kartı satırı.\n' * 120,
            'Threads.md': '# Threads\n## Active Threads\n' + 'Aktif konu satırı.\n' * 200,
            'Journal.md': '# Journal\n## 2026-10-16\nJOURNAL_CANARY\n',
        }

    def write(self, name, body):
        # A rewrite replaces the record, as sync does: a stale copy would add a notice line.
        source = f'{COMPANION}/{name}'
        (self.vault / source).write_text(body, encoding='utf-8', newline='\n')
        with self.store._connect() as db:
            db.execute('DELETE FROM records WHERE id=?', (name,))
        self.store.ingest({'id': name, 'kind': 'note', 'status': 'active', 'text': body,
                           'source': source, 'updated_at': '2026-10-16T10:00:00Z'})

    def snapshot(self, budget):
        return self.store.source_snapshot(list(companion.NAMES), budget_chars=budget,
                                          source_directory=COMPANION)

    def text(self, snapshot, name):
        return next(r['text'] for r in snapshot['records'] if r['source'].endswith('/' + name))

    def test_short_records_hand_their_share_to_a_long_rule_file(self):
        rules, _ = rules_file(45600)
        for name, body in dict(self.bodies, **{'Kurallar.md': rules}).items():
            self.write(name, body)
        result = self.snapshot(200000)
        self.assertEqual(self.text(result, 'Kurallar.md'), rules)
        self.assertFalse(result['truncated'])

    def test_rule_file_over_the_bound_keeps_its_head_and_its_tail(self):
        rules, count = rules_file(45600)
        for name, body in dict(self.bodies, **{'Kurallar.md': rules}).items():
            self.write(name, body)
        result = self.snapshot(30000)
        kept = self.text(result, 'Kurallar.md')
        self.assertTrue(kept.startswith(HEAD + '- R0001 '), kept[:120])
        self.assertTrue(kept.endswith(f'- R{count:04d} kural metni burada durur, kısa ve tek satır.\n'), kept[-120:])
        self.assertEqual(kept.count('\n[truncated]\n'), 1)
        self.assertLessEqual(len(engine._json(result)), 30000)
        # Journal keeps its newest lines at the bottom, as before.
        journal = '# Journal\n' + ''.join(f'gözlem {i}\n' for i in range(4000)) + 'NEWEST_JOURNAL_LINE\n'
        self.write('Journal.md', journal)
        kept = self.text(self.snapshot(30000), 'Journal.md')
        self.assertTrue(kept.startswith('[truncated]\n'))
        self.assertTrue(kept.endswith('NEWEST_JOURNAL_LINE\n'))

    def test_escape_overflow_is_cut_in_a_few_passes_not_one_character_each(self):
        # Every newline is two JSON characters; a 270k rule file overflows the 200k bound
        # by thousands of them, and a re-serialization per character took seconds.
        rules, count = rules_file(270000)
        for name, body in dict(self.bodies, **{'Kurallar.md': rules}).items():
            self.write(name, body)
        with mock.patch.object(engine, '_json', wraps=engine._json) as serialize:
            result = self.snapshot(200000)
        self.assertLess(serialize.call_count, 20)
        self.assertLessEqual(len(engine._json(result)), 200000)
        kept = self.text(result, 'Kurallar.md')
        self.assertTrue(kept.startswith(HEAD))
        self.assertTrue(kept.endswith(f'- R{count:04d} kural metni burada durur, kısa ve tek satır.\n'))

    def test_opening_report_is_the_ceiling_and_changes_nothing(self):
        for name, body in self.bodies.items():
            self.write(name, body)
        self.write('Kurallar.md', rules_file(45600)[0])
        report = {}
        text = companion.context(self.store, 5000, '0' * 24, 'claude', report=report)
        self.assertEqual(text, companion.context(self.store, 5000, '0' * 24, 'claude'))
        rules = report['Kurallar.md']
        self.assertTrue(rules['clipped'])
        self.assertEqual(rules['chars'], len(rules_file(45600)[0]))
        share = rules['share']
        self.assertIn(f'Memory hygiene: Kurallar.md opens clipped (share {share} of {rules["chars"]} chars).', text)
        self.assertEqual(companion.rules_opening(self.store, 5000), rules)
        # A rule file of exactly that size arrives whole; one character more is clipped.
        whole, _ = rules_file(share, exact=True)
        self.assertEqual(len(whole), share)
        self.write('Kurallar.md', whole)
        self.assertFalse(companion.rules_opening(self.store, 5000)['clipped'])
        whole_text = companion.context(self.store, 5000, '0' * 24, 'claude')
        self.assertNotIn('Memory hygiene: Kurallar.md opens clipped', whole_text)
        shown = section(whole_text, 'Kurallar.md')
        self.assertEqual(shown, whole)
        self.write('Kurallar.md', rules_file(share + 1, exact=True)[0])
        self.assertTrue(companion.rules_opening(self.store, 5000)['clipped'])


class InstalledOpeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='issue374-installed-')
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        home = base / 'home'
        home.mkdir()
        self.vault = base / 'Örnek Beyin'
        self.vault.mkdir()
        self.state = base / 'state'
        self.env = inherited_env(HOME=str(home), USERPROFILE=str(home), BEYIN_V3_NO_SPAWN='1',
                                 BEYIN_UPDATES_OFF='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        self.run_cli(ROOT / 'scripts/install_v3.py', '--vault', self.vault, '--state', self.state)

    def run_cli(self, script, *args, payload=None):
        result = subprocess.run([sys.executable, str(script), *map(str, args)],
                                input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, encoding='utf-8', env=self.env,
                                cwd=self.vault, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def rules(self, text):
        (self.vault / COMPANION / 'Kurallar.md').write_text(text, encoding='utf-8', newline='\n')
        self.run_cli(self.vault / 'beyin.py', '--vault', self.vault, '--state', self.state, 'sync')

    def session_start(self):
        payload = {'hook_event_name': 'SessionStart', 'session_id': 'issue-374', 'source': 'startup',
                   'cwd': str(self.vault)}
        output = json.loads(self.run_cli(self.vault / '.claude/scripts/beyin_v3_hook.py', '--vault', self.vault,
                                         '--state', self.state, '--harness', 'claude', payload=payload))
        return output['hookSpecificOutput']['additionalContext']

    def tree(self):
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for root in (self.vault, self.state) for path in sorted(root.rglob('*')) if path.is_file()}

    def test_session_start_keeps_the_summary_and_names_the_real_rule_numbers(self):
        text, count = rules_file(45600)
        self.rules(text)
        context = self.session_start()
        self.assertLessEqual(len(context.encode('utf-16-le')) // 2, 10000)
        self.assertIn('Memory hygiene: Kurallar.md opens clipped (share ', context)
        shown = section(context, 'Kurallar.md')
        self.assertTrue(shown.startswith(HEAD), shown[:160])
        marker = MARKER.search(shown)
        self.assertIsNotNone(marker, shown[-400:])
        first, last, omitted, total = map(int, marker.groups())
        self.assertEqual(total, count)
        self.assertEqual(omitted, last - first + 1)
        numbers = [int(n) for n in re.findall(r'(?m)^- R(\d{4}) ', shown)]
        self.assertEqual(numbers, list(range(1, first)) + list(range(last + 1, count + 1)))

    def test_doctor_names_the_opening_ceiling_and_writes_nothing(self):
        self.rules(rules_file(45600)[0])
        initial_context = self.session_start()
        before = self.tree()
        report = json.loads(self.run_cli(self.vault / 'beyin.py', 'doctor', '--json'))['companion_rules_opening']
        human = self.run_cli(self.vault / 'beyin.py', 'doctor', '--human')
        self.assertEqual(self.tree(), before, 'doctor must not write')
        self.assertEqual((report['status'], report['budget'], report['harness']), ('clipped', 5000, 'claude'))
        self.assertIn(f'Kurallar.md acilista kirpiliyor (bilgi): kural metni {report["chars"]} karakter; '
                      f'Claude Code acilisi bu ayarlarla (5000 karakter) en fazla {report["share"]} karakterini tam gosterir',
                      human)
        self.assertIn(f'Memory hygiene: Kurallar.md opens clipped (share {report["share"]} of {report["chars"]} chars).',
                      initial_context)
        # The number is the hook's own: a rule file of exactly that size arrives whole at SessionStart.
        whole, _ = rules_file(report['share'], exact=True)
        self.rules(whole)
        whole_context = self.session_start()
        self.assertEqual(section(whole_context, 'Kurallar.md'), whole)
        self.assertNotIn('Memory hygiene: Kurallar.md opens clipped', whole_context)
        self.assertEqual(json.loads(self.run_cli(self.vault / 'beyin.py', 'doctor', '--json'))
                         ['companion_rules_opening']['status'], 'ok')
        self.assertNotIn('Kurallar.md acilista kirpiliyor', self.run_cli(self.vault / 'beyin.py', 'doctor', '--human'))


if __name__ == '__main__':
    unittest.main()
