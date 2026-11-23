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
    def test_frontmatter_reader_never_consumes_unbounded_body(self):
        import io
        from unittest.mock import Mock
        limit = 1024 * 1024
        for separator in ('\n', '\x85', '\u2028', '\u2029', '\v'):
            for visibility in ('private', 'public', 'internal'):
                with self.subTest(separator=separator, visibility=visibility):
                    reads = []
                    class BoundedReader(io.StringIO):
                        def readline(self, size=-1):
                            self_check.assertGreater(size, 0)
                            self_check.assertLessEqual(size, limit + 1)
                            value = super().readline(size)
                            reads.append(len(value))
                            return value
                    self_check = self
                    text = separator.join(('---', 'visibility: ' + visibility, '---', '')) + 'x' * (2 * limit)
                    path = Mock()
                    path.open.return_value = BoundedReader(text)
                    self.assertEqual(triage_module._scan_front_values(path, ('visibility',)),
                                     {'visibility': visibility})
                    self.assertLessEqual(sum(reads), limit + 1)

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

    def thread_source(self, text, directory='Personal Echo', marker=False):
        target = self.vault / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / 'Core.md').write_text('# Synthetic identity\n', encoding='utf-8')
        source = target / 'Threads.md'
        source.write_text(text, encoding='utf-8')
        if marker:
            (self.state / 'companion-bootstrap.json').write_text(json.dumps({'directory': directory}), encoding='utf-8')
        return source

    def test_thread_dates_are_latest_valid_iso_mentions_never_mtime(self):
        source = self.thread_source('# Threads\n## Active Threads\n'
            '### Old topic (2026-09-01)\nUpdated 2026-09-27. Invalid 2026-99-01.\n'
            '### Fresh 2026-10-11\n##   \nOld reference 2020-01-01.\n'
            '### Undated\n28 Ekim; invalid 2026-02-30 and 12026-10-01.\n'
            '### Future 2027-01-01\n'
            '## Closed Threads\n### Closed 2020-01-01\n')
        before = source.read_bytes()
        self.set_age(source, 1000)
        report = triage_module.scan_stale_threads(self.vault, self.now, stale_days=14, state=self.state)
        self.assertEqual((report['open_count'], report['stale_count'], report['undated_count']), (4, 1, 1))
        self.assertEqual(report['stale_threads'][0]['last_date'], '2026-09-27')
        self.assertEqual(report['stale_threads'][0]['title'], 'Old topic (2026-09-01)')
        self.assertIsNone(report['undated_threads'][0]['age_days'])
        self.assertIsNone(report['undated_threads'][0]['last_date'])
        self.set_age(source, 0)
        self.assertEqual(report, triage_module.scan_stale_threads(self.vault, self.now, state=self.state))
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(triage_module.scan_stale_threads(self.vault, self.now, stale_days=16, state=self.state)['stale_count'], 0)

    def test_thread_parser_ignores_fences_and_closed_sections(self):
        self.thread_source('# Threads\n## Açık konular\n### Real 2026-09-01\n'
            '```md\n### Fake 2026-10-12\n## Closed Threads\n```\n'
            '~~~~text\n### Fake too 2026-10-12\n~~~\n2026-10-12\n~~~~\n'
            '## Kapalı konular\n### Private history 2020-01-01\n'
            '## ActiveAgain\n### Not an active section 2020-01-01\n'
            '## Aktif konular\n### Second\n```inline```\nUpdated 2026-09-02\n')
        report = triage_module.scan_stale_threads(self.vault, self.now, state=self.state)
        self.assertEqual(report['open_count'], 2)
        self.assertEqual([r['last_date'] for r in report['stale_threads']], ['2026-09-01', '2026-09-02'])

    def test_bullet_threads_counting_and_parsing_issue_783(self):
        # Test counting top-level list items when no ### headings exist, respecting active sections, ignoring - [x], counting - [ ] and plain bullets, merging nested sub-bullets into body, mixed ### + bullets counting ONLY ###, and fenced code blocks ignored.
        content = (
            '# Threads\n'
            '## Active Threads\n'
            '- [x] Closed task 2026-10-01\n'
            '- [ ] Open task with checkbox 2026-10-02\n'
            '- **Örnek konu:** Açık iş 2026-10-03\n'
            '  - Nested sub-bullet detail 2026-10-04\n'
            '  - Another detail line\n'
            '```markdown\n'
            '- [ ] Fenced bullet should be ignored 2026-10-05\n'
            '```\n'
            '## Closed / Kapananlar\n'
            '- Closed section bullet 2026-10-06\n'
        )
        source = self.thread_source(content, 'Personal Echo', marker=True)
        report = triage_module.scan_stale_threads(self.vault, self.now, state=self.state)
        comp_report = triage_module.scan_companion(self.vault, state=self.state, now=self.now)

        # Should count exactly 2 threads (- [ ] and - **Örnek konu:**), ignoring - [x] and closed section
        self.assertEqual(report['open_count'], 2)
        self.assertEqual(comp_report['active_threads'], 2)

        # Test mixed ### headings + bullets: should count ONLY ### headings
        content_mixed = (
            '# Threads\n'
            '## Active Threads\n'
            '- Unindented bullet 2026-10-01\n'
            '### Heading thread 2020-01-01\n'
        )
        self.thread_source(content_mixed, 'Personal Echo', marker=True)
        report_mixed = triage_module.scan_stale_threads(self.vault, self.now, state=self.state)
        comp_report_mixed = triage_module.scan_companion(self.vault, state=self.state, now=self.now)
        self.assertEqual(report_mixed['open_count'], 1)
        self.assertEqual(report_mixed['stale_threads'][0]['title'], 'Heading thread 2020-01-01')
        self.assertEqual(comp_report_mixed['active_threads'], 1)

    def test_completed_top_level_bullet_does_not_refresh_previous_open_topic(self):
        for marker in ('-', '*', '+', '1.', '2)'):
            for checked in ('x', 'X'):
                with self.subTest(marker=marker, checked=checked):
                    source = self.thread_source(
                        '# Threads\n## Active Threads\n'
                        '- Open old topic 2026-09-01\n'
                        f'{marker} [{checked}] Finished topic 2026-10-11\n'
                        '  Finished detail 2026-10-12\n'
                        '\nFinished continuation 2026-10-12\n', marker=True)
                    before = source.read_bytes()
                    report = triage_module.scan_stale_threads(self.vault, self.now, state=self.state)
                    self.assertEqual((report['open_count'], report['stale_count']), (1, 1))
                    self.assertEqual(report['stale_threads'][0]['last_date'], '2026-09-01')
                    self.assertEqual(report['stale_threads'][0]['title'], 'Open old topic 2026-09-01')
                    self.assertEqual(triage_module.scan_companion(
                        self.vault, state=self.state, now=self.now)['active_threads'], 1)
                    self.assertEqual(source.read_bytes(), before)

    def test_completed_bullets_keep_adjacent_open_topics_separate(self):
        self.thread_source(
            '# Threads\n## Active Threads\n'
            '- [x] Finished first 2026-10-12\n'
            '  First finished detail 2026-10-12\n'
            '- Undated open topic\n'
            '- [X] Finished between topics 2026-10-12\n'
            '  Finished detail 2026-10-12\n'
            '- [ ] Other open topic 2026-09-02\n'
            '  Open detail 2026-09-03\n'
            '- [x]\n'
            '  Final finished detail 2026-10-12\n', marker=True)
        report = triage_module.scan_stale_threads(self.vault, self.now, state=self.state)
        self.assertEqual((report['open_count'], report['stale_count'], report['undated_count']), (2, 1, 1))
        self.assertEqual(report['undated_threads'][0]['title'], 'Undated open topic')
        self.assertEqual(report['stale_threads'][0]['last_date'], '2026-09-03')
        self.assertEqual(triage_module.scan_companion(
            self.vault, state=self.state, now=self.now)['active_threads'], 2)

    def test_nested_completed_bullet_remains_part_of_its_open_topic(self):
        self.thread_source(
            '# Threads\n## Active Threads\n'
            '- Open topic 2026-09-01\n'
            '  - [x] Nested completed step 2026-09-03\n'
            '  Remaining detail 2026-09-04\n', marker=True)
        report = triage_module.scan_stale_threads(self.vault, self.now, state=self.state)
        self.assertEqual((report['open_count'], report['stale_count']), (1, 1))
        self.assertEqual(report['stale_threads'][0]['last_date'], '2026-09-04')

    def test_bullet_section_boundaries_do_not_refresh_previous_topic(self):
        source = self.thread_source(
            '# Threads\n## Active Threads\n- Old open 2026-09-01\n'
            '## Closed Threads\n- Closed 2026-10-12\n'
            '## Active Threads\nSection reviewed 2026-10-11\n'
            '- Another open 2026-09-02\n', marker=True)
        before = (source.read_bytes(), source.stat().st_mtime_ns)
        report = triage_module.scan_stale_threads(self.vault, self.now, state=self.state)
        self.assertEqual((report['open_count'], report['stale_count']), (2, 2))
        self.assertEqual([row['last_date'] for row in report['stale_threads']],
                         ['2026-09-01', '2026-09-02'])
        self.assertEqual(triage_module.scan_companion(
            self.vault, self.state, self.now)['active_threads'], 2)
        self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), before)

    def test_bullet_counters_share_active_section_gates(self):
        for active in ('Active Threads', 'Open topics', 'Aktif konular', 'Açık konular'):
            for closed in ('Closed Threads', 'Kapalı konular', 'Kapananlar', 'Other'):
                with self.subTest(active=active, closed=closed):
                    self.thread_source(
                        f'# Threads\n## {active}\n- Open 2026-09-01\n'
                        f'## {closed}\n- Not open 2026-10-12\n', marker=True)
                    report = triage_module.scan_stale_threads(self.vault, self.now, state=self.state)
                    self.assertEqual((report['open_count'], report['stale_count']), (1, 1))
                    self.assertEqual(triage_module.scan_companion(
                        self.vault, self.state, self.now)['active_threads'], 1)

    def test_companion_legacy_status_records_take_precedence_over_action_bullets(self):
        self.thread_source(
            '# Threads\n## Active Threads\n**First topic**\n**Status:** active\n'
            '- Next action: email contact\n**Second topic**\n**Durum:** 🟢 Aktif\n',
            marker=True)
        self.assertEqual(triage_module.scan_companion(
            self.vault, self.state, self.now)['active_threads'], 2)

    def test_companion_bullet_fallback_excludes_frontmatter(self):
        source = self.thread_source(
            '---\ntags:\n- alpha\n- beta\n---\n# Threads\nNothing open.\n', marker=True)
        before = (source.read_bytes(), source.stat().st_mtime_ns)
        self.assertEqual(triage_module.scan_companion(
            self.vault, self.state, self.now)['active_threads'], 0)
        self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), before)

    def test_thread_heading_empty_after_folding_does_not_abort_scan(self):
        self.thread_source('# Threads\n## Active Threads\n### Before 2020-01-01\n'
                           '## \u0307 \u0307\n### Unknown section 2020-01-01\n'
                           '## Active Threads\n### After 2020-01-01\n')
        report = triage_module.scan_stale_threads(self.vault, self.now, state=self.state)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['open_count'], 2)
        self.assertEqual([row['title'] for row in report['stale_threads']],
                         ['Before 2020-01-01', 'After 2020-01-01'])

    def test_thread_scan_respects_configured_identity_and_privacy(self):
        source = self.thread_source('# Threads\n### Configured 2026-09-01\n', 'Custom Memory', marker=True)
        self.thread_source('# Threads\n### Other 2026-09-01\n')
        report = triage_module.scan_stale_threads(self.vault, self.now, state=self.state)
        self.assertEqual(report['stale_threads'][0]['path'], 'Custom Memory/Threads.md')
        for front in ('visibility: private', 'trust: untrusted', 'trusted: false'):
            source.write_text('---\n' + front + '\n---\n# Threads\n### SECRET 2020-01-01\n', encoding='utf-8')
            self.assertEqual(triage_module.scan_stale_threads(self.vault, self.now, state=self.state)['open_count'], 0)
            for padding in ('', 'description: ' + 'x' * 5000 + '\n'):
                source.write_text('\ufeff---\n' + padding + front + '\n---\n# Threads\n### SECRET 2020-01-01\n', encoding='utf-8')
                private_report = triage_module.scan_stale_threads(self.vault, self.now, state=self.state)
                self.assertEqual(private_report['status'], 'unavailable')
                self.assertNotIn('SECRET', json.dumps(private_report))
        source.write_text('# Threads\n### SECRET 2020-01-01\n', encoding='utf-8')
        (self.state / 'index.json').write_text(json.dumps({'schema': 1, 'excluded_dirs': ['Custom Memory']}), encoding='utf-8')
        self.assertEqual(triage_module.scan_stale_threads(self.vault, self.now, state=self.state)['open_count'], 0)

    def test_thread_scan_rejects_unsafe_markers_ambiguity_and_symlinks(self):
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (outside / 'Threads.md').write_text('# Threads\n### SECRET 2020-01-01\n', encoding='utf-8')
        marker = self.state / 'companion-bootstrap.json'
        for value in ('../outside', str(outside), 'C:/outside', '', ['outside']):
            marker.write_text(json.dumps({'directory': value}), encoding='utf-8')
            report = triage_module.scan_stale_threads(self.vault, self.now, state=self.state)
            self.assertEqual(report['open_count'], 0)
            self.assertEqual(report['status'], 'unavailable')
        marker.unlink()
        source = self.thread_source('# Threads\n### First 2020-01-01\n')
        self.thread_source('# Threads\n### Second 2020-01-01\n', 'Other Echo')
        report = triage_module.scan_stale_threads(self.vault, self.now, state=self.state)
        self.assertEqual(report['status'], 'unavailable')
        self.assertEqual(report['reason'], 'ambiguous_directory')
        try:
            (self.vault / 'linked').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('symlinks unavailable: ' + str(exc))
        marker.write_text(json.dumps({'directory': 'linked'}), encoding='utf-8')
        self.assertEqual(triage_module.scan_stale_threads(self.vault, self.now, state=self.state)['open_count'], 0)
        source.unlink()
        source.symlink_to(outside / 'Threads.md')
        marker.write_text(json.dumps({'directory': 'Personal Echo'}), encoding='utf-8')
        self.assertEqual(triage_module.scan_stale_threads(self.vault, self.now, state=self.state)['open_count'], 0)

    def test_installed_triage_cli_reports_threads_without_source_writes(self):
        vault = Path(self.tmp.name) / 'installed'
        state = Path(self.tmp.name) / 'installed-state'
        home = Path(self.tmp.name) / 'isolated-home'
        home.mkdir()
        folder = vault / 'Personal Echo'
        folder.mkdir(parents=True)
        (folder / 'Core.md').write_text('# Identity\n', encoding='utf-8')
        source = folder / 'Threads.md'
        source.write_text('# Threads\n## Active Threads\n### Stale 2000-01-01\n### Undated\n', encoding='utf-8')
        env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                            LOCALAPPDATA=str(home / 'local'), PYTHONDONTWRITEBYTECODE='1')
        installed = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault', str(vault),
                                    '--state', str(state)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        before = {p.relative_to(vault): p.read_bytes() for p in vault.rglob('*') if p.is_file()}
        command = [sys.executable, str(vault / 'beyin.py'), 'triage', '--stale-days', '7']
        result = subprocess.run(command, cwd=vault, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['schema'], 1)
        self.assertEqual(report['threads']['stale_days_threshold'], 7)
        self.assertEqual(report['threads']['stale_count'], 1)
        human = subprocess.run(command + ['--human'], cwd=vault, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn('[Acik Konular / Threads]', human.stdout)
        self.assertIn('Undated', human.stdout)
        self.assertEqual(before, {p.relative_to(vault): p.read_bytes() for p in vault.rglob('*') if p.is_file()})

    def test_yakala_recommendation_requires_skill_file(self):
        inbox = self.vault / 'inbox'
        inbox.mkdir()
        (inbox / 'pending.md').write_text('# Pending\n', encoding='utf-8')
        skill = self.vault / '.agents/skills/beyin-yakala'
        skill.mkdir(parents=True)
        report = triage_module.scan_inbox(self.vault, self.now, self.state)
        self.assertFalse(report['has_yakala'])
        recs = triage_module.generate_recommendations({'count': 1}, {}, {'digest_days': 7}, {}, vault=self.vault)
        self.assertNotIn('beyin-yakala', ' '.join(recs))
        (skill / 'SKILL.md').write_text('# Yakala\n', encoding='utf-8')
        self.assertTrue(triage_module.scan_inbox(self.vault, self.now, self.state)['has_yakala'])
        recs = triage_module.generate_recommendations({'count': 1}, {}, {'digest_days': 7}, {}, vault=self.vault)
        self.assertIn('beyin-yakala', ' '.join(recs))

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

    def test_triage_terminal_statuses_unification_issue_761(self):
        # Issue #761: bitti, cozuldu, resolved, canceled must be recognized as terminal in triage
        tasks = self.vault / 'tasks_761'
        tasks.mkdir(parents=True, exist_ok=True)
        for name, status in [('t_bitti.md', 'bitti'), ('t_cozuldu.md', 'cozuldu'),
                             ('t_resolved.md', 'resolved'), ('t_canceled.md', 'canceled')]:
            f = tasks / name
            f.write_text(f'---\ntitle: {name}\nstatus: {status}\n---\nBody\n', encoding='utf-8')
            self.set_age(f, 30)
        report = triage_module.scan_stale_tasks(self.vault, self.now, stale_days=14)
        open_titles = [t['title'] for t in report.get('stale_tasks', []) + report.get('undated_tasks', [])]
        for name in ['t_bitti.md', 't_cozuldu.md', 't_resolved.md', 't_canceled.md']:
            self.assertNotIn(name, open_titles)

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
        old_note.write_text('---\ntitle: Eski Bilgi\ncreated: 2026-07-01\nupdated: 2026-10-11\n---\n# Eski\n', encoding='utf-8')
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
        report = triage_module.triage(self.vault, now=self.now)
        human = triage_module.format_human(report)
        self.assertIn('Tarih onceligi: created, yoksa updated, yoksa mtime', human)
        self.assertNotIn('Uretilen/guncellenen', human)
        self.assertFalse(any('uretildi/guncellendi' in item for item in report['recommendations']))



if __name__ == '__main__':
    unittest.main()
