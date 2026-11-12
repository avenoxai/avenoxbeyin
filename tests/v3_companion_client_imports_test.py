"""Import references do not prove client delivery or permit dropping companion context.

Real temporary files only; no Claude process or live instruction-loading claim. The parser
remains a diagnostic helper, while openings keep their normal source and budget gates.
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

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_companion as companion
import beyin_v3_hook as hook

DIRECTORY = '🔮 850-Companion'
POINTER = '@import; not repeated here]'


class ClientImportsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='client-imports-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'vault'
        (self.vault / DIRECTORY).mkdir(parents=True)
        self.store = runtime.MemoryStore(self.root / 'state', self.vault)

    def write(self, source, text):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        if source.startswith(DIRECTORY):
            self.store.ingest(dict(id=source, source=source, text=text, kind='note', status='active',
                                   updated_at='2026-11-01T00:00:00Z'))

    def link(self, link, target):
        path = self.vault / link
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Windows stores relative symlink targets verbatim: use native separators.
            os.symlink(Path(target), path)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links unavailable')
        self.assertTrue(path.is_symlink())
        self.assertEqual(path.resolve(strict=True), (path.parent / target).resolve(strict=True))

    def seed(self):
        self.rules = [f'- Kural {i}: Şifre ve Müşteri ayrıntısını koru, sentetik talimat {i}.' for i in range(30)]
        self.write(f'{DIRECTORY}/Kurallar.md', '# Kurallar\n' + '\n'.join(
            rule + ' **neden:** ' + 'Gerekçe ayrıntısı. ' * 10 for rule in self.rules) + '\n')
        self.write(f'{DIRECTORY}/Core.md', '# Kimlik\nCORE_CANARY ' + 'Kimlik ayrıntısı. ' * 60 + '\n')
        self.write(f'{DIRECTORY}/Threads.md', '## Active Threads\n' + '\n\n'.join(
            f'### Konu {i}\n**Durum:** DURUM_{i} hazır.\n**Sonraki adım:** ADIM_{i} çalıştır.\n\n'
            + 'Geçmiş ayrıntı. ' * 50 for i in range(14)) + '\n')
        self.write(f'{DIRECTORY}/Last-Session.md', '# Son oturum\n## 2026-11-01 10:00 · a · 11111111\nKART_A '
                   + 'devir. ' * 120 + '\n## 2026-10-31 09:00 · b · 22222222\nKART_B ' + 'devir. ' * 120 + '\n')

    def imports(self, cwd=None, harness='claude'):
        loaded = companion.client_imports(self.vault, harness, self.vault if cwd is None else cwd)
        names = set()
        for key in loaded:
            for path in self.vault.rglob('*.md'):
                if companion.file_key(path) == key:
                    names.add(path.relative_to(self.vault).as_posix())
        return names

    def rules_path(self):
        return f'{DIRECTORY}/Kurallar.md'

    def test_symlinked_imports_from_the_issue_are_detected(self):
        self.seed()
        self.link('.claude/core.md', f'../{DIRECTORY}/Core.md')
        self.link('.claude/kurallar.md', f'../{DIRECTORY}/Kurallar.md')
        self.write('CLAUDE.md', '# Vault\n\n@.claude/core.md\n@.claude/kurallar.md\n')
        names = self.imports()
        self.assertIn(f'{DIRECTORY}/Core.md', names)
        self.assertIn(self.rules_path(), names)
        self.assertNotIn(f'{DIRECTORY}/Threads.md', names)

    def test_import_syntax_follows_claude_code(self):
        self.seed()
        escaped = DIRECTORY.replace(' ', chr(92) + ' ')
        cases = {
            # Claude Code imports a path only when it starts with ./ ~/ / or [A-Za-z0-9._-]:
            # the default folder starts with an emoji, so a bare path is not imported.
            'escaped space, emoji first': (f'@{escaped}/Kurallar.md\n', False),
            'escaped space after ./': (f'@./{escaped}/Kurallar.md\n', True),
            'inline in prose': (f'Kurallar: @./{escaped}/Kurallar.md burada.\n', True),
            'list item': (f'- Kurallar: @./{escaped}/Kurallar.md\n', True),
            'fragment is cut': (f'@./{escaped}/Kurallar.md#Genel\n', True),
            'table without a delimiter row is prose': (f'| Kurallar | @./{escaped}/Kurallar.md |\n', True),
            'table cell (older clients skip it)': (f'| Kurallar | @./{escaped}/Kurallar.md |\n|---|---|\n', False),
            'unescaped space ends the path': (f'@./{DIRECTORY}/Kurallar.md\n', False),
            'backslash ends the path': (f'@.\\{escaped}\\Kurallar.md\n', False),
            'quoted path': (f'@"{DIRECTORY}/Kurallar.md"\n', False),
            'code span': (f'`@./{escaped}/Kurallar.md`\n', False),
            'code span over two lines': (f'Bkz. `kural\n@./{escaped}/Kurallar.md` dosyası\n', False),
            'fenced block': (f'```\n@./{escaped}/Kurallar.md\n```\n', False),
            'indented code': (f'Örnek:\n\n    @./{escaped}/Kurallar.md\n', False),
            'html comment': (f'<!-- @./{escaped}/Kurallar.md -->\n', False),
            'html block': (f'<details>\n@./{escaped}/Kurallar.md\n</details>\n', False),
            'link title': (f'[Kurallar](x.md "bkz @./{escaped}/Kurallar.md") burada\n', False),
            'not an import': (f'mail me@./{escaped}/Kurallar.md\n', False),
            'missing file': ('@.claude/yok.md\n', False),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name):
                self.write('CLAUDE.md', '# Vault\n' + text)
                self.assertEqual(self.rules_path() in self.imports(), expected)
        with self.subTest('frontmatter'):
            self.write('CLAUDE.md', f'---\nkurallar: @./{escaped}/Kurallar.md\n---\n# Vault\n')
            self.assertNotIn(self.rules_path(), self.imports())

    def test_turkish_first_letter_is_not_imported_bare(self):
        self.seed()
        self.write('Şablonlar/Kurallar.md', '- Şablon kuralı.\n')
        self.write('CLAUDE.md', '@Şablonlar/Kurallar.md\n')
        self.assertNotIn('Şablonlar/Kurallar.md', self.imports())
        self.write('CLAUDE.md', '@./Şablonlar/Kurallar.md\n')
        self.assertIn('Şablonlar/Kurallar.md', self.imports())

    def test_oversized_and_non_text_files_do_not_count(self):
        self.seed()
        escaped = DIRECTORY.replace(' ', chr(92) + ' ')
        self.write('CLAUDE.md', f'@./{escaped}/Kurallar.md\n')
        self.assertIn(self.rules_path(), self.imports())
        with patch.object(companion, 'IMPORT_SIZE', 100):  # the client skips a larger file
            self.assertNotIn(self.rules_path(), self.imports())
        self.write('notes.pdf', f'@./{escaped}/Kurallar.md\n')
        self.write('CLAUDE.md', '@./notes.pdf\n')
        self.assertNotIn(self.rules_path(), self.imports())

    def test_other_instruction_files_and_nested_imports_up_to_four_hops(self):
        self.seed()
        target = f'@../{DIRECTORY.replace(" ", chr(92) + " ")}/Kurallar.md\n'
        self.write('docs/hop4.md', target)
        self.write('docs/hop3.md', '@hop4.md\n')
        self.write('docs/hop2.md', '@hop3.md\n')
        self.write('docs/hop1.md', '@hop2.md\n')
        self.write('CLAUDE.local.md', '@docs/hop2.md\n')  # hop 4 for Kurallar.md: loaded
        self.assertIn(self.rules_path(), self.imports())
        (self.vault / 'CLAUDE.local.md').write_text('@docs/hop1.md\n', encoding='utf-8')  # hop 5: not loaded
        self.assertNotIn(self.rules_path(), self.imports())
        self.write('.claude/CLAUDE.md', '@../docs/hop4.md\n')
        self.assertIn(self.rules_path(), self.imports())

    def test_cycles_terminate(self):
        self.seed()
        self.write('a.md', '@b.md\n')
        self.write('b.md', '@a.md\n@CLAUDE.md\n')
        self.write('CLAUDE.md', '@a.md\n')
        self.assertEqual(self.imports(), {'CLAUDE.md', 'a.md', 'b.md'})

    def test_external_imports_and_other_clients_do_not_count(self):
        self.seed()
        escaped = DIRECTORY.replace(' ', '\\ ')
        self.write('CLAUDE.md', f'@./{escaped}/Kurallar.md\n')
        self.write('AGENTS.md', f'@./{escaped}/Core.md\n')
        project = self.vault / 'projects/Alfa'
        project.mkdir(parents=True)
        # Launched in a subfolder, the vault CLAUDE.md still loads but its import resolves
        # outside the working directory: Claude Code waits for an approval the hook cannot see.
        self.assertEqual(self.imports(cwd=project), {'CLAUDE.md'})
        # A session outside the vault never loads the vault CLAUDE.md.
        outside = self.root / 'elsewhere'
        outside.mkdir()
        self.assertEqual(companion.client_imports(self.vault, 'claude', outside), {})
        # Codex reads AGENTS.md verbatim: no import syntax, every source stays.
        for harness in ('codex', 'opencode', 'hermes', 'antigravity', 'kiro', 'omp'):
            with self.subTest(harness=harness):
                self.assertEqual(companion.client_imports(self.vault, harness, self.vault), {})

    def test_project_instruction_file_in_the_working_directory_counts(self):
        self.seed()
        self.write('projects/Alfa/Kurallar.md', '- Alfa kuralı.\n')
        self.write('projects/Alfa/CLAUDE.md', '@Kurallar.md\n')
        self.assertIn('projects/Alfa/Kurallar.md', self.imports(cwd=self.vault / 'projects/Alfa'))
        self.assertNotIn('projects/Alfa/Kurallar.md', self.imports())

    # Tiny budgets may truncate the receipt header, so keep its full real-sized
    # input deterministic instead of stripping output after budget allocation.
    @patch.object(companion, 'receipt_header',
                  lambda session: f'Receipt session={session}; now=2026-11-12 15:18 +0000; ')
    def test_import_candidates_cannot_replace_source_text_or_change_allocation(self):
        self.seed()
        escaped = DIRECTORY.replace(' ', chr(92) + ' ')
        self.write('CLAUDE.md', f'@./{escaped}/Kurallar.md\n@./{escaped}/Core.md\n')
        loaded = companion.client_imports(self.vault, 'claude', self.vault)
        self.assertIn(companion.file_key(self.vault / self.rules_path()), loaded)
        originals = {p: p.read_bytes() for p in self.vault.rglob('*.md')}
        for harness in ('claude', 'codex', 'hermes', 'antigravity', 'opencode', 'omp', 'kiro', 'pi'):
            for budget in (0, 1, 200, 1000, 5000, 7500, 9500, 24000):
                with self.subTest(harness=harness, budget=budget):
                    report, baseline_report = {}, {}
                    text = companion.context(self.store, budget, 's' * 24, harness,
                                             report=report, loaded=loaded)
                    baseline = companion.context(self.store, budget, 's' * 24, harness,
                                                 report=baseline_report)
                    self.assertEqual(text, baseline)
                    self.assertEqual(report, baseline_report)
                    self.assertLessEqual(len(text), budget)
                    self.assertNotIn('Loaded in full', text)
                    self.assertNotIn('imported', report.get('Kurallar.md', {}))
                    if budget >= 7500:
                        for rule in self.rules:
                            self.assertIn(rule, text)
                        self.assertIn('CORE_CANARY', text)
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)

    def run_hook(self, cwd, harness='claude', event='SessionStart', **extra):
        payload = dict(hook_event_name=event, session_id='imports-' + harness, cwd=str(cwd), **extra)
        output = io.StringIO()
        argv = ['hook', '--vault', str(self.vault), '--state', str(self.store.state_dir), '--harness', harness]
        with patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                patch.object(sys, 'stdout', output), \
                patch.dict(os.environ, inherited_env(BEYIN_V3_NO_SPAWN='1', BEYIN_UPDATES_OFF='1'), clear=True):
            hook.main()
        result = json.loads(output.getvalue())
        text = (result['injectSteps'][0]['ephemeralMessage'] if harness == 'antigravity' else
                result['hookSpecificOutput']['additionalContext'])
        cap = companion.CLIENT_TEXT_LIMITS.get(harness)
        if cap:
            self.assertLessEqual(companion.client_size(harness, text), cap[0])
        return text

    def test_session_start_keeps_sources_even_with_valid_import_aliases(self):
        self.seed()
        companion.save_context(self.store.state_dir, 9500)
        self.link('.claude/kurallar.md', f'../{DIRECTORY}/Kurallar.md')
        self.write('CLAUDE.md', '# Vault\n@.claude/kurallar.md\n')
        text = self.run_hook(self.vault)
        self.assertIn(self.rules[0], text)
        self.assertNotIn(POINTER, text)
        # Codex does not read CLAUDE.md: its opening keeps the rules.
        codex = self.run_hook(self.vault, harness='codex')
        self.assertIn(self.rules[0], codex)
        self.assertNotIn(POINTER, codex)
        # A Claude session launched outside the vault keeps them as well.
        outside = self.root / 'elsewhere'
        outside.mkdir()
        elsewhere = self.run_hook(outside)
        self.assertIn(self.rules[0], elsewhere)
        self.assertNotIn(POINTER, elsewhere)

    def test_session_start_keeps_rules_claude_code_does_not_import(self):
        # Review repro: a bare path into the default folder starts with an emoji, so
        # Claude Code imports nothing; the opening must still carry the rules.
        self.seed()
        escaped = DIRECTORY.replace(' ', chr(92) + ' ')
        for line in (f'@{escaped}/Kurallar.md', f'| Kurallar | @{escaped}/Kurallar.md |',
                     f'Bkz. `@./{escaped}/Kurallar.md`'):
            with self.subTest(line):
                self.write('CLAUDE.md', '# Vault\n' + line + '\n')
                text = self.run_hook(self.vault)
                self.assertIn(self.rules[0], text)
                self.assertNotIn(POINTER, text)
        self.write('CLAUDE.md', f'# Vault\n@./{escaped}/Kurallar.md\n')
        text = self.run_hook(self.vault)
        self.assertIn(self.rules[0], text)
        self.assertNotIn(POINTER, text)

    def test_doctor_reports_actual_allocation_not_inferred_import_delivery(self):
        self.seed()
        baseline = companion.rules_opening(self.store, 1000)
        self.assertTrue(baseline['clipped'])
        self.write('CLAUDE.md', f'@./{DIRECTORY.replace(" ", chr(92) + " ")}/Kurallar.md\n')
        rules = companion.rules_opening(self.store, 1000)
        self.assertEqual(rules, baseline)
        self.assertNotIn('imported', rules)

    def test_claude_excludes_do_not_suppress_independently_available_rules(self):
        self.seed()
        self.write('CLAUDE.md', f'@./{DIRECTORY.replace(" ", chr(92) + " ")}/Kurallar.md\n')
        # The documented Claude setting excludes this instruction file, not the
        # independently indexed companion source. No live Claude loading is asserted.
        for settings in ('settings.json', 'settings.local.json'):
            with self.subTest(settings=settings):
                self.write('.claude/' + settings,
                           json.dumps({'claudeMdExcludes': [str(self.vault / 'CLAUDE.md')]}))
                text = self.run_hook(self.vault)
                self.assertIn(self.rules[0], text)
                self.assertNotIn(POINTER, text)
                self.assertNotIn('imported', companion.rules_opening(self.store, 5000))

    def test_all_clients_keep_sources_at_start_resume_compact_and_continuity(self):
        self.seed()
        companion.save_context(self.store.state_dir, 9500)
        self.write('CLAUDE.md', f'@./{DIRECTORY.replace(" ", chr(92) + " ")}/Kurallar.md\n')
        for harness in ('claude', 'codex', 'hermes', 'antigravity', 'opencode', 'omp', 'kiro', 'pi'):
            for source in ('startup', 'resume', 'compact'):
                with self.subTest(harness=harness, source=source):
                    text = self.run_hook(self.vault, harness, source=source)
                    self.assertIn(self.rules[0], text)
                    self.assertNotIn(POINTER, text)
            with self.subTest(harness=harness, event='UserPromptSubmit'):
                text = self.run_hook(self.vault, harness, event='UserPromptSubmit',
                                     prompt='Where did we leave off?')
                self.assertIn(self.rules[0], text)
                self.assertNotIn(POINTER, text)

    def test_opening_and_doctor_do_not_scan_unverified_client_imports(self):
        self.seed()
        with patch.object(companion, 'client_imports', side_effect=AssertionError('not load evidence')) as scan:
            self.assertIn(self.rules[0], self.run_hook(self.vault))
            self.assertIn(self.rules[0], self.run_hook(self.vault, event='UserPromptSubmit',
                                                    prompt='Where did we leave off?'))
            self.assertNotIn('imported', companion.rules_opening(self.store, 5000))
            scan.assert_not_called()

    def test_import_candidates_do_not_bypass_privacy_trust_or_freshness(self):
        self.seed()
        for filename, fields in (('Core.md', {'visibility': 'private'}),
                                 ('Soul.md', {'trust': 'untrusted'})):
            source = f'{DIRECTORY}/{filename}'
            text = filename.upper() + '_HIDDEN_CANARY'
            path = self.vault / source
            path.write_text('---\n' + json.dumps(fields) + '\n---\n' + text, encoding='utf-8')
            with self.store._connect() as db:
                db.execute('DELETE FROM records WHERE id=?', (source,))
            self.store.ingest(dict(id=source, source=source, text=text, kind='note', status='active',
                                   updated_at='2026-11-01T00:00:00Z', **fields))
        (self.vault / DIRECTORY / 'Last-Session.md').write_text('CHANGED_HANDOFF_CANARY')
        escaped = DIRECTORY.replace(' ', chr(92) + ' ')
        self.write('CLAUDE.md', ''.join(f'@./{escaped}/{name}\n' for name in companion.NAMES))
        loaded = companion.client_imports(self.vault, 'claude', self.vault)
        for harness in ('claude', 'codex', 'hermes', 'antigravity', 'opencode', 'omp', 'kiro', 'pi'):
            with self.subTest(harness=harness):
                text = companion.context(self.store, 9500, 's' * 24, harness, loaded=loaded)
                for canary in ('CORE.MD_HIDDEN_CANARY', 'SOUL.MD_HIDDEN_CANARY',
                               'CHANGED_HANDOFF_CANARY', 'KART_A', 'KART_B'):
                    self.assertNotIn(canary, text)
                self.assertIn(self.rules[0], text)
                self.assertNotIn(POINTER, text)
                self.assertIn('Missing/unavailable companion sources', text)
                self.assertIn('Changed companion sources excluded', text)


if __name__ == '__main__':
    unittest.main()
