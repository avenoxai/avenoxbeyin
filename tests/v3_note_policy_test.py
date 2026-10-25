"""Tests for Paket 3: Safe Note Policy, Idempotency Key, and Evidence Refs."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_sync import SyncEngine
from beyin_v3_notes import LOCAL_DIRECTORY, NoteConflict, locked


class NotePolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / 'vault'
        self.vault.mkdir()
        self.state = Path(self.temp.name) / 'state'
        self.engine = SyncEngine(self.vault, self.state)
        self.addCleanup(self.engine.store.close)
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop('CLAUDE_CODE_SESSION_ID', None)
        os.environ.pop('BEYIN_V3_NO_RECEIPT_REMINDER', None)

    def write(self, source='Threads.md', raw=b'alpha\nbeta\n'):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def test_cached_key_rejects_changed_payload_for_edit_and_create(self):
        request = dict(source='notes/key.md', expected_sha256=None, content='original', idempotency_key='key')
        self.engine.note_edit(request)
        with self.assertRaisesRegex(ValueError, 'different request'):
            self.engine.note_edit(dict(request, content='changed'))
        self.engine.note_create('notes/create.md', 'original', idempotency_key='create')
        with self.assertRaisesRegex(ValueError, 'different request'):
            self.engine.note_create('notes/create.md', 'changed', idempotency_key='create')

    def test_fixed_temporary_symlink_does_not_escape_vault(self):
        outside = self.vault.parent / 'outside.txt'
        outside.write_text('preserve', encoding='utf-8')
        cache = self.vault / LOCAL_DIRECTORY / 'idempotency'
        cache.mkdir(parents=True)
        try:
            (cache / 'key.tmp').symlink_to(outside)
        except OSError:
            self.skipTest('symlink unavailable')
        self.engine.note_edit(dict(source='notes/key.md', expected_sha256=None,
                                   content='hello', idempotency_key='key'))
        self.assertEqual(outside.read_text(), 'preserve')

    def test_note_create_rejects_linked_idempotency_directory(self):
        outside = self.vault.parent / 'outside-cache'
        outside.mkdir()
        folder = self.vault / LOCAL_DIRECTORY
        folder.mkdir()
        try:
            (folder / 'idempotency').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink unavailable')
        with self.assertRaises(ValueError):
            self.engine.note_create('notes/key.md', 'hello', idempotency_key='key')
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.vault / 'notes/key.md').exists())

    def test_create_retry_recovers_interruption_after_source_write(self):
        original = self.engine.sync
        def interrupted():
            original()
            raise OSError('synthetic interruption after source/index write')
        with patch.object(self.engine, 'sync', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.engine.note_create('notes/recover.md', 'durable', idempotency_key='recover')
        self.assertTrue((self.vault / 'notes/recover.md').is_file())
        result = self.engine.note_create('notes/recover.md', 'durable', idempotency_key='recover')
        self.assertTrue(result['idempotent'])

    def test_edit_retry_recovers_interruption_after_source_write(self):
        request = dict(source='notes/recover.md', expected_sha256=None,
                       content='durable', idempotency_key='recover')
        with patch.object(self.engine, 'sync', side_effect=OSError('synthetic interruption')):
            with self.assertRaises(NoteConflict):
                self.engine.note_edit(request)
        self.assertTrue((self.vault / 'notes/recover.md').is_file())
        result = self.engine.note_edit(request)
        self.assertTrue(result['idempotent'])

    def test_expected_source_hash_alias(self):
        self.write(source='notes/policy.md', raw=b'# Policy Note\ncontent here\n')
        current_hash = self.engine.note_read('notes/policy.md')['sha256']
        # Use expected_source_hash instead of expected_sha256
        req = {
            'source': 'notes/policy.md',
            'expected_source_hash': current_hash,
            'edits': [{'old': 'content here', 'new': 'updated content'}]
        }
        res = self.engine.note_edit(req)
        self.assertEqual(res['status'], 'written')
        self.assertIn('updated content', (self.vault / 'notes/policy.md').read_text(encoding='utf-8'))

    def test_conflicting_hash_fields_rejected(self):
        self.write(source='notes/policy.md', raw=b'# Policy Note\ncontent here\n')
        current_hash = self.engine.note_read('notes/policy.md')['sha256']
        bogus_hash = 'a' * 64
        req = {
            'source': 'notes/policy.md',
            'expected_sha256': current_hash,
            'expected_source_hash': bogus_hash,
            'edits': [{'old': 'content here', 'new': 'updated content'}]
        }
        with self.assertRaises(ValueError) as ctx:
            self.engine.note_edit(req)
        self.assertIn('must match if both provided', str(ctx.exception))

    def test_idempotency_key_replay_returns_cached_success(self):
        self.write(source='notes/idemp.md', raw=b'first line\nsecond line\n')
        initial_hash = self.engine.note_read('notes/idemp.md')['sha256']
        key = 'unique-turn-req-42'
        req = {
            'source': 'notes/idemp.md',
            'expected_sha256': initial_hash,
            'edits': [{'old': 'first line', 'new': 'FIRST LINE'}],
            'idempotency_key': key
        }
        # First call: executes edit
        res1 = self.engine.note_edit(req)
        self.assertEqual(res1['status'], 'written')
        self.assertNotIn('idempotent', res1)

        # Second call with the same request & key: returns cached success instead of NoteConflict
        res2 = self.engine.note_edit(req)
        self.assertEqual(res2['status'], 'written')
        self.assertEqual(res2['source'], 'notes/idemp.md')
        self.assertEqual(res2['sha256'], res1['sha256'])
        self.assertTrue(res2.get('idempotent'))

        # Check that idempotency record exists on disk
        idemp_file = self.vault / LOCAL_DIRECTORY / 'idempotency' / (key + '.json')
        self.assertTrue(idemp_file.is_file())

    def test_idempotency_key_rejects_different_source(self):
        self.write(source='notes/first.md', raw=b'first content\n')
        self.write(source='notes/second.md', raw=b'second content\n')
        key = 'shared-key-1'
        req1 = {
            'source': 'notes/first.md',
            'expected_sha256': self.engine.note_read('notes/first.md')['sha256'],
            'edits': [{'old': 'first content', 'new': 'FIRST CONTENT'}],
            'idempotency_key': key
        }
        res1 = self.engine.note_edit(req1)
        self.assertEqual(res1['status'], 'written')

        # Attempt to reuse the same key for a different source
        req2 = {
            'source': 'notes/second.md',
            'expected_sha256': self.engine.note_read('notes/second.md')['sha256'],
            'edits': [{'old': 'second content', 'new': 'SECOND CONTENT'}],
            'idempotency_key': key
        }
        with self.assertRaises(ValueError) as ctx:
            self.engine.note_edit(req2)
        self.assertIn('already used for a different source', str(ctx.exception))

    def test_evidence_refs_preserved_in_result_and_backup(self):
        self.write(source='notes/evidence.md', raw=b'# Evidence Note\nstatement\n')
        h = self.engine.note_read('notes/evidence.md')['sha256']
        refs = ['session-turn-1234', 'candidate-uuid-5678', 'task-9012']
        req = {
            'source': 'notes/evidence.md',
            'expected_sha256': h,
            'edits': [{'old': 'statement', 'new': 'proven fact'}],
            'evidence_refs': refs
        }
        res = self.engine.note_edit(req)
        self.assertEqual(res['status'], 'written')
        self.assertEqual(res.get('evidence_refs'), refs)
        self.assertIsNotNone(res.get('backup'))
        backup_path = self.vault / res['backup']
        self.assertTrue(backup_path.is_file())
        backup_data = json.loads(backup_path.read_text(encoding='utf-8'))
        self.assertEqual(backup_data.get('evidence_refs'), refs)

    def test_note_create_with_idempotency_and_evidence(self):
        source = 'knowledge/concepts/quantum-test.md'
        body = '# Quantum Test\nEvidence-backed concept.\n'
        key = 'create-quantum-idemp-1'
        refs = ['session-abc-1']
        metadata = {'project': 'alpha', 'kind': 'concept'}
        res1 = self.engine.note_create(
            source, body, metadata=metadata, idempotency_key=key, evidence_refs=refs
        )
        self.assertEqual(res1['status'], 'succeeded')
        self.assertEqual(res1['source'], source)
        self.assertEqual(res1.get('evidence_refs'), refs)

        # Verify frontmatter has evidence_refs
        content = (self.vault / source).read_text(encoding='utf-8')
        self.assertIn('evidence_refs', content)
        self.assertIn('session-abc-1', content)

        # Retry with same idempotency key returns cached success without failing on 'source exists'
        res2 = self.engine.note_create(
            source, body, metadata=metadata, idempotency_key=key, evidence_refs=refs
        )
        self.assertEqual(res2['status'], 'succeeded')
        self.assertTrue(res2.get('idempotent'))

    def test_cli_note_edit_and_create_with_idempotency_flags(self):
        # Test CLI execution
        source = 'notes/cli-note.md'
        self.write(source=source, raw=b'hello cli\n')
        h = self.engine.note_read(source)['sha256']
        edit_payload = {
            'source': source,
            'expected_sha256': h,
            'edits': [{'old': 'hello cli', 'new': 'HELLO CLI'}]
        }
        cmd = [
            sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
            '--vault', str(self.vault),
            '--state', str(self.state),
            'note-edit',
            '--idempotency-key', 'cli-key-1',
            '--evidence-ref', 'cli-ref-1',
            '--file', '-'
        ]
        proc = subprocess.run(
            cmd, input=json.dumps(edit_payload), text=True, capture_output=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data['status'], 'written')
        self.assertEqual(data.get('evidence_refs'), ['cli-ref-1'])

        # Re-run identical CLI command with same idempotency key
        proc2 = subprocess.run(
            cmd, input=json.dumps(edit_payload), text=True, capture_output=True
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        data2 = json.loads(proc2.stdout)
        self.assertTrue(data2.get('idempotent'))


if __name__ == '__main__':
    unittest.main()
