"""Unreadable task sources must not look like a complete, empty task scan."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.v3_package_helpers import install, isolated_env, snapshot

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_triage as triage


class TaskScanErrorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='triage-task-errors-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()
        self.state.mkdir()
        self.now = datetime(2026, 11, 11, tzinfo=timezone.utc).timestamp()

    def note(self, relative='tasks/readable.md', front=''):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\nstatus: open\nupdated_at: 2020-01-01\n' + front +
                        '---\n# Synthetic task\n', encoding='utf-8')
        return path

    def scan(self):
        return triage.scan_stale_tasks(self.vault, self.now, state=self.state)

    def assert_partial(self, report, expected=0):
        self.assertEqual(report.get('status'), 'partial', report)
        self.assertEqual(report.get('reason'), 'task_scan_incomplete')
        self.assertEqual(report.get('errors'), ['PermissionError'])
        self.assertEqual(report['open_count'], expected)
        self.assertEqual(report['stale_count'], expected)
        human = triage.format_human({'tasks': report})
        self.assertIn('Gorev taramasi eksik', human)
        self.assertIn('Taranabilen acik gorev:', human)
        self.assertNotIn('Acik gorev: 0, Hareketsiz: 0', human)
        self.assertNotIn('SECRET-PATH', json.dumps(report) + human)
        recs = triage.generate_recommendations({}, report, {'digest_days': 7}, {})
        self.assertIn('Gorev taramasi eksik', ' '.join(recs))

    def test_healthy_and_absent_task_shape_is_unchanged(self):
        expected = {'open_count': 0, 'stale_count': 0, 'undated_count': 0,
                    'stale_days_threshold': 14, 'stale_tasks': [], 'undated_tasks': []}
        self.assertEqual(self.scan(), expected)
        self.note()
        report = self.scan()
        self.assertEqual(report['open_count'], 1)
        self.assertNotIn('status', report)

    def test_frontmatter_read_error_keeps_readable_tasks(self):
        blocked = self.note('tasks/blocked.md')
        self.note()
        original = Path.open
        def read(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError('SECRET-PATH')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', read):
            self.assert_partial(self.scan(), 1)

    def test_body_read_error_does_not_look_like_an_empty_scan(self):
        blocked = self.note()
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError('SECRET-PATH')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            self.assert_partial(self.scan())

    def test_date_read_error_does_not_label_task_undated(self):
        blocked = self.note()
        original = Path.open
        calls = 0
        def read(path, *args, **kwargs):
            nonlocal calls
            if path == blocked:
                calls += 1
                if calls == 3:
                    raise PermissionError('SECRET-PATH')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', read):
            report = self.scan()
        self.assertEqual(calls, 3)
        self.assert_partial(report)
        self.assertEqual(report['undated_count'], 0)

    def test_root_discovery_failure_is_visible(self):
        original = Path.iterdir
        def entries(path):
            if path == self.vault:
                raise PermissionError('SECRET-PATH')
            return original(path)
        with patch.object(Path, 'iterdir', entries):
            self.assert_partial(self.scan())

    def test_walk_error_is_visible_without_raw_path(self):
        self.note()
        def walk(path, **kwargs):
            if kwargs.get('onerror'):
                kwargs['onerror'](PermissionError('SECRET-PATH'))
            return iter(())
        with patch.object(triage.os, 'walk', walk):
            self.assert_partial(self.scan())

    def test_discovery_stat_and_resolve_errors_are_visible(self):
        target = self.note('project-tasks/hidden.md').parent
        for method in ('lstat', 'resolve'):
            with self.subTest(method=method):
                original = getattr(Path, method)
                def fail(path, *args, **kwargs):
                    if path == target:
                        raise PermissionError('SECRET-PATH')
                    return original(path, *args, **kwargs)
                with patch.object(Path, method, fail):
                    self.assert_partial(self.scan())
        # A conventional task folder is probed again after top-level discovery.
        target.rename(self.vault / 'tasks')
        target = self.vault / 'tasks'
        original = Path.lstat
        with patch.object(Path, 'lstat', fail):
            self.assert_partial(self.scan())

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission fixture')
    def test_real_discovery_without_search_permission(self):
        self.note('project-tasks/hidden.md')
        self.vault.chmod(0o400)
        try:
            try:
                (self.vault / 'project-tasks').stat()
            except PermissionError:
                pass
            else:
                self.skipTest('Current user can search chmod(0400) directory')
            self.assert_partial(self.scan())
        finally:
            self.vault.chmod(0o700)

    def test_intentionally_excluded_sources_are_not_errors(self):
        for relative, front in (('tasks/private.md', 'visibility: private\n'),
                                ('tasks/untrusted.md', 'trust: untrusted\n'),
                                ('tasks/malformed.md', 'visibility: [private]\n'),
                                ('tasks/vendor/hidden.md', ''),
                                ('tasks/Şifreler/hidden.md', '')):
            self.note(relative, front)
        (self.state / 'index.json').write_text(
            '{"schema":1,"excluded_dirs":["vendor"]}', encoding='utf-8')
        report = self.scan()
        self.assertEqual(report['open_count'], 0)
        self.assertNotIn('status', report)

    def test_excluded_directories_and_non_markdown_are_not_probed(self):
        hidden = self.note('tasks/vendor/hidden.md')
        non_markdown = self.note('tasks/attachment.txt')
        (self.state / 'index.json').write_text(
            '{"schema":1,"excluded_dirs":["vendor"]}', encoding='utf-8')
        original = Path.lstat
        def fail(path, *args, **kwargs):
            if path in (hidden.parent, hidden, non_markdown):
                raise PermissionError('SECRET-PATH')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', fail):
            report = self.scan()
        self.assertEqual(report['open_count'], 0)
        self.assertNotIn('status', report)
        (self.vault / 'tasks').rename(self.vault / 'project-tasks')
        (self.state / 'index.json').write_text(
            '{"schema":1,"excluded_dirs":["project-tasks"]}', encoding='utf-8')
        def excluded(path, *args, **kwargs):
            if path == self.vault / 'project-tasks':
                raise PermissionError('SECRET-PATH')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', excluded):
            report = self.scan()
        self.assertEqual(report['open_count'], 0)
        self.assertNotIn('status', report)


@unittest.skipUnless(os.name == 'posix', 'POSIX permission fixture')
class InstalledTaskScanErrorsTest(unittest.TestCase):
    setUp = TaskScanErrorsTest.setUp
    note = TaskScanErrorsTest.note
    assert_partial = TaskScanErrorsTest.assert_partial

    def test_installed_unreadable_file_and_directory(self):
        env = isolated_env(self.root / 'home')
        result = install(self.vault, self.state, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        command = [sys.executable, str(self.vault / 'beyin.py'), 'triage']
        warmed = subprocess.run(command, cwd=self.vault, env=env, capture_output=True, timeout=30)
        self.assertEqual(warmed.returncode, 0, warmed.stderr)
        self.note()
        blocked = self.note('tasks/blocked/hidden.md')
        before_vault, before_state = snapshot(self.vault), snapshot(self.state)
        before_times = {p: p.stat().st_mtime_ns for p in self.vault.rglob('*.md')}
        for target, mode, expected in ((blocked, 0, 1), (blocked.parent, 0, 1),
                                       (blocked.parent.parent, 0o400, 0)):
            with self.subTest(kind='file' if target == blocked else 'directory'):
                target.chmod(mode)
                try:
                    try:
                        blocked.read_bytes()
                    except PermissionError:
                        pass
                    else:
                        self.skipTest('Current user can read chmod(0) sources')
                    reports = []
                    for mode in ([], ['--json'], ['--human']):
                        result = subprocess.run(command + mode, cwd=self.vault, env=env,
                                                capture_output=True, text=True, encoding='utf-8', timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stderr, '')
                        if mode == ['--human']:
                            self.assertIn('Gorev taramasi eksik', result.stdout)
                            self.assertIn(f'Taranabilen acik gorev: {expected}', result.stdout)
                        else:
                            report = json.loads(result.stdout)['tasks']
                            self.assert_partial(report, expected)
                            self.assertNotIn('blocked', json.dumps(report))
                            reports.append(report)
                    self.assertEqual(reports[0], reports[1])
                finally:
                    target.chmod(0o600 if target == blocked else 0o700)
                self.assertEqual(snapshot(self.vault), before_vault)
                self.assertEqual(snapshot(self.state), before_state)
                self.assertEqual({p: p.stat().st_mtime_ns for p in before_times}, before_times)


if __name__ == '__main__':
    unittest.main()
