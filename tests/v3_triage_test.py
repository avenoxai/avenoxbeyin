"""Unit tests for triage and digestion routines (Issue #288 Item 3)."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE_SCRIPTS = str(ROOT / 'template/.claude/scripts')
if _TEMPLATE_SCRIPTS not in sys.path:
    sys.path.insert(0, _TEMPLATE_SCRIPTS)
_SCRIPTS = str(ROOT / 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.append(_SCRIPTS)

import beyin_v3_triage as triage_module
import beyin_v3_hygiene as hygiene_module
from beyin_entry import human_result
from tests.v3_package_helpers import inherited_env


class TriageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-triage-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = (Path(self.tmp.name) / 'vault').resolve()
        self.vault.mkdir()
        self.state = (Path(self.tmp.name) / 'state').resolve()
        self.state.mkdir()

        # Initialize mock runtime for beyin.py entry tests
        (self.vault / '.beyin-runtime.json').write_text(
            json.dumps({'state': str(self.state), 'schema': 1}), encoding='utf-8')
        (self.vault / '.beyin-version').write_text('3.10.0\n', encoding='utf-8')
        shutil.copy2(ROOT / 'scripts/beyin_entry.py', self.vault / 'beyin.py')

        claude_scripts = self.vault / '.claude' / 'scripts'
        claude_scripts.mkdir(parents=True)
        shutil.copy2(ROOT / 'scripts/beyin_v3.py', claude_scripts / 'beyin_v3_cli.py')
        for f in (ROOT / 'template/.claude/scripts').glob('*.py'):
            shutil.copy2(f, claude_scripts / f.name)

        # Reference timestamp: 2026-10-12 12:00:00 UTC
        self.now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc).timestamp()

    def set_age(self, path: Path, days: float):
        t = self.now - (days * 86400)
        os.utime(path, (t, t))

    def test_nested_sensitive_private_and_excluded_notes_are_not_reported(self):
        for folder in ('tasks', 'knowledge'):
            for suffix, front in (('Şifreler/hidden.md', ''), ('visible/private.md', 'visibility: private\n'), ('vendor/hidden.md', '')):
                target = self.vault / folder / suffix
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('---\n' + front + 'updated_at: 2026-09-01\n---\n# PRIVATE-MARKER', encoding='utf-8')
        (self.state / 'index.json').write_text(json.dumps({'schema': 1, 'excluded_dirs': ['vendor']}), encoding='utf-8')
        tasks = triage_module.scan_stale_tasks(self.vault, self.now, state=self.state)
        digest = triage_module.scan_knowledge_digest(self.vault, self.now, digest_days=100, state=self.state)
        self.assertEqual(tasks['open_count'], 0)
        self.assertEqual(digest['count'], 0)

    def test_timestamp_respects_explicit_offset(self):
        self.assertEqual(triage_module._parse_timestamp('2026-10-12T15:00:00+03:00'), self.now)
        self.assertEqual(triage_module._parse_timestamp('2026-10-12T12:00:00Z'), self.now)

    def test_inbox_scanning_with_hygiene_report_and_sync_immunity(self):
        # 1. Emoji inbox folder matching generic INBOX_WORDS
        inbox1 = self.vault / '📥 000-Inbox'
        inbox1.mkdir(parents=True)
        # Note with frontmatter date 20 days ago, but mtime set to 1 day ago (simulating git sync / pull)
        f1 = inbox1 / 'capture-old.md'
        f1.write_text('---\ncreated: 2026-09-22\ntur: note\n---\nEski not.', encoding='utf-8')
        self.set_age(f1, 1)  # mtime is 1 day ago

        # 2. Config/asset file (.json): must NOT be counted as a pending capture note
        f2_json = inbox1 / 'beyne-at-web-clipper.json'
        f2_json.write_text('{"setting": true}', encoding='utf-8')
        self.set_age(f2_json, 5)

        # 3. Processed yakala card: should be ignored
        f3 = inbox1 / 'processed.md'
        f3.write_text('---\ntur: yakala\ndurum: islendi\n---\nIslenmis kart.', encoding='utf-8')
        self.set_age(f3, 10)

        # 4. Sensitive folder: must NEVER be scanned
        secret_box = self.vault / '🔐 Kasa' / 'inbox'
        secret_box.mkdir(parents=True)
        (secret_box / 'passwords.md').write_text('---\ncreated: 2026-09-01\n---\nsecret', encoding='utf-8')

        # 5. User-configured custom inbox folder via inbox-report.json (#253)
        custom_folder = self.vault / 'Yakala'
        custom_folder.mkdir(parents=True)
        f4 = custom_folder / 'web-note.md'
        f4.write_text('---\ncreated: 2026-10-10\n---\nYeni not.', encoding='utf-8')
        self.set_age(f4, 2)
        # Write inbox-report.json configuring 'Yakala' in state
        (self.state / 'inbox-report.json').write_text(
            json.dumps({'schema': 1, 'enabled': True, 'max_items': 10, 'max_days': 7, 'folders': ['Yakala']}),
            encoding='utf-8'
        )

        report = triage_module.scan_inbox(self.vault, self.now, state=self.state)
        # Should find 1 note in Yakala (since folders setting restricts to configured folders)
        self.assertEqual(report['count'], 1)
        folder_names = [f['folder'] for f in report['folders']]
        self.assertIn('Yakala', folder_names)

        # Without state / without custom folders configured: defaults to INBOX_WORDS folders
        report_default = triage_module.scan_inbox(self.vault, self.now, state=None)
        # Only f1 in '📥 000-Inbox' should be counted (.json, processed and sensitive ignored)
        self.assertEqual(report_default['count'], 1)
        self.assertGreaterEqual(report_default['oldest_days'], 19)

    def test_stale_tasks_detection_sync_immunity_and_waiting_status(self):
        tasks = self.vault / 'tasks'
        tasks.mkdir(parents=True)

        # 1. Fresh task (3 days old)
        t_fresh = tasks / 'fresh.md'
        t_fresh.write_text('---\nstatus: active\nupdated_at: 2026-10-09\n---\n# Taze Gorev\n- [ ] Is\n', encoding='utf-8')
        self.set_age(t_fresh, 3)

        # 2. Stale task: frontmatter updated_at is 25 days ago, but file mtime is fresh (sync checkout)
        t_stale = tasks / 'stale.md'
        t_stale.write_text('---\nstatus: open\nupdated_at: 2026-09-17\n---\n# Hareketsiz Gorev\n- [ ] Eski is\n', encoding='utf-8')
        self.set_age(t_stale, 1)  # mtime 1 day ago

        # 3. Waiting task: updated 18 days ago -> should be reported as stale with status 'waiting'
        t_waiting = tasks / 'waiting.md'
        t_waiting.write_text('---\nstatus: waiting\nupdated_at: 2026-09-24\n---\n# Bekleyen Gorev\n', encoding='utf-8')
        self.set_age(t_waiting, 1)  # mtime 1 day ago

        # 4. Undated task: no frontmatter date at all, mtime set to 1 day ago
        # Must NOT invent age from mtime; listed in undated_tasks
        t_undated = tasks / 'undated.md'
        t_undated.write_text('---\nstatus: open\n---\n# Tarihsiz Gorev\n', encoding='utf-8')
        self.set_age(t_undated, 1)

        # 5. Completed task (25 days old, but closed)
        t_done = tasks / 'done.md'
        t_done.write_text('---\nstatus: completed\n---\n# Biten Gorev\n- [x] Bitti\n', encoding='utf-8')
        self.set_age(t_done, 25)

        # 6. Kapandi task (Turkish terminal status)
        t_kapandi = tasks / 'kapandi.md'
        t_kapandi.write_text('---\nstatus: kapandi\n---\n# Kapanan Gorev\n', encoding='utf-8')
        self.set_age(t_kapandi, 30)

        report = triage_module.scan_stale_tasks(self.vault, self.now, stale_days=14)
        # Open tasks: fresh, stale, waiting, undated = 4
        self.assertEqual(report['open_count'], 4)
        # Stale tasks: stale (25d) + waiting (18d) = 2
        self.assertEqual(report['stale_count'], 2)
        # Undated tasks: 1
        self.assertEqual(report['undated_count'], 1)
        self.assertEqual(report['undated_tasks'][0]['title'], 'Tarihsiz Gorev')
        self.assertIsNone(report['undated_tasks'][0]['age_days'])

        stale_titles = [t['title'] for t in report['stale_tasks']]
        self.assertIn('Hareketsiz Gorev', stale_titles)
        self.assertIn('Bekleyen Gorev', stale_titles)

        waiting_task = next(t for t in report['stale_tasks'] if t['title'] == 'Bekleyen Gorev')
        self.assertEqual(waiting_task['status'], 'waiting')
        self.assertGreaterEqual(waiting_task['age_days'], 17.0)

    def test_knowledge_digest_scanning(self):
        knowledge = self.vault / '🧠 500-Knowledge' / 'concepts'
        knowledge.mkdir(parents=True)

        k_recent = knowledge / 'sqlite-caching.md'
        k_recent.write_text('---\ntitle: SQLite Onbellek\nupdated_at: 2026-10-10\n---\nDetaylar...\n', encoding='utf-8')
        self.set_age(k_recent, 2)

        k_old = knowledge / 'legacy-db.md'
        k_old.write_text('---\ntitle: Eski DB\nupdated_at: 2026-08-01\n---\nDetaylar...\n', encoding='utf-8')
        self.set_age(k_old, 60)

        # outcomes.md in vault root
        outcomes = self.vault / 'outcomes.md'
        outcomes.write_text('# Outcomes\nSonuclar...\n', encoding='utf-8')
        self.set_age(outcomes, 4)

        report = triage_module.scan_knowledge_digest(self.vault, self.now, digest_days=7)
        self.assertEqual(report['count'], 2)
        titles = [item['title'] for item in report['items']]
        self.assertIn('SQLite Onbellek', titles)
        self.assertIn('Kazanimlar ve Sonuclar', titles)
        self.assertNotIn('Eski DB', titles)

    def test_companion_status_parsing_and_active_threads_from_headings(self):
        comp = self.vault / '🔮 850-Companion'
        comp.mkdir(parents=True)

        (comp / 'Kurallar.md').write_text(
            '# Kurallar\n\n- Kural 1\n* Kural 2\n1. Kural 3\n- Kural 4\n', encoding='utf-8')

        # Upstream template format with emoji in status line
        threads_content = (
            "# Threads\n\n"
            "Ongoing storylines that span multiple sessions.\n\n"
            "## Active Threads\n"
            "### Thread: Setting up the second brain\n"
            "**Status:** 🟢 Active: created 2026-10-01\n\n"
            "### Thread: Multi-agent coordination\n"
            "**Status:** 🟢 Active: ongoing\n\n"
            "## Closed Threads\n"
            "### Thread: Legacy migration\n"
            "**Status:** ⚪ Closed\n"
        )
        (comp / 'Threads.md').write_text(threads_content, encoding='utf-8')
        (comp / 'Last-Session.md').write_text(
            '# Son\n## 2026-10-12 06:30 · deneme · abcd1234\nDevir.\n', encoding='utf-8')

        # Presence file
        presence_dir = self.vault / '.presence'
        presence_dir.mkdir(parents=True)
        (presence_dir / 'agent-1.json').write_text(json.dumps({'heartbeat': self.now - 300}), encoding='utf-8')

        report = triage_module.scan_companion(self.vault, now=self.now)
        self.assertEqual(report['rules_count'], 4)
        # Exactly 2 active threads under ## Active Threads (closed one excluded!)
        self.assertEqual(report['active_threads'], 2)
        self.assertEqual(report['last_session'], '2026-10-12 06:30')
        self.assertEqual(report['active_presence'], 1)

    def test_triage_full_and_format_human(self):
        inbox = self.vault / '📥 000-Inbox'
        inbox.mkdir(parents=True)
        f = inbox / 'quick-note.md'
        f.write_text('---\ncreated: 2026-10-07\n---\nFikir.', encoding='utf-8')
        self.set_age(f, 5)

        full_report = triage_module.triage(self.vault, stale_days=14, digest_days=7, now=self.now)
        self.assertEqual(full_report['schema'], 1)
        self.assertEqual(full_report['inbox']['count'], 1)

        human_text = triage_module.format_human(full_report)
        self.assertIn("=== Ikinci Beyin Sindirim ve Durum Raporu (Triage) ===", human_text)
        self.assertIn("[Gelen Kutusu / Yakalananlar]", human_text)
        self.assertIn("Bekleyen kaynak: 1", human_text)
        self.assertIn("[Sindirim Onerileri]", human_text)

    def test_cli_triage_json_output(self):
        cmd = [sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
               '--vault', str(self.vault), '--state', str(self.state), 'triage']
        result = subprocess.run(cmd, env=inherited_env(), capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data.get('schema'), 1)
        self.assertIn('inbox', data)
        self.assertIn('tasks', data)
        self.assertIn('digest', data)
        self.assertIn('companion', data)
        self.assertIn('recommendations', data)

    def test_cli_human_via_beyin_entry(self):
        env = inherited_env(PYTHONIOENCODING='utf-8')
        cmd = [sys.executable, str(self.vault / 'beyin.py'), 'triage', '--human']
        result = subprocess.run(cmd, cwd=str(self.vault), env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("=== Ikinci Beyin Sindirim ve Durum Raporu (Triage) ===", result.stdout)


    def test_inbox_index_file_not_counted_as_pending_capture(self):
        """Inbox index note ('type: index' / Inbox.md) must not be counted as a pending capture note (#469)."""
        inbox = self.vault / '📥 000-Inbox'
        inbox.mkdir(parents=True, exist_ok=True)
        # Entrance index note
        index_note = inbox / 'Inbox.md'
        index_note.write_text('---\ntype: index\ncreated: 2026-09-01\n---\n# Gelen Kutusu Giris\n', encoding='utf-8')
        self.set_age(index_note, 30)

        # Empty Dump directory or valid captures
        report = triage_module.scan_inbox(self.vault, self.now, state=self.state)
        self.assertEqual(report['count'], 0)

        # When an actual capture note is present
        dump = inbox / 'Dump'
        dump.mkdir(exist_ok=True)
        capture = dump / 'kaynak.md'
        capture.write_text('---\ncreated: 2026-10-10\n---\nKaynak\n', encoding='utf-8')
        self.set_age(capture, 2)

        report2 = triage_module.scan_inbox(self.vault, self.now, state=self.state)
        self.assertEqual(report2['count'], 1)

    def test_triage_recommendations_skill_presence_branching(self):
        """When beyin-yakala is not installed, recommendation suggests note-create (#469)."""
        # 1. Without yakala installed
        recs_no_yakala = triage_module.generate_recommendations(
            inbox={'count': 2, 'has_yakala': False}, tasks={}, digest={'count': 1, 'digest_days': 7}, companion={}
        )
        self.assertIn("note-create", recs_no_yakala[0])
        self.assertNotIn("'beyin-yakala'", recs_no_yakala[0])

        # 2. With yakala installed
        recs_yakala = triage_module.generate_recommendations(
            inbox={'count': 2, 'has_yakala': True}, tasks={}, digest={'count': 1, 'digest_days': 7}, companion={}
        )
        self.assertIn("beyin-yakala", recs_yakala[0])

    def test_knowledge_digest_prioritizes_created_date_over_fresh_mtime(self):
        """Knowledge digest prioritizes frontmatter created date over rewritten mtime (#469)."""
        knowledge = self.vault / 'knowledge'
        knowledge.mkdir(parents=True, exist_ok=True)

        # Old note with created 100 days ago, but refreshed mtime (e.g. bulk ID addition or folder move)
        old_note = knowledge / 'eski-bilgi.md'
        old_note.write_text('---\ntitle: Eski Bilgi\ncreated: 2026-07-01\n---\n# Eski\n', encoding='utf-8')
        self.set_age(old_note, 1)  # mtime is 1 day ago

        # Truly new note created 2 days ago
        new_note = knowledge / 'yeni-bilgi.md'
        new_note.write_text('---\ntitle: Yeni Bilgi\ncreated: 2026-10-10\n---\n# Yeni\n', encoding='utf-8')
        self.set_age(new_note, 2)

        # Undated note with fresh mtime
        undated_note = knowledge / 'tarihsiz.md'
        undated_note.write_text('# Tarihsiz\nNot\n', encoding='utf-8')
        self.set_age(undated_note, 2)

        digest = triage_module.scan_knowledge_digest(self.vault, self.now, digest_days=7)
        # Old note with created 2026-07-01 must NOT be in 7-day digest!
        paths = [item['path'] for item in digest['items']]
        self.assertNotIn('knowledge/eski-bilgi.md', paths)
        self.assertIn('knowledge/yeni-bilgi.md', paths)
        self.assertIn('knowledge/tarihsiz.md', paths)
        self.assertEqual(digest['mtime_count'], 1)


if __name__ == '__main__':
    unittest.main()
