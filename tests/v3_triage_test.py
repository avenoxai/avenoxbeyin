"""Unit tests for triage and digestion routines (Issue #288 Item 3)."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_triage as triage_module


class TriageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-triage-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.state = Path(self.tmp.name) / 'state'
        self.state.mkdir()
        self.now = time.time()

    def set_age(self, path: Path, days: float):
        t = self.now - (days * 86400)
        os.utime(path, (t, t))

    def test_inbox_scanning_and_oldest(self):
        inbox = self.vault / '000-Inbox'
        inbox.mkdir(parents=True)

        f1 = inbox / 'note1.md'
        f1.write_text('Not 1', encoding='utf-8')
        self.set_age(f1, 2)

        f2 = inbox / 'article.pdf'
        f2.write_bytes(b'%PDF-test')
        self.set_age(f2, 10)

        report = triage_module.scan_inbox(self.vault, self.now)
        self.assertEqual(report['count'], 2)
        self.assertEqual(report['oldest']['name'], 'article.pdf')
        self.assertEqual(report['oldest']['kind'], 'media')
        self.assertAlmostEqual(report['oldest']['age_days'], 10.0, places=1)

    def test_stale_tasks_detection(self):
        tasks = self.vault / 'tasks'
        tasks.mkdir(parents=True)

        # 1. Fresh task (3 days old)
        t_fresh = tasks / 'fresh.md'
        t_fresh.write_text('# Taze Görev\nstatus: open\n- [ ] Yapılacak\n', encoding='utf-8')
        self.set_age(t_fresh, 3)

        # 2. Stale task (20 days old)
        t_stale = tasks / 'stale.md'
        t_stale.write_text('# Hareketsiz Görev\nstatus: open\n- [ ] Eski iş\n', encoding='utf-8')
        self.set_age(t_stale, 20)

        # 3. Completed task (25 days old, but closed)
        t_done = tasks / 'done.md'
        t_done.write_text('# Biten Görev\nstatus: completed\n- [x] Bitti\n', encoding='utf-8')
        self.set_age(t_done, 25)

        report = triage_module.scan_stale_tasks(self.vault, self.now, stale_days=14)
        self.assertEqual(report['open_count'], 2)
        self.assertEqual(report['stale_count'], 1)
        self.assertEqual(report['stale_tasks'][0]['title'], 'Hareketsiz Görev')

    def test_knowledge_digest_scanning(self):
        knowledge = self.vault / 'knowledge' / 'concepts'
        knowledge.mkdir(parents=True)

        k_recent = knowledge / 'sqlite-caching.md'
        k_recent.write_text('# SQLite Önbellek Mimarisi\nDetaylar...\n', encoding='utf-8')
        self.set_age(k_recent, 3)

        k_old = knowledge / 'legacy-db.md'
        k_old.write_text('# Eski DB\nDetaylar...\n', encoding='utf-8')
        self.set_age(k_old, 30)

        report = triage_module.scan_knowledge_digest(self.vault, self.now, digest_days=7)
        self.assertEqual(report['count'], 1)
        self.assertEqual(report['items'][0]['title'], 'SQLite Önbellek Mimarisi')

    def test_companion_status_parsing(self):
        comp = self.vault / '🔮 850-Companion'
        comp.mkdir(parents=True)

        (comp / 'Kurallar.md').write_text(
            '# Kurallar\n\n- Kural 1\n- Kural 2\n- Kural 3\n', encoding='utf-8')
        (comp / 'Threads.md').write_text(
            '## Threads\n### T1\n**Status:** active\n### T2\n**Status:** active\n', encoding='utf-8')
        (comp / 'Last-Session.md').write_text(
            '# Son\n## 2026-10-12 06:30 · deneme · abcd1234\nDevir.\n', encoding='utf-8')

        report = triage_module.scan_companion(self.vault)
        self.assertEqual(report['rules_count'], 3)
        self.assertEqual(report['active_threads'], 2)
        self.assertEqual(report['last_session'], '2026-10-12 06:30')

    def test_triage_full_and_format_human(self):
        inbox = self.vault / '000-Inbox'
        inbox.mkdir(parents=True)
        f = inbox / 'quick-note.md'
        f.write_text('Fikir.', encoding='utf-8')
        self.set_age(f, 5)

        full_report = triage_module.triage(self.vault, stale_days=14, digest_days=7, now=self.now)
        self.assertEqual(full_report['schema'], 1)
        self.assertEqual(full_report['inbox']['count'], 1)

        human_text = triage_module.format_human(full_report)
        self.assertIn("=== İkinci Beyin Sindirim ve Durum Raporu (Triage) ===", human_text)
        self.assertIn("[Gelen Kutusu / Yakalananlar]", human_text)
        self.assertIn("Bekleyen kaynak: 1", human_text)
        self.assertIn("[Sindirim Onerileri]", human_text)

    def test_cli_triage_subcommand(self):
        cmd = [sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
               '--vault', str(self.vault), '--state', str(self.state), 'triage']
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data.get('schema'), 1)
        self.assertIn('inbox', data)
        self.assertIn('recommendations', data)


if __name__ == '__main__':
    unittest.main()
