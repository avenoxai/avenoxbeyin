"""Companion hygiene (#96): measurable limits, a SessionStart notice, doctor, lossless compaction.

Synthetic data only. The two oversized fixtures mirror the shapes reported in #96:
newest-first dated paragraphs above a never-pruned Previous section (Last-Session.md)
and many dated updates per thread plus a closed section (Threads.md).
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from v3_package_helpers import inherited_env

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
SCRIPTS = ROOT / 'template/.claude/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import beyin_v3_compact as compact_module  # noqa: E402
import beyin_v3_companion as companion_module  # noqa: E402

COMPANION = '🔮 850-Companion'
NOW = datetime(2026, 9, 25, 4, 30, tzinfo=timezone.utc)
FILLER = 'Karar gerekçesi, kaynak bağlantısı ve açık kalan adım burada ayrıntılı olarak anlatılıyor. '



def human_entry():
    """The installed CLI's human formatter (scripts/beyin_entry.py), loaded once."""
    if not hasattr(human_entry, 'module'):
        import importlib.util
        spec = importlib.util.spec_from_file_location('hygiene_entry_cli', ROOT / 'scripts/beyin_entry.py')
        human_entry.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(human_entry.module)
    return human_entry.module

def oversized_last_session(entries=80):
    moment = datetime(2026, 9, 24, 21, 10)
    parts = ['# Son oturum\n\n']
    for index in range(entries):
        stamp = (moment - timedelta(hours=3 * index)).strftime('%Y-%m-%d %H:%M')
        parts.append(f'**{stamp} (Codex)**: ENTRY_{index:03d} ' + FILLER * 11 + '\n\n')
    parts.append('## Previous Sessions\n')
    parts.extend(f'### Session: 2026-08-{day:02d}\nPREVIOUS_{day:02d} ' + FILLER * 3 + '\n\n' for day in range(1, 29))
    return ''.join(parts)


def oversized_threads(count=15, updates=16):
    parts = ['# Threads\n\nBirden fazla oturuma yayılan konular.\n\n## Active Threads\n']
    for thread in range(count):
        parts.append(f'### Thread: Konu {thread:02d}\n**Status:** 🟢 Active, 2026-08-01\n**Sahip:** Derya\n')
        for update in range(updates):
            day = datetime(2026, 9, 24) - timedelta(days=update)
            parts.append(f'{day:%Y-%m-%d}: THREAD_{thread:02d}_UPDATE_{update:02d} ' + FILLER * 2 + '\n')
        parts.append('\n')
    parts.append('## Closed Threads\n')
    parts.extend(f'### Thread: Eski {index}\nCLOSED_{index} ' + FILLER + '\n' for index in range(12))
    return ''.join(parts)


class HygieneCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='companion-hygiene-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault = self.base / 'Örnek Beyin'
        self.directory = self.vault / COMPANION
        self.directory.mkdir(parents=True)
        self.state = self.base / 'state'
        self.env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        self.write('Core.md', '# Kimlik\nIDENTITY_CANARY: düşünme ortağı.\n')
        self.write('Journal.md', '# Journal\n## 2026-09-20\nJOURNAL_CANARY\n' + ('## 2026-08-01\neski gözlem\n' * 900))

    def write(self, name, text):
        path = self.directory / name
        path.write_bytes(text.encode('utf-8'))
        return path

    def run_cli(self, *args, payload=None, script='scripts/beyin_v3.py', ok=True):
        prefix = [] if script != 'scripts/beyin_v3.py' else ['--vault', self.vault, '--state', self.state]
        result = subprocess.run([sys.executable, str(ROOT / script), *map(str, prefix + list(args))],
                                input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, encoding='utf-8', env=self.env,
                                cwd=self.vault, timeout=60)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return json.loads(result.stderr)

    def hook(self, event='SessionStart', **kwargs):
        payload = dict(hook_event_name=event, session_id='synthetic-hygiene', event_id=event + str(kwargs), **kwargs)
        output = self.run_cli('--vault', self.vault, '--state', self.state, '--harness', 'codex',
                              payload=payload, script='template/.claude/scripts/beyin_v3_hook.py')
        return output.get('hookSpecificOutput', {}).get('additionalContext', '')

    def oversized(self):
        last = self.write('Last-Session.md', oversized_last_session())
        threads = self.write('Threads.md', oversized_threads())
        self.run_cli('sync')
        return last, threads

    def test_oversized_handoff_files_get_one_notice_inside_every_budget(self):
        last, threads = self.oversized()
        sizes = len(last.read_text(encoding='utf-8')), len(threads.read_text(encoding='utf-8'))
        self.assertGreater(sizes[0], 90000)
        self.assertGreater(sizes[1], 50000)
        text = self.hook()
        self.assertTrue(text.startswith(f'Memory hygiene: Last-Session.md is {sizes[0]} chars (limit 3000); '
                                        f'Threads.md is {sizes[1]} chars (limit 8000).'), text[:300])
        self.assertEqual(text.count('Memory hygiene'), 1)
        self.assertIn('companion-compact', text)
        self.assertIn('ENTRY_000', text)
        self.assertLessEqual(len(text), 5000)
        (self.vault / '.beyin-preferences.json').write_text(json.dumps({'context_chars': 1000, 'context_mode': 'session'}))
        small = self.hook()
        self.assertTrue(small.startswith('Memory hygiene: '))
        self.assertLessEqual(len(small), 1000)

    def test_limit_counts_characters_not_bytes_or_utf16_units(self):
        body = '# Son oturum\n' + 'ş🔮' * 1493
        self.assertEqual(len(body), 2999)
        self.assertGreater(len(body.encode('utf-8')), 3000)
        self.assertGreater(len(body.encode('utf-16-le')) // 2, 3000)
        path = self.write('Last-Session.md', body + 'x')
        self.write('Threads.md', '# Threads\n## Active Threads\n### Thread: A\n**Status:** active\n')
        self.run_cli('sync')
        self.assertNotIn('Memory hygiene', self.hook())
        # One more entry on a file sitting at the limit is caught at the next session start.
        path.write_bytes((body + 'x\n2026-09-25: yeni kayıt\n').encode('utf-8'))
        self.assertIn('Memory hygiene: Last-Session.md is 3024 chars (limit 3000).', self.hook())
        self.assertNotIn('Threads.md is', self.hook())

    def test_limits_are_configurable_machine_local_and_validated_before_saving(self):
        self.oversized()
        saved = self.run_cli('preferences', '--last-session-chars', '0', '--threads-chars', '200000')
        self.assertEqual(saved['status'], 'saved')
        self.assertEqual(saved['companion_limits'], {'Last-Session.md': 0, 'Threads.md': 200000})
        self.assertFalse((self.vault / '.beyin-preferences.json').exists(), 'vault preference schema must not change')
        stored = json.loads((self.state / 'companion-limits.json').read_text(encoding='utf-8'))
        self.assertEqual(stored, {'schema': 1, 'Last-Session.md': 0, 'Threads.md': 200000})
        self.assertNotIn('Memory hygiene', self.hook())
        error = self.run_cli('preferences', '--context-chars', '3000', '--threads-chars', '999', ok=False)
        self.assertIn('Threads.md limit', error['message'])
        self.assertFalse((self.vault / '.beyin-preferences.json').exists(), 'a rejected limit must not save other fields')
        self.run_cli('preferences', '--profile', 'economical')
        self.assertEqual(self.run_cli('preferences')['companion_limits'], {'Last-Session.md': 0, 'Threads.md': 200000})
        (self.state / 'companion-limits.json').write_text('{broken', encoding='utf-8')
        self.assertIn('Memory hygiene', self.hook(), 'a damaged limits file falls back to the defaults')
        before = (self.vault / '.beyin-preferences.json').read_bytes()
        self.run_cli('preferences', '--context-chars', '4000', '--threads-chars', '9000', ok=False)
        self.assertEqual((self.state / 'companion-limits.json').read_text(encoding='utf-8'), '{broken')
        self.assertEqual((self.vault / '.beyin-preferences.json').read_bytes(), before, 'nothing saved on a damaged limits file')

    def test_doctor_reports_sizes_as_hygiene_not_as_a_sync_failure(self):
        self.oversized()
        doctor = self.run_cli('doctor')
        hygiene = doctor['companion_hygiene']
        self.assertEqual(hygiene['status'], 'over_limit')
        self.assertEqual(hygiene['over_limit'], ['Last-Session.md', 'Threads.md'])
        self.assertEqual(hygiene['files']['Last-Session.md']['limit'], 3000)
        self.assertTrue(hygiene['files']['Threads.md']['over_limit'])
        self.assertNotIn('limit', hygiene['files']['Journal.md'], 'the Journal accumulates by design')
        self.assertGreater(hygiene['files']['Journal.md']['chars'], 8000)
        self.assertNotEqual(doctor['status'], 'needs_attention')

    def test_compaction_is_lossless_fits_the_limits_and_clears_the_notice(self):
        last, threads = self.oversized()
        originals = {path.name: path.read_text(encoding='utf-8') for path in (last, threads)}
        journal = (self.directory / 'Journal.md').read_bytes()
        plan = self.run_cli('companion-compact', '--dry-run')
        self.assertEqual(plan['status'], 'dry_run')
        self.assertEqual({name: path.read_text(encoding='utf-8') for name, path in
                          (('Last-Session.md', last), ('Threads.md', threads))}, originals)
        self.assertFalse((self.directory / 'Arşiv').exists())
        result = self.run_cli('companion-compact')
        self.assertEqual(result['status'], 'compacted')
        self.assertEqual(result['deleted_chars'], 0)
        self.assertFalse(result['model_calls'])
        self.assertEqual(result['sync']['status'], 'succeeded')
        self.assertEqual((self.directory / 'Journal.md').read_bytes(), journal)
        month = datetime.now(timezone.utc).strftime('%Y-%m')
        for path, limit in ((last, 3000), (threads, 8000)):
            live = path.read_text(encoding='utf-8')
            archive = self.directory / 'Arşiv' / f'{path.stem}-{month}.md'
            stored = archive.read_text(encoding='utf-8')
            self.assertLessEqual(len(live), limit)
            self.assertEqual(result['files'][path.name]['chars_after'], len(live))
            self.assertTrue(stored.startswith('---\n{"kind": "note", "visibility": "private"'))
            self.assertEqual(live.count(f'Arşivlenen metin: `{COMPANION}/Arşiv/{archive.name}`'), 1)
            # Every original line is still somewhere, as often as it was: nothing dropped.
            missing = Counter(originals[path.name].splitlines()) - (Counter(live.splitlines()) + Counter(stored.splitlines()))
            self.assertEqual(missing, Counter())
        live = last.read_text(encoding='utf-8')
        self.assertIn('ENTRY_000', live)
        self.assertNotIn('PREVIOUS_01', live)
        self.assertNotIn('ENTRY_059', live)
        live = threads.read_text(encoding='utf-8')
        for thread in range(15):
            self.assertIn(f'### Thread: Konu {thread:02d}\n**Status:** 🟢 Active, 2026-08-01\n**Sahip:** Derya\n'
                          f'2026-09-24: THREAD_{thread:02d}_UPDATE_00', live)
        self.assertNotIn('CLOSED_0', live)
        text = self.hook()
        self.assertNotIn('Memory hygiene', text)
        self.assertIn('ENTRY_000', text)
        for canary in ('ENTRY_059', 'PREVIOUS_01', 'CLOSED_0', 'UPDATE_15'):
            self.assertNotIn(canary, text)
        found = self.run_cli('context', 'PREVIOUS_01 ENTRY_059 CLOSED_3')
        self.assertFalse([r for r in found['records'] if '/Arşiv/' in r['source']], 'archives are private')
        self.assertEqual(self.run_cli('doctor')['companion_hygiene']['status'], 'ok')
        again = self.run_cli('companion-compact')
        self.assertEqual(again['status'], 'within_limit')

    def test_a_second_compaction_appends_to_the_month_archive_with_one_pointer(self):
        last = self.write('Last-Session.md', oversized_last_session(8))
        first = self.run_cli('companion-compact')
        self.assertEqual(first['files']['Last-Session.md']['status'], 'compacted')
        archive = self.directory / first['files']['Last-Session.md']['archive'].split('/', 1)[1]
        before = archive.read_text(encoding='utf-8')
        newer = ''.join(f'**2026-09-25 0{hour}:00 (Claude)**: NEWER_{hour} ' + FILLER * 11 + '\n\n' for hour in range(1, 5))
        text = last.read_text(encoding='utf-8')
        last.write_text(text.replace('# Son oturum\n\n', '# Son oturum\n\n' + newer, 1), encoding='utf-8')
        second = self.run_cli('companion-compact')
        self.assertEqual(second['files']['Last-Session.md']['status'], 'compacted')
        after = archive.read_text(encoding='utf-8')
        self.assertTrue(after.startswith(before))
        self.assertEqual(after.count('---\n{"kind"'), 1)
        live = last.read_text(encoding='utf-8')
        self.assertIn('NEWER_4', live)
        self.assertEqual(live.count('Arşivlenen metin:'), 1)
        self.assertIn('NEWER_1', after)

    def test_undated_prose_over_the_limit_is_reported_not_cut(self):
        path = self.write('Last-Session.md', '# Son oturum\n\n' + FILLER * 60)
        before = path.read_bytes()
        result = self.run_cli('companion-compact')
        self.assertEqual(result['status'], 'needs_rewrite')
        self.assertEqual(result['files']['Last-Session.md']['status'], 'needs_rewrite')
        self.assertEqual(path.read_bytes(), before)
        # Issue #168: full file is backed up verbatim to Arşiv before rewriting
        archive = self.directory / 'Arşiv' / f'Last-Session-{datetime.now(timezone.utc).strftime("%Y-%m")}.md'
        self.assertTrue(archive.exists())
        archive_text = archive.read_text(encoding='utf-8')
        self.assertIn(FILLER, archive_text)
        self.assertIn('tam metin yedeği (needs_rewrite)', archive_text)
        self.assertEqual(result['files']['Last-Session.md'].get('archive'),
                         f'{COMPANION}/Arşiv/{archive.name}')
        self.assertEqual(result['files']['Last-Session.md']['backup'], 'written')
        self.assertIn(before.decode('utf-8'), archive_text)

    def test_dry_run_undated_prose_does_not_create_archive(self):
        path = self.write('Last-Session.md', '# Son oturum\n\n' + FILLER * 60)
        before = path.read_bytes()
        result = self.run_cli('companion-compact', '--dry-run')
        self.assertEqual(result['status'], 'dry_run')
        self.assertEqual(result['files']['Last-Session.md']['status'], 'needs_rewrite')
        self.assertEqual(result['files']['Last-Session.md']['backup'], 'planned')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.directory / 'Arşiv').exists())
        # The human dry-run line must not claim a backup that was never written.
        human = human_entry().human_result(result, 'companion-compact')
        self.assertNotIn('yedeklendi', human)
        self.assertIn('--dry-run olmadan calistir', human)

    def test_needs_rewrite_avoids_duplicate_snapshots_on_repeated_runs(self):
        body = '# Threads\n\n## Active Threads\n' + ('### Thread\nAktif konu detay metni.\n' * 300)
        path = self.write('Threads.md', body)
        first = self.run_cli('companion-compact')
        self.assertEqual(first['files']['Threads.md']['status'], 'needs_rewrite')
        archive = self.directory / 'Arşiv' / f'Threads-{datetime.now(timezone.utc).strftime("%Y-%m")}.md'
        self.assertTrue(archive.exists())
        first_size = len(archive.read_text(encoding='utf-8'))
        # Second run without changes should not duplicate the snapshot
        second = self.run_cli('companion-compact')
        self.assertEqual(second['files']['Threads.md']['status'], 'needs_rewrite')
        second_size = len(archive.read_text(encoding='utf-8'))
        self.assertEqual(first_size, second_size)
        self.assertEqual(second['files']['Threads.md']['backup'], 'exists')

    def test_a_file_still_over_the_limit_after_moving_is_backed_up_whole(self):
        # #168 with a few dated updates: they move, the file stays over the limit and the agent
        # rewrites it next, so the remaining text must reach the archive verbatim first.
        parts = ['# Threads\n\n## Active Threads\n']
        for thread in range(13):
            parts.append(f'### Thread: Konu {thread:02d}\n**Status:** 🟢 Active: created 2026-09-01\n'
                         f'Durum [[Karar-{thread:02d}]] OPEN_{thread:02d} ' + FILLER * 8 + '\n')
            if thread < 2:
                parts.append(f'2026-09-10: OLD_UPDATE_{thread} ' + FILLER * 2 + '\n2026-09-20: NEW_UPDATE_{thread}\n')
            parts.append('\n')
        original = ''.join(parts)
        self.assertGreater(len(original), 8000)
        path = self.write('Threads.md', original)
        first = self.run_cli('companion-compact')
        entry = first['files']['Threads.md']
        self.assertEqual((first['status'], entry['status'], entry['within_limit_after'], entry['backup']),
                         ('needs_rewrite', 'compacted', False, 'written'))
        live = path.read_text(encoding='utf-8')
        self.assertNotIn('OLD_UPDATE_0', live)
        archive = self.directory / 'Arşiv' / f'Threads-{datetime.now(timezone.utc).strftime("%Y-%m")}.md'
        text = archive.read_text(encoding='utf-8')
        self.assertIn(live, text)
        self.assertIn('OLD_UPDATE_0', text)
        human = human_entry().human_result(first, 'companion-compact')
        self.assertIn('hala sinirin (8000) ustunde (tam metin arsive yedeklendi: ', human)
        again = self.run_cli('companion-compact')
        self.assertEqual((again['files']['Threads.md']['status'], again['files']['Threads.md']['backup']),
                         ('needs_rewrite', 'exists'))
        self.assertEqual(archive.read_text(encoding='utf-8'), text)
        # The agent summarizes; every line of the original is still in the archive.
        path.write_text('# Threads\n\n## Active Threads\n### Thread: Konu 00\nÖzet.\n', encoding='utf-8')
        self.assertEqual(self.run_cli('companion-compact')['status'], 'within_limit')
        text = archive.read_text(encoding='utf-8')
        for line in original.splitlines():
            self.assertIn(line, text)

    def test_symlinked_archive_directory_is_refused(self):
        self.write('Last-Session.md', oversized_last_session(8))
        outside = self.base / 'outside'; outside.mkdir()
        try:
            (self.directory / 'Arşiv').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        before = (self.directory / 'Last-Session.md').read_bytes()
        error = self.run_cli('companion-compact', ok=False)
        self.assertIn('symlink', error['message'])
        self.assertEqual((self.directory / 'Last-Session.md').read_bytes(), before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_multi_card_parallel_sessions_compacted_losslessly_keeping_newest_card(self):
        moment = datetime(2026, 9, 26, 16, 30)
        cards = ['# Son oturum\n\n']
        for i in range(12):
            stamp = (moment - timedelta(hours=2 * i)).strftime('%Y-%m-%d %H:%M')
            cards.append(f'## {stamp} · session_{i:02d} · a{i:07x}\n'
                         f'CARD_{i:02d}: Oturum kararı ve sonuçları. ' + FILLER * 5 + '\n\n')
        last = self.write('Last-Session.md', ''.join(cards))
        self.run_cli('sync')
        self.assertIn('Memory hygiene: Last-Session.md is', self.hook())
        result = self.run_cli('companion-compact')
        self.assertEqual(result['status'], 'compacted')
        live = last.read_text(encoding='utf-8')
        self.assertLessEqual(len(live), 3000)
        self.assertIn('CARD_00', live, 'the newest session card is preserved')
        self.assertNotIn('CARD_11', live, 'oldest session card is moved to archive')
        month = datetime.now(timezone.utc).strftime('%Y-%m')
        archive = (self.directory / 'Arşiv' / f'Last-Session-{month}.md').read_text(encoding='utf-8')
        self.assertIn('CARD_11', archive, 'oldest session card is safely preserved in archive')

    def test_instructions_ask_for_a_rewrite_not_an_append(self):
        skill = (ROOT / 'template/.agents/skills/beyin/SKILL.md').read_text(encoding='utf-8')
        installer = (ROOT / 'scripts/install_v3.py').read_text(encoding='utf-8')
        for text in (skill, installer):
            text = ' '.join(text.split())
            self.assertIn('companion-compact', text)
            self.assertIn('baştan yeniden', text)
            self.assertIn('her cevapta', text)
            self.assertIn('kendi kartını', text)
        # #112: every place that tells an agent how to write Last-Session.md names the same
        # per-session card heading, so no client falls back to rewriting the whole file.
        router = (ROOT / 'template/CLAUDE.md').read_text(encoding='utf-8')
        starter = companion_module.STARTERS['Last-Session.md']
        heading = '## YYYY-MM-DD HH:MM · <etiket> · <session_id[:8]>'
        for text in (skill, installer, router, starter):
            text = ' '.join(text.split())
            self.assertIn(heading, text)
            self.assertIn('kendi kartını', text)
        for text in (skill, installer, router):
            text = ' '.join(text.split())
            self.assertIn('en üste', text)
            self.assertIn('dosyanın tamamını yeniden yazma', text)
            self.assertIn('Receipt session', text)
        for text in (skill, installer, router, starter,
                     (ROOT / 'template/.agents/skills/beyin-doktor/SKILL.md').read_text(encoding='utf-8')):
            for forbidden in ('\u2014', '\u2013', '\u00e2'):  # em dash, en dash, circumflex a
                self.assertNotIn(forbidden, text)

    def test_compaction_refuses_damaged_limits_and_non_utf8_sources(self):
        last = self.write('Last-Session.md', oversized_last_session(8))
        before = last.read_bytes()
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'companion-limits.json').write_text('{broken', encoding='utf-8')
        refused = self.run_cli('companion-compact')
        self.assertEqual((refused['status'], refused['limits_file']), ('needs_attention', 'invalid'))
        self.assertEqual(last.read_bytes(), before)
        (self.state / 'companion-limits.json').unlink()
        last.write_bytes(before + b'\xff\xfe eski kodlama\n')
        result = self.run_cli('companion-compact')
        self.assertEqual(result['files']['Last-Session.md']['status'], 'needs_attention')
        self.assertEqual(last.read_bytes(), before + b'\xff\xfe eski kodlama\n')
        self.assertFalse((self.directory / 'Arşiv').exists())


class HumanOutputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location('hygiene_entry', ROOT / 'scripts/beyin_entry.py')
        cls.entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.entry)

    def test_doctor_preferences_and_compaction_read_as_short_turkish_lines(self):
        doctor = self.entry.human_result({'status': 'observed_metadata', 'companion_hygiene': {
            'over_limit': ['Threads.md'], 'files': {'Threads.md': {'chars': 57012, 'limit': 8000}}}}, 'doctor', '3.3.0')
        self.assertIn('Hafiza hijyeni: Threads.md 57012 karakter (sinir 8000).', doctor)
        self.assertIn('beyin.py companion-compact', doctor)
        prefs = self.entry.human_result({'preferences': {'auto_sync': True, 'interval_minutes': 0, 'context_mode': 'turn',
                                                         'context_chars': 5000, 'secret_filter': False},
                                         'companion_limits': {'Last-Session.md': 0, 'Threads.md': 8000}}, 'preferences')
        self.assertIn('Hafiza dosyasi siniri, Last-Session.md: kapali', prefs)
        self.assertIn('Hafiza dosyasi siniri, Threads.md: 8000 karakter', prefs)
        compacted = self.entry.human_result({'status': 'needs_rewrite', 'files': {
            'Last-Session.md': {'status': 'compacted', 'chars': 94000, 'chars_after': 2900, 'moved_chars': 91200,
                                'limit': 3000, 'within_limit_after': True},
            'Threads.md': {'status': 'needs_rewrite', 'chars': 9000, 'limit': 8000}}}, 'companion-compact')
        self.assertIn('Last-Session.md: 94000 -> 2900 karakter, 91200 karakter arsive tasindi.', compacted)
        self.assertIn('Threads.md: tasinacak tarihli eski kayit yok', compacted)
        self.assertTrue(compacted.endswith('Hicbir metin silinmedi; model cagrilmadi.'))
        planned = self.entry.human_result({'status': 'dry_run', 'files': {
            'Threads.md': {'status': 'planned', 'chars': 9000, 'chars_after': 8100, 'moved_chars': 900,
                           'limit': 8000, 'within_limit_after': False}}}, 'companion-compact')
        self.assertIn('8100 karakter olacak, 900 karakter arsive tasinacak.', planned)
        self.assertIn('Threads.md hala sinirin (8000) ustunde', planned)
        odd = self.entry.human_result({'status': 'needs_attention', 'files': {
            'Threads.md': {'status': 'needs_attention', 'reason': 'not UTF-8; nothing moved'}}}, 'companion-compact')
        self.assertIn('Threads.md: not UTF-8; nothing moved.', odd)
        refused = self.entry.human_result({'status': 'needs_attention', 'files': {}, 'limits_file': 'invalid'}, 'companion-compact')
        self.assertIn('Sinir ayari okunamadi', refused)


class CompactionPlanTest(unittest.TestCase):
    POINTER = 'Arşivlenen metin: `X/Arşiv/A-2026-09.md`'

    def plan(self, text, name, limit):
        result = compact_module.plan(text, name, limit, self.POINTER)
        lines = compact_module.LINE.findall(text)
        moved = set(result['moved_lines'])
        kept = ''.join(line for index, line in enumerate(lines) if index not in moved)
        newline = result['newline']
        pointer = self.POINTER + newline
        if pointer in result['live'] and compact_module.ADDED_HISTORY[name] not in result['live']:
            self.assertEqual(result['live'].replace(pointer, '', 1), kept)
        else:
            self.assertEqual(result['live'], (kept if kept.endswith('\n') else kept + newline) + newline +
                             compact_module.ADDED_HISTORY[name] + newline + newline + pointer)
        position = 0
        for index in sorted(moved):  # moved lines appear verbatim and in order in the archive
            found = result['archive'].find(lines[index].rstrip('\r\n'), position)
            self.assertGreaterEqual(found, 0, lines[index])
            position = found
        return result

    def test_append_ordered_entries_keep_the_bottom_one(self):
        text = ('# Son oturum\n\n' + ''.join(f'2026-09-{day:02d}: GÜN_{day} ' + FILLER + '\n\n' for day in range(1, 11)))
        result = self.plan(text, 'Last-Session.md', 400)
        # Oldest first and only until the file fits: the two newest stay.
        self.assertEqual(result['moved_entries'], 8)
        for day in (9, 10):
            self.assertIn(f'GÜN_{day} ', result['live'])
        for day in range(1, 9):
            self.assertNotIn(f'GÜN_{day} ', result['live'])
        self.assertLessEqual(len(result['live']), 400)
        tight = self.plan(text, 'Last-Session.md', 150)
        self.assertEqual(tight['moved_entries'], 9, 'the newest entry never moves, even when the file stays over')
        self.assertIn('GÜN_10', tight['live'])

    def test_same_day_entries_follow_the_file_direction(self):
        newest_first = '# Son oturum\n\n2026-09-24: ÜST\n\n2026-09-24: ALT ' + FILLER * 3 + '\n'
        self.assertIn('ÜST', self.plan(newest_first, 'Last-Session.md', 120)['live'])
        timed = '# Son oturum\n\n2026-09-24 09:00: SABAH ' + FILLER * 3 + '\n\n2026-09-24 21:30: AKŞAM\n'
        live = self.plan(timed, 'Last-Session.md', 120)['live']
        self.assertIn('AKŞAM', live)
        self.assertNotIn('SABAH', live)

    def test_same_day_thread_updates_keep_the_bottom_one(self):
        # The reproduction from #163: thread updates are appended, so the bottom one is newest.
        text = ('# Threads\n\n## Active Threads\n### Thread: Volt\n**Status:** Active\n'
                '2026-09-28 kontrol: ÜST hâlâ 0 testçi. ' + FILLER + '\n'
                '2026-09-28: ORTA 8 testçi kayıtlı. ' + FILLER + '\n'
                '**2026-09-28: ALT 12+ testçi, kapalı test başladı.**\n\n## Closed Threads\n')
        result = self.plan(text, 'Threads.md', len(text) - 60)
        self.assertIn('ALT 12+', result['live'])
        self.assertNotIn('ÜST', result['live'])
        self.assertIn('ÜST', result['archive'])

    def test_v2_session_headings_and_structured_handoff_blocks(self):
        text = ('# Last Session\n\n## Session: 2026-09-24 (Codex)\nYENİ\n### Açık kalanlar\n- test\n\n'
                '## Session: 2026-09-20\nESKİ ' + FILLER * 4 + '\n\n## Previous Sessions\n(none yet)\n')
        result = self.plan(text, 'Last-Session.md', 200)
        self.assertIn('## Session: 2026-09-24 (Codex)\nYENİ\n### Açık kalanlar\n- test\n', result['live'])
        self.assertIn('## Previous Sessions\n' + self.POINTER + '\n', result['live'])
        self.assertIn('## Session: 2026-09-20', result['archive'])
        self.assertIn('## Previous Sessions\n(none yet)\n', result['archive'])

    def test_multi_card_parallel_session_headings_keep_newest_card(self):
        text = (
            '# Son oturum\n\n'
            '## 2026-09-26 16:30 · ev · 3f9a1c2b\n'
            'EN_YENİ_KART: 4. oturum kararı ve sonraki adım.\n- test adımı\n\n'
            '## 2026-09-26 14:15 · ofis · 7c8b2d1a\n'
            'KART_3: 3. oturum kararı. ' + FILLER * 3 + '\n\n'
            '## 2026-09-26 11:00 · ev · 1a2b3c4d\n'
            'KART_2: 2. oturum kararı. ' + FILLER * 3 + '\n\n'
            '## 2026-09-26 09:00 · ofis · 9e8d7c6b\n'
            'EN_ESKİ_KART_1: 1. oturum kararı. ' + FILLER * 3 + '\n'
        )
        result = self.plan(text, 'Last-Session.md', 350)
        self.assertIn('## 2026-09-26 16:30 · ev · 3f9a1c2b\nEN_YENİ_KART: 4. oturum kararı', result['live'])
        self.assertNotIn('EN_ESKİ_KART_1', result['live'])
        self.assertIn('EN_ESKİ_KART_1', result['archive'])
        self.assertIn('## Önceki oturumlar\n\n' + self.POINTER, result['live'])
        self.assertLessEqual(len(result['live']), 350)

    def test_thread_fields_with_dates_code_fences_and_crlf_stay(self):
        text = ('# Threads\r\n## Active Threads\r\n### Thread: A\r\n**Status:** waiting, 2026-10-01\r\n'
                'Next action: 2026-09-30 tarihine kadar karar.\r\n- 2026-09-24 A_YENİ\r\n- 2026-09-10 A_ESKİ ' + FILLER * 3 + '\r\n'
                '```\r\n## 2026-01-01 kod içindeki başlık\r\n2026-01-02: kod satırı\r\n```\r\n'
                '### Thread: B\r\n2026-09-01: B_TEK ' + FILLER + '\r\n')
        result = self.plan(text, 'Threads.md', 400)
        live = result['live']
        self.assertNotIn('\n', live.replace('\r\n', ''))
        for kept in ('**Status:** waiting, 2026-10-01', 'Next action: 2026-09-30', 'A_YENİ', '### Thread: B\r\n2026-09-01: B_TEK'):
            self.assertIn(kept, live)
        # The fenced block is part of the older update: it moves with it, whole and in
        # order, and the heading-like line inside the fence never became a section.
        self.assertIn('### Thread: A\r\n- 2026-09-10 A_ESKİ ' + FILLER * 3 + '\r\n```\r\n## 2026-01-01 kod içindeki başlık\r\n'
                      '2026-01-02: kod satırı\r\n```\r\n', result['archive'])
        self.assertEqual(result['moved_entries'], 1)
        self.assertIn('## Kapanan konular\r\n\r\n' + self.POINTER + '\r\n', live)

    def test_undated_structure_after_an_old_entry_stays(self):
        text = ('# Son oturum\n\n## Session: 2026-09-24\nYENİ\n\n## Session: 2026-09-01\nESKİ ' + FILLER * 3 +
                '\n#### Ayrıntı\nESKİ_ALT\n\n## Notlar\nNOT_KALIR\n\n2026-08-01: ÇOK_ESKİ ' + FILLER + '\n\n### Bağlantılar\nBAĞ_KALIR\n')
        result = self.plan(text, 'Last-Session.md', 200)
        for kept in ('## Notlar\nNOT_KALIR\n', '### Bağlantılar\nBAĞ_KALIR\n', 'YENİ'):
            self.assertIn(kept, result['live'])
        for moved in ('ESKİ ', '#### Ayrıntı\nESKİ_ALT', 'ÇOK_ESKİ'):
            self.assertIn(moved, result['archive'])
            self.assertNotIn(moved, result['live'])

    def test_dated_lines_inside_the_newest_card_never_split_it(self):
        # #118: an older date quoted inside a card is card content, not an older entry, so
        # the newest card keeps its next step and the older card moves whole.
        newest_head = '## 2026-09-27 10:00 · ev · 955bfafa\nEN_YENİ_KART: PR hazırlandı.\n'
        next_step = '- Sıradaki: gözden geçirme.\n\n'
        older = ('## 2026-09-26 14:05 · ofis · 3f9a1c2b\nESKİ_KART: ' + FILLER * 3 + '\n'
                 '- 2026-09-25 ESKİ_KARTIN_MADDESİ\n### Ayrıntı\n2026-09-24: ESKİ_ALT\n')
        for inner in ('- 2026-09-20 kararı hâlâ geçerli.\n', '- Karar (2026-09-20) geçerli.\n',
                      '\n2026-09-20 kararı geçerli.\n', '### 2026-09-20 notları\nnot\n',
                      '- 2026-10-01 teslim tarihi.\n'):
            with self.subTest(inner=inner):
                newest = newest_head + inner + next_step
                result = self.plan('# Son oturum\n\n' + newest + older, 'Last-Session.md', 450)
                self.assertIn(newest, result['live'])
                self.assertIn(older, result['archive'])
                self.assertEqual(result['moved_entries'], 1, 'each card moves whole, as one entry')
                for moved in ('ESKİ_KART', 'ESKİ_KARTIN_MADDESİ', 'ESKİ_ALT'):
                    self.assertNotIn(moved, result['live'])
                self.assertLessEqual(len(result['live']), 450)

    def test_a_card_ends_at_a_heading_of_its_own_level(self):
        text = ('# Son oturum\n\n## 2026-09-27 10:00 · ev\nYENİ\n\n## 2026-09-26 09:00 · ofis\nORTA ' + FILLER * 2 +
                '\n\n## Notlar\nNOT_KALIR\n2026-09-01: NOTLARDAKİ_ESKİ ' + FILLER * 2 + '\n')
        result = self.plan(text, 'Last-Session.md', 200)
        self.assertEqual(result['moved_entries'], 2)
        self.assertIn('## 2026-09-27 10:00 · ev\nYENİ\n', result['live'])
        self.assertIn('## Notlar\nNOT_KALIR\n', result['live'])
        for moved in ('ORTA', 'NOTLARDAKİ_ESKİ'):
            self.assertIn(moved, result['archive'])
            self.assertNotIn(moved, result['live'])

    def test_dated_update_headings_keep_their_dated_items_in_threads(self):
        text = ('# Threads\n## Active Threads\n### Thread: A\n#### 2026-09-24 güncelleme\nA_YENİ\n'
                '- 2026-09-01 A_YENİNİN_MADDESİ\n#### 2026-09-10 güncelleme\nA_ESKİ ' + FILLER * 3 + '\n'
                '- 2026-09-02 A_ESKİNİN_MADDESİ\n')
        result = self.plan(text, 'Threads.md', 250)
        self.assertEqual(result['moved_entries'], 1)
        self.assertIn('#### 2026-09-24 güncelleme\nA_YENİ\n- 2026-09-01 A_YENİNİN_MADDESİ\n', result['live'])
        self.assertIn('### Thread: A\n#### 2026-09-10 güncelleme\nA_ESKİ ' + FILLER * 3 + '\n- 2026-09-02 A_ESKİNİN_MADDESİ\n',
                      result['archive'])

    def test_dated_lines_directly_under_active_threads_never_move(self):
        text = ('# Threads\n2026-09-02: BAŞLIK_ALTI\n## Active Threads\n' +
                ''.join(f'- 2026-09-{day:02d} KONU_{day} ' + FILLER + '\n' for day in range(1, 20)) +
                '### Thread: A\n2026-09-24: A_YENİ\n2026-09-01: A_ESKİ ' + FILLER + '\n')
        result = self.plan(text, 'Threads.md', 300)
        self.assertEqual(result['moved_entries'], 1)
        self.assertIn('A_ESKİ', result['archive'])
        for day in range(1, 20):
            self.assertIn(f'KONU_{day} ', result['live'])
        self.assertIn('BAŞLIK_ALTI', result['live'])
        self.assertIsNone(compact_module.plan(text.replace('2026-09-01: A_ESKİ', 'A_ESKİ'), 'Threads.md', 300, self.POINTER))
        turkish = text.replace('## Active Threads', '## Açık konular')
        self.assertEqual(self.plan(turkish, 'Threads.md', 300)['moved_entries'], 1)

    def test_nothing_movable_returns_none(self):
        text = '# Threads\n## Active Threads\n### Thread: A\n2026-09-24: TEK ' + FILLER * 5 + '\n'
        self.assertIsNone(compact_module.plan(text, 'Threads.md', 100, self.POINTER))
        self.assertIsNone(compact_module.plan('# Son oturum\n' + FILLER * 5, 'Last-Session.md', 100, self.POINTER))

    def test_frontmatter_stays_and_is_never_an_entry(self):
        text = ('---\nupdated: 2026-09-24\n---\n# Son oturum\n2026-09-24: YENİ\n\n2026-09-01: ESKİ ' + FILLER * 3 + '\n')
        result = self.plan(text, 'Last-Session.md', 150)
        self.assertTrue(result['live'].startswith('---\nupdated: 2026-09-24\n---\n# Son oturum\n2026-09-24: YENİ\n'))


class CompactionRaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='companion-race-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.directory = self.vault / COMPANION
        self.directory.mkdir(parents=True)
        self.state = Path(self.tmp.name) / 'state'
        self.live = self.directory / 'Last-Session.md'
        self.live.write_text(oversized_last_session(8), encoding='utf-8')
        self.archive = self.directory / 'Arşiv' / 'Last-Session-2026-09.md'

    def racing(self):
        original = compact_module._write

        def write(path, text):
            original(path, text)
            if Path(path).parent.name == 'Arşiv':
                with self.live.open('a', encoding='utf-8') as handle:
                    handle.write('CONCURRENT_EDIT\n')
        return mock.patch.object(compact_module, '_write', write)

    def test_a_concurrent_edit_wins_and_a_new_archive_is_removed(self):
        with self.racing():
            result = compact_module.compact(self.vault, self.state, now=NOW)
        self.assertEqual(result['status'], 'conflict')
        self.assertTrue(self.live.read_text(encoding='utf-8').endswith('CONCURRENT_EDIT\n'))
        self.assertIn('ENTRY_007', self.live.read_text(encoding='utf-8'))
        self.assertFalse(self.archive.exists())

    def test_a_concurrent_edit_during_a_needs_rewrite_backup_removes_the_backup(self):
        self.live.write_text('# Son oturum\n\n' + FILLER * 60, encoding='utf-8')
        with self.racing():
            result = compact_module.compact(self.vault, self.state, now=NOW)
        self.assertEqual(result['files']['Last-Session.md']['status'], 'conflict')
        self.assertNotIn('backup', result['files']['Last-Session.md'])
        self.assertTrue(self.live.read_text(encoding='utf-8').endswith('CONCURRENT_EDIT\n'))
        self.assertFalse(self.archive.exists())

    def test_a_concurrent_edit_restores_an_existing_archive_byte_for_byte(self):
        self.archive.parent.mkdir()
        self.archive.write_bytes(b'# Kullanicinin arsivi\n')
        with self.racing():
            result = compact_module.compact(self.vault, self.state, now=NOW)
        self.assertEqual(result['files']['Last-Session.md']['status'], 'conflict')
        self.assertEqual(self.archive.read_bytes(), b'# Kullanicinin arsivi\n')

    def test_concurrent_call_archive_is_not_reverted_on_conflict(self):
        """#193: A conflicting compact must not roll back if the archive was modified by another caller."""
        original = compact_module._write

        def write(path, text):
            original(path, text)
            if Path(path).parent.name == 'Arşiv':
                # Simulate another caller committing its own archive and live update
                self.archive.write_bytes(b'# CONCURRENT_CALLER_ARCHIVE_DATA\n')
                with self.live.open('a', encoding='utf-8') as handle:
                    handle.write('CONCURRENT_EDIT\n')

        with mock.patch.object(compact_module, '_write', write):
            result = compact_module.compact(self.vault, self.state, now=NOW)
        self.assertEqual(result['status'], 'conflict')
        self.assertTrue(self.archive.exists())
        self.assertEqual(self.archive.read_bytes(), b'# CONCURRENT_CALLER_ARCHIVE_DATA\n')

    def test_hygiene_counts_characters_and_names_the_directory(self):
        report = companion_module.hygiene(self.vault, self.state)
        self.assertEqual(report['directory'], COMPANION)
        # Characters as stored: on Windows this fixture is written with CRLF, and each CRLF counts
        # as two, the same measure companion-compact fits the file to.
        self.assertEqual(report['files']['Last-Session.md']['chars'], len(self.live.read_bytes().decode('utf-8')))
        self.assertEqual(report['over_limit'], ['Last-Session.md'])
        self.assertIn('Memory hygiene: Last-Session.md is', companion_module.hygiene_notice(report))


if __name__ == '__main__':
    unittest.main()
