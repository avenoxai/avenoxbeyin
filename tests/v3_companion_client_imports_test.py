"""#610: companion files Claude Code already loads through CLAUDE.md @imports are not repeated.

Real temporary files only. A path the hook cannot verify as loaded keeps its source in the
opening: a duplicate is cheaper than a lost rule.
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
            os.symlink(target, path)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links unavailable')

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

    def test_symlinked_imports_from_the_issue_are_loaded(self):
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
        cases = {
            'escaped space': (f'@{DIRECTORY.replace(" ", chr(92) + " ")}/Kurallar.md\n', True),
            'inline in prose': (f'Kurallar: @./{DIRECTORY.replace(" ", chr(92) + " ")}/Kurallar.md burada.\n', True),
            'unescaped space ends the path': (f'@{DIRECTORY}/Kurallar.md\n', False),
            'quoted path': (f'@"{DIRECTORY}/Kurallar.md"\n', False),
            'code span': (f'`@{DIRECTORY.replace(" ", chr(92) + " ")}/Kurallar.md`\n', False),
            'fenced block': (f'```\n@{DIRECTORY.replace(" ", chr(92) + " ")}/Kurallar.md\n```\n', False),
            'html comment': (f'<!-- @{DIRECTORY.replace(" ", chr(92) + " ")}/Kurallar.md -->\n', False),
            'not an import': (f'mail me@{DIRECTORY.replace(" ", chr(92) + " ")}/Kurallar.md\n', False),
            'missing file': ('@.claude/yok.md\n', False),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name):
                self.write('CLAUDE.md', '# Vault\n' + text)
                self.assertEqual(self.rules_path() in self.imports(), expected)

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
        self.write('CLAUDE.md', f'@{escaped}/Kurallar.md\n')
        self.write('AGENTS.md', f'@{escaped}/Core.md\n')
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

    def test_opening_replaces_imported_sources_with_a_pointer(self):
        self.seed()
        escaped = DIRECTORY.replace(' ', '\\ ')
        self.write('CLAUDE.md', f'@{escaped}/Kurallar.md\n@{escaped}/Core.md\n')
        loaded = companion.client_imports(self.vault, 'claude', self.vault)
        report = {}
        text = companion.context(self.store, 7500, 's' * 24, 'claude', report=report, loaded=loaded)
        self.assertLessEqual(len(text), 7500)
        for rule in self.rules:
            self.assertNotIn(rule, text)
        self.assertNotIn('CORE_CANARY', text)
        self.assertIn(f'[{self.rules_path()}]\n[Loaded in full by CLAUDE.md {POINTER}', text)
        self.assertIn(f'[{DIRECTORY}/Core.md]\n[Loaded in full by CLAUDE.md {POINTER}', text)
        self.assertNotIn('opens clipped', text)
        self.assertEqual(report['Kurallar.md']['imported'], True)
        self.assertFalse(report['Kurallar.md']['clipped'])
        # The room the second copy took goes to the open topics and the handoff.
        without = companion.context(self.store, 7500, 's' * 24, 'claude')
        shown = sum(f'ADIM_{i}' in text for i in range(14))
        self.assertEqual(shown, 14)
        self.assertGreater(len([i for i in range(14) if f'Konu {i}\n**Durum:** DURUM_{i} hazır.\n**Sonraki adım:**'
                                f' ADIM_{i} çalıştır.\n\nGeçmiş' in text]),
                           len([i for i in range(14) if f'ADIM_{i} çalıştır.\n\nGeçmiş' in without]))
        self.assertIn('KART_B', text)

    def run_hook(self, cwd, harness='claude'):
        payload = dict(hook_event_name='SessionStart', session_id='imports-' + harness, cwd=str(cwd))
        output = io.StringIO()
        argv = ['hook', '--vault', str(self.vault), '--state', str(self.store.state_dir), '--harness', harness]
        with patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                patch.object(sys, 'stdout', output), \
                patch.dict(os.environ, inherited_env(BEYIN_V3_NO_SPAWN='1', BEYIN_UPDATES_OFF='1'), clear=True):
            hook.main()
        text = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
        cap = companion.CLIENT_TEXT_LIMITS.get(harness)
        if cap:
            self.assertLessEqual(companion.client_size(harness, text), cap[0])
        return text

    def test_session_start_hook_skips_imported_rules_only_where_claude_loads_them(self):
        self.seed()
        companion.save_context(self.store.state_dir, 9500)
        self.link('.claude/kurallar.md', f'../{DIRECTORY}/Kurallar.md')
        self.write('CLAUDE.md', '# Vault\n@.claude/kurallar.md\n')
        text = self.run_hook(self.vault)
        self.assertNotIn(self.rules[0], text)
        self.assertIn(POINTER, text)
        for i in range(14):
            self.assertIn(f'ADIM_{i}', text)
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

    def test_doctor_rules_opening_reports_imported_rules(self):
        self.seed()
        self.write('CLAUDE.md', f'@{DIRECTORY.replace(" ", chr(92) + " ")}/Kurallar.md\n')
        rules = companion.rules_opening(self.store, 5000)
        self.assertTrue(rules['imported'])
        self.assertFalse(rules['clipped'])
        (self.vault / 'CLAUDE.md').write_text('# no imports\n', encoding='utf-8')
        self.assertNotIn('imported', companion.rules_opening(self.store, 5000))


if __name__ == '__main__':
    unittest.main()
