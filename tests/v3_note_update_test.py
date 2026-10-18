"""Unit and integration tests for safe note-update and idempotent write policy (RFC #368 P0-E / Paket 3)."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_sync import SyncEngine, RevisionConflict, parse, _hash


class NoteUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-note-update-test-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.state = Path(self.tmp.name) / 'state'
        self.state.mkdir()
        self.engine = SyncEngine(self.vault, self.state)
        self.initial_meta = {
            'id': 'concept-wal',
            'kind': 'concept',
            'project': 'storage',
            'visibility': 'internal',
            'status': 'active',
            'facts': {'engine': 'sqlite'}
        }
        self.initial_text = 'WAL mode ensures zero write contention for concurrent readers.'
        self.created = self.engine.note_create('knowledge/wal.md', self.initial_text, self.initial_meta)
        self.path = self.vault / 'knowledge/wal.md'
        self.initial_hash = _hash(self.path.read_bytes())

    def test_note_update_happy_path_modifies_body_and_merges_metadata(self):
        """note_update replaces body and merges metadata when expected_hash matches."""
        new_text = 'WAL mode provides concurrent readers and serial writes via shm.'
        meta_changes = {'status': 'active', 'facts': {'engine': 'sqlite', 'wal_autocheckpoint': 1000}}
        
        result = self.engine.note_update(
            'knowledge/wal.md',
            self.initial_hash,
            text=new_text,
            metadata=meta_changes
        )
        self.assertEqual(result['status'], 'succeeded')
        self.assertFalse(result.get('noop', True))
        self.assertEqual(result['source'], 'knowledge/wal.md')

        # Read back on disk
        parsed_meta, parsed_body = parse(self.path.read_text(encoding='utf-8'))
        self.assertEqual(parsed_body.strip(), new_text)
        self.assertEqual(parsed_meta['id'], 'concept-wal')
        self.assertEqual(parsed_meta['project'], 'storage')
        self.assertEqual(parsed_meta['facts']['wal_autocheckpoint'], 1000)

        # Verify SQLite index projection
        with self.engine.store._connect() as db:
            row = db.execute("SELECT payload FROM records WHERE id='concept-wal'").fetchone()
            self.assertIsNotNone(row)
            rec = json.loads(row[0])
            self.assertEqual(rec['source_sha256'], _hash(self.path.read_bytes()))

    def test_note_update_raises_conflict_on_mismatched_expected_hash(self):
        """note_update refuses to overwrite when expected_hash differs from disk content."""
        wrong_hash = '0000000000000000000000000000000000000000000000000000000000000000'
        with self.assertRaises(RevisionConflict) as ctx:
            self.engine.note_update('knowledge/wal.md', wrong_hash, text='Conflicting write')
        self.assertIn('source changed', str(ctx.exception))
        
        # Disk content remains completely untouched
        self.assertEqual(self.path.read_text(encoding='utf-8'), self.path.read_text(encoding='utf-8'))
        self.assertEqual(_hash(self.path.read_bytes()), self.initial_hash)

    def test_note_update_is_idempotent_on_identical_payload(self):
        """note_update returns succeeded with noop=True when target content matches disk."""
        result = self.engine.note_update(
            'knowledge/wal.md',
            self.initial_hash,
            text=self.initial_text,
            metadata=self.initial_meta
        )
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['noop'])
        self.assertTrue(result['idempotent'])
        self.assertEqual(result['source_sha256'], self.initial_hash)

    def test_note_update_rejects_disallowed_paths_and_kinds(self):
        """note_update restricts paths to notes/ and knowledge/ and forbids kind=task."""
        # Non-semantic path
        with self.assertRaisesRegex(ValueError, 'semantic note updates belong under'):
            self.engine.note_update('other/file.md', self.initial_hash, text='Forbidden')

        # Forbid changing kind to task
        with self.assertRaisesRegex(ValueError, 'use task-update for tasks'):
            self.engine.note_update('knowledge/wal.md', self.initial_hash, metadata={'kind': 'task'})

        # Embedded frontmatter in body
        with self.assertRaisesRegex(ValueError, 'text must be body only'):
            self.engine.note_update('knowledge/wal.md', self.initial_hash, text='---\nid: x\n---\nBody')

    def test_note_update_cli_interface_and_session_edit_marking(self):
        """CLI `beyin.py note-update` runs cleanly and marks CLAUDE_CODE_SESSION_ID."""
        cli = ROOT / 'scripts/beyin_v3.py'
        session_id = 'test-cli-update-session'
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID=session_id)
        
        payload = {
            'source': 'knowledge/wal.md',
            'expected_hash': self.initial_hash[:16],  # Prefix match allowed
            'text': 'Updated via CLI successfully.',
            'metadata': {'facts': {'cli_tested': True}}
        }
        
        proc = subprocess.run(
            [sys.executable, str(cli), '--vault', str(self.vault), '--state', str(self.state), 'note-update'],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=str(self.vault),
            env=env,
            timeout=30
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        resp = json.loads(proc.stdout)
        self.assertEqual(resp['status'], 'succeeded')
        self.assertEqual(resp['source'], 'knowledge/wal.md')

        # Verify receipt reminder edit window was marked
        digest = hashlib.sha256(session_id.encode('utf-8')).hexdigest()
        edited_marker = self.state / 'receipt-reminders' / (digest + '.edited')
        self.assertTrue(edited_marker.is_file(), 'note-update must mark session edit for Claude Code')


if __name__ == '__main__':
    unittest.main()
