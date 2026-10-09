"""MMS-derived hygiene mechanisms: doctor reports plus opt-in hook signals.

Read-only for the vault: no model call, no network. The only writes are the
opt-in hook artifacts — soru cooldown markers and the touch log — under a
state directory that must stay outside the vault. The doctor never writes."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unicodedata

from v3_package_helpers import inherited_env

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
MODULE = ROOT / 'template/.claude/scripts' / 'beyin_v3_hygiene.py'
spec = importlib.util.spec_from_file_location('hygiene', MODULE)
hygiene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hygiene)


class BoundaryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-bound-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vault = self.root / 'Beyin'
        self.vault.mkdir()

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

    def test_turkish_diacritic_sensitive_folders_are_reported(self):
        # Regression for the PR review: diacritic names must match, not only ASCII.
        for name in ('Finans', 'Şifre', 'Müşteriler', 'Özel', 'Maaşlar'):
            (self.vault / name).mkdir()
        report = hygiene.boundary(self.vault)
        self.assertEqual(sorted(report['sensitive_excluded']),
                         ['Finans', 'Maaşlar', 'Müşteriler', 'Özel', 'Şifre'])
        self.assertTrue(any(f.startswith('kasa_excluded:') for f in report['findings']))

    def test_sensitive_folder_with_long_note_stays_out_of_report_body(self):
        # The guarantee is name-based exclusion; nothing reads the folder contents here.
        (self.vault / 'Şifreler').mkdir()
        (self.vault / 'Şifreler/kasa.md').write_text('içerik ' * 600, encoding='utf-8')
        report = hygiene.boundary(self.vault)
        self.assertEqual(report['sensitive_excluded'], ['Şifreler'])
        self.assertFalse(any('kasa.md' in finding for finding in report['findings']))

    def test_real_folder_name_shapes_match_and_archives_are_not_kasa(self):
        # Maintainer regression: the anchored pattern missed prefixed, plural, upper-case
        # Turkish and NFD names, i.e. every folder shape the template itself uses.
        names = ['🔐 Kasa', '410-Şifreler', 'MÜŞTERİLER', 'GİZLİ', 'Kimlik Belgeleri',
                 unicodedata.normalize('NFD', 'Özel Notlar'), 'Private Notes']
        for name in names:
            self.assertTrue(hygiene.sensitive_excluded(name), name)
        for name in ('📦 900-Archive', 'Arşiv', '🏰 300-Projects', '🔮 850-Companion', 'Kasaba', 'Özellikler'):
            self.assertFalse(hygiene.sensitive_excluded(name), name)

    def test_walk_stops_at_code_trees_and_nested_repositories(self):
        (self.vault / '.obsidian').mkdir()
        (self.vault / 'app/node_modules/pkg/.git').mkdir(parents=True)
        (self.vault / 'clone/.git').mkdir(parents=True)
        (self.vault / 'clone/deep/inner/.git').mkdir(parents=True)
        (self.vault / 'worktree').mkdir()
        (self.vault / 'worktree/.git').write_text('gitdir: /elsewhere\n', encoding='utf-8')
        report = hygiene.boundary(self.vault)
        self.assertEqual(report['code_dirs'], ['app/node_modules'])
        self.assertEqual(report['nested_repositories'], ['clone', 'worktree'])

    def test_conflict_copy_needs_its_original(self):
        (self.vault / '.obsidian').mkdir()
        for name in ('Plan.md', 'Plan 2.md', 'Bolum 3.md', 'eski.bak'):
            (self.vault / name).write_text('x', encoding='utf-8')
        self.assertEqual(hygiene.boundary(self.vault)['backup_artifacts'], ['Plan 2.md', 'eski.bak'])


class ClosedTasksTest(unittest.TestCase):
    def test_tasks_written_by_v3_itself_are_found(self):
        # Maintainer regression: V3 writes JSON frontmatter ("status": "done"); the old
        # line pattern never matched a task created or updated through the CLI.
        state = Path(tempfile.mkdtemp(prefix='v3-hygiene-state-'))
        self.addCleanup(shutil.rmtree, state, True)
        env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1')
        for name, status in (('bitti', 'done'), ('iptal', 'cancelled'), ('suren', 'active')):
            record = {'source': 'tasks/alt/' + name + '.md', 'text': '# ' + name + '\nstatus: done\n',
                      'metadata': {'id': name, 'status': status, 'owner': 'synthetic'}}
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                     '--state', str(state), 'task-create', '--file', '-'],
                                    input=json.dumps(record), capture_output=True, text=True, encoding='utf-8',
                                    env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.vault / 'tasks/alt/bitti.md').read_text(encoding='utf-8').startswith('---\n{'))
        now = time.time()
        for path in (self.vault / 'tasks/alt').glob('*.md'):
            os.utime(path, (now - 40 * 86400, now - 40 * 86400))
        before = {path: path.stat().st_mtime_ns for path in self.vault.rglob('*')}
        report = hygiene.closed_tasks(self.vault, days=30, now=now)
        self.assertEqual([entry['source'] for entry in report['closed']], ['tasks/alt/bitti.md', 'tasks/alt/iptal.md'])
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in self.vault.rglob('*')}, 'report only, never a move')

    def test_body_line_is_not_a_status(self):
        path = self.vault / 'tasks/not.md'
        path.write_text('---\nid: n\nstatus: active\n---\nstatus: done\n', encoding='utf-8')
        then = time.time() - 90 * 86400
        os.utime(path, (then, then))
        self.assertEqual(hygiene.closed_tasks(self.vault)['closed'], [])

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

    def test_turkish_kapandi_status_is_recognized(self):
        old = self.vault / 'tasks/eski-tr.md'
        old.write_text('---\nid: e\nstatus: kapandı\n---\n# eski-tr\n', encoding='utf-8')
        now = time.time()
        os.utime(old, (now - 45 * 86400, now - 45 * 86400))
        report = hygiene.closed_tasks(self.vault, days=30, now=now)
        self.assertEqual([entry['source'] for entry in report['closed']], ['tasks/eski-tr.md'])

    def test_active_tasks_never_listed_and_missing_folder_is_empty(self):
        active = self.vault / 'tasks/aktif.md'
        active.write_text('---\nid: a\nstatus: active\n---\n# aktif\n')
        then = time.time() - 90 * 86400
        os.utime(active, (then, then))
        self.assertEqual(hygiene.closed_tasks(self.vault)['closed'], [])
        self.assertEqual(hygiene.closed_tasks(self.vault / 'tasks')['closed_count'], 0)


class CompanionExemptionTest(unittest.TestCase):
    """#130 decision: tam muafiyet — companion memory files never enter a scan."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-companion-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.state = root / 'state'
        self.state.mkdir()
        self.companion = self.vault / '🔮 850-Companion'
        self.companion.mkdir()
        # A personalized directory name from the runtime bootstrap marker.
        (self.state / 'companion-bootstrap.json').write_text(
            json.dumps({'schema': 1, 'directory': '🔮 850-Companion'}), encoding='utf-8')
        (self.companion / 'Journal.md').write_text('gunluk ' * 600, encoding='utf-8')
        (self.companion / 'Last-Session.md').write_text('kart ' * 700, encoding='utf-8')

    def test_companion_files_never_counted_and_never_warned(self):
        scanned = hygiene.cap_scan(self.vault, state=self.state)
        self.assertEqual(scanned['over'], [])
        payload = {'hook_event_name': 'PostToolUse',
                   'tool_input': {'file_path': str(self.companion / 'Journal.md')}}
        self.assertEqual(hygiene.hook_cap_warning(self.vault, payload, harness='claude', state=self.state), '')
        # Without state the default companion name still applies.
        self.assertEqual(hygiene.cap_scan(self.vault)['over'], [])

    def test_companion_folder_never_asked_and_never_cold(self):
        then = time.time() - 60 * 86400
        os.utime(self.companion, (then, then))
        self.assertEqual(hygiene.folder_questions(self.vault, self.state, cooldown_days=14), [])
        self.assertFalse((self.state / 'soruldu').exists())
        promo = hygiene.promotion(self.vault, self.state)
        self.assertFalse([entry for entry in promo['cold'] if 'Companion' in entry['folder']])

    def test_companion_touch_never_logged(self):
        payload = {'hook_event_name': 'PostToolUse',
                   'tool_input': {'file_path': str(self.companion / 'Journal.md')}}
        hygiene.touch_log(self.state, self.vault, payload)
        self.assertFalse((self.state / 'touch-log.tsv').exists())

    def test_personalized_companion_directory_is_respected(self):
        import shutil
        shutil.rmtree(self.companion)  # a renamed bootstrap replaces the default folder
        other = self.vault / 'Aklım'
        other.mkdir()
        (other / 'Journal.md').write_text('not ' * 600, encoding='utf-8')
        (self.state / 'companion-bootstrap.json').write_text(
            json.dumps({'schema': 1, 'directory': 'Aklım'}), encoding='utf-8')
        self.assertEqual(hygiene.cap_scan(self.vault, state=self.state)['over'], [])
        self.assertEqual(hygiene.folder_questions(self.vault, self.state, cooldown_days=14), [])


class FolderQuestionsBudgetTest(unittest.TestCase):
    """#130: SessionStart never rglobs the vault — shallow mtimes only."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-soru-budget-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()

    def test_deep_fresh_file_cannot_block_the_question(self):
        # A folder whose activity is only nested and old: the shallow scan reads
        # the folder stamp and its direct children, never the deep tree.
        (self.vault / 'Makaleler').mkdir()
        deep = self.vault / 'Makaleler/2026-09-27-konferans'
        deep.mkdir()
        (deep / 'not.md').write_text('# not\n', encoding='utf-8')
        then = time.time() - 60 * 86400
        os.utime(self.vault / 'Makaleler', (then, then))
        os.utime(deep, (then, then))
        questions = hygiene.folder_questions(self.vault, self.state, cooldown_days=14)
        self.assertEqual(len(questions), 1)
        self.assertIn('Makaleler', questions[0])

    def test_recent_nested_folder_keeps_a_folder_warm(self):
        # Gaining a new subfolder is top-level activity: the folder is not silent.
        (self.vault / 'Makaleler').mkdir()
        (self.vault / 'Makaleler/2026-09-27-konferans').mkdir()
        (self.vault / 'Makaleler/2026-09-27-konferans/yeni.md').write_text('# yeni\n', encoding='utf-8')
        self.assertEqual(hygiene.folder_questions(self.vault, self.state, cooldown_days=14), [])

    def test_top_level_activity_warms_the_folder(self):
        (self.vault / 'Makaleler').mkdir()
        (self.vault / 'Makaleler/konu.md').write_text('# konu\n', encoding='utf-8')
        self.assertEqual(hygiene.folder_questions(self.vault, self.state, cooldown_days=14), [])

    def test_generic_system_names_are_skipped_and_personal_names_are_not_in_code(self):
        for name in ('daily', 'knowledge', 'tasks', 'node_modules', 'scripts', 'tests', 'docs', 'template'):
            (self.vault / name).mkdir()
        questions = hygiene.folder_questions(self.vault, self.state, cooldown_days=14)
        self.assertEqual(questions, [])
        # The reviewer's finding 6: a user's personal layout (Finans/Müşteriler/gptpro)
        # must not be hard-coded; kasa-class names are covered by SENSITIVE_DIRS instead.
        source = hygiene.__dict__.get('SORU_SKIP_DIRS')
        self.assertIsNotNone(source)
        pattern = source.pattern
        for personal in ('Finans', 'Musteriler', 'Müşteriler', 'gptpro'):
            self.assertNotIn(personal, pattern)


class HarnessGateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-harness-')
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name)
        self.note = self.vault / 'not.md'
        self.note.write_text('kelime ' * 600, encoding='utf-8')
        self.payload = {'hook_event_name': 'PostToolUse', 'tool_input': {'file_path': str(self.note)}}

    def test_only_claude_and_codex_receive_the_warning(self):
        self.assertIn('Bolum SINYALI', hygiene.hook_cap_warning(self.vault, self.payload, harness='claude'))
        self.assertIn('Bolum SINYALI', hygiene.hook_cap_warning(self.vault, self.payload, harness='codex'))
        for harness in ('antigravity', 'opencode', 'omp', 'hermes'):
            self.assertEqual(hygiene.hook_cap_warning(self.vault, self.payload, harness=harness), '')


class TouchLogOptInTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-touch-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.state = root / 'state'
        self.state.mkdir()
        (self.vault / 'Notlar').mkdir()
        (self.vault / 'Notlar/a.md').write_text('# a\n', encoding='utf-8')

    def payload(self, path):
        return {'hook_event_name': 'PostToolUse', 'tool_input': {'file_path': str(path)}}

    def test_touch_log_and_promotion_roundtrip(self):
        hygiene.touch_log(self.state, self.vault, self.payload(self.vault / 'Notlar/a.md'))
        promo = hygiene.promotion(self.vault, self.state, days=30)
        self.assertEqual([entry['folder'] for entry in promo['hot']], ['Notlar'])

    def test_untouched_user_folder_is_cold_without_deep_scan(self):
        (self.vault / 'Beden').mkdir()
        os.utime(self.vault / 'Beden', (time.time() - 60 * 86400,) * 2)
        promo = hygiene.promotion(self.vault, self.state, days=30)
        self.assertEqual([entry['folder'] for entry in promo['cold']], ['Beden'])


class RealLayoutTest(unittest.TestCase):
    """Maintainer regressions re-derived on template-shaped names, not synthetic bare names."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-real-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.vault, self.state = root / 'vault', root / 'state'
        self.vault.mkdir()
        self.state.mkdir()
        then = time.time() - 60 * 86400
        for name in ('📦 900-Archive', '📋 Templates', '🔐 Kasa', 'node_modules', '📥 000-Inbox', 'Makaleler'):
            (self.vault / name).mkdir()
            os.utime(self.vault / name, (then, then))

    def test_quiet_template_archive_kasa_and_code_folders_are_never_asked(self):
        questions = hygiene.folder_questions(self.vault, self.state, cooldown_days=14)
        self.assertEqual(len(questions), 1)
        self.assertTrue(questions[0].startswith('Makaleler/ klasoru 60 gundur bos.'), questions[0])
        cold = [entry['folder'] for entry in hygiene.promotion(self.vault, self.state)['cold']]
        self.assertEqual(cold, ['Makaleler'])

    def test_cap_scan_skips_code_trees_kasa_and_nested_repositories(self):
        for relative in ('node_modules/pkg/README.md', '🔐 Kasa/hesap.md', 'clone/.git/x.md', 'clone/big.md'):
            (self.vault / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.vault / relative).write_text('kelime ' * 900, encoding='utf-8')
        (self.vault / 'Makaleler/uzun.md').write_text('kelime ' * 900, encoding='utf-8')
        self.assertEqual([entry['file'] for entry in hygiene.cap_scan(self.vault)['over']], ['Makaleler/uzun.md'])

    def test_archive_marker_is_read_from_frontmatter_only(self):
        body = 'kelime ' * 900
        cases = {'gecmis.md': ('---\ntype: gecmis\n---\n' + body, None),
                 'json.md': ('---\n{"durum": "Arşiv"}\n---\n' + body, None),
                 'metin.md': ('---\ntitle: x\n---\narsiv notlari\n' + body, True)}
        for name, (text, expected) in cases.items():
            (self.vault / 'Makaleler' / name).write_text(text, encoding='utf-8')
            measured = hygiene.file_over_cap(self.vault, self.vault / 'Makaleler' / name)
            self.assertEqual(None if measured is None else measured[1], expected, name)

    def test_nfd_companion_and_root_files_never_enter_the_touch_log(self):
        companion = unicodedata.normalize('NFD', 'Aklım Özü')
        (self.vault / companion).mkdir()
        (self.state / 'companion-bootstrap.json').write_text(
            json.dumps({'schema': 1, 'directory': unicodedata.normalize('NFC', 'Aklım Özü')}), encoding='utf-8')
        for path in (self.vault / companion / 'Journal.md', self.vault / 'kok.md', self.vault / '🔐 Kasa/hesap.md'):
            hygiene.touch_log(self.state, self.vault, {'hook_event_name': 'PostToolUse',
                                                       'tool_input': {'file_path': str(path)}})
        self.assertFalse((self.state / 'touch-log.tsv').exists())


class SettingsAndDoctorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-settings-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.vault, self.state = root / 'vault', root / 'state'
        self.vault.mkdir()

    def doctor(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'doctor'], capture_output=True, text=True,
                                encoding='utf-8', env=inherited_env(BEYIN_V3_NO_SPAWN='1'), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_settings_schema_is_strict(self):
        self.assertEqual(hygiene.read_settings(self.state), (hygiene.SETTINGS_DEFAULTS, True))
        for bad in ({'max_words': 5}, {'word_cap_warning': 1}, {'unknown': True}, {'schema': 2}):
            with self.assertRaises(ValueError):
                hygiene.check_settings(bad)
        self.assertEqual(hygiene.save_settings(self.state, {'max_words': 700})['max_words'], 700)
        self.assertEqual(json.loads((self.state / 'hygiene.json').read_text(encoding='utf-8'))['schema'], 1)

    def test_doctor_reports_follow_the_opt_in_and_never_write(self):
        (self.vault / 'Makaleler').mkdir()
        (self.vault / 'Makaleler/uzun.md').write_text('kelime ' * 900, encoding='utf-8')
        then = time.time() - 60 * 86400
        for path in (self.vault / 'Makaleler/uzun.md', self.vault / 'Makaleler'):
            os.utime(path, (then, then))
        default = self.doctor()
        self.assertEqual((default['word_cap'], default['promotion']), ({'enabled': False}, {'enabled': False}))
        hygiene.save_settings(self.state, {'word_cap_warning': True, 'max_words': 1000, 'promotion': True,
                                           'folder_questions': True})
        self.assertEqual(self.doctor()['word_cap']['over_count'], 0, 'the configured cap applies')
        hygiene.save_settings(self.state, {'max_words': 800})
        vault_before = {path: path.stat().st_mtime_ns for path in self.vault.rglob('*')}
        report = self.doctor()
        self.assertEqual([entry['file'] for entry in report['word_cap']['over']], ['Makaleler/uzun.md'])
        self.assertEqual([entry['folder'] for entry in report['promotion']['cold']], ['Makaleler'])
        self.assertEqual(vault_before, {path: path.stat().st_mtime_ns for path in self.vault.rglob('*')})
        self.assertFalse((self.state / 'soruldu').exists(), 'doctor never stamps a question')
        self.assertFalse((self.state / 'touch-log.tsv').exists())


class InboxReportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-hygiene-inbox-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.vault, self.state = root / 'vault', root / 'state'
        self.now = time.time()
        for relative, days in (('📥 000-Inbox/a.md', 1), ('📥 000-Inbox/Dump/b.md', 12), ('📥 000-Inbox/c.txt', 90),
                               ('📥 000-Inbox/.gizli/d.md', 90), ('00_INBOX/e.md', 2), ('Gelen Kutusu/f.md', 0),
                               ('Notes/g.md', 400), ('🔐 Kasa Inbox/h.md', 400), ('🔮 850-Companion/inbox.md', 400)):
            path = self.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# not\n', encoding='utf-8')
            os.utime(path, (self.now - days * 86400, self.now - days * 86400))

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), *args], capture_output=True, text=True,
                                encoding='utf-8', env=inherited_env(BEYIN_V3_NO_SPAWN='1'), timeout=60)
        return result

    def test_counts_waiting_notes_and_marks_folders_over_a_threshold(self):
        before = {path: path.stat().st_mtime_ns for path in self.vault.rglob('*')}
        report = hygiene.inbox_report(self.vault, self.state, max_items=10, max_days=7, now=self.now)
        rows = {row['folder']: row for row in report['folders']}
        # Only inbox-named top-level folders; kasa-class and companion folders never appear.
        self.assertEqual(sorted(rows), ['00_INBOX', 'Gelen Kutusu', '📥 000-Inbox'])
        self.assertEqual((rows['📥 000-Inbox']['notes'], rows['📥 000-Inbox']['oldest_days']), (2, 12))
        self.assertTrue(rows['📥 000-Inbox']['attention'], 'oldest note is past max_days')
        self.assertFalse(rows['00_INBOX']['attention'])
        self.assertTrue(report['attention'])
        self.assertTrue(hygiene.inbox_report(self.vault, max_items=1, max_days=3650, now=self.now)['folders'][0]['attention'],
                        'a folder at max_items is marked too')
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in self.vault.rglob('*')}, 'report only')

    def test_a_created_date_outlives_a_reset_mtime(self):
        # A clone or sync client rewrites mtimes; the note's own date keeps its age.
        folder = self.vault / 'Inbox'
        folder.mkdir()
        created = time.strftime('%Y-%m-%d', time.localtime(self.now - 20 * 86400))
        (folder / 'kopya.md').write_text('---\ncreated: ' + created + '\n---\n# kopya\n', encoding='utf-8')
        (folder / 'bozuk.md').write_text('---\ncreated: 2026-13-40\n---\n# bozuk\n', encoding='utf-8')
        rows = {row['folder']: row for row in hygiene.inbox_report(self.vault, now=self.now)['folders']}
        self.assertEqual((rows['Inbox']['notes'], rows['Inbox']['oldest_days']), (2, 20))
        (folder / 'kopya.md').unlink()
        rows = {row['folder']: row for row in hygiene.inbox_report(self.vault, now=self.now)['folders']}
        self.assertEqual(rows['Inbox']['oldest_days'], 0, 'a malformed date falls back to the mtime')

    def test_doctor_shows_it_only_after_opt_in_and_keeps_hygiene_json_untouched(self):
        default = json.loads(self.cli('doctor').stdout)
        self.assertEqual(default['inbox'], {'enabled': False})
        shown = json.loads(self.cli('preferences').stdout)
        self.assertEqual(shown['inbox_report'], {'enabled': False, 'max_items': 10, 'max_days': 7, 'folders': []})
        saved = json.loads(self.cli('preferences', '--inbox-report', 'on', '--inbox-max-days', '30').stdout)
        self.assertEqual(saved['inbox_report'], {'enabled': True, 'max_items': 10, 'max_days': 30, 'folders': []})
        self.assertFalse((self.state / 'hygiene.json').exists(), 'an older release must still read hygiene.json')
        report = json.loads(self.cli('doctor').stdout)['inbox']
        self.assertFalse(report['attention'], 'no folder reaches 10 notes or 30 days')
        before = (self.state / 'inbox-report.json').read_bytes()
        refused = self.cli('preferences', '--inbox-max-items', '0', '--inbox-report', 'off')
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(before, (self.state / 'inbox-report.json').read_bytes(), 'an invalid value changes nothing')
        (self.state / 'inbox-report.json').write_text('{"enabled": "yes"}', encoding='utf-8')
        damaged = json.loads(self.cli('preferences').stdout)
        self.assertFalse(damaged['inbox_report']['enabled'])
        self.assertIn('inbox_report_notice', damaged)
        self.assertEqual(json.loads(self.cli('doctor').stdout)['inbox'], {'enabled': False})

    def test_folder_words_are_narrow_and_a_named_folder_list_replaces_them(self):
        import unicodedata
        for relative in ('Gelen Belgeler/belge.md', 'GelenKutusu/x.MD',
                         unicodedata.normalize('NFD', 'Yakalama Çekmecesi') + '/y.md'):
            path = self.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# not\n', encoding='utf-8')
        rows = {row['folder']: row for row in hygiene.inbox_report(self.vault, now=self.now)['folders']}
        # "gelen" alone is an ordinary word ("Gelen Belgeler" = incoming documents), not an inbox.
        self.assertNotIn('Gelen Belgeler', rows)
        self.assertEqual(rows['GelenKutusu']['notes'], 1, 'a .MD note counts like .md, as sync reads it')
        # A layout the words do not know is named by the user; the list replaces word detection.
        self.cli('preferences', '--inbox-report', 'on')
        saved = json.loads(self.cli('preferences', '--inbox-folder', 'Yakalama Çekmecesi',
                                    '--inbox-folder', 'Missing').stdout)
        self.assertEqual(saved['inbox_report']['folders'], ['Yakalama Çekmecesi', 'Missing'])
        report = json.loads(self.cli('doctor').stdout)['inbox']
        rows = {unicodedata.normalize('NFC', row['folder']): row for row in report['folders']}
        self.assertEqual(sorted(rows), ['Missing', 'Yakalama Çekmecesi'])
        self.assertEqual(rows['Yakalama Çekmecesi']['notes'], 1, 'an NFD folder matches the NFC name typed')
        self.assertEqual(rows['Missing']['error'], 'not_found')
        self.assertTrue(report['attention'])
        for name in ('../escape', '🔐 Kasa'):
            refused = self.cli('preferences', '--inbox-folder', name)
            self.assertNotEqual(refused.returncode, 0, name)
        reset = json.loads(self.cli('preferences', '--inbox-folder', '').stdout)
        self.assertEqual(reset['inbox_report']['folders'], [])
        self.assertIn('📥 000-Inbox', {row['folder'] for row in json.loads(self.cli('doctor').stdout)['inbox']['folders']})

    def test_human_doctor_and_preferences_show_the_opted_in_report(self):
        spec = importlib.util.spec_from_file_location('beyin_entry_inbox', ROOT / 'scripts/beyin_entry.py')
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        report = {'folders': [{'folder': '📥 000-Inbox', 'notes': 14, 'oldest_days': 12, 'attention': True},
                              {'folder': 'Quiet', 'notes': 1, 'oldest_days': 0, 'attention': False},
                              {'folder': 'Gone', 'notes': 0, 'oldest_days': None, 'attention': False,
                               'error': 'not_found'}], 'attention': True}
        text = entry.human_result({'status': 'observed_metadata', 'inbox': report}, 'doctor', '3.8.1')
        self.assertIn('Gelen kutusunda bekleyen (bilgi, isleme karari senin): 📥 000-Inbox (14 not, en eskisi 12 gun), '
                      'Gone (okunamadi: not_found).', text)
        quiet = entry.human_result({'status': 'observed_metadata', 'inbox': {'enabled': False}}, 'doctor', '3.8.1')
        self.assertNotIn('Gelen kutusu', quiet)
        prefs = {'auto_sync': True, 'interval_minutes': 0, 'context_mode': 'auto', 'context_chars': 6000,
                 'secret_filter': True}
        shown = entry.human_result({'preferences': prefs, 'inbox_report': {
            'enabled': True, 'max_items': 10, 'max_days': 7, 'folders': ['Yakalama']}}, 'preferences')
        self.assertIn('Gelen kutusu raporu (varsayilan kapali): acik (10 not / 7 gun; klasorler: Yakalama)', shown)


if __name__ == '__main__':
    unittest.main(verbosity=2)
