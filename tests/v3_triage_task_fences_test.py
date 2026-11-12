"""Task triage reads live status and headings, not fenced Markdown examples."""
from datetime import datetime, timezone
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


class TaskFenceTriageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='triage-task-fences-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        (self.vault / 'tasks').mkdir(parents=True)
        self.state.mkdir()
        self.source = self.vault / 'tasks' / 'open-task.md'
        self.now = datetime(2026, 11, 11, tzinfo=timezone.utc).timestamp()

    def scan(self, body, header='status: open\nupdated_at: 2026-09-01', newline='\n'):
        text = '---\n' + header + '\n---\n' + body
        self.source.write_bytes(text.replace('\n', newline).encode('utf-8'))
        before = self.source.read_bytes(), self.source.stat().st_mtime_ns
        report = triage.scan_stale_tasks(self.vault, self.now, state=self.state)
        self.assertEqual((self.source.read_bytes(), self.source.stat().st_mtime_ns), before)
        return report

    def test_fenced_terminal_status_never_closes_a_live_task(self):
        for mark in ('`', '~'):
            for newline in ('\n', '\r\n'):
                for indent in ('', ' ', '   '):
                    for false_close in (mark * 3, ('~' if mark == '`' else '`') * 4,
                                        mark * 4 + ' trailing', '    ' + mark * 4):
                        with self.subTest(mark=mark, newline=newline, indent=indent, false_close=false_close):
                            body = (indent + mark * 4 + 'yaml\n# Example heading\n' + false_close
                                    + '\nstatus: done\n' + indent + mark * 5 + ' \t\n# Real task\n')
                            report = self.scan(body, newline=newline)
                            self.assertEqual((report['open_count'], report['stale_count']), (1, 1))
                            self.assertEqual(report['stale_tasks'][0]['title'], 'Real task')
                            self.assertEqual(report['stale_tasks'][0]['status'], 'open')

    def test_unclosed_fences_cannot_hide_tasks_or_supply_titles(self):
        for mark in ('```', '~~~'):
            with self.subTest(mark=mark):
                report = self.scan(mark + 'yaml\n# Example heading\nstatus: closed\n')
                self.assertEqual(report['open_count'], 1)
                self.assertEqual(report['stale_tasks'][0]['title'], 'open-task')

    def test_fenced_heading_does_not_become_a_live_task_title(self):
        for mark in ('```', '~~~'):
            with self.subTest(mark=mark):
                report = self.scan(mark + 'md\n# Example heading\n' + mark + '\n# Real task\n')
                self.assertEqual(report['stale_tasks'][0]['title'], 'Real task')
                titled = self.scan(mark + 'md\n# Example heading\n' + mark + '\n# Body heading\n',
                                  header='title: Metadata title\nstatus: waiting\nupdated_at: 2026-09-01')
                self.assertEqual(titled['stale_tasks'][0]['title'], 'Metadata title')
                self.assertEqual(titled['stale_tasks'][0]['status'], 'waiting')

    def test_real_frontmatter_and_unfenced_legacy_statuses_are_preserved(self):
        for status in ('done', 'completed', 'cancelled', 'closed', 'tamamlandi', 'iptal', 'kapandi'):
            with self.subTest(status=status):
                self.assertEqual(self.scan('# Real task\nstatus: ' + status + '\n')['open_count'], 0)
                self.assertEqual(self.scan('```yaml\nstatus: open\n```\n',
                                           header='status: ' + status)['open_count'], 0)
        # An inline backtick run and a four-space-indented run are not fenced blocks.
        for opener in ('```inline```', '    ```'):
            with self.subTest(opener=opener):
                self.assertEqual(self.scan(opener + '\nstatus: done\n')['open_count'], 0)
        report = self.scan('```yaml\nstatus: done\n```\n# Waiting task\n', header='status: blocked')
        self.assertEqual((report['open_count'], report['undated_count']), (1, 1))
        self.assertEqual(report['undated_tasks'][0]['status'], 'blocked')

    def test_installed_outputs_keep_the_real_task_and_user_note(self):
        self.scan('```yaml\n# Example heading\nstatus: done\n```\n# Real task\n')
        env = isolated_env(self.root / 'home')
        for key, directory in (('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                               ('XDG_CACHE_HOME', 'cache'), ('XDG_STATE_HOME', 'xdg-state')):
            env[key] = str(self.root / directory)
        installed = install(self.vault, self.state, env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        before = self.source.read_bytes(), self.source.stat().st_mtime_ns
        for output in (None, '--json', '--human'):
            with self.subTest(output=output):
                args = [sys.executable, str(self.vault / 'beyin.py'), 'triage']
                if output:
                    args.append(output)
                result = subprocess.run(args, cwd=self.vault, env=env, capture_output=True,
                                        text=True, encoding='utf-8', timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                if output == '--human':
                    self.assertIn('Acik gorev: 1, Hareketsiz: 1', result.stdout)
                    self.assertIn('tasks/open-task.md: Real task', result.stdout)
                else:
                    report = json.loads(result.stdout)
                    self.assertEqual((report['tasks']['open_count'], report['tasks']['stale_count']), (1, 1))
                    self.assertEqual(report['tasks']['stale_tasks'][0]['title'], 'Real task')
                self.assertNotIn('Example heading', result.stdout)
        self.assertEqual((self.source.read_bytes(), self.source.stat().st_mtime_ns), before)


if __name__ == '__main__':
    unittest.main()
