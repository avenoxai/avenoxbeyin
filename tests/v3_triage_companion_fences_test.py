"""Triage summary ignores examples without changing legacy topic counting."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_triage as triage
from tests.v3_package_helpers import install, isolated_env


class CompanionFenceSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='triage-companion-fences-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.folder = self.vault / 'Companion'
        self.folder.mkdir(parents=True)
        self.state.mkdir()
        (self.folder / 'Core.md').write_text('# Synthetic identity\n', encoding='utf-8')
        self.source = self.folder / 'Threads.md'
        (self.state / 'companion-bootstrap.json').write_text(
            json.dumps({'directory': 'Companion'}), encoding='utf-8')

    def count(self, text):
        self.source.write_bytes(text.encode('utf-8'))
        before = self.source.read_bytes()
        result = triage.scan_companion(self.vault, self.state)['active_threads']
        self.assertEqual(self.source.read_bytes(), before)
        return result

    def test_fence_boundaries_ignore_example_headings_and_statuses(self):
        for mark in ('`', '~'):
            for newline in ('\n', '\r\n'):
                for indent in ('', ' ', '   '):
                    for false_close in (mark * 3, ('~' if mark == '`' else '`') * 4,
                                        mark * 4 + ' trailing', '    ' + mark * 4):
                        with self.subTest(mark=mark, newline=newline, indent=indent, false_close=false_close):
                            lines = ['# Threads', '## Active Threads', indent + mark * 4 + 'md',
                                     '### Example', false_close, '### Still example',
                                     '**Status:** active', indent + mark * 5 + ' \t',
                                     '### Real', '**Status:** closed']
                            self.assertEqual(self.count(newline.join(lines)), 1)
                            # Exercise the no-heading legacy status path separately.
                            lines = [line for line in lines if not line.startswith('###')]
                            lines[-1] = '**Durum:** 🟢 Aktif'
                            self.assertEqual(self.count(newline.join(lines)), 1)

    def test_fenced_sections_do_not_stop_or_start_live_sections(self):
        cases = [
            ('## Active Threads\n```md\n## Closed Threads\n```\n### Real\n', 1),
            ('```md\n## Active Threads\n### Example\n```\n### Real\n', 1),
            ('## Active Threads\n~~~md\n### Example\n**Status:** active\n', 0),
            ('```md\n**Status:** active\n```\n', 0),
            ('## Active Threads\n```inline```\n### Real\n', 1),
            ('## Active Threads\n    ```\n### Real\n', 1),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.count(text), expected)

    def test_non_fenced_legacy_and_section_semantics_are_preserved(self):
        cases = [
            ('**Status:** 🟢 Active: first\n**Durum:** aktif: second\n', 2),
            ('## Active Threads\n**Status:** active\n**Durum:** 🟢 Aktif\n', 2),
            ('## Active Threads\n### Real\n**Status:** closed\n**Status:** active\n', 1),
            ('## Active Threads\n### First\n## Other\n### Second\n', 2),
            ('### First\n## Closed Threads\n### Historical\n', 1),
            # Existing empty-active fallback semantics are intentionally unchanged.
            ('## Active Threads\n## Closed Threads\n**Status:** active\n', 1),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.count(text), expected)

    def test_installed_json_and_human_do_not_recommend_false_compaction(self):
        text = '# Threads\n## Active Threads\n```md\n'
        text += ''.join('### Example %d\n' % n for n in range(6))
        text += '```\n### Real topic\n**Status:** active\n'
        self.source.write_text(text, encoding='utf-8')
        env = isolated_env(self.root / 'home')
        # XDG directories must also remain inside this synthetic environment.
        for key, directory in (('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                               ('XDG_CACHE_HOME', 'cache'), ('XDG_STATE_HOME', 'xdg-state')):
            env[key] = str(self.root / directory)
        installed = install(self.vault, self.state, env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        before = {p.relative_to(self.vault): p.read_bytes()
                  for p in self.vault.rglob('*') if p.is_file()}
        for output in ('--json', '--human'):
            result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'triage', output],
                                    cwd=self.vault, env=env, capture_output=True,
                                    text=True, encoding='utf-8', timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('companion-compact', result.stdout)
            if output == '--json':
                report = json.loads(result.stdout)
                self.assertEqual(report['companion']['active_threads'], 1)
                self.assertEqual(report['threads']['open_count'], 1)
            else:
                self.assertIn('Acik is parcaciklari: 1 thread', result.stdout)
        self.assertEqual(before, {p.relative_to(self.vault): p.read_bytes()
                                 for p in self.vault.rglob('*') if p.is_file()})


if __name__ == '__main__':
    unittest.main()
