import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_learning as learning
import beyin_v3_hook as hook
from beyin_v3_sync import SyncEngine


class LearningCandidateLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        (self.vault / 'notes').mkdir(parents=True, exist_ok=True)
        (self.vault / 'notes' / 'task.md').write_text('# task\n', encoding='utf-8')
        (self.vault / 'notes' / 'doc.md').write_text('# doc\n', encoding='utf-8')
        self.state = self.root / 'state'
        self.state.mkdir()
        self.engine = SyncEngine(self.vault, self.state)
        # The agent session running the suite must not name the fixture's session (#169, #212).
        from v3_package_helpers import inherited_env
        self.env = inherited_env(PYTHONIOENCODING='utf-8')

    def test_resolution_preserves_malformed_lines_verbatim(self):
        ledger = self.state / 'learning-state.jsonl'
        raw = b'  {broken JSON   \r\n\r\n  [1,2]  \n  "literal"  '
        ledger.write_bytes(raw)
        self.assertEqual(learning.resolve_candidates(self.state, 'session'), 0)
        self.assertEqual(ledger.read_bytes(), raw)

    def test_stage_and_list_candidates(self):
        session = "session-1234567890abcdef"
        rec = learning.stage_candidate(self.state, session, "WAL timeout must be at least 5s",
                                       source="notes/task.md", scope="database")
        self.assertEqual(rec["candidate"], "WAL timeout must be at least 5s")
        self.assertEqual(rec["source"], "notes/task.md")
        self.assertEqual(rec["scope"], "database")
        self.assertEqual(rec["status"], "candidate")

        # Verify persistence in state/learning-state.jsonl
        ledger = self.state / "learning-state.jsonl"
        self.assertTrue(ledger.is_file())

        # List candidates
        active = learning.list_candidates(self.state, session=session)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], rec["id"])

        self.assertTrue(learning.has_candidates(self.state, session=session))
        self.assertFalse(learning.has_candidates(self.state, session="different-session"))

    def test_resolve_candidates(self):
        session = "session-resolve-abc"
        learning.stage_candidate(self.state, session, "Learning 1")
        learning.stage_candidate(self.state, session, "Learning 2")
        self.assertEqual(len(learning.list_candidates(self.state, session=session)), 2)

        count = learning.resolve_candidates(self.state, session, target_status="distilled")
        self.assertEqual(count, 2)

        # Now active candidates should be empty
        self.assertFalse(learning.has_candidates(self.state, session=session))
        # But distilled ones are preserved
        distilled = learning.list_candidates(self.state, session=session, status="distilled")
        self.assertEqual(len(distilled), 2)

    def test_cli_learn_command(self):
        res = subprocess.run([
            sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
            '--vault', str(self.vault), '--state', str(self.state),
            'learn', '--candidate', 'CLI staged learning', '--source', 'notes/doc.md', '--scope', 'infra',
            '--session', 'cli-sess'
        ], capture_output=True, text=True, check=True, env=self.env, timeout=30)
        output = json.loads(res.stdout)
        self.assertEqual(output["candidate"], "CLI staged learning")
        self.assertEqual(output["scope"], "infra")

        # List via CLI
        list_res = subprocess.run([
            sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
            '--vault', str(self.vault), '--state', str(self.state),
            'learn', '--list'
        ], capture_output=True, text=True, check=True, env=self.env, timeout=30)
        list_output = json.loads(list_res.stdout)
        self.assertIn("candidates", list_output)
        self.assertEqual(len(list_output["candidates"]), 1)
        self.assertEqual(list_output["candidates"][0]["candidate"], "CLI staged learning")

    def test_stop_hook_challenges_explicit_no_learning_after_edits(self):
        session_id = 'test-no-learning-bypass'
        session_hash = hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:24]

        # Simulate edit
        payload_edit = {'session_id': session_id, 'hook_event_name': 'PostToolUse'}
        hook.receipt_reminder(payload_edit, self.state, 'claude', 'PostToolUse', vault=self.vault)

        # Agent writes receipt claiming 'Öğrenilen: yok'
        time.sleep(0.02)
        self.engine.receipt('no-learning-1', 'Refactored auth module.\nÖğrenilen: yok',
                            ['notes/task.md'], 'claude', session=session_hash)

        # Stop hook must block once challenging the 'Öğrenilen: yok' claim!
        payload_stop = {'session_id': session_id, 'hook_event_name': 'Stop'}
        res = hook.receipt_reminder(payload_stop, self.state, 'claude', 'Stop', vault=self.vault)
        self.assertIsNotNone(res)
        self.assertEqual(res.get('decision'), 'block')
        self.assertIn("Öğrenilen: yok", res.get('reason', ''))
        # The command in the reminder runs on every platform and names this session, so a
        # harness without a session variable (Codex) stages into the right window.
        self.assertIn('python3 beyin.py learn --session ' + session_hash + ' --candidate', res['reason'])
        self.assertNotIn('py -3', res['reason'])

        # Subsequent Stop passes open (agent answered challenge or routine edit)
        res2 = hook.receipt_reminder(payload_stop, self.state, 'claude', 'Stop', vault=self.vault)
        self.assertIsNone(res2)

    def test_an_empty_label_heading_a_list_is_not_challenged_as_no_learning(self):
        session_id = 'list-under-label'
        session_hash = hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:24]
        hook.receipt_reminder({'session_id': session_id}, self.state, 'claude', 'PostToolUse', vault=self.vault)
        time.sleep(0.02)
        self.engine.receipt('list-1', 'İş bitti.\nÖğrenilen:\n- WAL zaman aşımı en az 5 sn', ['notes/task.md'],
                            'claude', session=session_hash)
        self.assertIsNone(hook.receipt_reminder({'session_id': session_id}, self.state, 'claude', 'Stop',
                                                vault=self.vault))
        self.assertFalse(hook._is_bare_no_learning('Öğrenilen:'))
        for bare in ('Öğrenilen: yok', 'ÖĞRENİLEN: YOK', 'Ogrenilen: hiçbiri', 'Learned: none'):
            self.assertTrue(hook._is_bare_no_learning('İş bitti.\n' + bare), bare)
        self.assertFalse(hook._is_bare_no_learning('Öğrenilen: yok (rutin kontrol)'))

    def test_staged_candidate_satisfies_learning_contract(self):
        session_id = 'test-staged-candidate'
        session_hash = hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:24]

        # Simulate edit
        payload_edit = {'session_id': session_id, 'hook_event_name': 'PostToolUse'}
        hook.receipt_reminder(payload_edit, self.state, 'claude', 'PostToolUse', vault=self.vault)

        # Stage candidate in ledger
        learning.stage_candidate(self.state, session_hash, "Staged learning before receipt", source="auth.py")

        # Agent writes receipt declaring the learning
        time.sleep(0.02)
        self.engine.receipt('staged-1', 'Refactored auth module.\nÖğrenilen: Staged learning before receipt',
                            ['notes/task.md'], 'claude', session=session_hash)

        # Stop hook passes because candidate was already safely staged!
        payload_stop = {'session_id': session_id, 'hook_event_name': 'Stop'}
        res = hook.receipt_reminder(payload_stop, self.state, 'claude', 'Stop', vault=self.vault)
        self.assertIsNone(res)

    def test_cli_to_stop_hook_integration(self):
        session_id = 'test-cli-hook-int'
        session_hash = hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:24]

        # 1. Edit a file
        payload_edit = {'session_id': session_id, 'hook_event_name': 'PostToolUse'}
        hook.receipt_reminder(payload_edit, self.state, 'claude', 'PostToolUse', vault=self.vault)

        # 2. Stage candidate via CLI without --session flag (a shell without a session variable)
        time.sleep(0.02)
        res_cli = subprocess.run([
            sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
            '--vault', str(self.vault), '--state', str(self.state),
            'learn', '--candidate', 'Connection pooling must handle timeouts gracefully'
        ], capture_output=True, text=True, check=True, env=self.env, timeout=30)
        self.assertEqual(json.loads(res_cli.stdout)['session'], 'cli')

        # 3. Write receipt with 'Öğrenilen: yok'
        self.engine.receipt('cli-int-1', 'Fixed edge case.\nÖğrenilen: yok',
                            ['notes/task.md'], 'claude', session=session_hash)

        # 4. The first Stop passes: the CLI candidate staged in this edit window was detected.
        # (A second Stop would pass anyway once .kdone exists, so it proves nothing.)
        payload_stop = {'session_id': session_id, 'hook_event_name': 'Stop'}
        res = hook.receipt_reminder(payload_stop, self.state, 'claude', 'Stop', vault=self.vault)
        self.assertIsNone(res)

    def test_candidate_from_before_the_edit_window_does_not_answer_for_a_new_session(self):
        # A "cli" candidate carries no session. One staged days ago must not silence the
        # existing knowledge reminder, nor the new challenge, for every later session.
        learning.stage_candidate(self.state, 'cli', 'Eski bir Codex oturumunda kaydedilen aday')
        ledger = self.state / 'learning-state.jsonl'
        old = json.loads(ledger.read_text(encoding='utf-8'))
        old['created_at'] = '2026-10-01T09:00:00+00:00'
        ledger.write_text(json.dumps(old, ensure_ascii=False) + '\n', encoding='utf-8')
        cases = {
            'declared': ('Öğrenilen: WAL zaman aşımı en az 5 sn olmalı', hook.KNOWLEDGE_REMINDER),
            'bare-yok': ('Öğrenilen: yok', hook.NO_LEARNING_CHALLENGE_REMINDER),
        }
        for name, (line, reason) in cases.items():
            with self.subTest(name):
                session_id = 'new-session-' + name
                session_hash = hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:24]
                reason = reason.format(session=session_hash)
                hook.receipt_reminder({'session_id': session_id}, self.state, 'claude', 'PostToolUse', vault=self.vault)
                time.sleep(0.02)
                self.engine.receipt('old-cli-' + name, 'İş bitti.\n' + line, ['notes/task.md'], 'claude',
                                    session=session_hash)
                res = hook.receipt_reminder({'session_id': session_id}, self.state, 'claude', 'Stop', vault=self.vault)
                self.assertEqual(res, {'decision': 'block', 'reason': reason})

    def test_stop_walks_the_knowledge_tree_only_when_a_reminder_is_due(self):
        from unittest import mock
        for name, summary in (('plain', 'Refactored auth module.'),
                              ('explained-yok', 'Refactored auth module.\nÖğrenilen: yok (rutin)')):
            with self.subTest(name):
                session_id = 'walk-' + name
                session_hash = hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:24]
                hook.receipt_reminder({'session_id': session_id}, self.state, 'claude', 'PostToolUse', vault=self.vault)
                time.sleep(0.02)
                self.engine.receipt('walk-' + name, summary, ['notes/task.md'], 'claude', session=session_hash)
                with mock.patch.object(hook, '_has_knowledge_update', return_value=False) as walk, \
                        mock.patch.object(hook, '_has_staged_candidates', return_value=False) as ledger:
                    res = hook.receipt_reminder({'session_id': session_id}, self.state, 'claude', 'Stop', vault=self.vault)
                self.assertIsNone(res)
                self.assertEqual((walk.call_count, ledger.call_count), (0, 0))

    def test_secret_redaction_in_candidate(self):
        secret_token = "sk-" + "a" * 30
        rec = learning.stage_candidate(self.state, "sess-sec", f"Use API token {secret_token} for auth")
        self.assertNotIn(secret_token, rec["candidate"])
        self.assertIn("[REDACTED]", rec["candidate"])

    def test_a_secret_is_never_stored_raw_when_redaction_cannot_finish(self):
        secret_token = "sk-" + "b" * 30
        ledger = self.state / "learning-state.jsonl"
        # The health counter cannot be written: the candidate is still stored redacted.
        (self.state / "secret-filter.sqlite3").mkdir()  # a directory: sqlite3 cannot open it
        rec = learning.stage_candidate(self.state, "sess-sec", f"Use API token {secret_token} for auth")
        self.assertNotIn(secret_token, rec["candidate"])
        self.assertNotIn(secret_token, ledger.read_text(encoding="utf-8"))
        # The filter itself cannot run: the candidate is refused, nothing raw reaches the ledger.
        (self.state / "secret-patterns.txt").write_text("ab\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            learning.stage_candidate(self.state, "sess-sec", f"Use API token {secret_token} for auth")
        self.assertNotIn(secret_token, ledger.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            learning.stage_candidate(self.state, "sess-sec", "   ")

    def test_untrusted_or_private_source_rejected(self):
        from contextlib import closing
        import sqlite3
        db_path = self.state / "memory.sqlite3"
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("INSERT OR REPLACE INTO records VALUES (?, ?)",
                       ("private-doc", json.dumps({"id": "private-doc", "visibility": "private"})))
            db.execute("INSERT OR REPLACE INTO records VALUES (?, ?)",
                       ("untrusted-doc", json.dumps({"id": "untrusted-doc", "trust": "untrusted"})))
            db.commit()

        with self.assertRaises(ValueError):
            learning.stage_candidate(self.state, "sess-priv", "Discovered private leak", source="private-doc")

        with self.assertRaises(ValueError):
            learning.stage_candidate(self.state, "sess-untr", "Discovered untrusted leak", source="untrusted-doc")

    def test_private_source_is_found_even_when_another_note_mentions_its_path(self):
        # Real rows written by the sync engine; the earlier row only cites the private path.
        (self.vault / 'notes' / 'a-özet.md').write_text('Bkz. notes/z-gizli.md\n', encoding='utf-8')
        (self.vault / 'notes' / 'z-gizli.md').write_text('---\nvisibility: private\n---\nGizli içerik\n',
                                                         encoding='utf-8')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        with self.assertRaises(ValueError):
            learning.stage_candidate(self.state, "sess-priv", "Gizli nottan aday", source="notes/z-gizli.md")
        rec = learning.stage_candidate(self.state, "sess-priv", "Özetten aday", source="notes/a-özet.md")
        self.assertEqual(rec["source"], "notes/a-özet.md")

    def test_private_source_aliases_cannot_bypass_real_sync_metadata(self):
        import unicodedata
        source = 'notes/özel.md'
        (self.vault / source).write_text('---\nvisibility: private\n---\nSentetik gizli not\n',
                                         encoding='utf-8', newline='\n')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        for alias in (' notes/özel.md ', 'notes/./özel.md', 'notes/alt/../özel.md',
                      'notes\\özel.md', 'NOTES/ÖZEL.MD', unicodedata.normalize('NFD', source)):
            with self.subTest(source=alias), self.assertRaises(ValueError):
                learning.stage_candidate(self.state, 'private-alias', 'Sentetik aday', source=alias)
        self.assertFalse((self.state / 'learning-state.jsonl').exists())

    def test_private_source_check_uses_a_percent_encoded_sqlite_uri(self):
        state = self.root / 'state # Ölçüm %'
        (self.vault / 'notes/private.md').write_text('---\nvisibility: private\n---\nSentetik gizli not\n',
                                                   encoding='utf-8', newline='\n')
        self.assertEqual(SyncEngine(self.vault, state).sync()['status'], 'succeeded')
        with self.assertRaises(ValueError):
            learning.stage_candidate(state, 'private-uri', 'Sentetik aday', source='notes/private.md')
        self.assertFalse((state / 'learning-state.jsonl').exists())

    def test_scope_isolation(self):
        learning.stage_candidate(self.state, "sess-1", "Global learning", scope="global")
        learning.stage_candidate(self.state, "sess-2", "Project A learning", scope="projectA")
        learning.stage_candidate(self.state, "sess-3", "Project B learning", scope="projectB")

        # Listing for Project A returns Project A + global, but NOT Project B
        cands_a = learning.list_candidates(self.state, scope="projectA")
        texts_a = [c["candidate"] for c in cands_a]
        self.assertIn("Global learning", texts_a)
        self.assertIn("Project A learning", texts_a)
        self.assertNotIn("Project B learning", texts_a)

        # Listing for Project B returns Project B + global, but NOT Project A
        cands_b = learning.list_candidates(self.state, scope="projectB")
        texts_b = [c["candidate"] for c in cands_b]
        self.assertIn("Global learning", texts_b)
        self.assertIn("Project B learning", texts_b)
        self.assertNotIn("Project A learning", texts_b)

    def test_corrupted_lines_are_preserved_during_resolution(self):
        # Create a ledger with one valid and one corrupted line
        ledger = self.state / "learning-state.jsonl"
        valid_rec = learning.stage_candidate(self.state, "sess-1", "Valid learning")
        
        # Manually append a corrupted line
        corrupt_line = '{"id": "corrupt", "status": "candidate", "session": "sess-1", "candidate": "Broken JSON'
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(corrupt_line + "\n")
        
        # Run resolution on sess-1
        count = learning.resolve_candidates(self.state, "sess-1", "distilled")
        self.assertEqual(count, 1) # Only the valid line is resolved
        
        # Verify the corrupted line survived the rewrite intact
        content = ledger.read_text(encoding="utf-8")
        self.assertIn(corrupt_line, content)

    def test_basic_auth_redaction_in_candidate(self):
        secret_token = "dXNlcm5hbWU6cGFzc3dvcmQ="
        rec = learning.stage_candidate(self.state, "sess-sec", f"Use auth Basic {secret_token} for access")
        self.assertNotIn(secret_token, rec["candidate"])
        self.assertIn("[REDACTED]", rec["candidate"])
        self.assertEqual(rec["candidate"], "Use auth [REDACTED] for access")

    @mock.patch('sqlite3.connect')
    def test_source_trust_fails_closed_on_db_error(self, mock_connect):
        import sqlite3
        mock_connect.side_effect = sqlite3.OperationalError("database is locked")
        
        # Ensure that staging fails completely when trust cannot be established
        with self.assertRaisesRegex(ValueError, "trust check failed.*database is locked"):
            learning.stage_candidate(self.state, "sess-err", "Leak?", source="unknown-source")


class InstalledLearnCommandTest(unittest.TestCase):
    """The release installs only beyin_v3*.py; `learn` must run from an installed vault (#151)."""

    def test_learn_runs_in_an_installed_vault(self):
        from v3_package_helpers import install, isolated_env
        tmp = tempfile.TemporaryDirectory(prefix='learn-installed-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        vault, state = root / 'Örnek Beyin', root / 'state'
        vault.mkdir()
        env = isolated_env(root / 'home')
        result = install(vault, state, env)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertFalse((vault / '.claude/scripts/_portalock.py').exists())
        command = [sys.executable, str(vault / 'beyin.py')]
        staged = subprocess.run(command + ['learn', '--candidate', 'Kurulu kasada aday'], cwd=vault, env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
        self.assertEqual(json.loads(staged.stdout)['candidate'], 'Kurulu kasada aday')
        listed = subprocess.run(command + ['learn', '--list'], cwd=vault, env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertEqual([c['candidate'] for c in json.loads(listed.stdout)['candidates']], ['Kurulu kasada aday'])


if __name__ == '__main__':
    unittest.main()
