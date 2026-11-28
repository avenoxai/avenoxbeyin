"""Plain-language Turkish daily summary for `recap --human [--days N] [--write]` (#970).

Synthetic fixtures only.
"""
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'tests') not in sys.path:
    sys.path.insert(0, str(ROOT / 'tests'))

from v3_package_helpers import install, isolated_env, run_python


FORBIDDEN_JARGON = (
    'refs_withheld',
    'undated_omitted',
    'stale_count',
    'recent_missing_receipts',
    'open_tasks',
    'dead-weight',
    'missing_source_omitted',
    'summary_withheld',
    'source_sync',
    'view_conflicts',
    'over_limit',
    'due_at',
)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_receipt_file(vault: Path, event_id: str, summary: str, refs: list[str],
                        created_at: str, session: str | None = None, harness: str = 'codex'):
    receipts_dir = vault / 'receipts'
    receipts_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        'kind': 'receipt',
        'event_id': event_id,
        'harness': harness,
        'refs': refs,
        'visibility': 'internal',
        'created_at': created_at,
    }
    if session is not None:
        metadata['session'] = session
    filename = hashlib.sha256(event_id.encode('utf-8')).hexdigest() + '.md'
    body = '---\n' + json.dumps(metadata, ensure_ascii=False, indent=2) + '\n---\n' + summary + '\n'
    (receipts_dir / filename).write_text(body, encoding='utf-8')


def _section_text(output: str, heading: str, next_heading: str | None = None) -> str:
    start = output.index(heading) + len(heading)
    if next_heading is None:
        return output[start:]
    end = output.index(next_heading, start)
    return output[start:end]


class RecapHumanDailySummaryTest(unittest.TestCase):
    def _make_vault(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-recap-human-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        vault, state = root / 'vault', root / 'state'
        vault.mkdir()
        env = isolated_env(root / 'home')
        installed = install(vault, state, env)
        self.assertEqual(installed.returncode, 0, installed.stderr.decode('utf-8', errors='replace'))
        return root, vault, state, env

    def _run_entry(self, vault: Path, args: list[str], env: dict[str, str]):
        return run_python(vault / 'beyin.py', args, vault, env)

    def _assert_no_jargon(self, text: str):
        for term in FORBIDDEN_JARGON:
            self.assertNotIn(term, text, f'Unexpected internal jargon {term!r} in output:\n{text}')

    def test_acceptance_970_plain_turkish_three_headings_no_jargon(self):
        """Fresh vault: 2 receipts (distinct sessions), 1 open task, 1 broken note."""
        _, vault, _, env = self._make_vault()
        (vault / 'notes').mkdir(parents=True, exist_ok=True)
        (vault / 'notes/rapor.md').write_text('# Rapor\n\nHaftalık rapor taslağı.\n', encoding='utf-8')
        (vault / 'notes/plan.md').write_text('# Plan\n\nQ4 hedefleri.\n', encoding='utf-8')
        # Broken/unreadable note (invalid frontmatter revision)
        (vault / 'notes/bozuk-not.md').write_text(
            '---\nrevision: invalid\n---\nBozuk not metni.\n',
            encoding='utf-8',
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        earlier_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        _write_receipt_file(
            vault, 'ev-sess-1', 'Rapor taslağı güncellendi', ['notes/rapor.md'],
            earlier_iso, session='sess-alpha',
        )
        _write_receipt_file(
            vault, 'ev-sess-2', 'Plan hedefleri netleştirildi', ['notes/plan.md'],
            now_iso, session='sess-beta',
        )

        task_req = vault / 'task-req.json'
        task_req.write_text(json.dumps({
            'source': 'tasks/teklif-gonder.md',
            'text': 'Müşteri teklifini son kez gözden geçir ve gönder.',
            'metadata': {
                'id': 'task-teklif-1',
                'title': 'Teklif dosyasını gönder',
                'kind': 'task',
                'revision': 1,
                'status': 'active',
                'owner': 'kullanici',
                'visibility': 'internal',
            },
        }, ensure_ascii=False), encoding='utf-8')
        created = self._run_entry(vault, ['task-create', '--file', str(task_req)], env)
        self.assertEqual(created.returncode, 0, created.stderr.decode('utf-8', errors='replace'))
        task_req.unlink()

        done = self._run_entry(vault, ['recap', '--human'], env)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(done.stderr, b'')
        text = done.stdout.decode('utf-8')

        self._assert_no_jargon(text)
        self.assertIn('Bugün yapılanlar', text)
        self.assertIn('Bekleyenler', text)
        self.assertIn('Sizden beklenen', text)

        sec_done = _section_text(text, 'Bugün yapılanlar', 'Bekleyenler')
        sec_pending = _section_text(text, 'Bekleyenler', 'Sizden beklenen')
        sec_attention = _section_text(text, 'Sizden beklenen')

        self.assertIn('Rapor taslağı güncellendi', sec_done)
        self.assertIn('notes/rapor.md', sec_done)
        self.assertIn('Plan hedefleri netleştirildi', sec_done)
        self.assertIn('notes/plan.md', sec_done)
        self.assertNotIn(' / (', sec_done)

        self.assertIn('Teklif dosyasını gönder', sec_pending)

        self.assertIn('notes/bozuk-not.md', sec_attention)
        # No raw body from broken note leaked
        self.assertNotIn('Bozuk not metni', text)

    def test_same_session_receipts_grouped_and_evidence_status_privacy_safe(self):
        """Receipts in the same session merge to one line; private and missing refs handled cleanly."""
        _, vault, _, env = self._make_vault()
        (vault / 'notes').mkdir(parents=True, exist_ok=True)
        (vault / 'notes/acik.md').write_text('# Açık\n\nAçık kayıt.\n', encoding='utf-8')
        (vault / 'notes/gizli.md').write_text(
            '---\nvisibility: private\n---\nGizli içerik.\n',
            encoding='utf-8',
        )

        now = datetime.now(timezone.utc)
        _write_receipt_file(
            vault, 'ev-g1', 'İlk adım tamamlandı', ['notes/acik.md'],
            (now - timedelta(minutes=10)).isoformat(), session='sess-same',
        )
        _write_receipt_file(
            vault, 'ev-g2', 'İkinci adım eklendi', ['notes/acik.md'],
            (now - timedelta(minutes=2)).isoformat(), session='sess-same',
        )
        _write_receipt_file(
            vault, 'ev-priv', 'Gizli özet sızmamalı', ['notes/gizli.md'],
            (now - timedelta(minutes=1)).isoformat(), session='sess-priv',
        )
        _write_receipt_file(
            vault, 'ev-miss', 'Silinmiş kaynakla iş', ['notes/artik-yok.md'],
            now.isoformat(), session='sess-miss',
        )

        done = self._run_entry(vault, ['recap', '--human'], env)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        text = done.stdout.decode('utf-8')
        self._assert_no_jargon(text)

        sec_done = _section_text(text, 'Bugün yapılanlar', 'Bekleyenler')
        bullets = [line for line in sec_done.splitlines() if line.strip().startswith('- ')]
        # 3 sessions total: sess-same (merged 1 line), sess-priv (1 line), sess-miss (1 line)
        self.assertEqual(len(bullets), 3, sec_done)
        merged_line = next(b for b in bullets if 'İlk adım tamamlandı' in b)
        self.assertIn('İkinci adım eklendi', merged_line)
        self.assertNotIn('Gizli özet sızmamalı', text)
        self.assertNotIn('notes/gizli.md', text)
        self.assertNotIn('Summary withheld', text)
        self.assertIn('özel kaynak', sec_done.lower())
        self.assertIn('bulunamıyor', sec_done.lower())

    def test_threads_next_inbox_over_limit_and_overdue_task(self):
        """Threads Next lines, unprocessed inbox items, over-limit file, and overdue task route cleanly."""
        _, vault, _, env = self._make_vault()
        comp = vault / '🔮 850-Companion'
        comp.mkdir(parents=True, exist_ok=True)
        (comp / 'Threads.md').write_text(
            '# Threads\n\n'
            '## Active Threads\n'
            '### Bütçe Planlaması\n'
            '**Status:** Aktif\n'
            '**Next:** Yönetici onayını al ve tabloyu paylaş\n\n'
            '```markdown\n'
            '### Fenced Example\n'
            '**Next:** Bu örnek satır görünmemeli\n'
            '```\n\n'
            '## Closed Threads\n'
            '### Eski Konu\n'
            '**Next:** Kapalı konu görünmemeli\n',
            encoding='utf-8',
        )
        # Over-limit Last-Session.md (> 3000 chars default limit)
        (comp / 'Last-Session.md').write_text(
            '# Last Session\n\n' + ('Uzun oturum kartı metni. ' * 200) + '\n',
            encoding='utf-8',
        )

        # Inbox: 1 unprocessed note + 1 already-processed yakala note
        inbox_dir = vault / 'inbox'
        inbox_dir.mkdir()
        (inbox_dir / 'ham-fikir.md').write_text('# Ham fikir\n\nSonra işlenecek.\n', encoding='utf-8')
        (inbox_dir / 'islenmis.md').write_text(
            '---\ntur: yakala\ndurum: islendi\n---\nİşlenmiş kaynak.\n',
            encoding='utf-8',
        )

        # Overdue open task + closed overdue task (closed must NOT appear)
        tasks_dir = vault / 'tasks'
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / 'geciken.md').write_text(
            '---\n'
            'id: task-overdue-1\n'
            'kind: task\n'
            'title: Fatura ödemesini tamamla\n'
            'status: active\n'
            'due_at: 2020-01-15\n'
            'revision: 1\n'
            'visibility: internal\n'
            '---\nÖdeme takibi.\n',
            encoding='utf-8',
        )
        (tasks_dir / 'tamamlanmis.md').write_text(
            '---\n'
            'id: task-closed-1\n'
            'kind: task\n'
            'title: Kapalı eski görev\n'
            'status: done\n'
            'due_at: 2020-01-10\n'
            'revision: 1\n'
            'visibility: internal\n'
            '---\nBitti.\n',
            encoding='utf-8',
        )

        done = self._run_entry(vault, ['recap', '--human'], env)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        text = done.stdout.decode('utf-8')
        self._assert_no_jargon(text)

        sec_pending = _section_text(text, 'Bekleyenler', 'Sizden beklenen')
        sec_attention = _section_text(text, 'Sizden beklenen')

        self.assertIn('Bütçe Planlaması', sec_pending)
        self.assertIn('Yönetici onayını al ve tabloyu paylaş', sec_pending)
        self.assertNotIn('Bu örnek satır görünmemeli', text)
        self.assertNotIn('Kapalı konu görünmemeli', text)
        self.assertIn('1 not', sec_pending)

        self.assertIn('Last-Session.md', sec_attention)
        self.assertIn('karakter', sec_attention)
        self.assertIn('Fatura ödemesini tamamla', sec_attention)
        self.assertIn('2020-01-15', sec_attention)
        self.assertNotIn('Kapalı eski görev', text)

    def test_empty_vault_shows_all_three_headings_with_calm_turkish_sentences(self):
        _, vault, _, env = self._make_vault()
        done = self._run_entry(vault, ['recap', '--human'], env)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        text = done.stdout.decode('utf-8')
        self._assert_no_jargon(text)

        sec_done = _section_text(text, 'Bugün yapılanlar', 'Bekleyenler').strip()
        sec_pending = _section_text(text, 'Bekleyenler', 'Sizden beklenen').strip()
        sec_attention = _section_text(text, 'Sizden beklenen').strip()

        self.assertTrue(sec_done, 'Empty done section must still have a calm Turkish sentence')
        self.assertTrue(sec_pending, 'Empty pending section must still have a calm Turkish sentence')
        self.assertTrue(sec_attention, 'Empty attention section must still have a calm Turkish sentence')

    def test_days_n_flag_and_json_backward_compatibility(self):
        _, vault, _, env = self._make_vault()
        (vault / 'notes').mkdir(parents=True, exist_ok=True)
        (vault / 'notes/kaynak.md').write_text('# Kaynak\n\nMetin.\n', encoding='utf-8')

        now = datetime.now(timezone.utc)
        three_days_ago = (now - timedelta(days=3)).isoformat()
        _write_receipt_file(vault, 'ev-old', 'Üç gün önceki çalışma', ['notes/kaynak.md'], three_days_ago, session='s-old')
        _write_receipt_file(vault, 'ev-now', 'Bugünkü çalışma', ['notes/kaynak.md'], now.isoformat(), session='s-now')

        # Default --human is 1 day (today)
        today_only = self._run_entry(vault, ['recap', '--human'], env)
        self.assertEqual(today_only.returncode, 0, today_only.stderr.decode('utf-8', errors='replace'))
        today_text = today_only.stdout.decode('utf-8')
        self.assertIn('Bugün yapılanlar', today_text)
        self.assertIn('Bugünkü çalışma', today_text)
        self.assertNotIn('Üç gün önceki çalışma', today_text)

        # Explicit --days 5 includes both and updates heading
        five_days = self._run_entry(vault, ['recap', '--human', '--days', '5'], env)
        self.assertEqual(five_days.returncode, 0, five_days.stderr.decode('utf-8', errors='replace'))
        five_text = five_days.stdout.decode('utf-8')
        self.assertIn('Son 5 günde yapılanlar', five_text)
        self.assertIn('Bugünkü çalışma', five_text)
        self.assertIn('Üç gün önceki çalışma', five_text)

        # Default piped recap (no --human) and explicit --json keep 7-day classic JSON contract
        for extra in ([], ['--json']):
            with self.subTest(extra=extra):
                raw = self._run_entry(vault, ['recap', *extra], env)
                self.assertEqual(raw.returncode, 0, raw.stderr.decode('utf-8', errors='replace'))
                payload = json.loads(raw.stdout.decode('utf-8'))
                self.assertEqual(payload['status'], 'ok')
                self.assertEqual(payload['timezone'], 'UTC')
                self.assertNotIn('daily_summary', payload)
                self.assertEqual(payload['total'], 2)
                self.assertEqual(sorted(payload['items'][0]), ['created_at', 'refs', 'source', 'summary'])

    def test_write_creates_and_updates_daily_note_preserving_user_content(self):
        _, vault, _, env = self._make_vault()
        (vault / 'notes').mkdir(parents=True, exist_ok=True)
        (vault / 'notes/kaynak.md').write_text('# Kaynak\n\nMetin.\n', encoding='utf-8')
        _write_receipt_file(
            vault, 'ev-w1', 'Sabah toplantısı notlandı', ['notes/kaynak.md'],
            datetime.now(timezone.utc).isoformat(), session='s-w1',
        )

        today_str = datetime.now().astimezone().date().isoformat()
        target = vault / 'daily' / f'{today_str}.md'
        self.assertFalse(target.exists())

        first = self._run_entry(vault, ['recap', '--human', '--write'], env)
        self.assertEqual(first.returncode, 0, first.stderr.decode('utf-8', errors='replace'))
        self.assertTrue(target.is_file())
        content1 = target.read_text(encoding='utf-8')
        self.assertIn('<!-- beyin-recap:start -->', content1)
        self.assertIn('<!-- beyin-recap:end -->', content1)
        self.assertIn('Günün özeti', content1)
        self.assertIn('Sabah toplantısı notlandı', content1)
        # Managed daily/v3 must NOT contain our recap markers
        v3_daily = vault / 'daily/v3' / f'{today_str}.md'
        if v3_daily.is_file():
            self.assertNotIn('<!-- beyin-recap:start -->', v3_daily.read_text(encoding='utf-8'))

        # Add user-written content before and after the managed block
        prefix = '# Kişisel Günlük\n\nSabah kahvesi notlarım burada.\n\n'
        suffix = '\n## Akşam düşünceleri\n\nKullanıcı notu korunmalı.\n'
        marker_start = content1.index('<!-- beyin-recap:start -->')
        marker_end = content1.index('<!-- beyin-recap:end -->') + len('<!-- beyin-recap:end -->')
        target.write_text(prefix + content1[marker_start:marker_end] + suffix, encoding='utf-8')

        # Add a second receipt and run --write again
        _write_receipt_file(
            vault, 'ev-w2', 'Öğleden sonra tasarım güncellendi', ['notes/kaynak.md'],
            datetime.now(timezone.utc).isoformat(), session='s-w2',
        )
        second = self._run_entry(vault, ['recap', '--human', '--write'], env)
        self.assertEqual(second.returncode, 0, second.stderr.decode('utf-8', errors='replace'))
        content2 = target.read_text(encoding='utf-8')
        self.assertTrue(content2.startswith(prefix), content2)
        self.assertTrue(content2.endswith(suffix), content2)
        self.assertEqual(content2.count('<!-- beyin-recap:start -->'), 1)
        self.assertEqual(content2.count('<!-- beyin-recap:end -->'), 1)
        self.assertIn('Sabah toplantısı notlandı', content2)
        self.assertIn('Öğleden sonra tasarım güncellendi', content2)

        # Idempotent third run with no changes
        third = self._run_entry(vault, ['recap', '--human', '--write'], env)
        self.assertEqual(third.returncode, 0, third.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(target.read_text(encoding='utf-8'), content2)

    def test_write_appends_to_existing_user_daily_note_without_markers(self):
        _, vault, _, env = self._make_vault()
        today_str = datetime.now().astimezone().date().isoformat()
        daily_dir = vault / 'daily'
        daily_dir.mkdir(parents=True, exist_ok=True)
        target = daily_dir / f'{today_str}.md'
        user_body = '# Benim Günlüğüm\n\nÖzel kullanıcı metni dokunulmadan kalmalı.\n'
        target.write_text(user_body, encoding='utf-8')

        done = self._run_entry(vault, ['recap', '--human', '--write'], env)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        updated = target.read_text(encoding='utf-8')
        self.assertTrue(updated.startswith(user_body), updated)
        self.assertIn('<!-- beyin-recap:start -->', updated)
        self.assertIn('Günün özeti', updated)
        self.assertIn('<!-- beyin-recap:end -->', updated)

    def test_write_requires_human_and_rejects_range_and_malformed_markers(self):
        _, vault, _, env = self._make_vault()
        for args in (['recap', '--write'], ['recap', '--write', '--json']):
            with self.subTest(args=args):
                res = self._run_entry(vault, args, env)
                self.assertNotEqual(res.returncode, 0)
                self.assertEqual(res.stdout, b'')
                self.assertIn('--write requires --human', res.stderr.decode('utf-8', errors='replace'))
        self.assertFalse((vault / 'daily').exists())

        ranged = self._run_entry(vault, ['recap', '--human', '--write', '--since', '2026-09-24'], env)
        self.assertNotEqual(ranged.returncode, 0)
        self.assertEqual(ranged.stdout, b'')
        self.assertFalse((vault / 'daily').exists())

        # Malformed unclosed marker refuses to overwrite user file
        today_str = datetime.now().astimezone().date().isoformat()
        (vault / 'daily').mkdir()
        target = vault / 'daily' / f'{today_str}.md'
        broken = '# Günlük\n\n<!-- beyin-recap:start -->\nYarım kalmış blok\n'
        target.write_text(broken, encoding='utf-8')
        bad = self._run_entry(vault, ['recap', '--human', '--write'], env)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(target.read_text(encoding='utf-8'), broken)

    def test_write_rejects_managed_receipt_index_and_symlink_or_junction(self):
        root, vault, _, env = self._make_vault()
        today_str = datetime.now().astimezone().date().isoformat()

        # Case 1: managed flat receipt-index file at daily/YYYY-MM-DD.md
        (vault / 'daily').mkdir()
        target = vault / 'daily' / f'{today_str}.md'
        managed = '---\ngenerated: true\nkind: receipt-index\n---\n# Recorded outcomes\n'
        target.write_text(managed, encoding='utf-8')
        refused = self._run_entry(vault, ['recap', '--human', '--write'], env)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(target.read_text(encoding='utf-8'), managed)
        target.unlink()
        (vault / 'daily').rmdir()

        # Case 2: real symlink (POSIX) or Windows directory junction (no admin needed) on daily/
        outside = root / 'outside-daily'
        outside.mkdir()
        daily_link = vault / 'daily'
        created_link = False
        if sys.platform == 'win32':
            mk = subprocess.run(
                ['cmd', '/c', 'mklink', '/J', str(daily_link), str(outside)],
                capture_output=True, text=True,
            )
            created_link = mk.returncode == 0
        else:
            try:
                daily_link.symlink_to(outside, target_is_directory=True)
                created_link = True
            except OSError:
                created_link = False

        if created_link:
            res = self._run_entry(vault, ['recap', '--human', '--write'], env)
            self.assertNotEqual(res.returncode, 0)
            self.assertEqual(list(outside.iterdir()), [], 'Must never write through symlink/junction')

        # Case 3: direct unit test of reparse/symlink rejection on target file (portable across OSes)
        recap_mod = _load_module('v3_recap_unit', ROOT / 'template/.claude/scripts/beyin_v3_recap.py')
        with tempfile.TemporaryDirectory(prefix='v3-recap-sec-') as sec_tmp:
            v2 = Path(sec_tmp) / 'vault'
            (v2 / 'daily').mkdir(parents=True)
            real_lstat = os.lstat

            class _FakeReparse:
                st_mode = stat.S_IFREG | 0o644
                st_reparse_tag = 0xA0000003
                st_file_attributes = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
                st_size = 10

            target2 = v2 / 'daily' / f'{today_str}.md'

            def fake_lstat(path, *a, **kw):
                if Path(path) == target2:
                    return _FakeReparse()
                return real_lstat(path, *a, **kw)

            with patch('os.lstat', side_effect=fake_lstat):
                with self.assertRaises(ValueError):
                    recap_mod.write_daily_summary(v2, '## Bugün yapılanlar\nTest.\n', today=date.fromisoformat(today_str))

    def test_non_utf8_stdout_fallback_and_control_char_sanitization(self):
        _, vault, state, env = self._make_vault()
        (vault / 'notes').mkdir(parents=True, exist_ok=True)
        (vault / 'notes/kaynak.md').write_text('# Kaynak\n\nMetin.\n', encoding='utf-8')
        _write_receipt_file(
            vault, 'ev-ctrl', 'Öz\x1b[31met\x07\u202e Şirket', ['notes/kaynak.md'],
            datetime.now(timezone.utc).isoformat(), session='s-ctrl',
        )

        # Direct CLI with strict ASCII stdout encoding must NOT crash with UnicodeEncodeError
        ascii_env = dict(env, PYTHONIOENCODING='ascii:strict')
        done = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
             '--vault', str(vault), '--state', str(state), 'recap', '--human'],
            cwd=vault, env=ascii_env, capture_output=True, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        ascii_out = done.stdout.decode('ascii')
        self.assertIn('Bugun yapilanlar', ascii_out)
        self.assertIn('Bekleyenler', ascii_out)
        self.assertIn('Sizden beklenen', ascii_out)
        self.assertNotIn('\x1b', ascii_out)
        self.assertNotIn('\x07', ascii_out)

        # Also check UTF-8 mode strips control/bidi chars cleanly while keeping Turkish letters
        utf8_done = self._run_entry(vault, ['recap', '--human'], env)
        self.assertEqual(utf8_done.returncode, 0, utf8_done.stderr.decode('utf-8', errors='replace'))
        utf8_out = utf8_done.stdout.decode('utf-8')
        self.assertFalse(any(ch in utf8_out for ch in '\x1b\x07\u202e'))
        self.assertIn('Şirket', utf8_out)

        # Direct stream wrapper fallback test without reconfigure()
        recap_mod = _load_module('v3_recap_stream', ROOT / 'template/.claude/scripts/beyin_v3_recap.py')
        buf = io.BytesIO()
        strict_stream = io.TextIOWrapper(buf, encoding='ascii', errors='strict')
        recap_mod.write_human_output('## Bugün yapılanlar\n- Şirket özeti.\n', stream=strict_stream)
        strict_stream.flush()
        written = buf.getvalue().decode('ascii')
        self.assertIn('Bugun yapilanlar', written)
        self.assertIn('Sirket ozeti', written)

    def test_skill_doc_mentions_recap_human(self):
        skill = (ROOT / 'template/.agents/skills/beyin/SKILL.md').read_text(encoding='utf-8')
        self.assertIn('recap --human', skill)
        self.assertIn('bugün ne yaptık', skill.lower())
        self.assertIn('nerede kaldık', skill.lower())


if __name__ == '__main__':
    unittest.main()
