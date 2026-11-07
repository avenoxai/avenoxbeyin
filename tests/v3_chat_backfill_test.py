"""Explicit Claude Code backfill into chat traces (#599): synthetic transcripts in a temp HOME."""
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import clean_environ, inherited_env, isolated_env

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))

CANARY = 'SYNTHETIC_HIDDEN_TEXT_MUST_NOT_SURFACE'
SECRET = 'sk-' + 'A1b2C3d4' * 4
TURKISH = 'Şifre kuralı, Müşteri dosyası ve Arşiv klasörü konuşuldu.'


def stamp(day):
    return day + 'T12:00:00.000Z'  # midday UTC keeps the local day stable in every timezone


class Transcript:
    """Records shaped like Claude Code 2.1 JSONL lines."""

    def __init__(self, sid, cwd, day):
        self.sid, self.cwd, self.day, self.lines = sid, str(cwd), day, []

    def add(self, kind, content, **extra):
        record = {'parentUuid': None, 'isSidechain': False, 'userType': 'external', 'cwd': self.cwd,
                  'sessionId': self.sid, 'version': '2.1.287', 'type': kind,
                  'uuid': '%s-%d' % (self.sid, len(self.lines)), 'timestamp': stamp(self.day)}
        if kind == 'user':
            record['message'] = {'role': 'user', 'content': content}
        else:
            record['message'] = {'id': 'msg', 'type': 'message', 'role': 'assistant',
                                 'model': 'claude-synthetic', 'content': content}
        record.update(extra)
        self.lines.append(json.dumps(record, ensure_ascii=False))
        return self

    def user(self, text, **extra):
        return self.add('user', text, **extra)

    def assistant(self, text, **extra):
        return self.add('assistant', [{'type': 'text', 'text': text}], **extra)

    def raw(self, line):
        self.lines.append(line)
        return self

    def save(self, folder, mtime):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (self.sid + '.jsonl')
        path.write_text('\n'.join(self.lines) + '\n', encoding='utf-8')
        os.utime(path, (mtime, mtime))
        return path


def h(raw):
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class BackfillTest(unittest.TestCase):
    def setUp(self):
        import beyin_v3_chat_backfill as backfill
        import beyin_v3_chat_trace as trace
        self.backfill, self.trace = backfill, trace
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-chat-backfill-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault, self.state = self.root / 'Sentetik Kasa Ş', self.root / 'state'
        self.vault.mkdir()
        self.config = self.root / 'claude-config'
        # Never the real ~/.claude: HOME and CLAUDE_CONFIG_DIR both point into the temp tree.
        env = clean_environ(HOME=str(self.root / 'home'), USERPROFILE=str(self.root / 'home'),
                            CLAUDE_CONFIG_DIR=str(self.config))
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('CLAUDE_CODE_SESSION_ID', None)
        self.folder = self.config / 'projects' / backfill.sanitized(str(self.vault))
        self.base = 1_790_000_000
        first = Transcript('11111111-aaaa-4aaa-8aaa-000000000001', self.vault, '2026-09-01')
        first.add('user', [{'type': 'text', 'text': '<system-reminder>' + CANARY + ' reminder</system-reminder>'},
                           {'type': 'text', 'text': TURKISH + ' Anahtar ' + SECRET + ' ve ada@example.com.'}])
        first.add('assistant', [{'type': 'thinking', 'thinking': CANARY + ' thinking'},
                                {'type': 'text', 'text': 'Kararı not ediyorum.'},
                                {'type': 'tool_use', 'id': 't1', 'name': 'Read', 'input': {'file_path': 'a.md'}}])
        first.add('user', [{'type': 'tool_result', 'tool_use_id': 't1', 'content': CANARY + ' tool output'}])
        first.user('Caveat: ' + CANARY + ' local command', isMeta=True)
        first.user('<command-name>/model</command-name><command-message>' + CANARY + '</command-message>')
        first.user(CANARY + ' sidechain prompt', isSidechain=True)
        first.assistant(CANARY + ' sidechain answer', isSidechain=True)
        first.user('[Request interrupted by user]')
        first.user('Uzun istek: ' + 'ç' * 3000)
        first.assistant('Uzun yanıt: ' + 'ğ' * 3000)
        self.first = first.save(self.folder, self.base)
        suppressed = Transcript('22222222-aaaa-4aaa-8aaa-000000000002', self.vault, '2026-09-10')
        suppressed.user('Bunu saklama lütfen [KAYDETME]').assistant(CANARY + ' gizli')
        suppressed.save(self.folder, self.base + 100)
        empty = Transcript('33333333-aaaa-4aaa-8aaa-000000000003', self.vault, '2026-09-11')
        empty.user('<task-notification>' + CANARY + '</task-notification>').assistant('tamam')
        empty.user('[Request interrupted by user for tool use]')
        empty.save(self.folder, self.base + 200)
        other = Transcript('44444444-aaaa-4aaa-8aaa-000000000004', self.root / 'baska-proje', '2026-09-12')
        other.user('Başka proje konuşması').save(self.folder, self.base + 300)
        latest = Transcript('55555555-aaaa-4aaa-8aaa-000000000005', self.vault, '2026-10-02')
        latest.user('Kamera rayı için ikinci deneme planlandı.').assistant('Plan kaydedildi.')
        self.latest = latest.save(self.folder, self.base + 400)
        self.s1, self.s5 = h(self.first.stem), h(self.latest.stem)

    def enable(self):
        self.trace.configure(self.state, True)

    def plan(self, **options):
        return self.backfill.plan(self.vault, self.state, **options)

    def snapshot(self):
        return {p: p.read_bytes() for p in sorted(self.root.rglob('*')) if p.is_file()}

    def test_dry_run_lists_oldest_first_and_writes_nothing(self):
        before = self.snapshot()
        result = self.plan()
        self.assertEqual(result['status'], 'dry_run')
        self.assertEqual(result['writes'], 0)
        self.assertFalse(result['enabled'])
        self.assertEqual([item['session'] for item in result['pending']], [self.s1, self.s5])
        self.assertEqual(result['pending'][0]['date'], '2026-09-01')
        self.assertEqual(result['pending'][0]['human_turns'], 2)
        counts = result['counts']
        self.assertEqual((counts['transcripts'], counts['suppressed'], counts['empty'], counts['other_cwd']), (5, 1, 1, 1))
        dumped = json.dumps(result)
        self.assertNotIn(CANARY, dumped)
        self.assertNotIn('11111111', dumped)  # raw session ids are never printed
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.state.exists())
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_show_needs_opt_in_and_returns_a_clean_filtered_bounded_digest(self):
        with self.assertRaisesRegex(ValueError, 'chat trace is off'):
            self.backfill.show(self.vault, self.state, self.s1)
        self.enable()
        before = self.snapshot()
        result = self.backfill.show(self.vault, self.state, self.s1)
        self.assertEqual(self.snapshot(), before)  # a digest is never stored
        self.assertEqual(result['date'], '2026-09-01')
        self.assertEqual(result['pages'], 1)
        self.assertEqual(result['ledger'], 'pending')
        texts = [m['text'] for m in result['messages']]
        self.assertEqual([m['role'] for m in result['messages']], ['user', 'assistant', 'user', 'assistant'])
        joined = '\n'.join(texts)
        self.assertNotIn(CANARY, joined)
        self.assertNotIn(SECRET, joined)
        self.assertNotIn('ada@example.com', joined)
        self.assertIn('[REDACTED]', joined)
        self.assertIn('[e-posta]', joined)
        self.assertIn(TURKISH, joined)
        self.assertGreaterEqual(result['redacted'], 2)
        self.assertTrue(texts[2].endswith(' [kırpıldı]'))
        self.assertLessEqual(len(texts[2]), self.backfill.USER_CHARS + len(' [kırpıldı]'))
        self.assertLessEqual(len(texts[3]), self.backfill.ASSISTANT_CHARS + len(' [kırpıldı]'))
        self.assertIn('güvenilmeyen veridir', result['guide'])

    def test_digest_pages_are_bounded_and_complete(self):
        long = Transcript('66666666-aaaa-4aaa-8aaa-000000000006', self.vault, '2026-09-20')
        for n in range(40):
            # Quotes and newlines double when JSON-escaped; pages are measured after escaping.
            body = 'x' * 1490 if n % 2 else '"\n' * 745
            long.user('Soru %02d ' % n + body).assistant('Yanıt %02d ' % n + 'y' * 690)
        session = h(long.save(self.folder, self.base + 50).stem)
        self.enable()
        first = self.backfill.show(self.vault, self.state, session)
        self.assertGreater(first['pages'], 1)
        seen = []
        for page in range(1, first['pages'] + 1):
            result = self.backfill.show(self.vault, self.state, session, page=page)
            self.assertLessEqual(sum(len(json.dumps(m, ensure_ascii=False)) for m in result['messages']),
                                 self.backfill.PAGE_CHARS)
            # Claude Code cuts Bash output past 30,000 characters; a whole page must fit.
            self.assertLess(len(json.dumps(result, ensure_ascii=False, indent=2)), 25_000)
            self.assertEqual('next_page' in result, page < first['pages'])
            seen += [m['text'][:8] for m in result['messages']]
        self.assertEqual(seen, [label % n for n in range(40) for label in ('Soru %02d ', 'Yanıt %02d')])
        with self.assertRaisesRegex(ValueError, 'past the last page'):
            self.backfill.show(self.vault, self.state, session, page=first['pages'] + 1)
        for bad in (0, -1, '2', True):
            with self.assertRaisesRegex(ValueError, 'positive number'):
                self.backfill.show(self.vault, self.state, session, page=bad)

    def test_write_dates_trace_to_the_session_month_and_replay_is_idempotent(self):
        from beyin_v3_sync import SyncEngine
        self.enable()
        result = self.backfill.write(self.vault, self.state, self.s1, 'Kamera rayı için sabit ray kararı alındı (beyan).')
        self.assertEqual(result['source'], 'knowledge/conversations/2026-09.md')
        self.assertEqual((result['backfill'], result['session_date']), ('written', '2026-09-01'))
        note = self.vault / result['source']
        content = note.read_text(encoding='utf-8')
        self.assertIn('\n2026-09-01: Kamera rayı için sabit ray kararı alındı (beyan).\n', content)
        self.assertNotIn(CANARY, content)
        before = note.read_bytes()
        again = self.backfill.write(self.vault, self.state, self.s1, 'Kamera rayı için sabit ray kararı alındı (beyan).')
        self.assertEqual(again['status'], 'unchanged')
        self.assertEqual(note.read_bytes(), before)
        ledger = json.loads((self.state / self.backfill.LEDGER).read_text(encoding='utf-8'))
        self.assertEqual(ledger, {'schema': 1, 'sessions': {self.s1: 'written'}})
        plan = self.plan()
        self.assertEqual([item['session'] for item in plan['pending']], [self.s5])
        self.assertEqual(plan['counts']['written'], 1)
        for path in self.state.rglob('*.json'):
            self.assertNotIn('11111111', path.read_text(encoding='utf-8'))
        found = SyncEngine(self.vault, self.state).store.context_for('claude', 'kamera sabit ray kararı', strict=True)
        self.assertTrue(any('sabit ray' in record['text'] for record in found['records']))

    def test_skip_is_remembered_and_never_downgrades_a_written_session(self):
        self.enable()
        self.assertEqual(self.backfill.skip(self.vault, self.state, self.s5)['backfill'], 'skipped')
        self.backfill.write(self.vault, self.state, self.s1, 'Sentetik özet: ray kararı.')
        self.assertEqual(self.backfill.skip(self.vault, self.state, self.s1)['backfill'], 'written')
        plan = self.plan()
        self.assertEqual(plan['pending'], [])
        self.assertEqual((plan['counts']['written'], plan['counts']['skipped']), (1, 1))
        # A later summary is still possible for a skipped session.
        self.assertEqual(self.backfill.write(self.vault, self.state, self.s5, 'Sentetik ikinci özet.')['backfill'], 'written')

    def test_opted_out_other_cwd_and_unknown_sessions_are_refused(self):
        self.enable()
        cases = ((h('22222222-aaaa-4aaa-8aaa-000000000002'), 'kaydetme'),
                 (h('33333333-aaaa-4aaa-8aaa-000000000003'), 'no human turn'),
                 (h('44444444-aaaa-4aaa-8aaa-000000000004'), 'did not start in this vault'),
                 ('0' * 24, 'not found'), ('not-a-session', '24-character'))
        for session, message in cases:
            with self.subTest(session=session):
                for call in (lambda: self.backfill.show(self.vault, self.state, session),
                             lambda: self.backfill.write(self.vault, self.state, session, 'Sentetik özet.'),
                             lambda: self.backfill.skip(self.vault, self.state, session)):
                    with self.assertRaisesRegex(ValueError, message):
                        call()
        self.assertFalse((self.vault / 'knowledge').exists())
        self.assertFalse((self.state / self.backfill.LEDGER).exists())

    def test_hook_opt_out_marker_also_blocks_backfill(self):
        self.enable()
        self.trace.hint(self.state, 'claude', {'session_id': self.latest.stem, 'prompt': 'Bu oturum [kaydetme]'})
        plan = self.plan()
        self.assertEqual([item['session'] for item in plan['pending']], [self.s1])
        self.assertEqual(plan['counts']['suppressed'], 2)
        with self.assertRaisesRegex(ValueError, 'kaydetme'):
            self.backfill.write(self.vault, self.state, self.s5, 'Sentetik özet.')

    def test_explicitly_chosen_folder_is_read_without_vault_cwd_check(self):
        folder = self.config / 'projects' / '-synthetic-code-repo'
        repo = Transcript('77777777-aaaa-4aaa-8aaa-000000000007', self.root / 'code repo', '2026-08-30')
        session = h(repo.user('Depo için test komutu belirlendi.').save(folder, self.base).stem)
        self.assertEqual(self.plan()['counts']['transcripts'], 5)
        explicit = self.plan(projects_dir=folder)
        self.assertEqual((explicit['scope'], [item['session'] for item in explicit['pending']]),
                         ('explicit_folder', [session]))
        self.enable()
        with self.assertRaisesRegex(ValueError, 'not found'):
            self.backfill.write(self.vault, self.state, session, 'Sentetik depo özeti.')
        result = self.backfill.write(self.vault, self.state, session, 'Sentetik depo özeti.', projects_dir=folder)
        self.assertEqual(result['source'], 'knowledge/conversations/2026-08.md')
        link = self.root / 'linked-folder'
        try:
            link.symlink_to(folder, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable on this platform')
        with self.assertRaisesRegex(ValueError, 'not a link'):
            self.plan(projects_dir=link)

    def test_summary_with_secret_or_email_is_rejected_before_any_write(self):
        self.enable()
        for text in ('Token ghp_' + 'a' * 36 + ' kullanıldı.', 'ada@example.com ile konuşuldu.'):
            with self.assertRaisesRegex(ValueError, 'secret-like value or an e-mail'):
                self.backfill.write(self.vault, self.state, self.s1, text)
        self.assertFalse((self.vault / 'knowledge').exists())

    def test_running_session_is_left_to_receipts_and_live_traces(self):
        self.enable()
        with patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': self.latest.stem}):
            plan = self.plan()
            self.assertEqual(plan['counts']['current_session'], 1)
            self.assertEqual([item['session'] for item in plan['pending']], [self.s1])
            with self.assertRaisesRegex(ValueError, 'running session'):
                self.backfill.show(self.vault, self.state, self.s5)

    def test_overlong_records_and_extra_files_stay_bounded(self):
        noisy = Transcript('88888888-aaaa-4aaa-8aaa-000000000008', self.vault, '2026-09-05')
        noisy.user('İlk soru kısa.')
        noisy.add('user', [{'type': 'tool_result', 'tool_use_id': 't', 'content': CANARY + 'z' * 9000}])
        noisy.raw('{"type":"user","message":{"role":"user","content":"' + 'q' * 9000)  # truncated tail line
        session = h(noisy.save(self.folder, self.base + 10).stem)
        self.enable()
        with patch.object(self.backfill, 'MAX_LINE', 4096):
            result = self.backfill.show(self.vault, self.state, session)
        self.assertEqual([m['text'] for m in result['messages']], ['İlk soru kısa.'])
        self.assertEqual(result['skipped_records'], {'long_lines': 2})
        with patch.object(self.backfill, 'MAX_FILES', 2):
            plan = self.plan()
        self.assertEqual((plan['counts']['scanned'], plan['counts']['over_file_limit']), (2, 4))
        self.assertEqual([item['session'] for item in plan['pending']], [self.s1, session])
        for bad in (0, 201):
            with self.assertRaisesRegex(ValueError, 'limit'):
                self.plan(limit=bad)
        self.assertEqual(len(self.plan(limit=1)['pending']), 1)

    def test_symlinked_transcript_is_not_read(self):
        outside = self.root / 'outside.jsonl'
        outside.write_text(self.first.read_text(encoding='utf-8'), encoding='utf-8')
        try:
            (self.folder / '99999999-aaaa-4aaa-8aaa-000000000009.jsonl').symlink_to(outside)
        except OSError:
            self.skipTest('symlinks unavailable on this platform')
        self.assertEqual(self.plan()['counts']['transcripts'], 5)

    def test_invalid_ledger_is_reported_and_preserved(self):
        self.enable()
        ledger = self.state / self.backfill.LEDGER
        for raw in ('{"schema":1,"sessions":{"' + self.s1 + '":"maybe"}}', 'not json', '{"schema":2,"sessions":{}}'):
            ledger.write_text(raw, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'ledger is invalid'):
                self.plan()
            with self.assertRaisesRegex(ValueError, 'ledger is invalid'):
                self.backfill.skip(self.vault, self.state, self.s1)
            self.assertEqual(ledger.read_text(encoding='utf-8'), raw)

    def test_trace_day_must_be_a_past_date(self):
        self.enable()
        session = hashlib.sha256(b'synthetic').hexdigest()[:24]
        tomorrow = date.today() + timedelta(days=2)
        for day in (tomorrow, '2026-09-01', date(1999, 12, 31)):
            with self.assertRaisesRegex(ValueError, 'past session day'):
                self.trace.write(self.vault, self.state, 'claude', session, 'Sentetik özet.', day=day)
        result = self.trace.write(self.vault, self.state, 'claude', session, 'Sentetik özet.', day=date(2026, 9, 3))
        self.assertEqual(result['source'], 'knowledge/conversations/2026-09.md')

    def test_vault_cwd_spellings_match_case_slash_and_normalization_variants(self):
        import unicodedata
        keys = self.backfill._vault_keys(self.vault)
        for spelling in (str(self.vault), str(self.vault).upper() + '/',
                         unicodedata.normalize('NFD', str(self.vault))):
            self.assertIn(self.backfill._key(spelling), keys)
        self.assertNotIn(self.backfill._key(str(self.vault) + '-kopya'), keys)
        self.assertEqual(self.backfill._key('C:\\Users\\Ada\\Kasa\\'), 'c:/users/ada/kasa')


class InstalledBackfillTest(unittest.TestCase):
    def setUp(self):
        import beyin_v3_chat_backfill as backfill
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-installed-backfill-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault, self.state = self.root / 'Sentetik Vault', self.root / 'runtime'
        self.vault.mkdir()
        self.config = self.root / 'claude-config'
        self.env = inherited_env(**isolated_env(self.root / 'home'), CLAUDE_CONFIG_DIR=str(self.config),
                                 BEYIN_UPDATES_OFF='1')
        self.env.pop('CLAUDE_CODE_SESSION_ID', None)
        done = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault', str(self.vault),
                               '--state', str(self.state)], capture_output=True, text=True, encoding='utf-8',
                              env=self.env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        folder = self.config / 'projects' / backfill.sanitized(str(self.vault))
        transcript = Transcript('aaaaaaaa-aaaa-4aaa-8aaa-00000000000a', self.vault, '2026-09-21')
        transcript.user(TURKISH).assistant('Not alındı.')
        self.session = h(transcript.save(folder, 1_790_000_000).stem)

    def cli(self, *arguments, stdin=None):
        done = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *arguments], input=stdin,
                              capture_output=True, text=True, encoding='utf-8', env=self.env,
                              cwd=self.vault, timeout=60)
        return done.returncode, done.stdout, done.stderr

    def test_installed_entrypoint_runs_the_whole_explicit_flow(self):
        code, out, err = self.cli('chat-trace', 'backfill')
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual(([item['session'] for item in plan['pending']], plan['writes']), ([self.session], 0))
        self.assertFalse((self.state / 'chat-trace.json').exists())
        code, out, err = self.cli('chat-trace', 'backfill', '--session', self.session, '--show')
        self.assertEqual(code, 1)
        self.assertIn('chat trace is off', err)
        self.assertEqual(self.cli('chat-trace', 'on')[0], 0)
        code, out, err = self.cli('chat-trace', 'backfill', '--session', self.session, '--show')
        self.assertEqual(code, 0, err)
        self.assertIn(TURKISH, out)  # readable UTF-8, not \\u escapes
        self.assertEqual(json.loads(out)['messages'][0]['text'], TURKISH)
        code, out, err = self.cli('chat-trace', 'backfill', '--session', self.session, '--file', '-',
                                  stdin='Şifre kuralı konuşuldu; Müşteri arşivi ayrı tutulacak (beyan).')
        self.assertEqual(code, 0, err + out)
        self.assertEqual(json.loads(out)['source'], 'knowledge/conversations/2026-09.md')
        note = (self.vault / 'knowledge/conversations/2026-09.md').read_text(encoding='utf-8')
        self.assertIn('2026-09-21: Şifre kuralı konuşuldu; Müşteri arşivi ayrı tutulacak (beyan).', note)
        code, out, err = self.cli('chat-trace', 'backfill')
        self.assertEqual((code, json.loads(out)['pending'], json.loads(out)['counts']['written']), (0, [], 1))
        self.assertFalse((self.vault / '.beyin-preferences.json').exists())

    def test_misused_arguments_are_rejected_without_side_effects(self):
        self.assertEqual(self.cli('chat-trace', 'on')[0], 0)
        cases = ((('chat-trace', 'write', '--show'), 'only valid with chat-trace backfill'),
                 (('chat-trace', 'status', '--projects-dir', str(self.root)), 'only valid with chat-trace backfill'),
                 (('chat-trace', 'backfill', '--session', self.session), 'need --session'),
                 (('chat-trace', 'backfill', '--show'), 'need --session'),
                 (('chat-trace', 'backfill', '--session', self.session, '--show', '--skip'), 'only one of'),
                 (('chat-trace', 'backfill', '--page', '2'), 'only valid with --show'),
                 (('chat-trace', 'backfill', '--session', self.session, '--skip', '--limit', '3'), 'dry run'),
                 (('chat-trace', 'backfill', '--harness', 'codex'), 'Claude Code transcripts only'))
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                code, out, err = self.cli(*arguments)
                self.assertEqual(code, 1, out)
                self.assertIn(message, err)
        self.assertFalse((self.vault / 'knowledge/conversations').exists())
        self.assertFalse((self.state / 'chat-trace-backfill.json').exists())


if __name__ == '__main__':
    unittest.main()
