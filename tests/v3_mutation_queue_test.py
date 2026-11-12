import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))

import beyin_v3_mutation as mutation
from beyin_v3_sync import SyncEngine, _hash
from v3_package_helpers import inherited_env
CLI = ROOT / "scripts/beyin_v3.py"


class Crash(BaseException):
    """Stands in for a killed process: no except-Exception handler may absorb it."""


class MutationQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="beyin-mqueue-test-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.sync = SyncEngine(self.vault, self.state)
        self.sync.sync()
        mutation.set_enabled(self.state, True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_note_update_uses_guarded_writer_and_preserves_backup(self):
        import base64
        path = self.vault / 'notes/empty.md'
        path.parent.mkdir()
        path.write_bytes(b'')
        mutation.enqueue_mutation(self.state, 'note_update', 'notes/empty.md',
            {'text': 'Recovered note', 'expected_hash': _hash(b'')})
        result = mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual(result['applied'], 1, result)
        self.assertEqual(result['failed'], 0, result)
        self.assertIn('Recovered note', path.read_text(encoding='utf-8'))
        backups = list((self.vault / '.beyin-note-write/backups').glob('*.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(base64.b64decode(json.loads(backups[0].read_text())['bytes_base64']), b'')

    def test_empty_note_update_preserves_an_intervening_write(self):
        from unittest.mock import patch
        path = self.vault / 'notes/empty.md'
        path.parent.mkdir()
        path.write_bytes(b'')
        mutation.enqueue_mutation(self.state, 'note_update', 'notes/empty.md',
            {'text': 'Queued update', 'expected_hash': _hash(b'')})
        original = self.sync.note_edit
        def intervening_edit(request):
            path.write_text('Newer live note', encoding='utf-8')
            return original(request)
        with patch.object(self.sync, 'note_edit', side_effect=intervening_edit):
            result = mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual(result['conflicts'], 1, result)
        self.assertEqual(path.read_text(encoding='utf-8'), 'Newer live note')

    def test_empty_edit_anchor_cannot_insert_into_nonempty_source(self):
        path = self.vault / 'notes/live.md'
        path.parent.mkdir()
        path.write_bytes(b'Live note')
        with self.assertRaisesRegex(ValueError, 'nonempty old'):
            self.sync.note_edit({'source': 'notes/live.md', 'expected_sha256': _hash(b'Live note'),
                'edits': [{'old': '', 'new': 'Unanchored insert'}]})
        self.assertEqual(path.read_bytes(), b'Live note')

    def test_malformed_queue_is_not_silently_rewritten(self):
        queue = self.state / mutation.MUTATION_QUEUE_FILE
        original = b'{broken JSON\n'
        queue.write_bytes(original)
        for operation in (lambda: mutation.replay_mutations(self.vault, self.state, self.sync),
                          lambda: mutation.clear_applied(self.state)):
            with self.assertRaises(ValueError):
                operation()
            self.assertEqual(queue.read_bytes(), original)

    def test_doctor_does_not_create_identity_or_state(self):
        empty = self.tmp / 'absent-state'
        self.assertEqual(mutation.doctor(empty), {'enabled': False})
        self.assertFalse(mutation.status(empty)['identity'])
        self.assertFalse(empty.exists())

    def test_update_requires_full_hash_and_preserves_private_metadata(self):
        from beyin_v3_sync import parse
        self.sync.note_create('notes/private.md', 'original', {'visibility': 'private', 'project': 'restricted'})
        path = self.vault / 'notes/private.md'
        original = path.read_bytes()
        for expected in ('', _hash(original)[:1]):
            with self.assertRaisesRegex(ValueError, 'full SHA-256'):
                mutation.enqueue_mutation(self.state, 'note_update', 'notes/private.md',
                    {'text': 'stale', 'expected_hash': expected})
            # Replay rechecks a record that reached the queue file some other way.
            item = mutation.enqueue_mutation(self.state, 'note_update', 'notes/private.md',
                {'text': 'stale', 'expected_hash': 'c' * 64})
            item['payload']['expected_hash'] = expected
            mutation._save_all_mutations(self.state, [item])
            self.assertEqual(mutation.replay_mutations(self.vault, self.state, self.sync)['failed'], 1)
            self.assertEqual(path.read_bytes(), original)
        mutation.enqueue_mutation(self.state, 'note_update', 'notes/private.md',
            {'text': 'updated', 'expected_hash': _hash(original)})
        result = mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual(result['applied'], 1, result)
        metadata, body = parse(path.read_text())
        self.assertEqual(metadata['visibility'], 'private')
        self.assertEqual(metadata['project'], 'restricted')
        self.assertEqual(body, 'updated\n')

    def test_replay_revalidates_source_even_for_tampered_queue(self):
        outside = self.tmp / 'victim.md'
        outside.write_text('preserve')
        item = mutation.enqueue_mutation(self.state, 'note_update', 'notes/valid.md',
            {'text': 'overwrite', 'expected_hash': _hash(b'preserve')})
        item['source'] = '../victim.md'
        mutation._save_all_mutations(self.state, [item])
        result = mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(outside.read_text(), 'preserve')

    def test_update_and_conflict_redact_secrets_and_keep_distinct_copies(self):
        from beyin_v3_preferences import save
        save(self.vault, {'secret_filter': True})
        token = 'sk-' + 'SyntheticOnly' * 3
        self.sync.note_create('notes/redact.md', 'original')
        path = self.vault / 'notes/redact.md'
        # Distinct texts: an update the note already reflects is acknowledged, not copied.
        for number, expected in enumerate((_hash(path.read_bytes()), 'a' * 64, 'b' * 64)):
            mutation.enqueue_mutation(self.state, 'note_update', 'notes/redact.md',
                {'text': f'{token} {number}', 'expected_hash': expected})
        result = mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual((result['applied'], result['conflicts']), (1, 2), result)
        copies = list(path.parent.glob('*.sync-conflict-*.md'))
        self.assertEqual(len(copies), 2)
        for note in [path] + copies:
            self.assertNotIn(token, note.read_text())
            self.assertIn('[REDACTED]', note.read_text())

    def test_note_and_task_create_replay_are_idempotent(self):
        for action, source, payload in [
            ('note_create', 'notes/repeat.md', {'text': 'same'}),
            ('task_create', 'tasks/repeat.md', {'text': 'same', 'metadata':
                {'id': 'repeat', 'owner': 'test', 'status': 'active'}})]:
            for _ in range(2):
                mutation.enqueue_mutation(self.state, action, source, payload)
                result = mutation.replay_mutations(self.vault, self.state, self.sync)
                self.assertEqual((result['applied'], result['conflicts']), (1, 0), result)

    def test_enqueue_during_replay_is_preserved(self):
        import threading
        from unittest.mock import patch
        mutation.enqueue_mutation(self.state, 'note_create', 'notes/first.md', {'text': 'first'})
        original = self.sync.note_create
        appended = threading.Event()
        errors = []
        def enqueue():
            try:
                mutation.enqueue_mutation(self.state, 'note_create', 'notes/second.md', {'text': 'second'})
            except Exception as exc:
                errors.append(exc)
            finally:
                appended.set()
        thread = threading.Thread(target=enqueue)
        def interleave(*args, **kwargs):
            thread.start()
            appended.wait(0.2)
            return original(*args, **kwargs)
        with patch.object(self.sync, 'note_create', side_effect=interleave):
            mutation.replay_mutations(self.vault, self.state, self.sync)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual({m['source'] for m in mutation.list_mutations(self.state)},
                         {'notes/first.md', 'notes/second.md'})

    def test_machine_id_generation_and_persistence(self):
        mid1 = mutation.get_machine_id(self.state)
        self.assertRegex(mid1, r"^[0-9a-f]{32}$")

        mid2 = mutation.get_machine_id(self.state)
        self.assertEqual(mid1, mid2)

        # Reuses parallel presence device if present
        other_state = self.tmp / "other_state"
        other_state.mkdir()
        presence_file = other_state / "parallel-presence-device.json"
        presence_file.write_text(json.dumps({"schema": 1, "device": "abcdef0123456789abcdef0123456789"}), encoding="utf-8")
        mid3 = mutation.get_machine_id(other_state)
        self.assertEqual(mid3, "abcdef0123456789abcdef0123456789")

    def test_enqueue_and_list_mutations(self):
        m1 = mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/first.md",
            {"text": "First note body", "metadata": {"title": "First"}},
        )
        self.assertEqual(m1["status"], "pending")
        self.assertEqual(m1["action"], "note_create")
        self.assertEqual(m1["source"], "notes/first.md")

        m2 = mutation.enqueue_mutation(
            self.state,
            "receipt",
            "receipts/rec1.md",
            {"event_id": "rec1", "summary": "Did work", "refs": ["notes/first.md"]},
        )
        self.assertEqual(m2["status"], "pending")

        items = mutation.list_mutations(self.state)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["mutation_id"], m1["mutation_id"])
        self.assertEqual(items[1]["mutation_id"], m2["mutation_id"])

        pending_items = mutation.list_mutations(self.state, status="pending")
        self.assertEqual(len(pending_items), 2)

        applied_items = mutation.list_mutations(self.state, status="applied")
        self.assertEqual(len(applied_items), 0)

    def test_replay_mutations_applies_note_and_receipt(self):
        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/test-note.md",
            {"text": "Hello offline world", "metadata": {"title": "Offline Note"}},
        )
        mutation.enqueue_mutation(
            self.state,
            "receipt",
            "receipts/rec-test.md",
            {"event_id": "rec-test-1", "summary": "Offline receipt", "refs": ["notes/test-note.md"]},
        )

        res = mutation.replay_mutations(self.vault, self.state, sync_engine=self.sync)
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(res["applied"], 2)
        self.assertEqual(res["conflicts"], 0)
        self.assertEqual(res["failed"], 0)

        # Verify files on disk
        note_file = self.vault / "notes/test-note.md"
        self.assertTrue(note_file.is_file())
        self.assertIn("Hello offline world", note_file.read_text(encoding="utf-8"))

        receipt_file = self.vault / "receipts" / f"{_hash('rec-test-1')}.md"
        self.assertTrue(receipt_file.is_file())

        # Verify queue status updated
        applied = mutation.list_mutations(self.state, status="applied")
        self.assertEqual(len(applied), 2)

        # Idempotent replay: enqueuing exact same note_create
        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/test-note.md",
            {"text": "Hello offline world", "metadata": {"title": "Offline Note"}},
        )
        res2 = mutation.replay_mutations(self.vault, self.state, sync_engine=self.sync)
        self.assertEqual(res2["applied"], 1)
        self.assertEqual(res2["conflicts"], 0)
        applied2 = mutation.list_mutations(self.state, status="applied")
        self.assertTrue(any(m.get("idempotent") for m in applied2))

    def test_replay_task_create_and_update(self):
        task_meta = {
            "id": "offline-task-1",
            "title": "Offline Task",
            "status": "inbox",
            "owner": "test-agent",
            "project": "core",
            "visibility": "internal",
        }
        mutation.enqueue_mutation(
            self.state,
            "task_create",
            "tasks/offline-task.md",
            {"text": "Task description", "metadata": task_meta},
        )
        res = mutation.replay_mutations(self.vault, self.state, sync_engine=self.sync)
        self.assertEqual(res["applied"], 1)
        self.assertTrue((self.vault / "tasks/offline-task.md").is_file())

        # Update task status to active
        mutation.enqueue_mutation(
            self.state,
            "task_update",
            "tasks/offline-task.md",
            {"id": "offline-task-1", "expected_revision": 1, "changes": {"status": "active"}},
        )
        res2 = mutation.replay_mutations(self.vault, self.state, sync_engine=self.sync)
        self.assertEqual(res2["applied"], 1)
        content = (self.vault / "tasks/offline-task.md").read_text(encoding="utf-8")
        self.assertIn('"status": "active"', content)
        self.assertIn('"revision": 2', content)

    def test_conflict_isolation_preserves_original_and_creates_stamped_copy(self):
        # Create an existing note in the vault
        note_path = self.vault / "notes/conflict.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        original_text = "---\n{\n  \"title\": \"Original\"\n}\n---\nOriginal content that must not be lost\n"
        note_path.write_text(original_text, encoding="utf-8")
        self.sync.sync()

        # Enqueue a conflicting note_create with different content
        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/conflict.md",
            {"text": "Conflicting incoming content", "metadata": {"title": "Incoming"}},
        )

        res = mutation.replay_mutations(self.vault, self.state, sync_engine=self.sync)
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(res["conflicts"], 1)
        self.assertEqual(res["applied"], 0)

        # Original note is UNTOUCHED
        self.assertEqual(note_path.read_text(encoding="utf-8"), original_text)

        # Conflicted copy is created
        mut = mutation.list_mutations(self.state, status="conflict")[0]
        conflict_rel = mut.get("conflict_path")
        self.assertIsNotNone(conflict_rel)
        conflict_file = self.vault / conflict_rel
        self.assertTrue(conflict_file.is_file())
        self.assertIn("Conflicting incoming content", conflict_file.read_text(encoding="utf-8"))

        # Regex check: adheres to _STAMPED_COPY format
        self.assertRegex(conflict_file.name, r"^conflict\.sync-conflict-\d{8}-\d{6}-[0-9a-f]{8}\.md$")

        # Sync recognizes the conflict copy and warns without overwriting
        sync_res = self.sync.sync()
        self.assertEqual(sync_res["status"], "degraded")
        self.assertTrue(any("file sync conflict copy" in w.get("reason", "") for w in sync_res["warnings"]))

    def test_clear_applied_prunes_queue(self):
        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/n1.md",
            {"text": "Note 1"},
        )
        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/n2.md",
            {"text": "Note 2"},
        )
        mutation.replay_mutations(self.vault, self.state, sync_engine=self.sync)

        # Add one more pending
        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/n3.md",
            {"text": "Note 3"},
        )

        all_muts = mutation.list_mutations(self.state)
        self.assertEqual(len(all_muts), 3)

        pruned_info = mutation.clear_applied(self.state)
        self.assertEqual(pruned_info["pruned"], 2)
        self.assertEqual(pruned_info["remaining"], 1)

        remaining = mutation.list_mutations(self.state)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["source"], "notes/n3.md")

    def test_doctor_diagnostics(self):
        diag1 = mutation.doctor(self.state)
        self.assertEqual(diag1["status"], "ok")
        self.assertEqual(diag1["queue_exists"], False)

        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/pending.md",
            {"text": "Pending note"},
        )
        diag2 = mutation.doctor(self.state)
        self.assertEqual(diag2["status"], "pending")
        self.assertEqual(diag2["pending_count"], 1)
        self.assertEqual(diag2["queue_exists"], True)

    def test_cli_mutation_queue_commands(self):
        def run_cli(*args):
            cmd = [sys.executable, str(CLI), "--vault", str(self.vault), "--state", str(self.state)] + list(args)
            proc = subprocess.run(cmd, capture_output=True, text=True, env=inherited_env(BEYIN_UPDATES_OFF="1"))
            try:
                data = json.loads(proc.stdout)
            except Exception:
                data = proc.stdout
            return data, proc.returncode

        # 1. doctor status via CLI
        res, code = run_cli("mutation-queue")
        self.assertEqual(code, 0)
        self.assertTrue(res["enabled"])
        self.assertNotIn("machine_id", res)
        self.assertEqual(res["pending_count"], 0)

        # 2. enqueue via CLI
        payload_file = self.tmp / "enqueue.json"
        payload_file.write_text(json.dumps({
            "action": "note_create",
            "source": "notes/cli-note.md",
            "payload": {"text": "From CLI", "metadata": {"title": "CLI Note"}}
        }), encoding="utf-8")

        res_enq, code_enq = run_cli("mutation-queue", "--enqueue", "--file", str(payload_file))
        self.assertEqual(code_enq, 0)
        self.assertEqual(res_enq["status"], "pending")
        self.assertEqual(res_enq["source"], "notes/cli-note.md")

        # 3. list via CLI
        res_list, code_list = run_cli("mutation-queue", "--list")
        self.assertEqual(code_list, 0)
        self.assertEqual(len(res_list), 1)

        # 4. replay via CLI
        res_rep, code_rep = run_cli("mutation-queue", "--replay")
        self.assertEqual(code_rep, 0)
        self.assertEqual(res_rep["applied"], 1)
        self.assertTrue((self.vault / "notes/cli-note.md").is_file())

        # 5. clear-applied via CLI
        res_clr, code_clr = run_cli("mutation-queue", "--clear-applied")
        self.assertEqual(code_clr, 0)
        self.assertEqual(res_clr["pruned"], 1)

    # Opt-in switch (#409 maintainer decision): off by default, machine-local, nothing written while off.

    def test_queue_is_off_by_default_and_writes_nothing(self):
        state = self.tmp / 'fresh-state'
        state.mkdir()
        for operation in (
                lambda: mutation.enqueue_mutation(state, 'note_create', 'notes/off.md', {'text': 'off'}),
                lambda: mutation.replay_mutations(self.vault, state, self.sync)):
            with self.assertRaisesRegex(ValueError, 'mutation queue is off'):
                operation()
        self.assertEqual(mutation.doctor(state), {'enabled': False})
        self.assertEqual(mutation.clear_applied(state)['pruned'], 0)
        self.assertEqual(mutation.list_mutations(state), [])
        self.assertEqual(list(state.iterdir()), [])
        self.assertFalse((self.vault / 'notes/off.md').exists())

    def test_switch_lives_in_state_and_never_in_vault_preferences(self):
        from beyin_v3_preferences import save
        save(self.vault, {'secret_filter': False})
        preferences = (self.vault / '.beyin-preferences.json').read_bytes()
        mutation.set_enabled(self.state, False)
        mutation.set_enabled(self.state, True)
        self.assertEqual(json.loads((self.state / mutation.SETTINGS_FILE).read_text()), {'schema': 1, 'enabled': True})
        self.assertEqual((self.vault / '.beyin-preferences.json').read_bytes(), preferences)
        mutation.set_enabled(self.state, False)
        with self.assertRaisesRegex(ValueError, 'mutation queue is off'):
            mutation.enqueue_mutation(self.state, 'note_create', 'notes/off.md', {'text': 'off'})

    def test_invalid_switch_reads_off_and_is_not_rewritten(self):
        settings = self.state / mutation.SETTINGS_FILE
        for broken in (b'{broken', b'{"schema": 1, "enabled": "yes"}', b'{"schema": 1, "enabled": true, "x": 1}'):
            settings.write_bytes(broken)
            with self.assertRaisesRegex(ValueError, 'invalid'):
                mutation.enqueue_mutation(self.state, 'note_create', 'notes/off.md', {'text': 'off'})
            with self.assertRaisesRegex(ValueError, 'invalid'):
                mutation.set_enabled(self.state, True)
            report = mutation.doctor(self.state)
            self.assertEqual((report['enabled'], report['valid'], report['status']), (False, False, 'needs_attention'))
            self.assertEqual(settings.read_bytes(), broken)

    def test_cli_queue_is_off_by_default(self):
        state = self.tmp / 'cli-state'
        payload_file = self.tmp / 'off.json'
        payload_file.write_text(json.dumps({'action': 'note_create', 'source': 'notes/off.md',
                                            'payload': {'text': 'off'}}), encoding='utf-8')
        def run_cli(*args):
            cmd = [sys.executable, str(CLI), '--vault', str(self.vault), '--state', str(state)] + list(args)
            return subprocess.run(cmd, capture_output=True, text=True, env=inherited_env(BEYIN_UPDATES_OFF='1'))
        for args in (('--enqueue', '--file', str(payload_file)), ('--replay',)):
            proc = run_cli('mutation-queue', *args)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn('mutation queue is off', json.loads(proc.stderr)['message'])
        status = json.loads(run_cli('mutation-queue').stdout)
        self.assertEqual((status['enabled'], status['queue_exists'], status['identity']), (False, False, False))
        for name in (mutation.MUTATION_QUEUE_FILE, mutation.DEVICE_FILE, mutation.SETTINGS_FILE, '.beyin-note-write'):
            self.assertFalse((state / name).exists(), name)
        self.assertFalse((self.vault / 'notes/off.md').exists())
        proc = run_cli('mutation-queue', '--enqueue', '--replay')
        self.assertEqual(proc.returncode, 2)

    # Crash consistency: an interrupted replay is reconciled, never double-applied or misreported.

    def _task(self):
        self.sync.task_create('tasks/crash.md', 'Crash body', {'id': 'crash-task', 'owner': 'test', 'status': 'inbox'})
        mutation.enqueue_mutation(self.state, 'task_update', 'tasks/crash.md',
            {'id': 'crash-task', 'expected_revision': 1, 'changes': {'status': 'active'}})
        return self.vault / 'tasks/crash.md'

    def test_crash_between_task_update_and_queue_ack_is_reconciled(self):
        from unittest.mock import patch
        from beyin_v3_sync import parse
        path = self._task()
        with patch.object(mutation, '_save_all_mutations', side_effect=Crash):
            with self.assertRaises(Crash):
                mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual(parse(path.read_text(encoding='utf-8'))[0]['revision'], 2)
        self.assertEqual(mutation.list_mutations(self.state)[0]['status'], 'pending')
        result = mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual((result['applied'], result['conflicts'], result['failed']), (1, 0, 0), result)
        item = mutation.list_mutations(self.state)[0]
        self.assertEqual((item['status'], item.get('reconciled')), ('applied', True))
        metadata = parse(path.read_text(encoding='utf-8'))[0]
        self.assertEqual((metadata['revision'], metadata['status']), (2, 'active'))

    def test_crash_inside_task_write_before_sync_is_recovered_and_reconciled(self):
        from unittest.mock import patch
        from beyin_v3_sync import parse
        path = self._task()
        # The source and the engine's write journal are on disk; the projection is not.
        with patch.object(self.sync, 'sync', side_effect=Crash):
            with self.assertRaises(Crash):
                mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual(parse(path.read_text(encoding='utf-8'))[0]['revision'], 2)
        result = mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual((result['applied'], result['conflicts'], result['failed']), (1, 0, 0), result)
        self.assertTrue(mutation.list_mutations(self.state)[0]['reconciled'])
        with self.sync.store._connect() as db:
            record = json.loads(db.execute("SELECT payload FROM records WHERE id='crash-task'").fetchone()[0])
        self.assertEqual((record['revision'], record['status']), (2, 'active'))

    def test_task_update_lost_to_another_writer_stays_a_conflict(self):
        path = self._task()
        self.sync.update_task('crash-task', 1, {'status': 'waiting'})
        result = mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual((result['applied'], result['conflicts']), (0, 1), result)
        self.assertIn('"status": "waiting"', path.read_text(encoding='utf-8'))
        self.assertFalse(list(path.parent.glob('*.sync-conflict-*.md')))

    def test_crash_between_note_update_and_queue_ack_leaves_no_conflict_copy(self):
        from unittest.mock import patch
        self.sync.note_create('notes/crash.md', 'before')
        path = self.vault / 'notes/crash.md'
        mutation.enqueue_mutation(self.state, 'note_update', 'notes/crash.md',
            {'text': 'after', 'expected_hash': _hash(path.read_bytes())})
        with patch.object(mutation, '_save_all_mutations', side_effect=Crash):
            with self.assertRaises(Crash):
                mutation.replay_mutations(self.vault, self.state, self.sync)
        written = path.read_bytes()
        result = mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual((result['applied'], result['conflicts']), (1, 0), result)
        self.assertTrue(mutation.list_mutations(self.state)[0]['reconciled'])
        self.assertEqual(path.read_bytes(), written)
        self.assertFalse(list(path.parent.glob('*.sync-conflict-*.md')))

    def test_crash_after_note_create_with_retry_key_or_evidence_is_reconciled(self):
        # note_create drops a metadata idempotency_key and stores evidence_refs canonically (#443);
        # the replay's byte prediction must match, or an interrupted create becomes a conflict copy.
        (self.vault / 'notes').mkdir(exist_ok=True)
        (self.vault / 'notes/ev.md').write_text('evidence\n', encoding='utf-8')
        for name, metadata in (('key', {'idempotency_key': 'retry-1'}),
                               ('refs', {'evidence_refs': ['notes/./ev.md']}),
                               ('both', {'idempotency_key': 'retry-2', 'evidence_refs': ['notes/ev.md'], 'title': 'T'})):
            with self.subTest(name=name):
                source = 'notes/created-' + name + '.md'
                mutation.enqueue_mutation(self.state, 'note_create', source, {'text': 'body', 'metadata': metadata})
                self.sync.note_create(source, 'body', dict(metadata))  # written, queue not yet acknowledged
                written = (self.vault / source).read_bytes()
                result = mutation.replay_mutations(self.vault, self.state, self.sync)
                self.assertEqual((result['applied'], result['conflicts'], result['failed']), (1, 0, 0), result)
                self.assertEqual((self.vault / source).read_bytes(), written)
        self.assertFalse(list((self.vault / 'notes').glob('*.sync-conflict-*.md')))
        # A different note at the same source is still isolated as a conflict.
        mutation.enqueue_mutation(self.state, 'note_create', 'notes/other.md',
                                  {'text': 'body', 'metadata': {'idempotency_key': 'retry-3'}})
        (self.vault / 'notes/other.md').write_text('someone else\n', encoding='utf-8')
        result = mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual((result['applied'], result['conflicts']), (0, 1), result)

    def test_crash_after_task_write_with_noncanonical_evidence_is_reconciled(self):
        # task_create and update_task store evidence_refs canonically (notes/./e.md -> notes/e.md);
        # the reconciliation after a crash before the queue acknowledgement must compare that form,
        # or the write becomes a false conflict (and, for a create, a task conflict copy).
        from unittest.mock import patch
        from beyin_v3_sync import parse
        (self.vault / 'notes').mkdir()
        for name in ('e', 'f'):
            (self.vault / f'notes/{name}.md').write_text('evidence\n', encoding='utf-8')
        path = self.vault / 'tasks/evidence.md'
        for action, payload, stored in (
                ('task_create', {'text': 'Evidence task', 'metadata': {
                    'id': 'evidence-task', 'owner': 'test', 'status': 'active', 'evidence_refs': ['notes/./e.md']}},
                 ['notes/e.md']),
                ('task_update', {'id': 'evidence-task', 'expected_revision': 1,
                                 'changes': {'evidence_refs': ['notes/./e.md', 'notes//f.md']}},
                 ['notes/e.md', 'notes/f.md'])):
            with self.subTest(action=action):
                mutation.enqueue_mutation(self.state, action, 'tasks/evidence.md', payload)
                with patch.object(mutation, '_save_all_mutations', side_effect=Crash):
                    with self.assertRaises(Crash):
                        mutation.replay_mutations(self.vault, self.state, self.sync)
                self.assertEqual(parse(path.read_text(encoding='utf-8'))[0]['evidence_refs'], stored)
                written = path.read_bytes()
                result = mutation.replay_mutations(self.vault, self.state, self.sync)
                self.assertEqual((result['applied'], result['conflicts'], result['failed']), (1, 0, 0), result)
                self.assertEqual(mutation.list_mutations(self.state)[-1]['status'], 'applied')
                self.assertEqual(path.read_bytes(), written)
        self.assertFalse(list((self.vault / 'tasks').glob('*.sync-conflict-*.md')))

    def test_each_mutation_is_acknowledged_before_the_next_one(self):
        from unittest.mock import patch
        for name in ('first', 'second'):
            mutation.enqueue_mutation(self.state, 'note_create', f'notes/{name}.md', {'text': name})
        original = self.sync.note_create
        def die_on_second(source, *args, **kwargs):
            if source == 'notes/second.md':
                raise Crash
            return original(source, *args, **kwargs)
        with patch.object(self.sync, 'note_create', side_effect=die_on_second):
            with self.assertRaises(Crash):
                mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual([m['status'] for m in mutation.list_mutations(self.state)], ['applied', 'pending'])
        result = mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual((result['replayed'], result['applied']), (1, 1), result)

    # Bounded, anonymous, Windows-safe local state.

    def test_queue_records_stay_bounded(self):
        notes = self.vault / 'notes'
        notes.mkdir()
        for number in range(20):
            (notes / f'x{number}.sync-conflict-20260101-000000-abc.md').write_text('x', encoding='utf-8')
        self.sync.task_create('tasks/bounded.md', 'Private task body', {'id': 'bounded', 'owner': 'test', 'status': 'inbox'})
        mutation.enqueue_mutation(self.state, 'note_create', 'notes/bounded.md', {'text': 'bounded'})
        mutation.enqueue_mutation(self.state, 'task_update', 'tasks/bounded.md',
            {'id': 'bounded', 'expected_revision': 1, 'changes': {'status': 'active'}})
        result = mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual(result['applied'], 2, result)
        for item in mutation.list_mutations(self.state):
            self.assertLessEqual(set(item['result']), set(mutation._RESULT_FIELDS))
        queue = (self.state / mutation.MUTATION_QUEUE_FILE).read_text(encoding='utf-8')
        self.assertNotIn('sync-conflict', queue)
        self.assertNotIn('Private task body', queue)

    def test_identity_is_anonymous_and_stays_out_of_vault_and_doctor(self):
        import getpass
        import socket
        mutation.enqueue_mutation(self.state, 'note_create', 'notes/anon.md', {'text': 'anon'})
        mutation.replay_mutations(self.vault, self.state, self.sync)
        device = json.loads((self.state / mutation.DEVICE_FILE).read_text(encoding='utf-8'))
        self.assertEqual(set(device), {'schema', 'device'})
        machine_id = device['device']
        self.assertEqual(mutation.list_mutations(self.state)[0]['machine_id'], machine_id)
        local = (self.state / mutation.DEVICE_FILE).read_text(encoding='utf-8') + \
            (self.state / mutation.MUTATION_QUEUE_FILE).read_text(encoding='utf-8')
        try:
            names = [getpass.getuser(), socket.gethostname()]
        except Exception:
            names = [socket.gethostname()]
        for name in names:
            if len(name) >= 4:
                self.assertNotIn(name, local)
        self.assertNotIn(str(Path.home()), local)
        self.assertNotIn(str(self.vault), local)
        for path in self.vault.rglob('*'):
            if path.is_file():
                self.assertNotIn(machine_id.encode(), path.read_bytes(), path)
        self.assertNotIn(machine_id, json.dumps(mutation.doctor(self.state)))

    @unittest.skipIf(os.name == 'nt', 'POSIX permission bits')
    def test_queue_file_is_owner_only_from_the_first_append(self):
        import stat
        mutation.enqueue_mutation(self.state, 'note_create', 'notes/private-text.md', {'text': 'private text'})
        for name in (mutation.MUTATION_QUEUE_FILE, mutation.DEVICE_FILE, mutation.SETTINGS_FILE):
            self.assertEqual(stat.S_IMODE((self.state / name).stat().st_mode), 0o600, name)

    def test_identity_is_stable_after_presence_appears(self):
        first = mutation.get_machine_id(self.state)
        (self.state / mutation.PRESENCE_DEVICE_FILE).write_text(
            json.dumps({'schema': 1, 'device': 'f' * 32}), encoding='utf-8')
        self.assertEqual(mutation.get_machine_id(self.state), first)

    def test_queue_rewrite_retries_windows_sharing_violations(self):
        from unittest.mock import patch
        import beyin_v3_sync
        mutation.enqueue_mutation(self.state, 'note_create', 'notes/win.md', {'text': 'win'})
        with patch.object(beyin_v3_sync, '_safe_replace', wraps=beyin_v3_sync._safe_replace) as safe:
            mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertIn(self.state / mutation.MUTATION_QUEUE_FILE, [Path(call.args[1]) for call in safe.call_args_list])
        self.assertFalse(list(self.state.glob('.mqueue-*')))

    def test_human_output_names_the_switch_and_counts(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        try:
            import beyin_entry
        finally:
            sys.path.remove(str(ROOT / 'scripts'))
        off = beyin_entry.human_result(mutation.status(self.tmp / 'absent-state'), 'mutation-queue')
        self.assertIn('kapali (acmak icin: beyin.py mutation-queue --enable)', off)
        item = mutation.enqueue_mutation(self.state, 'task_update', 'tasks/missing.md',
            {'id': 'missing', 'expected_revision': 1, 'changes': {'status': 'active'}})
        mutation.replay_mutations(self.vault, self.state, self.sync)
        dismissed = beyin_entry.human_result(mutation.resolve(self.state, item['mutation_id']), 'mutation-queue')
        self.assertEqual(dismissed, 'Kuyruk kaydi ' + item['mutation_id'] + ': dismissed.')
        lines = beyin_entry.human_result({'status': 'observed_metadata', 'mutation_queue': mutation.doctor(self.state)},
                                         'doctor').splitlines()
        self.assertIn('Cevrimdisi mutasyon kuyrugu: acik (0 bekleyen, 0 cakisma, 0 basarisiz)', lines)
        quiet = beyin_entry.human_result({'status': 'observed_metadata', 'mutation_queue': {'enabled': False}}, 'doctor')
        self.assertNotIn('kuyrug', quiet)

    # Input checks and review exits.

    def test_enqueue_rejects_malformed_payloads_before_queueing(self):
        for action, source, payload in [
                ('note_create', 'notes/a.md', {'text': ''}),
                ('note_create', 'notes/a.md', {'text': 'a', 'metadata': []}),
                ('note_create', 'C:/notes/a.md', {'text': 'a'}),
                ('note_create', 'notes/a:stream.md', {'text': 'a'}),
                ('task_create', 'tasks/a.md', {'text': 'a'}),
                ('task_update', 'tasks/a.md', {'id': 'a', 'expected_revision': True, 'changes': {'status': 'active'}}),
                ('task_update', 'tasks/a.md', {'id': 'a', 'expected_revision': 1, 'changes': {}}),
                ('receipt', 'receipts/a.md', {'event_id': 'a', 'summary': 'a', 'refs': []}),
                ('delete', 'notes/a.md', {})]:
            with self.assertRaises(ValueError, msg=(action, payload)):
                mutation.enqueue_mutation(self.state, action, source, payload)
        self.assertFalse((self.state / mutation.MUTATION_QUEUE_FILE).exists())

    def test_client_mutation_id_makes_a_resend_a_no_op(self):
        payload = {'text': 'once'}
        first = mutation.enqueue_mutation(self.state, 'note_create', 'notes/once.md', payload, mutation_id='client-0001')
        again = mutation.enqueue_mutation(self.state, 'note_create', 'notes/once.md', payload, mutation_id='client-0001')
        self.assertEqual(first['mutation_id'], 'client-0001')
        self.assertTrue(again['duplicate'])
        self.assertEqual(len(mutation.list_mutations(self.state)), 1)
        with self.assertRaisesRegex(ValueError, 'different mutation'):
            mutation.enqueue_mutation(self.state, 'note_create', 'notes/other.md', payload, mutation_id='client-0001')
        with self.assertRaisesRegex(ValueError, 'mutation_id'):
            mutation.enqueue_mutation(self.state, 'note_create', 'notes/once.md', payload, mutation_id='../x')

    def test_concurrent_first_enqueues_cannot_mix_vaults(self):
        from concurrent.futures import ThreadPoolExecutor
        from contextlib import contextmanager
        from threading import Barrier
        from unittest.mock import patch
        from beyin_v3 import MemoryStore
        state = self.tmp / 'racing-state'
        mutation.set_enabled(state, True)
        other = self.tmp / 'other-vault'
        other.mkdir()
        barrier = Barrier(2)
        original_connect = MemoryStore._connect

        class BindingCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchone(self):
                row = self.cursor.fetchone()
                if row is None:
                    # Both initializers observed an unbound database before either claims it.
                    barrier.wait(timeout=10)
                return row

        class Database:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, *args):
                cursor = self.db.execute(sql, *args)
                return BindingCursor(cursor) if sql == "SELECT value FROM metadata WHERE key='vault_root'" else cursor

            def __getattr__(self, name):
                return getattr(self.db, name)

        @contextmanager
        def connect(store):
            with original_connect(store) as db:
                yield Database(db)

        def enqueue(vault):
            try:
                mutation.enqueue_mutation(state, 'note_create', 'notes/' + vault.name + '.md',
                                          {'text': vault.name}, vault=vault)
                return vault, None
            except ValueError as exc:
                return vault, str(exc)

        with patch.object(MemoryStore, '_connect', connect), ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(enqueue, (self.vault, other)))
        accepted = [vault for vault, error in outcomes if error is None]
        rejected = [(vault, error) for vault, error in outcomes if error is not None]
        self.assertEqual(len(accepted), 1, outcomes)
        self.assertEqual(len(rejected), 1, outcomes)
        self.assertIn('runtime belongs to another vault', rejected[0][1])
        self.assertEqual(len(mutation.list_mutations(state)), 1)
        result = mutation.replay_mutations(accepted[0], state)
        self.assertEqual((result['applied'], result['failed']), (1, 0), result)
        self.assertTrue((accepted[0] / ('notes/' + accepted[0].name + '.md')).is_file())
        self.assertFalse((accepted[0] / ('notes/' + rejected[0][0].name + '.md')).exists())
        self.assertFalse((rejected[0][0] / 'notes').exists())

    def test_enqueue_and_replay_keep_the_runtime_vault_binding(self):
        # An enqueue for another vault is refused like an ordinary write, so a replay from this
        # vault cannot write that vault's content here (review on #409).
        other = self.tmp / 'other-vault'
        other.mkdir()
        with self.assertRaisesRegex(ValueError, 'runtime belongs to another vault'):
            mutation.enqueue_mutation(self.state, 'note_create', 'notes/other.md', {'text': 'other'}, vault=other)
        payload_file = self.tmp / 'other.json'
        payload_file.write_text(json.dumps({'action': 'note_create', 'source': 'notes/other.md',
                                            'payload': {'text': 'other'}}), encoding='utf-8')
        proc = subprocess.run([sys.executable, str(CLI), '--vault', str(other), '--state', str(self.state),
                               'mutation-queue', '--enqueue', '--file', str(payload_file)],
                              capture_output=True, text=True, env=inherited_env(BEYIN_UPDATES_OFF='1'))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('runtime belongs to another vault', json.loads(proc.stderr)['message'])
        self.assertEqual(mutation.list_mutations(self.state), [])
        # An unbound runtime is bound by its first enqueue, as by an ordinary write.
        fresh = self.tmp / 'fresh-state'
        mutation.set_enabled(fresh, True)
        mutation.enqueue_mutation(fresh, 'note_create', 'notes/other.md', {'text': 'other'}, vault=other)
        with self.assertRaisesRegex(ValueError, 'runtime belongs to another vault'):
            SyncEngine(self.vault, fresh)
        # A record keeps a digest of its binding, never the path. One bound to another vault
        # (a runtime reset after it was queued) fails without writing; an older record without
        # the field replays against this runtime's vault as before.
        bound = mutation.enqueue_mutation(self.state, 'note_create', 'notes/bound.md', {'text': 'bound'},
                                          vault=self.vault)
        queue = (self.state / mutation.MUTATION_QUEUE_FILE).read_text(encoding='utf-8')
        for path in (self.vault, self.vault.resolve()):
            self.assertNotIn(str(path), queue)
        foreign = dict(bound, mutation_id='foreign-01', source='notes/foreign.md',
                       vault_binding=mutation._vault_binding(other.resolve()))
        legacy = {key: value for key, value in bound.items() if key != 'vault_binding'}
        legacy.update(mutation_id='legacy-01', source='notes/legacy.md')
        mutation._save_all_mutations(self.state, [bound, foreign, legacy])
        result = mutation.replay_mutations(self.vault, self.state, self.sync)
        self.assertEqual((result['applied'], result['conflicts'], result['failed']), (2, 0, 1), result)
        items = {item['mutation_id']: item for item in mutation.list_mutations(self.state)}
        self.assertEqual((items['foreign-01']['status'], items['foreign-01']['error']),
                         ('failed', 'ValueError: queued mutation belongs to another vault'))
        self.assertTrue((self.vault / 'notes/bound.md').is_file())
        self.assertTrue((self.vault / 'notes/legacy.md').is_file())
        self.assertFalse((self.vault / 'notes/foreign.md').exists())
        self.assertFalse((other / 'notes').exists())

    def test_reviewed_conflicts_and_failures_can_be_retried_or_dismissed(self):
        item = mutation.enqueue_mutation(self.state, 'task_update', 'tasks/missing.md',
            {'id': 'missing', 'expected_revision': 1, 'changes': {'status': 'active'}})
        self.assertEqual(mutation.replay_mutations(self.vault, self.state, self.sync)['failed'], 1)
        self.assertEqual(mutation.doctor(self.state)['status'], 'needs_attention')
        self.assertEqual(mutation.resolve(self.state, item['mutation_id'], retry=True)['status'], 'pending')
        self.assertEqual(mutation.replay_mutations(self.vault, self.state, self.sync)['failed'], 1)
        self.assertEqual(mutation.resolve(self.state, item['mutation_id'])['status'], 'dismissed')
        with self.assertRaisesRegex(ValueError, 'only conflict or failed'):
            mutation.resolve(self.state, item['mutation_id'], retry=True)
        with self.assertRaisesRegex(ValueError, 'unknown mutation id'):
            mutation.resolve(self.state, 'not-a-real-id')
        self.assertEqual(mutation.doctor(self.state)['status'], 'ok')
        self.assertEqual(mutation.clear_applied(self.state), {'status': 'succeeded', 'pruned': 1, 'remaining': 0})


if __name__ == "__main__":
    unittest.main()
