"""Synthetic coverage for the standalone Codex session reader (#875)."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))

import beyin_v3_codex_sources as sources
from beyin_v3_chat_backfill import session_hash

STAMP = '2026-09-10T12:00:00Z'
LATER = '2026-09-12T12:00:00Z'
EPOCH = int(datetime.fromisoformat(STAMP).timestamp())
FIRST_DAY = datetime.fromisoformat(STAMP).astimezone().date().isoformat()
LAST_DAY = datetime.fromisoformat(LATER).astimezone().date().isoformat()
CANARY = 'SYNTHETIC_MESSAGE_CANARY'


class CodexSourcesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-codex-sources-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.home, self.vault = root / 'codex-home', root / 'Vault'
        self.home.mkdir()
        self.vault.mkdir()
        self.state = self.home / 'state_5.sqlite'
        self.history = self.home / 'thread_history_1.sqlite'

    def rollout(self, sid, *messages, cwd=None, originator='codex_cli_rs', source='cli'):
        folder = self.home / 'sessions/2026/09/10'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ('rollout-' + sid + '.jsonl')
        rows = [{'type': 'session_meta', 'payload': {'id': sid, 'timestamp': STAMP,
                 'cwd': str(self.vault if cwd is None else cwd), 'originator': originator, 'source': source}}]
        for role, text in messages:
            kind = 'input_text' if role == 'user' else 'output_text'
            rows.append({'type': 'response_item', 'timestamp': LATER,
                         'payload': {'type': 'message', 'role': role, 'content': [{'type': kind, 'text': text}]}})
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        return path

    def schema(self, *, wal=False):
        with closing(sqlite3.connect(self.state)) as db, db:
            if wal:
                db.execute('PRAGMA journal_mode = WAL')
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER,'
                       ' updated_at INTEGER, source TEXT, title TEXT, cwd TEXT)')
        with closing(sqlite3.connect(self.history)) as db, db:
            db.execute('CREATE TABLE thread_items (thread_id TEXT, rollout_ordinal INTEGER,'
                       ' created_at_ms INTEGER, item_json TEXT, item_type TEXT)')

    def sqlite_session(self, sid, *messages, cwd=None, source='cli', rollout=''):
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute('INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (sid, rollout, EPOCH, EPOCH + 86400, source, 'synthetic title',
                        str(self.vault if cwd is None else cwd)))
        with closing(sqlite3.connect(self.history)) as db, db:
            for ordinal, (role, text) in enumerate(messages):
                if role == 'user':
                    kind, item = 'userMessage', {'content': [{'type': 'text', 'text': text}]}
                else:
                    kind, item = 'agentMessage', {'text': text}
                db.execute('INSERT INTO thread_items VALUES (?, ?, ?, ?, ?)',
                           (sid, ordinal, EPOCH * 1000 + ordinal, json.dumps(item), kind))

    def plan(self, **kwargs):
        return sources.plan(self.vault, self.home, **kwargs)

    def snapshot(self):
        return {path.relative_to(self.home): (path.stat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None) for path in self.home.rglob('*')}

    def test_prefers_rollout_and_finds_sqlite_only_without_writes(self):
        sid, db_only = 'raw-dupe-id', 'raw-sqlite-id'
        self.schema(wal=True)  # Checkpointed WAL-mode DB; mode=ro alone can create sidecars.
        path = self.rollout(sid, ('user', CANARY), ('assistant', 'Synthetic answer'))
        self.sqlite_session(sid, ('user', 'DB copy'), ('user', 'Extra turn'), rollout=str(path))
        self.sqlite_session(db_only, ('user', 'Another synthetic prompt'))
        before = self.snapshot()
        result = self.plan()
        self.assertEqual(result['sqlite'], 'supported')
        by_hash = {entry['session']: entry for entry in result['pending']}
        self.assertEqual(len(result['pending']), 2)
        self.assertEqual(set(by_hash), {session_hash(sid), session_hash(db_only)})
        self.assertEqual(by_hash[session_hash(sid)], {
            'session': session_hash(sid), 'date': FIRST_DAY, 'last_activity': LAST_DAY,
            'human_turns': 1, 'kib': 1,
        })
        db_day = datetime.fromtimestamp(EPOCH + 86400, timezone.utc).astimezone().date().isoformat()
        self.assertEqual(by_hash[session_hash(db_only)]['last_activity'], db_day)
        self.assertEqual(result['writes'], 0)
        self.assertEqual(len(self.plan(limit=1)['pending']), 1)
        output = json.dumps(result)
        for private in (sid, db_only, str(self.vault), str(path), CANARY, 'synthetic title'):
            self.assertNotIn(json.dumps(private)[1:-1], output)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(p.name.endswith(('-wal', '-shm')) for p in self.home.iterdir()))

    def assert_cwd_scope(self, vault, matching, other):
        self.schema()
        expected = set()
        for write in (self.rollout, self.sqlite_session):
            for index, cwd in enumerate((*matching, other)):
                sid = f'{write.__name__}-{index}'
                write(sid, ('user', CANARY), cwd=cwd)
                if cwd != other:
                    expected.add(session_hash(sid))

        # Use the real key format without resolving a synthetic drive or network share.
        with patch.object(sources, '_vault_keys', return_value={sources._key(vault)}):
            result = sources.plan(vault, self.home)
        self.assertEqual(result['sqlite'], 'supported', result.get('sqlite_reason'))
        self.assertEqual({row['session'] for row in result['pending']}, expected)
        self.assertEqual(result['counts']['other_vault'], 2)

    def test_extended_drive_cwds_match_case_insensitively(self):
        self.assert_cwd_scope(
            r'C:\Vault', (r'\\?\c:\Vault', '//?/C:/Vault'), r'\\?\C:\Other',
        )

    def test_extended_unc_cwds_match(self):
        self.assertEqual(sources._codex_cwd(r'\\?\UNC\server\share\Vault'), r'\\server\share\Vault')
        self.assertEqual(sources._codex_cwd('//?/unc/server/share/Vault'), '//server/share/Vault')
        self.assert_cwd_scope(
            r'\\server\share\Vault',
            (r'\\?\UNC\server\share\Vault', '//?/unc/server/share/Vault'),
            r'\\?\UNC\server\share\Other',
        )

    def test_unknown_sqlite_keeps_rollouts(self):
        self.rollout('jsonl-only', ('user', CANARY))
        self.schema()

        def check(reason):
            result = self.plan()
            self.assertEqual(result['sqlite'], 'unsupported')
            self.assertEqual(result['sqlite_reason'], reason)
            self.assertEqual(result['counts']['unsupported'], 1)
            self.assertEqual([row['session'] for row in result['pending']], [session_hash('jsonl-only')])

        self.sqlite_session('invalid-timestamp', ('user', CANARY))
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("UPDATE threads SET created_at = 'invalid'")
        check('unexpected_type')
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute('ALTER TABLE threads DROP COLUMN cwd')
        check('missing_column')
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute('DROP TABLE threads')
        check('missing_table')
        self.state.write_bytes(b'not a database')
        check('unreadable_database')
        self.state.rename(self.home / 'state_6.sqlite')
        check('missing_database')

    def test_reads_uncheckpointed_wal_without_writing_database_or_wal(self):
        self.schema()
        sid = 'wal-only'
        paths = (self.state, self.history)
        with closing(sqlite3.connect(self.state)) as users, closing(sqlite3.connect(self.history)) as history:
            for db, table in ((users, 'threads'), (history, 'thread_items')):
                self.assertEqual(db.execute('PRAGMA journal_mode = WAL').fetchone(), ('wal',))
                db.execute('SELECT * FROM ' + table).fetchall()

            before_insert = {path: path.read_bytes() for path in paths}
            self.sqlite_session(sid, ('user', CANARY))
            sidecars = [path.with_name(path.name + suffix) for path in paths for suffix in ('-wal', '-shm')]
            self.assertTrue(all(path.is_file() for path in sidecars))
            self.assertEqual({path: path.read_bytes() for path in paths}, before_insert)

            files = set(self.home.rglob('*'))
            data_files = paths + tuple(path for path in sidecars if path.name.endswith('-wal'))
            before_read = {path: path.read_bytes() for path in data_files}
            result = self.plan()
            self.assertEqual(result['sqlite'], 'supported')
            self.assertEqual([row['session'] for row in result['pending']], [session_hash(sid)])
            self.assertEqual(result['writes'], 0)
            digest = sources.show(self.vault, self.home, session_hash(sid))
            self.assertEqual(digest['messages'], [{'role': 'user', 'text': CANARY}])
            self.assertEqual(set(self.home.rglob('*')), files)
            self.assertEqual({path: path.read_bytes() for path in data_files}, before_read)

    def test_wal_reader_does_not_walk_unrelated_home_directories(self):
        self.schema()
        sid = 'wal-with-unrelated-directory'
        unrelated = self.home / 'visualizations' / 'outputs'
        unrelated.mkdir(parents=True)
        scandir = sources.os.scandir

        def remove_before_scan(folder):
            if Path(folder) == unrelated:
                unrelated.rmdir()
            return scandir(folder)

        with closing(sqlite3.connect(self.state)) as writer:
            self.assertEqual(writer.execute('PRAGMA journal_mode = WAL').fetchone(), ('wal',))
            writer.execute('SELECT * FROM threads').fetchall()
            self.sqlite_session(sid, ('user', CANARY))
            with patch.object(sources.os, 'scandir', side_effect=remove_before_scan):
                result = self.plan()

        self.assertEqual(result['sqlite'], 'supported', result.get('sqlite_reason'))
        self.assertEqual([row['session'] for row in result['pending']], [session_hash(sid)])
        self.assertTrue(unrelated.is_dir())

    def test_rollout_scan_counts_vanished_and_dangling_entries(self):
        self.rollout('readable', ('user', CANARY))
        root = self.home / 'sessions'
        vanished = root / 'vanished'
        vanished.mkdir()
        dangling = root / 'dangling'
        try:
            dangling.symlink_to(root / 'missing-target', target_is_directory=True)
        except (OSError, NotImplementedError):
            dangling.mkdir()

        scandir = sources.os.scandir

        def scan(folder):
            if Path(folder) == vanished:
                vanished.rmdir()
            elif Path(folder) == dangling:
                raise FileNotFoundError
            return scandir(folder)

        with patch.object(sources.os, 'scandir', side_effect=scan):
            result = self.plan()

        self.assertEqual(result['counts']['unreadable'], 2)
        self.assertEqual([row['session'] for row in result['pending']], [session_hash('readable')])

    def test_incomplete_sidecars_and_rollback_journal_leave_files_untouched(self):
        self.schema()
        self.rollout('fallback', ('user', CANARY))
        for suffix, reason in (('-wal', 'partial_sidecars'), ('-shm', 'partial_sidecars'),
                               ('-journal', 'rollback_journal')):
            with self.subTest(suffix=suffix):
                sidecar = self.state.with_name(self.state.name + suffix)
                sidecar.touch()
                try:
                    before = self.snapshot()
                    result = self.plan()
                    self.assertEqual(result['sqlite'], 'unsupported')
                    self.assertEqual(result['sqlite_reason'], reason)
                    self.assertEqual([row['session'] for row in result['pending']], [session_hash('fallback')])
                    self.assertEqual(self.snapshot(), before)
                finally:
                    sidecar.unlink()

    def test_excludes_other_vaults_opt_out_and_non_interactive_origins(self):
        self.schema()
        other = self.vault.with_name('Other')
        self.rollout('other-rollout', ('user', CANARY), cwd=other)
        self.sqlite_session('other-sqlite', ('user', CANARY), cwd=other)
        self.rollout('subagent', ('user', CANARY), source={'subagent': 'review'})
        self.rollout('exec', ('user', CANARY), originator='codex_exec')
        self.sqlite_session('db-exec', ('user', CANARY), source='exec')
        self.rollout('rollout-optout', ('user', 'Do not keep [KaYdEtMe]'))
        self.sqlite_session('rollout-optout', ('user', CANARY))
        self.rollout('db-optout', ('user', CANARY))
        self.sqlite_session('db-optout', ('user', 'Do not keep [KAYDETME]'))
        self.rollout('valid', ('user', CANARY), cwd=str(self.vault).upper() + '/')
        result = self.plan()
        self.assertEqual([row['session'] for row in result['pending']], [session_hash('valid')])
        counts = result['counts']
        self.assertEqual((counts['other_vault'], counts['opted_out'], counts['non_interactive']), (2, 2, 3))
        for sid in ('db-optout', 'rollout-optout', 'other-rollout', 'subagent'):
            self.assertEqual(sources.show(self.vault, self.home, session_hash(sid))['status'], 'unavailable')

    def test_unparseable_rollout_can_fall_back_to_sqlite(self):
        self.schema()
        sid = 'malformed-rollout'
        path = self.rollout(sid)
        with path.open('ab') as handle:
            handle.write(b'{invalid]\n')
        self.sqlite_session(sid, ('user', 'Synthetic fallback prompt'))
        self.assertEqual([row['session'] for row in self.plan()['pending']], [session_hash(sid)])

    def test_show_filters_context_and_redacts_only_selected_digest(self):
        self.schema()
        injected = '# AGENTS.md instructions for fixture\n<INSTRUCTIONS>' + CANARY + '</INSTRUCTIONS>'
        self.rollout('selected', ('user', injected), ('user', '<environment_context>' + CANARY
                     + '</environment_context><user_instructions>[KAYDETME]</user_instructions>'
                     '<permissions instructions>' + CANARY + '</permissions instructions>'
                     'Email sample@example.invalid'), ('assistant', '-----BEGIN PRIVATE KEY-----\n'
                     'SYNTHETIC_KEY_BODY\n-----END PRIVATE KEY-----'))
        self.sqlite_session('other-selection', ('user', 'UNSELECTED_MESSAGE'))
        before = self.snapshot()
        listed = {row['session']: row for row in self.plan()['pending']}
        self.assertEqual(listed[session_hash('selected')]['human_turns'], 1)
        result = sources.show(self.vault, self.home, session_hash('selected'))
        self.assertEqual(result['status'], 'digest')
        self.assertEqual([item['role'] for item in result['messages']], ['user', 'assistant'])
        output = json.dumps(result, ensure_ascii=False)
        self.assertIn('[e-posta]', output)
        self.assertIn('[REDACTED]', output)
        for private in ('sample@example.invalid', 'SYNTHETIC_KEY_BODY', CANARY, 'UNSELECTED_MESSAGE'):
            self.assertNotIn(private, output)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
