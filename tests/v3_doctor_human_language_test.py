"""Tests for #965: plain Turkish doctor --human wording, diacritics, fallback, and summary header."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from v3_package_helpers import install, isolated_env, run_python

ROOT = Path(__file__).resolve().parents[1]


def load_entry():
    spec = importlib.util.spec_from_file_location('v3_doctor_human_lang_entry', ROOT / 'scripts/beyin_entry.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def healthy_report():
    return {
        'status': 'never_seen',
        'pending_events': 0,
        'acknowledged_events': 0,
        'lifecycle': {
            'codex': {'status': 'never_seen', 'events': []},
            'claude': {'status': 'never_seen', 'events': []},
            'antigravity': {'status': 'never_seen', 'events': []},
        },
        'hook-health.json': None,
        'hook-error.json': None,
        'hook_paths': {'status': 'ok', 'checked': 10},
        'tracked_machine_files': {'status': 'skipped', 'tracked': [], 'reason': 'not_git_repository'},
        'state_location': {'warnings': []},
        'updates': {'status': 'unknown'},
        'companion_hygiene': {'status': 'ok', 'over_limit': [], 'files': {}},
        'companion_rules_opening': {'status': 'ok', 'budget': 5000, 'chars': 300, 'share': 300, 'clipped': False},
        'instruction_references': {'checked_files': 4, 'dead_count': 0, 'dead': [], 'truncated': False},
        'jev': {'mode': 'off', 'configured': False},
        'knowledge_freshness': {
            'last_distilled_at': None,
            'days_ago': None,
            'receipts_since': 0,
            'total_receipts': 0,
            'latest_source': None,
        },
        'instruction_conflicts': [],
        'skill_conflicts': [],
        'receipt_view_conflicts': [],
        'task_completion': {'strict_issue_count': 0, 'strict_issues': []},
        'validity': {'ignored_rejection_count': 0, 'ignored_rejections': []},
        'supersedes': {'dead_count': 0, 'dead': [], 'truncated': False},
        'boundary': {'status': 'attention', 'findings': ['no_root_obsidian: missing']},
        'backup': {'status': 'unknown', 'backup_verified': False},
        'project_scopes': {'status': 'absent'},
    }


FORBIDDEN_JARGON = (
    'dead-weight',
    'olu agirlik',
    'not_git_repository',
    'skipped (',
    'git_unavailable',
    'git_timeout',
    'not_worktree',
    'makbuz',
    'Makbuz',
    'Jev: kapali',
    'Henuz ',
    'Hafiza hijyeni',
    'Son bilgi damitmasi',
)



# One fault per row: (label, report overrides, expected summary count). A counted row must
# print its own line; a check that prints nothing (an 'unavailable' optional report) counts 0.
ATTENTION_GRID = (
    ('sync degraded', {'hook-health.json': {'sync': {'status': 'degraded'}}}, 1),
    ('sync conflict', {'hook-health.json': {'sync': {'status': 'conflict'}}}, 1),
    ('hook error', {'hook-error.json': {'at': 1.0, 'error': 'OSError'}}, 1),
    ('pending events', {'status': 'pending', 'pending_events': 2}, 1),
    ('stale hook paths', {'hook_paths': {'status': 'stale', 'stale': [{'file': '.codex/hooks.json', 'reason': 'unreadable'}]}}, 1),
    ('stale bridges', {'global_bridge_paths': {'status': 'stale', 'stale': [{'harness': 'codex'}]}}, 1),
    ('stale omp copy', {'omp_global_hook': {'path': '/home/u/.omp/agent/hooks/pre/beyin-v3.ts', 'stale': True}}, 1),
    ('skill conflict', {'skill_conflicts': ['beyin']}, 1),
    ('instruction conflict', {'instruction_conflicts': [{'file': 'AGENTS.md', 'snippet': 'derle'}]}, 1),
    ('receipt view conflict', {'receipt_view_conflicts': [{'source': 'daily/v3/2026-11-01.md'}]}, 1),
    ('exclusions error', {'exclusions_error': 'not valid JSON'}, 1),
    ('companion over limit', {'companion_hygiene': {'status': 'over_limit', 'over_limit': ['Threads.md'],
                                                    'files': {'Threads.md': {'chars': 9000, 'limit': 8000}}}}, 1),
    ('companion unavailable', {'companion_hygiene': {'status': 'unavailable', 'error': 'OSError'}}, 1),
    ('rules clipped', {'companion_rules_opening': {'status': 'clipped', 'chars': 9000, 'budget': 5000, 'share': 2400}}, 1),
    ('rules stale', {'companion_rules_opening': {'status': 'stale'}}, 1),
    ('project rules clipped', {'companion_rules_opening': {'status': 'ok', 'budget': 5000, 'project_rules': [
        {'status': 'clipped', 'source': 'projects/a/Kurallar.md', 'chars': 4000, 'share': 2000}]}}, 1),
    ('project rules stale', {'companion_rules_opening': {'status': 'ok', 'project_rules': [
        {'status': 'stale', 'source': 'projects/a/Kurallar.md'}]}}, 1),
    ('task completion', {'task_completion': {'strict_issue_count': 2, 'strict_issues': []}}, 1),
    ('task completion error', {'task_completion': {'strict_issue_count': 0, 'error': 'OperationalError: x'}}, 1),
    ('validity', {'validity': {'ignored_rejection_count': 1, 'ignored_rejections': []}}, 1),
    ('validity error', {'validity': {'ignored_rejection_count': 0, 'error': 'OperationalError: x'}}, 1),
    ('project scopes error', {'project_scopes': {'status': 'error', 'error': 'invalid JSON: JSONDecodeError'}}, 1),
    ('project scopes warning', {'project_scopes': {'status': 'warning', 'missing_folders': ['Gone']}}, 1),
    ('project scopes unavailable', {'project_scopes': {'status': 'unavailable', 'error': 'OSError'}}, 0),
    ('recovery required', {'updates': {'status': 'recovery_required'}}, 1),
    ('context fill invalid', {'context_fill': {'status': 'invalid', 'error': 'bad value'}}, 1),
    ('jev config invalid', {'jev': {'mode': 'off', 'configured': True, 'config_valid': False}}, 1),
    ('knowledge freshness unavailable', {'knowledge_freshness': {'status': 'unavailable', 'error': 'OperationalError'}}, 1),
    ('references unavailable', {'instruction_references': {'status': 'unavailable', 'error': 'PermissionError'}}, 1),
    ('dead references', {'instruction_references': {'dead_count': 1, 'dead': [{'file': 'AGENTS.md', 'line': 1, 'target': 'Yok'}]}}, 1),
    ('dead supersedes', {'supersedes': {'dead_count': 1, 'dead': [
        {'source': 'a.md', 'value': 'b', 'reason': 'unresolved supersedes reference'}]}}, 1),
    ('parallel invalid', {'parallel_sessions': {'valid': False}}, 1),
    ('parallel unavailable', {'parallel_sessions': {'status': 'unavailable', 'error': 'OSError'}}, 0),
    ('dedupe invalid', {'context_dedupe': {'valid': False}}, 1),
    ('dedupe unavailable', {'context_dedupe': {'status': 'unavailable', 'error': 'OSError'}}, 0),
    ('queue settings invalid', {'mutation_queue': {'valid': False}}, 1),
    ('queue file unreadable', {'mutation_queue': {'valid': True, 'enabled': True, 'queue_exists': True, 'status': 'invalid'}}, 1),
    ('queue unavailable', {'mutation_queue': {'status': 'unavailable', 'error': 'OSError'}}, 0),
    ('devices invalid', {'devices': {'valid': False}}, 1),
    ('devices unavailable', {'devices': {'status': 'unavailable', 'error': 'OSError'}}, 0),
    ('index scope unavailable', {'index_exclusions': {'status': 'unavailable', 'error': 'OSError'}}, 1),
    ('index json invalid', {'index_exclusions': {'status': 'ok', 'excluded_dirs': [], 'error': 'not valid JSON'}}, 1),
    ('state split', {'state_location': {'warnings': ['state_split: two'], 'sibling_state_roots': ['/a', '/b']}}, 1),
    ('unknown state warning', {'state_location': {'warnings': ['future_code: later']}}, 0),
    ('receipt line endings', {'receipt_line_endings': {'status': 'warning'}}, 1),
    ('tracked machine files', {'tracked_machine_files': {'status': 'warning', 'tracked': ['.beyin-runtime.json']}}, 1),
    ('git check unavailable', {'tracked_machine_files': {'status': 'unavailable', 'reason': 'git_timeout'}}, 1),
    # Information only: never counted.
    ('backup remote', {'backup': {'status': 'remote_configured', 'backup_verified': False}}, 0),
    ('unmanaged skills', {'skill_unmanaged': ['mine']}, 0),
    ('note usage', {'note_usage': {'tracked': 3, 'dead_weight_count': 1, 'feedback_totals': {}}}, 0),
    ('receipt coverage', {'receipt_coverage': {'total': 4, 'covered': 1, 'ratio': 0.25, 'last_7d': {}}}, 0),
    ('storage compact', {'state_storage': {'compact_suggested': True, 'database_bytes': 9000000, 'free_bytes': 5000000}}, 0),
    ('features on', {'parallel_sessions': {'enabled': True, 'active': 1}, 'context_dedupe': {'enabled': True, 'sessions': 2},
                     'mutation_queue': {'enabled': True, 'valid': True}, 'devices': {'enabled': True, 'level': 'icloud'}}, 0),
)


class DoctorHumanLanguageUnitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entry = load_entry()

    def test_healthy_report_starts_with_all_good_summary_and_turkish_diacritics(self):
        text = self.entry.human_result(healthy_report(), 'doctor', '3.12.0')
        first_line = text.splitlines()[0]
        self.assertTrue(first_line.startswith('Her şey yolunda'), first_line)
        self.assertEqual(self.entry.doctor_attention_count(healthy_report()), 0)
        for ch in ('ş', 'ğ', 'ı', 'ö', 'ü', 'ç'):
            self.assertIn(ch, text, f'Expected Turkish character {ch!r} in doctor --human output')
        for forbidden in FORBIDDEN_JARGON:
            self.assertNotIn(forbidden, text, f'Forbidden jargon {forbidden!r} appeared in doctor --human')

    def test_git_skipped_and_unavailable_reasons_use_plain_turkish_without_raw_codes(self):
        cases = (
            ({'status': 'skipped', 'reason': 'not_git_repository'}, 'Git deposu değil', 0),
            ({'status': 'skipped', 'reason': 'not_worktree'}, 'Git çalışma alanı değil', 0),
            ({'status': 'skipped', 'reason': 'git_unavailable'}, 'Git bulunamadı', 0),
            ({'status': 'unavailable', 'reason': 'git_timeout'}, 'yanıt vermediği', 1),
            ({'status': 'unavailable', 'reason': 'sparse_checkout_or_index'}, 'Kısmi Git çalışma alanı', 1),
            ({'status': 'unavailable', 'reason': 'git_error'}, 'Git bilgisi okunamadığı', 1),
        )
        for tracked, expected_phrase, expected_count in cases:
            with self.subTest(tracked=tracked):
                report = dict(healthy_report(), tracked_machine_files=tracked)
                text = self.entry.human_result(report, 'doctor', '3.12.0')
                self.assertEqual(self.entry.doctor_attention_count(report), expected_count)
                self.assertIn(expected_phrase, text)
                self.assertNotIn('skipped (', text)
                self.assertNotIn(tracked['reason'], text)

    def test_note_usage_uses_plain_turkish_without_dead_weight_jargon(self):
        report = dict(
            healthy_report(),
            note_usage={
                'tracked': 12,
                'dead_weight_count': 3,
                'feedback_totals': {'ok': 5, 'wrong': 1, 'stale': 2},
            },
        )
        text = self.entry.human_result(report, 'doctor', '3.12.0')
        self.assertIn('Not kullanım takibi: 12 kayıt, 3 hiç kullanılmayan not', text)
        self.assertIn('faydalı: 5, yanlış: 1, eskimiş: 2', text)
        self.assertNotIn('dead-weight', text)
        self.assertNotIn('olu agirlik', text)

    def test_jev_line_explains_feature_in_plain_turkish(self):
        off_text = self.entry.human_result(healthy_report(), 'doctor', '3.12.0')
        self.assertIn('Akıllı bağlam desteği (Jev): kapalı', off_text)
        self.assertNotIn('Jev: kapali', off_text)

        on_report = dict(
            healthy_report(),
            jev={
                'mode': 'on',
                'provider': 'typesafe',
                'automatic_model_calls': True,
                'last_24h': {'calls': 4},
            },
        )
        on_text = self.entry.human_result(on_report, 'doctor', '3.12.0')
        self.assertIn('Akıllı bağlam desteği (Jev): açık', on_text)
        self.assertIn('otomatik bağlam: açık', on_text)
        self.assertIn('4 çağrı', on_text)

    def test_receipt_coverage_and_knowledge_freshness_replace_makbuz_with_plain_turkish(self):
        report = dict(
            healthy_report(),
            receipt_coverage={
                'total': 4,
                'covered': 3,
                'ratio': 0.75,
                'missing_no_observed_edit': 1,
                'last_7d': {'total': 4, 'ratio': 0.75},
                'hermes_without_terminal': 2,
            },
            knowledge_freshness={
                'last_distilled_at': '2026-10-01T00:00:00Z',
                'days_ago': 3,
                'receipts_since': 7,
                'total_receipts': 10,
            },
        )
        text = self.entry.human_result(report, 'doctor', '3.12.0')
        self.assertIn('Oturum kaydı kapsamı: %75 (3/4 oturum, son 7 gün: %75); 1 eksik oturumda düzenleme gözlenmedi', text)
        self.assertIn('Hermes: 2 oturumda son tur için kapanış sinyali gözlenmedi (oturum kaydı kapsamı dışında).', text)
        self.assertIn('Son bilgi damıtması: 3 gün önce (o tarihten beri 7 oturum kaydı)', text)
        self.assertNotIn('makbuz', text.lower())

    def test_actionable_summary_header_counts_real_issues(self):
        # 1) Over-limit companion file
        over_limit = dict(
            healthy_report(),
            companion_hygiene={
                'status': 'over_limit',
                'over_limit': ['Threads.md'],
                'files': {'Threads.md': {'chars': 12000, 'limit': 8000}},
            },
        )
        text = self.entry.human_result(over_limit, 'doctor', '3.12.0')
        self.assertEqual(text.splitlines()[0], 'Bakmanız gereken 1 şey var.')
        self.assertIn('Hafıza hijyeni: Threads.md 12000 karakter (sınır 8000).', text)
        self.assertIn('arşive taşınır', text)

        # 2) Degraded sync
        degraded = dict(
            healthy_report(),
            status='needs_attention',
            **{'hook-health.json': {'sync': {'status': 'degraded'}}},
        )
        text = self.entry.human_result(degraded, 'doctor', '3.12.0')
        self.assertEqual(text.splitlines()[0], 'Bakmanız gereken 1 şey var.')
        self.assertIn('beyin.py sync', text)

        # 3) Hook error (without leaking raw internal error fields)
        hook_err = dict(
            healthy_report(),
            status='needs_attention',
            **{'hook-error.json': {'error': 'INTERNAL_SECRET_TRACEBACK'}},
        )
        text = self.entry.human_result(hook_err, 'doctor', '3.12.0')
        self.assertEqual(text.splitlines()[0], 'Bakmanız gereken 1 şey var.')
        self.assertNotIn('INTERNAL_SECRET_TRACEBACK', text)

        # 4) Clipped Kurallar.md
        clipped = dict(
            healthy_report(),
            companion_rules_opening={
                'status': 'clipped',
                'chars': 9000,
                'budget': 5000,
                'share': 2400,
            },
        )
        text = self.entry.human_result(clipped, 'doctor', '3.12.0')
        self.assertEqual(text.splitlines()[0], 'Bakmanız gereken 1 şey var.')
        self.assertIn('Kurallar.md açılışta kırpılıyor', text)

        # 5) Combined multiple issues: 2 over-limit + 1 hook error + 1 degraded sync = 4
        combined = dict(
            healthy_report(),
            status='needs_attention',
            companion_hygiene={
                'status': 'over_limit',
                'over_limit': ['Threads.md', 'Last-Session.md'],
                'files': {
                    'Threads.md': {'chars': 12000, 'limit': 8000},
                    'Last-Session.md': {'chars': 6000, 'limit': 3000},
                },
            },
            **{
                'hook-health.json': {'sync': {'status': 'degraded'}},
                'hook-error.json': {'error': 'TimeoutError'},
            },
        )
        text = self.entry.human_result(combined, 'doctor', '3.12.0')
        self.assertEqual(text.splitlines()[0], 'Bakmanız gereken 4 şey var.')

        # 6) Safety net: bare needs_attention still counts as at least 1
        bare = {'status': 'needs_attention'}
        text = self.entry.human_result(bare, 'doctor', '3.12.0')
        self.assertEqual(text.splitlines()[0], 'Bakmanız gereken 1 şey var.')

    def test_every_counted_item_prints_its_own_line(self):
        baseline = self.entry.human_result(healthy_report(), 'doctor', '3.12.0').splitlines()
        self.assertEqual(baseline[0], 'Her şey yolunda; yapmanız gereken bir şey yok.')
        for label, overrides, expected in ATTENTION_GRID:
            with self.subTest(label):
                report = dict(healthy_report(), **overrides)
                count = self.entry.doctor_attention_count(report)
                self.assertEqual(count, expected)
                lines = self.entry.human_result(report, 'doctor', '3.12.0').splitlines()
                self.assertEqual(lines[0], 'Her şey yolunda; yapmanız gereken bir şey yok.' if expected == 0
                                 else f'Bakmanız gereken {expected} şey var.')
                new = [line for line in lines[1:] if line not in baseline and not line.startswith('Ajanınıza "beyin doktor"')]
                if expected:
                    self.assertTrue(new, 'counted but printed nothing: ' + label)

    def test_stream_encoding_fallback_handles_utf8_cp1254_cp1252_ascii_and_unknown(self):
        sample = self.entry.human_result(
            dict(
                healthy_report(),
                companion_hygiene={
                    'status': 'over_limit',
                    'over_limit': ['Threads.md'],
                    'files': {'Threads.md': {'chars': 9500, 'limit': 8000}},
                },
                index_exclusions={
                    'status': 'none',
                    'excluded_dirs': [],
                    'archive_hint': {
                        'notes': 2,
                        'dirs': [{'dir': '📦 900-Archive', 'notes': 2}],
                        'names': ['📦 900-Archive'],
                        'truncated': False,
                    },
                },
            ),
            'doctor',
            '3.12.0',
        )

        # UTF-8 strict stream preserves full Turkish diacritics and emoji
        utf8_buf = io.BytesIO()
        utf8_stream = io.TextIOWrapper(utf8_buf, encoding='utf-8', errors='strict')
        self.entry.print_human(sample, utf8_stream)
        utf8_stream.flush()
        utf8_out = utf8_buf.getvalue().decode('utf-8')
        self.assertIn('Bakmanız gereken 1 şey var.', utf8_out)
        self.assertIn('Hafıza hijyeni: Threads.md 9500 karakter (sınır 8000).', utf8_out)
        self.assertIn('📦 900-Archive', utf8_out)

        # Turkish Windows cp1254 strict stream preserves Turkish diacritics without crashing on emoji
        cp1254_buf = io.BytesIO()
        cp1254_stream = io.TextIOWrapper(cp1254_buf, encoding='cp1254', errors='strict')
        self.entry.print_human(sample, cp1254_stream)
        cp1254_stream.flush()
        cp1254_out = cp1254_buf.getvalue().decode('cp1254')
        self.assertIn('Bakmanız gereken 1 şey var.', cp1254_out)
        self.assertIn('Hafıza hijyeni', cp1254_out)

        # Strict ASCII and cp1252 streams transliterate Turkish letters cleanly without UnicodeEncodeError or '?'
        for encoding in ('ascii', 'cp1252'):
            with self.subTest(encoding=encoding):
                raw = io.BytesIO()
                stream = io.TextIOWrapper(raw, encoding=encoding, errors='strict')
                self.entry.print_human(sample, stream)
                stream.flush()
                decoded = raw.getvalue().decode(encoding)
                self.assertIn('Bakmaniz gereken 1 sey var.', decoded)
                self.assertIn('Hafiza hijyeni: Threads.md 9500 karakter (sinir 8000).', decoded)
                self.assertIn('arsive tasinir', decoded)
                self.assertNotIn('?ey', decoded)
                self.assertNotIn('s?n?r', decoded)
                self.assertNotIn('Haf?za', decoded)

        # Unknown codec stream falls back cleanly to ASCII
        class UnknownEncodingStream(io.StringIO):
            encoding = 'nonexistent-codec-965'

        unknown_stream = UnknownEncodingStream()
        self.entry.print_human(sample, unknown_stream)
        unknown_out = unknown_stream.getvalue()
        self.assertIn('Bakmaniz gereken 1 sey var.', unknown_out)
        self.assertTrue(unknown_out.isascii())

        # StringIO (encoding=None) keeps full UTF-8 Turkish diacritics
        mem_stream = io.StringIO()
        self.entry.print_human(sample, mem_stream)
        self.assertIn('Bakmanız gereken 1 şey var.', mem_stream.getvalue())


class DoctorHumanInstalledCliTest(unittest.TestCase):
    def test_installed_doctor_human_vs_json_healthy_and_over_limit(self):
        with tempfile.TemporaryDirectory(prefix='v3-doctor-human-tr-') as tmp:
            root = Path(tmp)
            vault = root / 'vault'
            vault.mkdir()
            state = root / 'state'
            env = isolated_env(root / 'home')
            installed = install(vault, state, env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            synced = run_python(vault / 'beyin.py', ['sync'], vault, env)
            self.assertEqual(synced.returncode, 0, synced.stderr)

            # Healthy vault: --human starts with all-good summary and proper Turkish, no jargon
            human = run_python(vault / 'beyin.py', ['doctor', '--human'], vault, env)
            self.assertEqual(human.returncode, 0, human.stderr)
            text = human.stdout.decode('utf-8')
            self.assertTrue(text.splitlines()[0].startswith('Her şey yolunda'), text)
            for ch in ('ş', 'ğ', 'ı', 'ö', 'ü', 'ç'):
                self.assertIn(ch, text)
            for forbidden in FORBIDDEN_JARGON:
                self.assertNotIn(forbidden, text)

            # --json stays completely machine-structured and unaffected
            machine = run_python(vault / 'beyin.py', ['doctor', '--json'], vault, env)
            self.assertEqual(machine.returncode, 0, machine.stderr)
            payload = json.loads(machine.stdout.decode('utf-8'))
            self.assertEqual(payload['status'], 'never_seen')
            self.assertEqual(payload['companion_hygiene']['over_limit'], [])
            self.assertIn('tracked_machine_files', payload)
            self.assertIn('jev', payload)
            self.assertIn('note_usage', payload)
            self.assertNotIn('Her şey yolunda', machine.stdout.decode('utf-8'))

            # Make Threads.md exceed the 8000-char limit -> summary header reports 1 item
            threads = vault / '🔮 850-Companion' / 'Threads.md'
            threads.write_text('# Threads\n\n' + ('- Aktif konu satırı.\n' * 500), encoding='utf-8')
            human_warn = run_python(vault / 'beyin.py', ['doctor', '--human'], vault, env)
            self.assertEqual(human_warn.returncode, 0, human_warn.stderr)
            warn_text = human_warn.stdout.decode('utf-8')
            self.assertEqual(warn_text.splitlines()[0], 'Bakmanız gereken 1 şey var.')
            self.assertIn('Hafıza hijyeni: Threads.md', warn_text)
            self.assertIn('arşive taşınır', warn_text)

            # --json still reports over_limit unchanged
            machine_warn = run_python(vault / 'beyin.py', ['doctor', '--json'], vault, env)
            self.assertEqual(machine_warn.returncode, 0, machine_warn.stderr)
            warn_payload = json.loads(machine_warn.stdout.decode('utf-8'))
            self.assertEqual(warn_payload['companion_hygiene']['over_limit'], ['Threads.md'])


if __name__ == '__main__':
    unittest.main()
