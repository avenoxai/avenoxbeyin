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

from v3_package_helpers import inherited_env, without_hook_clock

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

    def test_stale_diagnostic_is_specific_to_eligible_rules(self):
        self.write('Threads.md', '# Threads\nold\n')
        (self.vault / COMPANION / 'Threads.md').write_text('changed', encoding='utf-8')
        self.assertIsNone(companion.rules_opening(self.store, 5000))
        self.write('Kurallar.md', '# Rules\nold\n')
        (self.vault / COMPANION / 'Kurallar.md').write_text('changed', encoding='utf-8')
        self.assertEqual(companion.rules_opening(self.store, 5000), {'stale': True})
        for fields in ({'visibility': 'private'}, {'trust': 'untrusted'}, {'trusted': False}):
            with self.subTest(fields=fields):
                with self.store._connect() as db:
                    row = db.execute('SELECT payload FROM records WHERE id=?', ('Kurallar.md',)).fetchone()
                    original = row[0]
                    record = json.loads(original)
                    record.update(fields)
                    db.execute('UPDATE records SET payload=? WHERE id=?', (json.dumps(record), 'Kurallar.md'))
                try:
                    self.assertIsNone(companion.rules_opening(self.store, 5000))
                finally:
                    with self.store._connect() as db:
                        db.execute('UPDATE records SET payload=? WHERE id=?', (original, 'Kurallar.md'))

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
        self.assertEqual(without_hook_clock(text),
                         without_hook_clock(companion.context(self.store, 5000, '0' * 24, 'claude')))
        rules = report['Kurallar.md']
        self.assertTrue(rules['clipped'])
        self.assertEqual(rules['chars'], len(rules_file(45600)[0]))
        share = rules['share']
        self.assertIn(f'Memory hygiene: Kurallar.md opens clipped (fit ceiling {share} of {rules["chars"]} chars).', text)
        self.assertEqual(companion.rules_opening(self.store, 5000), rules)
        # A rule file of exactly that size arrives whole. With priority allocation a
        # shorter rule set can reclaim optional detail, so share is no fixed ceiling.
        whole, _ = rules_file(share, exact=True)
        self.assertEqual(len(whole), share)
        self.write('Kurallar.md', whole)
        self.assertFalse(companion.rules_opening(self.store, 5000)['clipped'])
        whole_text = companion.context(self.store, 5000, '0' * 24, 'claude')
        self.assertNotIn('Memory hygiene: Kurallar.md opens clipped', whole_text)
        shown = section(whole_text, 'Kurallar.md')
        self.assertEqual(shown, whole)
        self.write('Kurallar.md', rules_file(5000, exact=True)[0])
        self.assertTrue(companion.rules_opening(self.store, 5000)['clipped'])

    def test_opening_report_matches_final_hygiene_budget(self):
        # The clipped notice consumes opening budget. The report and its notice must
        # describe the final render, not the pre-notice allocation (#689).
        for name, body in self.bodies.items():
            self.write(name, body)
        self.write('Kurallar.md', rules_file(45600)[0])
        for budget in (2188, 2189, 2190, 5000):
            with self.subTest(budget=budget):
                report = {}
                text = companion.context(self.store, budget, '0' * 24, 'claude', report=report)
                rules = report['Kurallar.md']
                self.assertTrue(rules['clipped'])
                self.assertLessEqual(len(text), budget)
                self.assertIn(
                    f'Memory hygiene: Kurallar.md opens clipped (fit ceiling {rules["share"]} of {rules["chars"]} chars).',
                    text,
                )
                self.assertEqual(companion.rules_opening(self.store, budget), rules)
                # Check source-to-output, not two diagnostics sharing a stale number.
                expected = companion.clip(rules_file(45600)[0], rules['share'], both=True, tail=True)
                self.assertEqual(section(text, 'Kurallar.md'), expected)

    def test_pinned_rules_take_at_most_half_the_room_in_both_orders(self):
        # #890: pinned rules from the omitted range come before the marker, whole; the
        # marker and report count only what is left out; pins past half the room clip.
        text, count = rules_file(12000)
        pins = {7, 40, 90, 120}
        for n in pins:
            text = text.replace(f'- R{n:04d} ', f'- R{n:04d} <!-- beyin:pin --> ')
        for latest in (False, True):
            source = (companion.LATEST_RULES + '\n' if latest else '') + text
            for budget in (900, 1500, 2600, 4000):
                with self.subTest(latest=latest, budget=budget):
                    report = {}
                    shown = companion.clip(source, budget, both=True, tail=True, rules_report=report)
                    self.assertLessEqual(len(shown), budget)
                    first, last, omitted, total = map(int, MARKER.search(shown).groups())
                    numbers = [int(n) for n in re.findall(r'(?m)^- R(\d{4}) ', shown)]
                    self.assertEqual(len(numbers), len(set(numbers)))
                    inside = [n for n in numbers if first <= n <= last]
                    self.assertTrue(set(inside) <= pins, inside)
                    self.assertEqual(omitted, last - first + 1 - len(inside))
                    self.assertEqual(report['omitted_rules'], {'first': first, 'last': last, 'count': omitted, 'total': total})
                    self.assertEqual(report.get('pinned_omitted', 0),
                                     sum(first <= n <= last and n not in inside for n in pins))
                    gap = len(f'\n[truncated: rules {total}-{total} ({total} of {total}) omitted here; read source]\n')
                    pinned_chars = sum(len(re.search(rf'(?m)^- R{n:04d} .*\n', source)[0]) for n in inside)
                    self.assertLessEqual(pinned_chars, (budget - gap) // 2)
                    if budget >= 1500:
                        self.assertTrue(pins & set(numbers) >= {7, 40}, numbers)
        # Without a pin the cut is the one before #890.
        report = {}
        plain = companion.clip(rules_file(12000)[0], 1500, both=True, tail=True, rules_report=report)
        first, last, omitted, total = map(int, MARKER.search(plain).groups())
        self.assertEqual(omitted, last - first + 1)
        self.assertNotIn('pinned_omitted', report)

    def test_pin_written_after_the_reason_survives_dropping_reasons(self):
        # #890: the installed template writes `- **kural:** ... **neden:** ...`; a pin at the
        # end of that item, or on its reason line, must stay when the reasons go.
        def item(n, pin, continued):
            reason = f'**neden:** Gerekçe {n:02d} uzun bir açıklama taşır ve yer kaplar.'
            joint = '\n  ' if continued else ' '
            return f'- **kural:** Kural {n:02d} metni biraz uzun bir cümle olarak durur.{joint}{reason}{pin}\n'
        for continued in (False, True):
            with self.subTest(continued=continued):
                text = HEAD + ''.join(item(n, ' <!-- beyin:pin -->' if n == 20 else '', continued)
                                      for n in range(1, 41))
                stripped = companion.without_reasons(text)
                self.assertNotIn('Gerekçe', stripped)
                self.assertIn('Kural 20 metni biraz uzun bir cümle olarak durur. <!-- beyin:pin -->\n', stripped)
                self.assertEqual(len(companion.PINNED_RULE.findall(stripped)), 1)
                report = {}
                shown = companion.clip(stripped, 1500, both=True, tail=True, rules_report=report)
                self.assertLessEqual(len(shown), 1500)
                self.assertIn('Kural 20 ', shown)
                first, last, omitted, total = map(int, MARKER.search(shown).groups())
                self.assertTrue(first < 20 < last, (first, last))
                self.assertEqual((omitted, total), (last - first, 40))
                self.assertNotIn('pinned_omitted', report)

    def test_pin_marker_inside_fenced_code_pins_nothing(self):
        # #890: rule_starts does not count a list item inside a fenced block as a rule; a
        # marker shown there as an example does not pin the rule that carries the block.
        text, count = rules_file(12000)
        sample = '  ```markdown\n  - örnek <!-- beyin:pin -->\n  ```\n'
        for fenced, pinned in ((True, False), (False, True)):
            item = '- R0050 kural metni burada durur, kısa ve tek satır.\n'
            extra = sample if fenced else '  Not satırı <!-- beyin:pin -->\n'
            source = text.replace(item, item + extra)
            with self.subTest(fenced=fenced):
                self.assertEqual(companion._is_pinned(item + extra), pinned)
                report = {}
                shown = companion.clip(source, 1500, both=True, tail=True, rules_report=report)
                first, last, omitted, total = map(int, MARKER.search(shown).groups())
                self.assertTrue(first < 50 < last, (first, last))
                self.assertEqual('- R0050 ' in shown, pinned)
                self.assertEqual((omitted, total), (last - first + 1 - pinned, count))
                self.assertNotIn('pinned_omitted', report)

    def test_pinned_rule_alone_in_the_cut_names_no_empty_rule_range(self):
        # When the only rule in the cut is a pinned one, only the prose before the first rule
        # was left out; "rules 1-1 (0 of 5)" would misreport it. The plain marker stands where
        # that prose was, before the pin (the order COMPANION-PARITY.md documents).
        items = ''.join(f'- X{i:03d} ' + 'k' * 60 + (' <!-- beyin:pin -->' if i == 0 else '') + '\n'
                        for i in range(5))
        for latest in (False, True):
            text = (companion.LATEST_RULES + '\n' if latest else '') + '# Kurallar\n' + 'giriş metni ' * 30 + '\n' + items
            with self.subTest(latest=latest):
                report = {}
                shown = companion.clip(text, 419, both=True, tail=True, rules_report=report)
                self.assertLessEqual(len(shown), 419)
                self.assertIsNone(MARKER.search(shown), shown)
                self.assertTrue(shown.startswith(companion.CLIP_MARKER + '- X000 '), shown[:80])
                self.assertEqual(report, {})

    def test_doctor_reads_what_every_project_opening_shares_once(self):
        # #893: doctor measures each project's rules with SessionStart's own context(). The
        # knowledge map and the unqueried retrieval in it are the same for every project, so
        # each distinct read reaches the store once, and the report is the unshared one.
        for name, body in self.bodies.items():
            self.write(name, body)
        self.write('Kurallar.md', '# Rules\n- Short global rule.\n')
        sources = {'knowledge/index.md': '# Index\n- [[Kart]]\n'}
        for n in range(4):
            sources[f'projects/p{n}/Kurallar.md'] = rules_file(2000 + 5000 * n)[0]
        for source, body in sources.items():
            (self.vault / source).parent.mkdir(parents=True, exist_ok=True)
            (self.vault / source).write_text(body, encoding='utf-8', newline='\n')
            self.store.ingest({'id': source, 'kind': 'note', 'status': 'active', 'text': body,
                               'source': source, 'updated_at': '2026-10-16T10:00:00Z'})
        with mock.patch.object(companion, '_SharedOpeningStore', lambda store: store):
            unshared = companion.project_rules_opening(self.store, 5000)
        self.assertEqual([e['source'] for e in unshared], [f'projects/p{n}/Kurallar.md' for n in (1, 2, 3)])
        calls = []
        for name in ('snapshot_context', 'due_tasks_context', 'source_snapshot'):
            real = getattr(self.store, name)

            def counted(*args, _name=name, _real=real, **kwargs):
                if kwargs.get('text_transform') is None:
                    calls.append((_name, repr(args), repr(sorted(kwargs.items()))))
                return _real(*args, **kwargs)
            setattr(self.store, name, counted)
        self.assertEqual(companion.project_rules_opening(self.store, 5000), unshared)
        self.assertIn('knowledge', ''.join(call[1] + call[2] for call in calls))
        self.assertEqual(len(calls), len(set(calls)), calls)

    def test_doctor_says_how_many_project_rule_files_it_left_unmeasured(self):
        # #893: doctor measures at most max_projects project rule files per call; the cut is
        # reported (JSON counts and one --human line) instead of being silent.
        self.write('Kurallar.md', '# Rules\n- Short global rule.\n')
        for n in range(3):
            source = f'projects/p{n}/Kurallar.md'
            (self.vault / source).parent.mkdir(parents=True)
            body = rules_file(16000)[0]
            (self.vault / source).write_text(body, encoding='utf-8', newline='\n')
            self.store.ingest({'id': source, 'kind': 'note', 'status': 'active', 'text': body,
                               'source': source, 'updated_at': '2026-10-16T10:00:00Z'})
        for cap, checked in ((2, 2), (50, 3)):
            with self.subTest(cap=cap):
                scanned = {}
                results = companion.project_rules_opening(self.store, 5000, max_projects=cap, report=scanned)
                self.assertEqual(scanned, {'checked': checked, 'total': 3})
                self.assertEqual(len(results), checked)
        entry = load('beyin_entry_issue893', 'scripts/beyin_entry.py')
        line = 'Proje Kurallar.md ölçümü ilk 50 dosyayla sınırlı (bilgi): 57 dosyanın 7 tanesi ölçülmedi.'
        opening = {'status': 'ok', 'budget': 5000, 'project_rules_checked': 50, 'project_rules_total': 57}
        self.assertIn(line, entry.human_result({'status': 'ok', 'companion_rules_opening': opening}, 'doctor'))
        opening.update(project_rules_checked=57)
        self.assertNotIn('Proje Kurallar.md ölçümü', entry.human_result(
            {'status': 'ok', 'companion_rules_opening': opening}, 'doctor'))

    def test_rules_notice_keeps_long_receipt_within_total_budget(self):
        for name, body in self.bodies.items():
            self.write(name, body)
        self.write('Kurallar.md', rules_file(45600)[0])
        receipt = 'Receipt (historical):\n' + 'x' * 3000
        for budget in (600, 601, 620, 700, 1000, 1100):
            with self.subTest(budget=budget):
                text = companion.context(self.store, budget, '0' * 24, 'claude', receipt=receipt)
                self.assertLessEqual(len(text), budget)



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

    def test_doctor_distinguishes_stale_rules_without_writing_or_disclosing_text(self):
        self.rules('# Rules\n- Initial rule.\n')
        path = self.vault / COMPANION / 'Kurallar.md'
        path.write_text('# Rules\n- STALE_PRIVATE_CANARY.\n', encoding='utf-8')
        before = self.tree()
        raw = self.run_cli(self.vault / 'beyin.py', 'doctor', '--json')
        report = json.loads(raw)['companion_rules_opening']
        self.assertEqual(report['status'], 'stale')
        self.assertNotIn('STALE_PRIVATE_CANARY', raw)
        human = self.run_cli(self.vault / 'beyin.py', 'doctor', '--human')
        self.assertIn('beyin.py sync', human)
        self.assertEqual(self.tree(), before)
        self.assertNotIn('STALE_PRIVATE_CANARY', self.session_start())
        self.run_cli(self.vault / 'beyin.py', 'sync')
        self.assertEqual(json.loads(self.run_cli(self.vault / 'beyin.py', 'doctor', '--json'))
                         ['companion_rules_opening']['status'], 'ok')

    def test_session_start_keeps_the_summary_and_names_the_real_rule_numbers(self):
        text, count = rules_file(45600)
        self.rules(text)
        context = self.session_start()
        self.assertLessEqual(len(context.encode('utf-16-le')) // 2, 10000)
        self.assertIn('Memory hygiene: Kurallar.md opens clipped (fit ceiling ', context)
        shown = section(context, 'Kurallar.md')
        self.assertTrue(shown.startswith(HEAD), shown[:160])
        marker = MARKER.search(shown)
        self.assertIsNotNone(marker, shown[-400:])
        first, last, omitted, total = map(int, marker.groups())
        self.assertEqual(total, count)
        self.assertEqual(omitted, last - first + 1)
        numbers = [int(n) for n in re.findall(r'(?m)^- R(\d{4}) ', shown)]
        self.assertEqual(numbers, list(range(1, first)) + list(range(last + 1, count + 1)))

    def test_pinned_rules_remain_visible_when_rules_clipped(self):
        text, count = rules_file(25000)
        # Pin rule 50 (which would otherwise be in the omitted middle range)
        text = text.replace('- R0050 ', '- R0050 <!-- beyin:pin --> ')
        self.rules(text)
        context = self.session_start()
        self.assertLessEqual(len(context), 5000)
        self.assertLessEqual(len(context.encode('utf-16-le')) // 2, 10000)
        shown = section(context, 'Kurallar.md')
        self.assertIn('R0050', shown)
        self.assertIn('<!-- beyin:pin -->', shown)
        # The marker counts only the rules really left out: the pinned one is shown (#890).
        first, last, omitted, total = map(int, MARKER.search(shown).groups())
        self.assertTrue(first < 50 < last, (first, last))
        numbers = [int(n) for n in re.findall(r'(?m)^- R(\d{4}) ', shown)]
        self.assertEqual(omitted, last - first + 1 - sum(first <= n <= last for n in numbers))
        self.assertEqual(omitted, last - first)
        report = json.loads(self.run_cli(self.vault / 'beyin.py', 'doctor', '--json'))['companion_rules_opening']
        self.assertEqual(report['omitted_rules'], {'first': first, 'last': last, 'count': omitted, 'total': total})
        self.assertNotIn('pinned_omitted', report)

    def test_doctor_reports_pins_past_their_cap_for_main_and_project_rules(self):
        # Pinning every rule cannot bring the clipping back (#890): pins take at most half
        # the rule room, the rest clip as usual and doctor counts them.
        text, count = rules_file(25000)
        pinned = re.sub(r'(?m)^- (R\d{4}) ', r'- \1 <!-- beyin:pin --> ', text)
        (self.vault / 'projects' / 'alpha').mkdir(parents=True)
        (self.vault / 'projects' / 'alpha' / 'Kurallar.md').write_text(pinned, encoding='utf-8', newline='\n')
        self.rules(pinned)
        context = self.session_start()
        self.assertLessEqual(len(context.encode('utf-16-le')) // 2, 10000)
        before = self.tree()
        report = json.loads(self.run_cli(self.vault / 'beyin.py', 'doctor', '--json'))['companion_rules_opening']
        human = self.run_cli(self.vault / 'beyin.py', 'doctor', '--human')
        self.assertEqual(self.tree(), before, 'doctor must not write')
        self.assertEqual(report['status'], 'clipped')
        self.assertEqual(report['pinned_omitted'], report['omitted_rules']['count'])
        self.assertIn(f'Sabitlenmiş (beyin:pin) {report["pinned_omitted"]} kural açılış payına sığmıyor (bilgi)', human)
        project = report['project_rules'][0]
        self.assertEqual((project['source'], project['status']), ('projects/alpha/Kurallar.md', 'clipped'))
        self.assertEqual(project['pinned_omitted'], project['omitted_rules']['count'])
        self.assertIn(f'Sabitlenmiş (beyin:pin) {project["pinned_omitted"]} kural açılış payına sığmıyor '
                      '(projects/alpha/Kurallar.md) (bilgi)', human)

    def test_latest_rule_order_preserves_complete_newest_items(self):
        raw, count = rules_file(15600)
        self.rules('<!-- beyin:rules-order=latest -->\n' + raw)
        context = self.session_start()
        self.assertLessEqual(len(context), 5000)
        shown = section(context, 'Kurallar.md')
        first, last, omitted, total = map(int, MARKER.search(shown).groups())
        self.assertEqual((first, last, omitted, total), (1, omitted, omitted, count))
        numbers = [int(n) for n in re.findall(r'(?m)^- R(\d{4}) ', shown)]
        self.assertEqual(numbers, list(range(last + 1, count + 1)))
        self.assertTrue(numbers)
        self.assertNotIn('beyin:rules-order', shown)

    def test_doctor_names_the_opening_ceiling_and_writes_nothing(self):
        self.rules(rules_file(45600)[0])
        initial_context = self.session_start()
        before = self.tree()
        report = json.loads(self.run_cli(self.vault / 'beyin.py', 'doctor', '--json'))['companion_rules_opening']
        human = self.run_cli(self.vault / 'beyin.py', 'doctor', '--human')
        self.assertEqual(self.tree(), before, 'doctor must not write')
        self.assertEqual((report['status'], report['budget'], report['harness']), ('clipped', 5000, 'claude'))
        first, last, omitted, total = map(int, MARKER.search(section(initial_context, 'Kurallar.md')).groups())
        self.assertEqual(report['omitted_rules'], {'first': first, 'last': last, 'count': omitted, 'total': total})
        self.assertEqual(report['measurement'], 'reference_opening')
        self.assertIn('Oturuma özel bildirimler gerçek açılışta bu payı azaltabilir.', human)
        self.assertIn(f'Gösterilmeyen kural sıraları {first}-{last} ({omitted}/{total}).', human)

        self.assertIn(f'Kurallar.md açılışta kırpılıyor: kural metni {report["chars"]} karakter; '
                      f'Claude Code açılışı bu ayarlarla (5000 karakter) en fazla {report["share"]} karakterini tam gösterir',
                      human)
        self.assertIn(f'Memory hygiene: Kurallar.md opens clipped (fit ceiling {report["share"]} of {report["chars"]} chars).',
                      initial_context)
        # The number is the hook's own: a rule file of exactly that size arrives whole at SessionStart.
        whole, _ = rules_file(report['share'], exact=True)
        self.rules(whole)
        whole_context = self.session_start()
        self.assertEqual(section(whole_context, 'Kurallar.md'), whole)
        self.assertNotIn('Memory hygiene: Kurallar.md opens clipped', whole_context)
        self.assertEqual(json.loads(self.run_cli(self.vault / 'beyin.py', 'doctor', '--json'))
                         ['companion_rules_opening']['status'], 'ok')
        self.assertNotIn('Kurallar.md açılışta kırpılıyor', self.run_cli(self.vault / 'beyin.py', 'doctor', '--human'))

    def test_doctor_reports_clipped_and_stale_project_rules_without_disclosing_private(self):
        self.rules('# Rules\n- Short global rule.\n')
        proj_dir = self.vault / 'projects' / 'alpha'
        proj_dir.mkdir(parents=True, exist_ok=True)
        proj_rules = proj_dir / 'Kurallar.md'
        proj_rules.write_text(rules_file(16000)[0], encoding='utf-8', newline='\n')
        priv_dir = self.vault / 'projects' / 'gizli'
        priv_dir.mkdir(parents=True, exist_ok=True)
        (priv_dir / 'Kurallar.md').write_text(
            '---\nvisibility: private\n---\n' + rules_file(16000)[0], encoding='utf-8', newline='\n')
        self.run_cli(self.vault / 'beyin.py', '--vault', self.vault, '--state', self.state, 'sync')

        before = self.tree()
        raw = self.run_cli(self.vault / 'beyin.py', 'doctor', '--json')
        report = json.loads(raw)['companion_rules_opening']
        human = self.run_cli(self.vault / 'beyin.py', 'doctor', '--human')
        self.assertEqual(self.tree(), before, 'doctor must not write')
        self.assertEqual(report['status'], 'ok')
        project_entries = report.get('project_rules') or []
        self.assertEqual(len(project_entries), 1, project_entries)
        entry = project_entries[0]
        self.assertEqual((entry['project'], entry['source'], entry['status'], entry['clipped']),
                         ('alpha', 'projects/alpha/Kurallar.md', 'clipped', True))
        self.assertLess(entry['share'], entry['chars'])
        self.assertIn('omitted_rules', entry)
        # The count names nothing, and the file under a sensitive folder is not counted;
        # under the cap no --human line reports a cut.
        self.assertEqual((report['project_rules_checked'], report['project_rules_total']), (1, 1))
        self.assertNotIn('Proje Kurallar.md ölçümü', human)
        self.assertNotIn('gizli', raw)
        self.assertIn('projects/alpha/Kurallar.md', human)
        self.assertIn('Proje Kurallar.md açılışta kırpılıyor', human)

        # Stale project rules must be marked stale without disclosing text.
        proj_rules.write_text('# Rules\n- STALE_PROJECT_CANARY.\n', encoding='utf-8', newline='\n')
        stale_raw = self.run_cli(self.vault / 'beyin.py', 'doctor', '--json')
        self.assertNotIn('STALE_PROJECT_CANARY', stale_raw)
        stale_entries = json.loads(stale_raw)['companion_rules_opening'].get('project_rules') or []
        self.assertEqual([(e['project'], e['status']) for e in stale_entries], [('alpha', 'stale')])

        # Short synced project rules produce no clipped/stale warning.
        proj_rules.write_text('# Rules\n- Short project rule.\n', encoding='utf-8', newline='\n')
        self.run_cli(self.vault / 'beyin.py', '--vault', self.vault, '--state', self.state, 'sync')
        clean_report = json.loads(self.run_cli(self.vault / 'beyin.py', 'doctor', '--json'))['companion_rules_opening']
        self.assertNotIn('project_rules', clean_report)

    def test_doctor_reports_project_rules_under_the_parent_folder_real_spelling(self):
        # On a case-insensitive file system (macOS, Windows) `projects` opens `Projects/` and
        # `Projeler` opens `projeler/`; doctor must still use the spelling SessionStart labels
        # the rules with (#893).
        self.rules('# Rules\n- Short global rule.\n')
        sources = {'Projects': 'alpha', 'projeler': 'beta'}
        for parent, name in sources.items():
            (self.vault / parent / name).mkdir(parents=True)
            (self.vault / parent / name / 'Kurallar.md').write_text(
                rules_file(16000)[0], encoding='utf-8', newline='\n')
        self.run_cli(self.vault / 'beyin.py', '--vault', self.vault, '--state', self.state, 'sync')
        expected = sorted(f'{parent}/{name}/Kurallar.md' for parent, name in sources.items())
        for source in expected:
            payload = {'hook_event_name': 'SessionStart', 'session_id': 'issue-893', 'source': 'startup',
                       'cwd': str(self.vault / Path(source).parent)}
            context = json.loads(self.run_cli(
                self.vault / '.claude/scripts/beyin_v3_hook.py', '--vault', self.vault, '--state', self.state,
                '--harness', 'claude', payload=payload))['hookSpecificOutput']['additionalContext']
            self.assertIn(f'[{source}]', context)

        def project_rules():
            report = json.loads(self.run_cli(self.vault / 'beyin.py', 'doctor', '--json'))['companion_rules_opening']
            return sorted((e['source'], e['status']) for e in report.get('project_rules') or [])

        self.assertEqual(project_rules(), [(source, 'clipped') for source in expected])
        for parent, name in sources.items():
            (self.vault / parent / name / 'Kurallar.md').write_text(
                '# Rules\n- STALE_PROJECT_CANARY.\n', encoding='utf-8', newline='\n')
        self.assertEqual(project_rules(), [(source, 'stale') for source in expected])


if __name__ == '__main__':
    unittest.main()
