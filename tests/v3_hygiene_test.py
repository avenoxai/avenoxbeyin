"""MMS-derived hygiene mechanisms: word cap, folder questions, boundary, closed-task report.

Read-only for the vault; the only writes are soru cooldown markers under a state
directory that must stay outside the vault. No model calls, no network."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
MODULE = ROOT / 'template/.claude/scripts' / 'beyin_v3_hygiene.py'
spec = importlib.util.spec_from_file_location('hygiene', MODULE)
hygiene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hygiene)

COMPANION = '🔮 850-Companion'


class CapTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-cap-')
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name)
        self.state = Path(tmp.name).parent / ('state-' + tmp.name[:8])

    def test_over_cap_reported_and_frontmatter_exempt(self):
        (self.vault / 'notes').mkdir()
        long_note = self.vault / 'notes/uzun.md'
        long_note.write_text('---\ntitle: x\n---\n' + 'kelime ' * 600)
        short_note = self.vault / 'notes/kisa.md'
        short_note.write_text('# kisa\n')
        scanned = hygiene.cap_scan(self.vault)
        self.assertEqual(scanned['over'][0]['file'], 'notes/uzun.md')
        self.assertEqual(scanned['over_count'], 1)
        self.assertEqual(scanned['checked'], 2)

    def test_machine_dirs_and_archive_frontmatter_excluded(self):
        (self.vault / 'daily').mkdir()
        (self.vault / 'daily/gunluk.md').write_text('gunluk ' * 600)
        (self.vault / 'notlar').mkdir()
        (self.vault / 'notlar/ars').mkdir()
        (self.vault / 'notlar/ars/gecmis.md').write_text('---\ntype: gecmis\n---\n' + 'eski ' * 600)
        scanned = hygiene.cap_scan(self.vault)
        self.assertEqual(scanned['over'], [])

    def test_symlink_never_measured(self):
        target = self.vault / 'gercek.md'
        target.write_text('kelime ' * 600)
        link = self.vault / 'bag.md'
        link.symlink_to(target)
        self.assertIsNone(hygiene.file_over_cap(self.vault, link))


class HookWarningTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-hook-')
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name)

    def payload(self, path):
        return {'hook_event_name': 'PostToolUse', 'tool_input': {'file_path': str(path)}}

    def test_warns_past_the_cap_with_split_signal(self):
        note = self.vault / 'not.md'
        note.write_text('kelime ' * 600)
        warning = hygiene.hook_cap_warning(self.vault, self.payload(note))
        self.assertIn('not.md', warning)
        self.assertIn('Bolum SINYALI', warning)

    def test_under_cap_and_missing_path_are_silent(self):
        note = self.vault / 'not.md'
        note.write_text('# az\n')
        self.assertEqual(hygiene.hook_cap_warning(self.vault, self.payload(note)), '')
        self.assertEqual(hygiene.hook_cap_warning(self.vault,
                                                  {'hook_event_name': 'PostToolUse', 'tool_input': {}}), '')
        self.assertEqual(hygiene.hook_cap_warning(self.vault,
                                                  {'hook_event_name': 'Stop', 'tool_input': {}}), '')


class FolderQuestionsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-soru-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.state = root / 'state'
        self.state.mkdir()

    def test_empty_top_level_folder_is_asked_once_per_cooldown(self):
        (self.vault / 'Beden').mkdir()
        questions = hygiene.folder_questions(self.vault, self.state, cooldown_days=14)
        self.assertEqual(len(questions), 1)
        self.assertIn('Beden', questions[0])
        self.assertEqual(hygiene.folder_questions(self.vault, self.state, cooldown_days=14), [])

    def test_recent_file_warms_folder_and_clears_marker(self):
        (self.vault / 'Beden').mkdir()
        hygiene.folder_questions(self.vault, self.state)
        (self.vault / 'Beden/uyku.md').write_text('# not\n')
        os.utime(self.vault / 'Beden/uyku.md', (time.time(), time.time()))
        self.assertEqual(hygiene.folder_questions(self.vault, self.state, cooldown_days=14), [])

    def test_system_names_and_old_files_are_never_asked(self):
        for name in ('daily', 'knowledge', 'tasks', '.hidden-stuff'):
            (self.vault / name).mkdir()
        (self.vault / 'Makaleler').mkdir()
        stale = self.vault / 'Makaleler/eski.md'
        stale.write_text('# site\n')
        then = time.time() - 60 * 86400
        os.utime(stale, (then, then))
        questions = hygiene.folder_questions(self.vault, self.state, cooldown_days=14)
        for skipped in ('daily', 'knowledge', 'tasks', '.hidden-stuff'):
            self.assertEqual([item for item in questions if skipped in item], [])
        # Cooldown markers must live in the state, never inside the vault.
        self.assertFalse(any('stamp' in path.name for path in self.vault.rglob('*')))


class BoundaryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-bound-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vault = self.root / 'Beyin'
        self.vault.mkdir()
        self.state_dir = self.root / 'state'
        self.state_dir.mkdir()

    def test_nested_repo_and_code_folder_are_named(self):
        (self.vault / '.obsidian').mkdir()
        sub = self.vault / 'sub'
        sub.mkdir()
        (sub / '.git').mkdir()
        (self.vault / 'node_modules').mkdir()
        report = hygiene.boundary(self.vault)
        self.assertEqual(report['status'], 'attention')
        joined = ' ; '.join(report['findings'])
        self.assertIn('nested_git_repository: sub', joined)
        self.assertIn('node_modules', joined)

    def test_also_flags_parent_obsidian_and_clean_vault_is_ok(self):
        (self.root / '.obsidian').mkdir()
        report = hygiene.boundary(self.vault)
        self.assertIn('parent', report['findings'][0])
        (self.root / '.obsidian').rmdir()
        (self.vault / '.obsidian').mkdir()
        self.assertEqual(hygiene.boundary(self.vault)['status'], 'ok')

    def test_kasa_class_folders_are_excluded_and_reported(self):
        (self.vault / 'Finans').mkdir()
        (self.vault / 'Finans/kart.md').write_text('borc ' * 600)
        (self.vault / 'notes').mkdir()
        (self.vault / 'notes/normal.md').write_text('govde\n')
        report = hygiene.boundary(self.vault)
        self.assertEqual(report['sensitive_excluded'], ['Finans'])
        self.assertTrue(any(f.startswith('kasa_excluded:') for f in report['findings']))
        # Hygiene channels never read into a kasa-class folder.
        scanned = hygiene.cap_scan(self.vault)
        self.assertEqual(scanned['over'], [])
        self.assertEqual(hygiene.folder_questions(self.vault, self.state_dir, cooldown_days=14), [])
        promo = hygiene.promotion(self.vault, self.state_dir)
        self.assertFalse([entry for entry in promo['hot'] + promo['cold'] if 'Finans' in entry['folder']])


class ClosedTasksTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-kapali-')
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name)
        (self.vault / 'tasks').mkdir()

    def test_old_done_task_listed_young_one_reported_as_not(self):
        old = self.vault / 'tasks/eski.md'
        old.write_text('---\nid: e\nstatus: done\n---\n# eski\n')
        fresh = self.vault / 'tasks/yeni.md'
        fresh.write_text('---\nid: y\nstatus: done\n---\n# yeni\n')
        now = time.time()
        os.utime(old, (now - 40 * 86400, now - 40 * 86400))
        os.utime(fresh, (now, now))
        report = hygiene.closed_tasks(self.vault, days=30, now=now)
        self.assertEqual([entry['source'] for entry in report['closed']], ['tasks/eski.md'])
        self.assertEqual(report['closed'][0]['days_old'], 40)

    def test_active_tasks_never_listed_and_missing_folder_is_empty(self):
        active = self.vault / 'tasks/aktif.md'
        active.write_text('---\nid: a\nstatus: active\n---\n# aktif\n')
        then = time.time() - 90 * 86400
        os.utime(active, (then, then))
        self.assertEqual(hygiene.closed_tasks(self.vault)['closed'], [])
        self.assertEqual(hygiene.closed_tasks(self.vault / 'tasks')['closed_count'], 0)


class PromotionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-terfi-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.state = root / 'state'
        self.state.mkdir()
        (self.vault / 'Notlar').mkdir()
        (self.vault / 'Beden').mkdir()
        for name in ('Notlar/a.md', 'Notlar/b.md', 'Beden/eski.md'):
            (self.vault / name).write_text('# not ' + name + '\n')

    def log(self, entries):
        from time import time
        import json as _json
        payload = {'hook_event_name': 'PostToolUse', 'tool_input': {}}
        for path_value, days_ago in entries:
            payload['tool_input'] = {'file_path': path_value}
            hygiene.touch_log(self.state, self.vault, payload)

    def test_touch_log_growth_is_bounded_and_exclusive_to_state(self):
        for _ in range(30):
            self.log([(str(self.vault / 'Notlar/a.md'), 0)])
        self.assertFalse((self.vault / 'touch-log.tsv').exists())

    def test_hot_and_cold_report_uses_recency_permutation(self):
        now = time.time()
        for _ in range(5):
            self.log([(str(self.vault / 'Notlar/a.md'), 0)])
        os.utime(self.vault / 'Beden/eski.md', (now - 60 * 86400, now - 60 * 86400))
        os.utime(self.vault / 'Notlar/b.md', (now, now))
        report = hygiene.promotion(self.vault, self.state, days=30)
        self.assertEqual([entry['folder'] for entry in report['hot']], ['Notlar'])
        self.assertEqual([entry['folder'] for entry in report['cold']], ['Beden'])

    def test_outside_vault_and_generated_paths_never_logged(self):
        for path_value, _ in [(str(Path(tempfile.mkdtemp()) / 'ab.md'), 0),
                              (str(self.vault / 'knowledge/index.md'), 0),
                              (str(self.vault / 'secret.sqlite3'), 0)]:
            hygiene.touch_log(self.state, self.vault, {'hook_event_name': 'PostToolUse',
                                                       'tool_input': {'file_path': path_value}})
        log_path = self.state / 'touch-log.tsv'
        self.assertFalse(log_path.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
