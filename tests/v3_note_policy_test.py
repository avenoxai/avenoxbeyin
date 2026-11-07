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
import hashlib
import time
import beyin_v3_notes
import beyin_v3_sync
from beyin_v3_sync import SyncEngine
from beyin_v3_notes import LOCAL_DIRECTORY, NoteConflict, locked
from v3_package_helpers import inherited_env


def record_path(vault, key):
    return (vault / LOCAL_DIRECTORY / 'idempotency' /
            (hashlib.sha256(key.encode('ascii')).hexdigest() + '.json'))


class NoteFixture(unittest.TestCase):
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


class NotePolicyTest(NoteFixture):
    def test_replay_rechecks_source_after_sync(self):
        request = dict(source='notes/replay.md', expected_sha256=None,
                       content='original', idempotency_key='replay')
        self.engine.note_edit(request)
        original_sync = self.engine.sync
        def external_edit():
            result = original_sync()
            (self.vault / 'notes/replay.md').write_text('external change')
            return result
        with patch.object(self.engine, 'sync', side_effect=external_edit):
            with self.assertRaises(NoteConflict):
                self.engine.note_edit(request)
        self.assertEqual((self.vault / 'notes/replay.md').read_text(), 'external change')

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

        # Check that idempotency record exists on disk, without the request body
        idemp_file = record_path(self.vault, key)
        self.assertTrue(idemp_file.is_file())
        saved = json.loads(idemp_file.read_text(encoding='utf-8'))
        self.assertEqual(saved['state'], 'committed')
        self.assertNotIn('FIRST LINE', idemp_file.read_text(encoding='utf-8'))

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
        refs = ['receipts/session.md', 'notes/candidate.md', 'tasks/t.md']
        for ref in refs:
            self.write(source=ref, raw=b'source\n')
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
        refs = ['receipts/session-abc-1.md']
        self.write(source=refs[0], raw=b'receipt\n')
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
        self.assertIn('receipts/session-abc-1.md', content)

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
        self.write(source='notes/cli-ref-1.md', raw=b'evidence\n')
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
            '--evidence-ref', 'notes/cli-ref-1.md',
            '--file', '-'
        ]
        env = inherited_env(BEYIN_UPDATES_OFF='1')
        proc = subprocess.run(
            cmd, input=json.dumps(edit_payload), text=True, capture_output=True, env=env
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data['status'], 'written')
        self.assertEqual(data.get('evidence_refs'), ['notes/cli-ref-1.md'])

        # Re-run identical CLI command with same idempotency key
        proc2 = subprocess.run(
            cmd, input=json.dumps(edit_payload), text=True, capture_output=True, env=env
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        data2 = json.loads(proc2.stdout)
        self.assertTrue(data2.get('idempotent'))



class NoteEvidenceContractTest(NoteFixture):
    """Note evidence_refs follow the task source-path contract (#443 review)."""

    def test_free_form_ids_are_rejected_like_task_evidence(self):
        with self.assertRaisesRegex(ValueError, 'existing vault source'):
            self.engine.task_create('tasks/t.md', 'body', {
                'id': 't1', 'status': 'done', 'owner': 'me', 'completion_contract': 'strict',
                'completion_criterion': 'x', 'evidence_refs': ['session-7f3a']})
        with self.assertRaisesRegex(ValueError, 'existing vault source'):
            self.engine.note_create('notes/a.md', 'body', evidence_refs=['session-7f3a'])
        with self.assertRaisesRegex(ValueError, 'existing vault source'):
            self.engine.note_create('notes/a.md', 'body', {'evidence_refs': ['session-7f3a']})
        with self.assertRaisesRegex(ValueError, 'existing vault source'):
            self.engine.note_edit(dict(source='notes/b.md', expected_sha256=None, content='x',
                                       evidence_refs=['session-7f3a']))
        self.assertFalse((self.vault / 'notes/a.md').exists())
        self.assertFalse((self.vault / 'notes/b.md').exists())

    def test_invalid_shapes_and_sources_are_rejected_before_any_write(self):
        self.write('notes/target.md', b'alpha\n')
        self.write('Gizli Notlar/plan.md', b'secret plan\n')
        self.write('notes/evidence.md', b'evidence\n')
        outside = self.vault.parent / 'outside.md'
        outside.write_text('outside', encoding='utf-8')
        h = self.engine.note_read('notes/target.md')['sha256']
        cases = [
            ('notes/evidence.md', 'list of source paths'),
            ([''], 'list of source paths'),
            ([7], 'list of source paths'),
            (['../outside.md'], 'existing vault source'),
            ([str(outside)], 'existing vault source'),
            (['notes/missing.md'], 'existing vault source'),
            (['Gizli Notlar/plan.md'], 'sensitive directory'),
            (['notes/target.md'], 'note source itself'),
            (['notes/evidence.md', 'notes/./evidence.md'], 'repeat'),
        ]
        for refs, message in cases:
            with self.subTest(refs=refs):
                with self.assertRaisesRegex(ValueError, message):
                    self.engine.note_edit(dict(source='notes/target.md', expected_sha256=h,
                                               edits=[dict(old='alpha', new='ALPHA')], evidence_refs=refs))
        self.assertEqual((self.vault / 'notes/target.md').read_bytes(), b'alpha\n')
        self.assertFalse((self.vault / LOCAL_DIRECTORY / 'backups').exists())
        with self.assertRaisesRegex(ValueError, 'sensitive directory'):
            self.engine.note_create('notes/new.md', 'body', evidence_refs=['Gizli Notlar/plan.md'])
        self.assertFalse((self.vault / 'notes/new.md').exists())

    def test_accepted_refs_are_canonical_vault_paths_in_frontmatter_and_index(self):
        self.write('receipts/2026/session.md', b'receipt\n')
        result = self.engine.note_create('notes/fact.md', 'Doğrulanmış olgu.',
                                         evidence_refs=['receipts/./2026/session.md'])
        self.assertEqual(result['evidence_refs'], ['receipts/2026/session.md'])
        text = (self.vault / 'notes/fact.md').read_text(encoding='utf-8')
        self.assertIn('"receipts/2026/session.md"', text)
        self.assertNotIn('./', text)
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        with self.engine.store._connect() as db:
            row = db.execute('SELECT r.payload FROM records r JOIN markdown_sources m ON r.id=m.id '
                             'WHERE m.source=?', ('notes/fact.md',)).fetchone()
        self.assertEqual(json.loads(row[0])['evidence_refs'], ['receipts/2026/session.md'])

    def test_key_and_refs_given_twice_must_agree(self):
        self.write('notes/evidence.md', b'evidence\n')
        with self.assertRaisesRegex(ValueError, 'given twice'):
            self.engine.note_create('notes/a.md', 'body', {'idempotency_key': 'one'}, idempotency_key='two')
        with self.assertRaisesRegex(ValueError, 'given twice'):
            self.engine.note_create('notes/a.md', 'body', {'evidence_refs': []},
                                    evidence_refs=['notes/evidence.md'])
        result = self.engine.note_create('notes/a.md', 'body', {'idempotency_key': 'one'}, idempotency_key='one')
        self.assertEqual(result['status'], 'succeeded')
        self.assertNotIn('idempotency_key', (self.vault / 'notes/a.md').read_text(encoding='utf-8'))


class NoteRetryJournalTest(NoteFixture):
    """Idempotency journal semantics and bounds (#443 review)."""

    def edit(self, source, old, new, key, **extra):
        return self.engine.note_edit(dict(source=source, expected_sha256=self.engine.note_read(source)['sha256'],
                                          edits=[dict(old=old, new=new)], idempotency_key=key, **extra))

    def test_alias_spelling_does_not_split_a_retry(self):
        self.write('notes/alias.md', b'alpha\n')
        h = self.engine.note_read('notes/alias.md')['sha256']
        edits = [dict(old='alpha', new='ALPHA')]
        first = self.engine.note_edit(dict(source='notes/alias.md', expected_source_hash=h, edits=edits,
                                           idempotency_key='alias'))
        retry = self.engine.note_edit(dict(source='notes/alias.md', expected_sha256=h, edits=edits,
                                           idempotency_key='alias'))
        self.assertEqual(first['status'], 'written')
        self.assertTrue(retry['idempotent'])
        both = self.engine.note_edit(dict(source='notes/alias.md', expected_sha256=h, expected_source_hash=h,
                                          edits=edits, idempotency_key='alias'))
        self.assertTrue(both['idempotent'])

    def test_completed_request_is_never_applied_again_after_a_revert(self):
        self.write('notes/aba.md', b'alpha\n')
        h = self.engine.note_read('notes/aba.md')['sha256']
        request = dict(source='notes/aba.md', expected_sha256=h, edits=[dict(old='alpha', new='ALPHA')],
                       idempotency_key='aba')
        self.assertEqual(self.engine.note_edit(request)['status'], 'written')
        (self.vault / 'notes/aba.md').write_bytes(b'alpha\n')  # The user reverts by hand.
        with self.assertRaisesRegex(NoteConflict, 'already completed'):
            self.engine.note_edit(request)
        self.assertEqual((self.vault / 'notes/aba.md').read_bytes(), b'alpha\n')
        self.engine.note_create('notes/created.md', 'body', idempotency_key='created')
        os.unlink(self.vault / 'notes/created.md')  # The user deletes the new note.
        with self.assertRaisesRegex(NoteConflict, 'already completed'):
            self.engine.note_create('notes/created.md', 'body', idempotency_key='created')
        self.assertFalse((self.vault / 'notes/created.md').exists())

    def test_retry_after_interruption_before_replace_applies_once(self):
        self.write('notes/pending.md', b'alpha\n')
        h = self.engine.note_read('notes/pending.md')['sha256']
        request = dict(source='notes/pending.md', expected_sha256=h, edits=[dict(old='alpha', new='ALPHA')],
                       idempotency_key='pending')
        original = beyin_v3_sync._safe_replace
        def interrupted(src, dst):
            if Path(dst).name == 'pending.md':
                raise OSError('synthetic interruption before source replace')
            return original(src, dst)
        with patch.object(beyin_v3_sync, '_safe_replace', interrupted):
            with self.assertRaises(OSError):
                self.engine.note_edit(request)
        self.assertEqual((self.vault / 'notes/pending.md').read_bytes(), b'alpha\n')
        self.assertEqual(json.loads(record_path(self.vault, 'pending').read_text())['state'], 'pending')
        result = self.engine.note_edit(request)
        self.assertEqual(result['status'], 'written')
        self.assertNotIn('idempotent', result)
        self.assertEqual((self.vault / 'notes/pending.md').read_bytes(), b'ALPHA\n')
        self.assertTrue(self.engine.note_edit(request)['idempotent'])

    def test_seal_failure_keeps_a_verified_write_successful(self):
        self.write('notes/seal.md', b'alpha\n')
        original = beyin_v3_notes._save_idempotency
        def full_disk(path, details, fingerprint):
            if details.get('state') == 'committed':
                raise OSError(28, 'No space left on device')
            return original(path, details, fingerprint)
        with patch.object(beyin_v3_notes, '_save_idempotency', full_disk):
            result = self.edit('notes/seal.md', 'alpha', 'ALPHA', 'seal')
        self.assertEqual(result['status'], 'written')
        self.assertEqual(result['idempotency_journal'], 'pending')
        h = json.loads(record_path(self.vault, 'seal').read_text())
        self.assertEqual(h['state'], 'pending')
        retry = self.engine.note_edit(dict(source='notes/seal.md', expected_sha256=hashlib.sha256(b'alpha\n').hexdigest(),
                                           edits=[dict(old='alpha', new='ALPHA')], idempotency_key='seal'))
        self.assertTrue(retry['idempotent'])
        self.assertEqual(json.loads(record_path(self.vault, 'seal').read_text())['state'], 'committed')

    def test_unchanged_answer_replays_without_a_projection_check(self):
        self.write('notes/same.md', b'alpha\n')
        h = self.engine.note_read('notes/same.md')['sha256']
        request = dict(source='notes/same.md', expected_sha256=h, edits=[dict(old='alpha', new='alpha')],
                       idempotency_key='same')
        self.assertEqual(self.engine.note_edit(request)['status'], 'unchanged')
        with patch.object(self.engine, 'sync', side_effect=AssertionError('unchanged replay must not sync')):
            retry = self.engine.note_edit(request)
        self.assertEqual((retry['status'], retry['idempotent'], retry['sha256']), ('unchanged', True, h))

    def test_keys_differing_only_in_case_stay_distinct(self):
        first = self.engine.note_edit(dict(source='notes/x.md', expected_sha256=None, content='x',
                                           idempotency_key='Req'))
        second = self.engine.note_edit(dict(source='notes/y.md', expected_sha256=None, content='y',
                                            idempotency_key='req'))
        self.assertEqual((first['status'], second['status']), ('written', 'written'))

    def test_invalid_keys_are_rejected(self):
        for key in ('', 'a b', 'x' * 129, '../escape', 'ş', 7):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, 'idempotency_key'):
                    self.engine.note_edit(dict(source='notes/k.md', expected_sha256=None, content='k',
                                               idempotency_key=key))
        self.assertFalse((self.vault / 'notes/k.md').exists())

    def test_unreadable_record_is_an_error_not_a_crash(self):
        self.engine.note_edit(dict(source='notes/z.md', expected_sha256=None, content='z', idempotency_key='bad'))
        for body in ('[1, 2]', '{"state": "pending"}', 'not json', '\udcff'):
            with self.subTest(body=body):
                record_path(self.vault, 'bad').write_text(body, encoding='utf-8', errors='surrogateescape')
                with self.assertRaisesRegex(ValueError, 'unreadable'):
                    self.engine.note_edit(dict(source='notes/z.md', expected_sha256=None, content='z',
                                               idempotency_key='bad'))

    def test_retry_survives_a_later_evidence_removal(self):
        self.write('notes/ev.md', b'evidence\n')
        request = dict(source='notes/r.md', expected_sha256=None, content='r', idempotency_key='ev',
                       evidence_refs=['notes/ev.md'])
        self.engine.note_edit(request)
        os.unlink(self.vault / 'notes/ev.md')
        retry = self.engine.note_edit(request)
        self.assertTrue(retry['idempotent'])
        self.assertEqual(retry['evidence_refs'], ['notes/ev.md'])

    def test_index_excluded_source_replays(self):
        (self.state / 'index.json').write_text(json.dumps({'excluded_dirs': ['Arşiv']}), encoding='utf-8')
        self.write('Arşiv/old.md', b'alpha\n')
        h = self.engine.note_read('Arşiv/old.md')['sha256']
        request = dict(source='Arşiv/old.md', expected_sha256=h, edits=[dict(old='alpha', new='ALPHA')],
                       idempotency_key='archive')
        self.assertEqual(self.engine.note_edit(request)['status'], 'written')
        with self.engine.store._connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM markdown_sources WHERE source=?',
                                         ('Arşiv/old.md',)).fetchone())
        self.assertTrue(self.engine.note_edit(request)['idempotent'])

    def test_journal_is_bounded_by_count_and_age(self):
        directory = self.vault / LOCAL_DIRECTORY / 'idempotency'
        with patch.object(beyin_v3_notes, 'IDEMPOTENCY_MAX_RECORDS', 3):
            for index in range(6):
                self.engine.note_edit(dict(source=f'notes/n{index}.md', expected_sha256=None,
                                           content=str(index), idempotency_key=f'k{index}'))
        records = sorted(path.name for path in directory.iterdir() if path.suffix == '.json')
        self.assertEqual(len(records), 3)
        self.assertTrue(record_path(self.vault, 'k5').is_file())
        # A pruned key only loses its replay answer; the late retry cannot write twice.
        with self.assertRaisesRegex(NoteConflict, 'source changed'):
            self.engine.note_edit(dict(source='notes/n0.md', expected_sha256=None, content='0',
                                       idempotency_key='k0'))
        old = time.time() - beyin_v3_notes.IDEMPOTENCY_MAX_AGE - 60
        for name in records:
            os.utime(directory / name, (old, old))
        leftover = directory / '.idempotency-crash.tmp'
        leftover.write_text('{}', encoding='utf-8')
        foreign = directory / 'notes.txt'
        foreign.write_text('keep', encoding='utf-8')
        self.engine.note_edit(dict(source='notes/fresh.md', expected_sha256=None, content='f',
                                   idempotency_key='fresh'))
        remaining = sorted(path.name for path in directory.iterdir())
        self.assertEqual(remaining, sorted([record_path(self.vault, 'fresh').name, 'notes.txt']))

    def test_pruning_never_follows_links(self):
        outside = self.vault.parent / 'outside-records'
        outside.mkdir()
        victim = outside / ('a' * 64 + '.json')
        victim.write_text('keep', encoding='utf-8')
        directory = self.vault / LOCAL_DIRECTORY / 'idempotency'
        directory.mkdir(parents=True)
        try:
            (directory / ('b' * 64 + '.json')).symlink_to(victim)
        except OSError:
            self.skipTest('symlink unavailable')
        old = time.time() - beyin_v3_notes.IDEMPOTENCY_MAX_AGE - 60
        os.utime(victim, (old, old))
        self.engine.note_edit(dict(source='notes/link.md', expected_sha256=None, content='l',
                                   idempotency_key='link'))
        self.assertEqual(victim.read_text(encoding='utf-8'), 'keep')
        self.assertTrue((directory / ('b' * 64 + '.json')).is_symlink())


if __name__ == '__main__':
    unittest.main()
