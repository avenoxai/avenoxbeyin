"""Tests for #965: plain Turkish doctor --human wording, diacritics, fallback, and summary header."""
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import time
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



# beyin_v3_mutation.status() for an enabled queue file with no records in it.
QUEUE_ON = {'enabled': True, 'valid': True, 'identity': True, 'queue_exists': True, 'pending_count': 0,
            'conflict_count': 0, 'applied_count': 0, 'failed_count': 0, 'dismissed_count': 0, 'status': 'ok'}


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
    # Counts as beyin_v3_mutation.status() reports them: a conflict or failed record is not applied
    # until it is retried or dismissed, a pending one until --replay runs.
    ('queue conflict', {'mutation_queue': dict(QUEUE_ON, status='needs_attention', conflict_count=1)}, 1),
    ('queue failed', {'mutation_queue': dict(QUEUE_ON, status='needs_attention', failed_count=1)}, 1),
    ('queue pending', {'mutation_queue': dict(QUEUE_ON, status='pending', pending_count=2)}, 1),
    ('queue off with conflict', {'mutation_queue': dict(QUEUE_ON, enabled=False, status='needs_attention', conflict_count=1)}, 1),
    ('queue settings invalid and conflict', {'mutation_queue': dict(QUEUE_ON, valid=False, enabled=False,
                                                                    status='needs_attention', conflict_count=1)}, 2),
    ('jev cannot run in context mode', {'jev': {'mode': 'on', 'provider': 'typesafe', 'automatic_model_calls': True,
                                                'context_mode': 'session', 'last_24h': {'calls': 0},
                                                'context_mode_notice': "Automatic context requires context_mode 'turn'"}}, 1),
    ('scheduled check found something', {'scheduled_check': {'status': 'needs_attention', 'age_hours': 3,
                                                             'issues': {'sync_conflict': 1}}}, 1),
    ('scheduled check failed', {'scheduled_check': {'status': 'error', 'age_hours': 3, 'error': 'RuntimeError'}}, 1),
    # A live finding already covers what the earlier scheduled run saw: no second count.
    ('scheduled check and live fault', {'scheduled_check': {'status': 'needs_attention', 'age_hours': 3, 'issues': {'hook': 1}},
                                        'hook-error.json': {'at': 1.0, 'error': 'OSError'}}, 1),
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
    ('queue on and empty', {'mutation_queue': dict(QUEUE_ON, status='ok', applied_count=3, dismissed_count=1)}, 0),
    ('scheduled check healthy', {'scheduled_check': {'status': 'ok', 'age_hours': 3}}, 0),
    ('jev on in turn mode', {'jev': {'mode': 'on', 'provider': 'typesafe', 'automatic_model_calls': True,
                                     'context_mode': 'turn', 'last_24h': {'calls': 4}}}, 0),
    ('new release', {'updates': {'status': 'available', 'version': '9.9.9', 'release_url': 'https://example.invalid/r'}}, 0),
)

# Lines a report may carry under "nothing to do": '(bilgi)' advice, feature states, client states
# and the optional release notice. Anything else is a problem line and must be counted.
INFORMATION_LINE = re.compile('|'.join((
    r'.*\(bilgi\)',
    r'Beyin \S', r'Henüz gerçek bir istemci oturumu gözlenmedi', r'Oturum olayları normal şekilde gözleniyor',
    r'Sağlık kontrolü tamamlandı', r'Bekleyen iş: 0$',
    r'Kullanılmayan istemciler: ', r'[a-z]+: olay görüldü$',
    r'Akıllı bağlam desteği \(Jev\): ', r'Bağlam doluluk hatırlatması: (etkin|kapalı)',
    r'Son bilgi damıtması: (bugün|\d+ gün önce|henüz kavram notu)', r'Oturum kaydı kapsamı: ', r'Hermes: \d+ oturumda',
    r'Not kullanım takibi: ', r'Paralel oturum bildirimi: açık ', r'Oturum içi tekrar önleme: açık ',
    r'Çevrimdışı işlem kuyruğu: (açık|kapalı) \(0 bekleyen, 0 çakışma, 0 başarısız\)$', r'Çoklu cihaz: ',
    r'Sır süzgeci ', r'Hariç tutulan bileşenler: ', r'Sıcak klasörler ', r'Soğuk klasörler ',
    r'Beyin güncel: ', r'Yeni sürüm: ', r'Sürüm notları: ', r'Güncellemek için: ', r'Son kontrol: ',
    r'Sürüm (bildirimleri kapalı|kontrolü henüz yapılmadı|kontrolü yapılamadı)', r'Kurulu sürüm resmi',
)))


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

    def test_nothing_to_do_summary_never_sits_above_a_problem_line(self):
        # The reverse direction: under "Her şey yolunda" every line is information, and a
        # sub-report whose own verdict is needs_attention is always counted.
        reports = [('healthy', healthy_report())] + [(label, dict(healthy_report(), **overrides))
                                                     for label, overrides, _ in ATTENTION_GRID]
        for label, report in reports:
            with self.subTest(label):
                count = self.entry.doctor_attention_count(report)
                verdicts = [key for key, value in report.items()
                            if isinstance(value, dict) and value.get('status') == 'needs_attention']
                if verdicts:
                    self.assertGreater(count, 0, 'needs_attention not counted: ' + ', '.join(verdicts))
                if count:
                    continue
                for line in self.entry.human_result(report, 'doctor', '3.12.0').splitlines()[1:]:
                    self.assertTrue(INFORMATION_LINE.match(line), 'problem line under "nothing to do": ' + line)

    def test_docs_quote_the_lines_doctor_prints(self):
        # Every doctor --human line the user guides quote, pinned to the report that prints it:
        # UPDATE.md step 7 has users scan for a prefix before they delete a state directory.
        laya = {'mode': 'shadow', 'provider': 'laya', 'automatic_model_calls': False, 'last_24h': {'calls': 0}}
        vendored = {'status': 'none', 'excluded_dirs': [], 'vendored_hint': {
            'notes': 3, 'dirs': [{'dir': 'vendor/lib', 'notes': 3}], 'names': ['vendor'], 'truncated': False}}
        rows = (
            ('docs/v3/UPDATE.md', '`State konumu (bilgi):`', 'State konumu (bilgi): ',
             {'state_location': {'warnings': ['state_split: two'], 'sibling_state_roots': ['/a', '/b']}}),
            ('docs/v3/PREFERENCES.md', '`Son bilgi damıtması: 3 gün önce (o tarihten beri 12 oturum kaydı)`',
             'Son bilgi damıtması: 3 gün önce (o tarihten beri 12 oturum kaydı)',
             {'knowledge_freshness': {'last_distilled_at': '2026-11-01T00:00:00Z', 'days_ago': 3, 'receipts_since': 12}}),
            ('docs/v3/PREFERENCES.md', '`Son bilgi damıtması: henüz kavram notu damıtılmadı (toplam N oturum kaydı; çalıştıkça oluşur)`',
             'Son bilgi damıtması: henüz kavram notu damıtılmadı (toplam 0 oturum kaydı; çalıştıkça oluşur)', {}),
            ('docs/v3/PREFERENCES.md', '`Son bilgi damıtması: ölçülemedi (<hata>); daha sonra tekrar deneyin.`',
             'Son bilgi damıtması: ölçülemedi (OperationalError); daha sonra tekrar deneyin.',
             {'knowledge_freshness': {'status': 'unavailable', 'error': 'OperationalError'}}),
            ('docs/v3/PREFERENCES.md', '`Talimat çelişkisi: ...`', 'Talimat çelişkisi: ',
             {'instruction_conflicts': [{'file': 'AGENTS.md', 'snippet': 'derleyici yönetir'}]}),
            ('docs/v3/PREFERENCES.md', '`İndeks kapsamı (bilgi): ...`', 'İndeks kapsamı (bilgi): ', {'index_exclusions': vendored}),
            ('docs/v3/PREFERENCES.md', '`İndeks dışı klasör adları`', 'İndeks dışı klasör adları',
             {'index_exclusions': {'status': 'ok', 'excluded_dirs': ['3rdparty']}}),
            ('docs/v3/PREFERENCES.md', '`index.json geçersiz (...)`', 'index.json geçersiz (',
             {'index_exclusions': {'status': 'ok', 'excluded_dirs': [], 'error': 'not valid JSON'}}),
            ('docs/v3/PREFERENCES.md', '`İndeks kapsamı denetlenemedi (...)`', 'İndeks kapsamı denetlenemedi (',
             {'index_exclusions': {'status': 'unavailable', 'error': 'OSError'}}),
            ('docs/v3/JEV.md', '`doctor --human` çıktısında `gölge (yalnız)`', 'Akıllı bağlam desteği (Jev): gölge (yalnız)', {'jev': laya}),
        )
        for doc, quoted, printed, overrides in rows:
            with self.subTest(doc=doc, quoted=quoted):
                self.assertIn(quoted, (ROOT / doc).read_text(encoding='utf-8'))
                text = self.entry.human_result(dict(healthy_report(), **overrides), 'doctor', '3.12.0')
                self.assertIn(printed, text)
        # UPDATE.md's prefix reads the same on a console that cannot print Turkish letters.
        self.assertEqual(self.entry.turkish_to_ascii('State konumu (bilgi): '), 'State konumu (bilgi): ')
        self.assertIn('`Son bilgi damitmasi: ...`', (ROOT / 'docs/v3/PREFERENCES.md').read_text(encoding='utf-8'))
        self.assertIn('golge (yalniz)', self.entry.human_result(dict(laya, features={}), 'jev'))
        self.assertIn('`jev status` insan çıktısında `golge (yalniz)`', (ROOT / 'docs/v3/JEV.md').read_text(encoding='utf-8'))

    def test_queue_line_names_the_step_for_each_waiting_record(self):
        lines = {label: self.entry.human_result(dict(healthy_report(), **overrides), 'doctor', '3.12.0')
                 for label, overrides, _ in ATTENTION_GRID if label.startswith('queue')}
        self.assertIn('Çevrimdışı işlem kuyruğu: açık (0 bekleyen, 1 çakışma, 0 başarısız); incelemek için: '
                      'python3 beyin.py mutation-queue --list --status conflict; inceledikten sonra kayıt kimliğiyle '
                      '--retry ya da --dismiss', lines['queue conflict'].replace('py -3', 'python3'))
        self.assertIn('--list --status failed;', lines['queue failed'])
        self.assertIn('(2 bekleyen, 0 çakışma, 0 başarısız); kasaya uygulamak için: ', lines['queue pending'])
        self.assertIn('beyin.py mutation-queue --replay', lines['queue pending'])
        both = dict(healthy_report(), mutation_queue=dict(QUEUE_ON, status='needs_attention', pending_count=1,
                                                          conflict_count=1, failed_count=1))
        text = self.entry.human_result(both, 'doctor', '3.12.0')
        self.assertEqual(self.entry.doctor_attention_count(both), 1)  # one queue, one line
        self.assertIn('--replay; incelemek için: ', text)
        self.assertIn('beyin.py mutation-queue --list; inceledikten', text)  # both kinds: no --status filter
        self.assertNotIn('incelemek', lines['queue file unreadable'])

    def test_scheduled_record_read_by_the_real_reader_is_counted_and_named(self):
        scripts = ROOT / 'template/.claude/scripts'
        spec = importlib.util.spec_from_file_location('v3_doctor_human_scheduled', scripts / 'beyin_v3_scheduled.py')
        scheduled = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scheduled)
        finished = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 7200))
        with tempfile.TemporaryDirectory(prefix='v3-doctor-human-scheduled-') as tmp:
            record = Path(tmp) / scheduled.RECORD_FILE
            for status, issues, expected in (('needs_attention', {'sync_conflict': 1, 'sync_degraded': 2}, 1),
                                             ('error', {}, 1), ('ok', {}, 0)):
                with self.subTest(status):
                    record.write_text(json.dumps({'schema': 1, 'status': status, 'started_at': finished,
                                                  'finished_at': finished, 'issues': issues}),
                                      encoding='utf-8', newline='\n')
                    report = dict(healthy_report(), scheduled_check=scheduled.doctor(tmp))
                    self.assertEqual(report['scheduled_check']['status'], status)
                    self.assertEqual(self.entry.doctor_attention_count(report), expected)
                    text = self.entry.human_result(report, 'doctor', '3.12.0')
                    line = next(line for line in text.splitlines() if line.startswith('Zamanlanmış denetim'))
                    if status == 'needs_attention':
                        self.assertIn('2 saat önce, dikkat gerekiyor (eşitleme çakışması (1), eşitleme uyarısı (2)).', line)
                    if expected:
                        self.assertTrue(line.endswith(' beyin.py sync'), line)
                        self.assertNotIn('(bilgi)', line)
                    else:
                        self.assertEqual(line, 'Zamanlanmış denetim (bilgi): son kontrol 2 saat önce, sağlıklı.')

    def test_ascii_fallback_folds_every_letter_doctor_prints(self):
        # Doctor's own wording uses only letters the fallback folds, so a cp1252/cp437/ascii
        # console never shows '?' in it (resmî and hâlâ did before).
        reports = [healthy_report()] + [dict(healthy_report(), **overrides) for _, overrides, _ in ATTENTION_GRID]
        reports.append(dict(healthy_report(), updates={'status': 'ahead'}))
        reports.append(dict(healthy_report(), lifecycle={'claude': {'status': 'observed_metadata', 'events': ['Stop']}}))
        for report in reports:
            text = self.entry.human_result(report, 'doctor', '3.12.0')
            folded = self.entry.turkish_to_ascii(text)
            self.assertTrue(folded.isascii(), sorted({ch for ch in folded if not ch.isascii()}))

    def test_ascii_fallback_composes_decomposed_letters_and_keeps_utf8_bytes(self):
        import unicodedata
        decomposed = unicodedata.normalize('NFD', 'Arşiv İstanbul Kâğıt ığüşöç')
        self.assertNotEqual(decomposed, unicodedata.normalize('NFC', decomposed))
        for encoding in ('ascii', 'cp1252', 'cp437'):
            with self.subTest(encoding=encoding):
                stream = io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors='strict')
                self.assertEqual(self.entry.safe_stream_text(decomposed, stream), 'Arsiv Istanbul Kagit igusoc')
        stream = io.TextIOWrapper(io.BytesIO(), encoding='cp1254', errors='strict')
        self.assertEqual(self.entry.safe_stream_text(decomposed, stream), unicodedata.normalize('NFC', decomposed))
        # A stream that takes the text as it is gets it unchanged, decomposed or not.
        stream = io.TextIOWrapper(io.BytesIO(), encoding='utf-8', errors='strict')
        self.assertEqual(self.entry.safe_stream_text(decomposed, stream), decomposed)

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

    def test_installed_queue_conflict_is_counted_until_dismissed(self):
        # A queued note_create on a path that already exists replays as a conflict; doctor's own
        # status stays never_seen, so only the queue report says something is waiting.
        with tempfile.TemporaryDirectory(prefix='v3-doctor-human-queue-') as tmp:
            root = Path(tmp)
            vault = root / 'vault'
            vault.mkdir()
            env = isolated_env(root / 'home')
            installed = install(vault, root / 'state', env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            (vault / 'notes').mkdir(exist_ok=True)
            (vault / 'notes' / 'conflict.md').write_text('# Conflict\n\nbody\n', encoding='utf-8', newline='\n')
            mutation = root / 'mutation.json'
            mutation.write_text(json.dumps({'action': 'note_create', 'source': 'notes/conflict.md',
                                            'payload': {'text': '# New\n'}}), encoding='utf-8', newline='\n')

            def cli(*args):
                ran = run_python(vault / 'beyin.py', list(args), vault, env)
                self.assertEqual(ran.returncode, 0, ran.stderr)
                return ran.stdout.decode('utf-8')

            cli('sync')
            cli('mutation-queue', '--enable')
            cli('mutation-queue', '--enqueue', '--file', mutation)
            pending = cli('doctor', '--human')
            self.assertEqual(pending.splitlines()[0], 'Bakmanız gereken 1 şey var.', pending)
            self.assertIn('(1 bekleyen, 0 çakışma, 0 başarısız); kasaya uygulamak için: ', pending)
            replayed = json.loads(cli('mutation-queue', '--replay'))
            self.assertEqual(replayed['conflicts'], 1)
            payload = json.loads(cli('doctor', '--json'))
            self.assertEqual(payload['status'], 'never_seen')
            self.assertEqual(payload['mutation_queue']['status'], 'needs_attention')
            human = cli('doctor', '--human')
            self.assertEqual(human.splitlines()[0], 'Bakmanız gereken 1 şey var.', human)
            self.assertIn('(0 bekleyen, 1 çakışma, 0 başarısız); incelemek için: ', human)
            listed = json.loads(cli('mutation-queue', '--list', '--status', 'conflict'))
            records = listed['mutations'] if isinstance(listed, dict) else listed
            self.assertEqual(len(records), 1)
            cli('mutation-queue', '--dismiss', records[0]['mutation_id'])
            settled = cli('doctor', '--human')
            self.assertEqual(settled.splitlines()[0], 'Her şey yolunda; yapmanız gereken bir şey yok.', settled)


if __name__ == '__main__':
    unittest.main()
