"""Synthetic, deterministic cross-process races at compaction's late write windows."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_compact as compact
import beyin_v3_companion as companion
import beyin_v3_notes as notes
from beyin_v3_sync import SyncEngine

NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
LIVE = '850-Companion/Last-Session.md'
ARCHIVE = '850-Companion/Arşiv/Last-Session-2026-10.md'
LEDGER = '850-Companion/Kararlar.md'


def paused_compact(vault, state, window, ready, resume, output):
    original_write, original_plan = compact._write, compact.plan

    def pause():
        ready.set()
        if not resume.wait(40):
            raise TimeoutError('test did not release compaction')

    def write(path, text):
        relative = Path(path).relative_to(vault).as_posix()
        if relative == window:
            pause()
        original_write(path, text)

    def plan(*args):
        if window == 'plan':
            pause()
        return original_plan(*args)

    compact._write, compact.plan = write, plan
    output.put(compact.compact(vault, state, now=NOW))


def edit_contender(vault, state, request, output):
    # Bound lock contention, not scheduling: parent keeps compact paused until
    # this child has either committed or returned a visible conflict.
    original = notes.locked

    @contextmanager
    def bounded(engine, timeout=15):
        with original(engine, timeout=0.25) as folder:
            yield folder

    notes.locked = bounded
    engine = SyncEngine(vault, state)
    try:
        output.put(engine.note_edit(request))
    except notes.NoteConflict as exc:
        output.put({'status': 'conflict', 'reason': str(exc)})
    finally:
        engine.store.close()


def hold_notes(vault, ready, resume):
    with notes.vault_locked(vault):
        ready.set()
        resume.wait(40)


class NoteCompactLockTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()
        self.state.mkdir()
        self.write(LIVE, '# Handoff\r\n\r\n## 2026-10-01 09:00\r\nOLD_CARD\r\n'
                   '**Decision:** Synthetic choice.\r\n' + 'old data ' * 130 + '\r\n\r\n'
                   '## 2026-10-25 09:00\r\nCURRENT_CARD\r\n')
        self.write(ARCHIVE, '\ufeff# Archive\r\nARCHIVE_ANCHOR\r\n')
        self.write(LEDGER, '---\r\n{"kind":"note","remote_allowed":false}\r\n---\r\n'
                   '# Decisions\r\nLEDGER_ANCHOR\r\n')
        (self.state / 'companion-limits.json').write_text(
            json.dumps({'schema': 1, 'Last-Session.md': 1000}), encoding='utf-8')

    def write(self, source, text):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))

    def stop(self, child):
        if child.is_alive():
            child.terminate()
        child.join(10)

    def race(self, window, source, anchor):
        ctx = mp.get_context('spawn')
        ready, resume = ctx.Event(), ctx.Event()
        compact_out, edit_out = ctx.Queue(), ctx.Queue()
        raw = (self.vault / source).read_bytes()
        request = {'source': source, 'expected_sha256': hashlib.sha256(raw).hexdigest(),
                   'edits': [{'old': anchor, 'new': anchor + '\r\nFRESH_UPDATE'}]}
        slow = ctx.Process(target=paused_compact,
                           args=(self.vault, self.state, window, ready, resume, compact_out))
        slow.start()
        self.addCleanup(self.stop, slow)
        try:
            self.assertTrue(ready.wait(20), 'compaction never reached the write window')
            # A separate state proves that only the vault lock coordinates them.
            fast = ctx.Process(target=edit_contender,
                               args=(self.vault, self.root / 'other-state', request, edit_out))
            fast.start()
            self.addCleanup(self.stop, fast)
            fast.join(20)
            self.assertFalse(fast.is_alive(), 'writer hung')
            self.assertEqual(fast.exitcode, 0)
            edited = edit_out.get(timeout=2)
        finally:
            resume.set()
        slow.join(20)
        self.assertFalse(slow.is_alive(), 'compaction hung')
        self.assertEqual(slow.exitcode, 0)
        compacted = compact_out.get(timeout=2)
        self.assertEqual(compacted['status'], 'compacted')
        final = (self.vault / source).read_bytes()
        self.assertFalse(edited['status'] == 'written' and b'FRESH_UPDATE' not in final,
                         'both writers succeeded but compaction lost the update')
        self.assertEqual(edited['status'], 'conflict')
        # A rebuilt targeted edit after compaction succeeds, preserving both changes.
        engine = SyncEngine(self.vault, self.state)
        try:
            request['expected_sha256'] = engine.note_read(source)['sha256']
            self.assertEqual(engine.note_edit(request)['status'], 'written')
        finally:
            engine.store.close()
        self.assertIn(b'FRESH_UPDATE', (self.vault / source).read_bytes())
        self.assertIn(b'OLD_CARD', (self.vault / ARCHIVE).read_bytes())
        self.assertNotIn(b'OLD_CARD', (self.vault / LIVE).read_bytes())
        self.assertIn(b'Synthetic choice.', (self.vault / LEDGER).read_bytes())

    def test_plan_read_is_inside_the_note_lock(self):
        self.race('plan', LIVE, 'CURRENT_CARD')

    def test_late_live_write_cannot_erase_a_successful_note_edit(self):
        self.race(LIVE, LIVE, 'CURRENT_CARD')

    def test_archive_append_shares_the_note_lock(self):
        self.race(ARCHIVE, ARCHIVE, 'ARCHIVE_ANCHOR')

    def test_decision_ledger_append_shares_the_note_lock(self):
        self.race(LEDGER, LEDGER, 'LEDGER_ANCHOR')

    def test_note_lock_blocks_compact_and_bootstrap_and_survives_process_death(self):
        ctx = mp.get_context('spawn')
        ready, resume = ctx.Event(), ctx.Event()
        child = ctx.Process(target=hold_notes, args=(self.vault, ready, resume))
        child.start()
        self.addCleanup(self.stop, child)
        try:
            self.assertTrue(ready.wait(20))
            before = (self.vault / LIVE).read_bytes()
            lock = self.vault / notes.LOCAL_DIRECTORY / 'writer.lock'
            self.assertTrue(lock.exists())
            original = notes.vault_locked
            with patch.object(compact, 'vault_locked', lambda vault: original(vault, timeout=0.1)):
                self.assertEqual(compact.compact(self.vault, self.state)['status'], 'conflict')
            with patch.object(notes, 'vault_locked', lambda vault: original(vault, timeout=0.1)):
                with self.assertRaises(notes.NoteConflict):
                    companion.initialize(self.vault, self.state)
            # DB -> note lock is nonblocking: pending note recovery is deferred.
            engine = SyncEngine(self.vault, self.state)
            try:
                intent = engine.journal / 'synthetic.json'
                intent.write_text(json.dumps({'kind': 'note', 'source': 'notes/pending.md',
                                             'old_hash': None, 'content': 'Recovered note\n'}), encoding='utf-8')
                engine.sync()
                self.assertTrue(intent.exists())
                self.assertFalse((self.vault / 'notes/pending.md').exists())
            finally:
                engine.store.close()
            self.assertEqual((self.vault / LIVE).read_bytes(), before)
        finally:
            child.terminate()
            child.join(10)
        self.assertTrue(lock.exists())  # release never unlinks the persistent file
        engine = SyncEngine(self.vault, self.state)
        try:
            engine.sync()
            self.assertFalse(intent.exists())
            self.assertEqual((self.vault / 'notes/pending.md').read_bytes(), b'Recovered note\n')
        finally:
            engine.store.close()
        self.assertEqual(compact.compact(self.vault, self.state, now=NOW)['status'], 'compacted')

    def test_bootstrap_cannot_overwrite_a_note_edit_created_starter(self):
        engine = SyncEngine(self.vault, self.state)
        try:
            source = '850-Companion/Core.md'
            engine.note_edit({'source': source, 'expected_sha256': None, 'content': 'Custom identity\r\n'})
            companion.initialize(self.vault, self.state)
            self.assertEqual((self.vault / source).read_bytes(), b'Custom identity\r\n')
        finally:
            engine.store.close()

    def test_common_lock_io_failure_is_visible_and_preserves_notes(self):
        before = (self.vault / LIVE).read_bytes()
        with patch.object(compact, 'vault_locked', side_effect=OSError('synthetic lock failure')):
            result = compact.compact(self.vault, self.state, now=NOW)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertIn('cannot acquire note lock', result['reason'])
        self.assertEqual((self.vault / LIVE).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
