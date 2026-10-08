"""Synthetic migration/continuity acceptance tests, fixed before implementation."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'vault'
        self.root.mkdir()
        self.state = Path(self.temp.name) / 'state'
        self.m = importlib.import_module('beyin_v3_migrate')

    def source(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode('utf-8'))
        return p

    def test_sources_and_v2_state_preserved_idempotent_cutover(self):
        paths = [self.source('daily/2025-01-01.md', 'Human day\r\n'),
                 self.source('knowledge/idea.md', 'Existing knowledge ölçüm\n'),
                 self.source('Companion/Core.md', 'Existing persona\n'),
                 self.source('.claude/scripts/.state/compile-state.json', '{"last":"2025-01-01"}')]
        before = {p: p.read_bytes() for p in paths}
        first = self.m.migrate_v2(self.root, self.state)
        second = self.m.migrate_v2(self.root, self.state)
        self.assertEqual(first['cutover_at'], second['cutover_at'])
        self.assertTrue(all(p.read_bytes() == value for p, value in before.items()))
        self.assertTrue((self.state / 'v2-migration.json').exists())

    def test_inflight_writer_blocks_without_success_receipt(self):
        self.source('.claude/scripts/.state/flush-example.json', '{"status":"inflight"}')
        with self.assertRaisesRegex(RuntimeError, r'flush-example\.json.*reconcile'):
            self.m.migrate_v2(self.root, self.state)
        self.assertFalse((self.state / 'v2-migration.json').exists())

    def test_legacy_lock_is_guarded_but_excluded_from_inventory(self):
        lock = self.source('.claude/scripts/.state/compile.lock', '')
        state_file = self.source('.claude/scripts/.state/compile-state.json', '{"status":"ok"}')
        with self.m.migration_guard(self.root, self.state) as plan:
            self.assertNotIn(lock.relative_to(self.root).as_posix(), plan['legacy_state'])
            self.assertIn(state_file.relative_to(self.root).as_posix(), plan['legacy_state'])

    def test_legacy_state_symlink_escape_rejected_without_copy(self):
        external = Path(self.temp.name)/'external'
        external.mkdir()
        (external/'flush-private.json').write_text('{}')
        parent = self.root/'.claude/scripts'
        parent.mkdir(parents=True)
        try:
            (parent/'.state').symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest('symlink unavailable')
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            self.m.migrate_v2(self.root, self.state)
        self.assertFalse((self.state/'v2-preserved-state').exists())

    def test_existing_active_legacy_lock_rejected(self):
        lock = self.source('.claude/scripts/.state/compile.lock', '0')
        with self.m._legacy_lock(lock):
            with self.assertRaisesRegex(RuntimeError, 'legacy writer lock'):
                self.m.migrate_v2(self.root, self.state)

    def test_uncut_legacy_runner_prevents_success_watermark(self):
        self.source('.claude/scripts/flush.py', 'print("old writer")\n')
        with self.assertRaisesRegex(RuntimeError, 'runner'):
            self.m.migrate_v2(self.root, self.state)
        self.assertFalse((self.state/'v2-migration.json').exists())

    def test_concurrent_content_edit_blocks_finalize(self):
        p = self.source('Companion/Core.md', 'old')
        with self.m.migration_guard(self.root, self.state) as plan:
            p.write_text('new', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                self.m.finalize_migration(self.root, self.state, plan)
        self.assertEqual(p.read_text(), 'new')

    def test_finalize_with_an_earlier_success_receipt_ignores_later_source_edits(self):
        # An earlier cutover already succeeded, so finalizing again writes nothing and
        # must not fail because a note changed since the plan was taken (interrupted
        # update, edit, recover).
        p = self.source('Companion/Core.md', 'old')
        first = self.m.migrate_v2(self.root, self.state)
        with self.m.migration_guard(self.root, self.state) as plan:
            self.assertEqual(plan['previous'], first)
            p.write_text('new', encoding='utf-8')
            self.assertEqual(self.m.finalize_migration(self.root, self.state, plan), first)
        self.assertEqual(p.read_text(), 'new')
        self.assertEqual(json.loads((self.state / 'v2-migration.json').read_text(encoding='utf-8')), first)

    def test_receipts_project_new_views_without_old_day_recapture(self):
        from beyin_v3_sync import SyncEngine
        day = self.source('daily/2025-01-01.md', 'Human daily stays exact\n')
        old_receipt = self.source('receipts/old.md', 'Historical receipt must not be recaptured')
        self.m.migrate_v2(self.root, self.state)
        source = self.source('notes/example.md', 'Synthetic completed artifact')
        engine = SyncEngine(self.root, self.state)
        engine.receipt('new-event', 'Verified synthetic outcome.', ['notes/example.md'], 'codex')
        engine.sync()
        daily = list((self.root / 'daily/v3').glob('*.md'))
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0].read_text().count('Verified synthetic outcome.'), 1)
        self.assertNotIn('Historical receipt', daily[0].read_text())
        self.assertEqual(day.read_bytes(), b'Human daily stays exact\n')
        self.assertEqual(old_receipt.read_text(), 'Historical receipt must not be recaptured')
        self.assertIn('Verified synthetic outcome.', (self.root / 'knowledge/v3/outcomes.md').read_text())

    def test_manual_generated_view_edit_preserved_and_reported(self):
        from beyin_v3_sync import SyncEngine
        self.source('notes/example.md', 'Synthetic source')
        engine = SyncEngine(self.root, self.state)
        engine.receipt('one', 'One outcome.', ['notes/example.md'], 'codex')
        target = self.root / 'knowledge/v3/outcomes.md'
        target.write_text('Manual edit', encoding='utf-8')
        result = engine.sync()
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(target.read_text(), 'Manual edit')

    def test_companion_private_metadata_excluded(self):
        from beyin_v3_sync import SyncEngine
        self.source('Companion/Core.md', '---\n{"visibility":"private"}\n---\nPRIVATE_PERSONA_CANARY')
        engine = SyncEngine(self.root, self.state)
        engine.sync()
        self.assertTrue(engine.store.retrieve('PRIVATE_PERSONA_CANARY')['abstained'])

    def test_semantic_note_create_preserves_existing_source(self):
        from beyin_v3_sync import SyncEngine
        engine = SyncEngine(self.root, self.state)
        engine.note_create('knowledge/lesson.md', 'Learned synthetic lesson.', {'project': 'demo'})
        original = (self.root/'knowledge/lesson.md').read_bytes()
        with self.assertRaises(ValueError):
            engine.note_create('knowledge/lesson.md', 'Overwrite', {})
        self.assertEqual((self.root/'knowledge/lesson.md').read_bytes(), original)

    def test_receipt_gap_is_only_a_signal_and_closes_on_matching_receipt(self):
        from beyin_v3_sync import SyncEngine
        from beyin_v3_projections import record_checkpoints
        self.source('notes/example.md', 'Synthetic source')
        engine = SyncEngine(self.root, self.state)
        record_checkpoints(engine, [{'event': 'Stop', 'harness': 'codex', 'session': 'synthetic', 'at': 1}])
        engine.sync()
        gaps = json.loads((self.state/'receipt-gaps.json').read_text())
        self.assertEqual(gaps['potential_missing_receipts'], 1)
        self.assertFalse((self.root/'daily/v3').exists())
        engine.receipt('session-outcome', 'Explicit synthetic outcome.', ['notes/example.md'], 'codex', session='synthetic')
        engine.sync()
        self.assertEqual(json.loads((self.state/'receipt-gaps.json').read_text())['potential_missing_receipts'], 0)

    def test_prior_turn_receipt_does_not_cover_next_turn(self):
        from beyin_v3_sync import SyncEngine
        from beyin_v3_projections import record_checkpoints
        import time
        self.source('notes/example.md', 'Synthetic source')
        engine = SyncEngine(self.root, self.state)
        record_checkpoints(engine, [{'event': 'UserPromptSubmit', 'harness': 'codex', 'session': 'same', 'at': time.time()-10}])
        engine.receipt('turn-one', 'First outcome.', ['notes/example.md'], 'codex', session='same')
        record_checkpoints(engine, [{'event': 'UserPromptSubmit', 'harness': 'codex', 'session': 'same', 'at': time.time()+1}, {'event': 'Stop', 'harness': 'codex', 'session': 'same', 'at': time.time()+2}])
        engine.sync()
        self.assertEqual(json.loads((self.state/'receipt-gaps.json').read_text())['potential_missing_receipts'], 1)

    def install_manifest(self, *kept):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'v3-install.json').write_text(
            json.dumps({'files': {}, 'kept_legacy': list(kept)}), encoding='utf-8')

    def test_guard_plan_reports_kept_legacy_runners_from_the_install_manifest(self):
        self.install_manifest('.claude/scripts/flush.py')
        self.source('.claude/scripts/flush.py', 'print("user owned flush")\n')
        with self.m.migration_guard(self.root, self.state) as plan:
            self.assertEqual(plan['kept_legacy'], ['.claude/scripts/flush.py'])

    def test_kept_legacy_runner_finalizes_unretired_and_unchanged(self):
        self.install_manifest('.claude/scripts/flush.py')
        runner = self.source('.claude/scripts/flush.py', 'print("user owned flush")\n')
        before = runner.read_bytes()
        result = self.m.migrate_v2(self.root, self.state)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(runner.read_bytes(), before)
        self.assertNotIn(b'BEYIN_V3_LEGACY_RETIRED', runner.read_bytes())
        self.assertTrue((self.state / 'v2-migration.json').exists())

    def test_kept_legacy_runner_symlink_is_still_refused(self):
        self.install_manifest('.claude/scripts/flush.py')
        external = Path(self.temp.name) / 'external-flush.py'
        external.write_text('print("external writer")\n')
        parent = self.root / '.claude/scripts'
        parent.mkdir(parents=True)
        try:
            (parent / 'flush.py').symlink_to(external)
        except OSError:
            self.skipTest('symlink unavailable')
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            self.m.migrate_v2(self.root, self.state)
        self.assertFalse((self.state / 'v2-migration.json').exists())

    def test_legacy_tags_converted_to_list(self):
        from beyin_v3_sync import parse
        content = "---\ntags: a, b, c\n---\nbody"
        meta, _ = parse(content)
        self.assertEqual(meta.get('tags'), ['a', 'b', 'c'])

    def test_legacy_tags_yaml_list_converted_to_list(self):
        from beyin_v3_sync import parse
        content = "---\ntags: [a, b]\n---\nbody"
        meta, _ = parse(content)
        self.assertEqual(meta.get('tags'), ['a', 'b'])

    def test_date_conversion(self):
        from beyin_v3 import MemoryStore
        store = MemoryStore(self.state, self.root, read_only=False)
        self.source("t.md", "body")
        r1 = store._validate({"id": "1", "text": "t", "source": "t.md", "updated": "2026/09/20"})
        self.assertEqual(r1.get("updated_at"), "2026-09-20")

        r2 = store._validate({"id": "2", "text": "t", "source": "t.md", "updated": "20-09-2026"})
        self.assertEqual(r2.get("updated_at"), "2026-09-20")
        r5 = store._validate({"id": "5", "text": "t", "source": "t.md", "updated": "20.09.2026"})
        self.assertEqual(r5.get("updated_at"), "2026-09-20")
        r6 = store._validate({"id": "6", "text": "t", "source": "t.md", "updated": "20.09-2026"})
        self.assertNotIn("updated_at", r6)
        # Month-first text is not a day-first date; it stays unset instead of 2026-31-12.
        r3 = store._validate({"id": "3", "text": "t", "source": "t.md", "updated": "12/31/2026"})
        self.assertNotIn("updated_at", r3)
        r4 = store._validate({"id": "4", "text": "t", "source": "t.md", "updated": "2026/02/30", "modified": "2026-02-01"})
        self.assertEqual(r4.get("updated_at"), "2026-02-01")

    def test_comma_lists_only_for_tags_and_aliases(self):
        from beyin_v3_sync import parse
        meta, _ = parse("---\ntitle: Merhaba, dunya\nproject: Acme, Inc\naliases: Alfa, Beta\ntags: \"a, b\"\n---\nbody")
        self.assertEqual(meta['title'], 'Merhaba, dunya')
        self.assertEqual(meta['project'], 'Acme, Inc')
        self.assertEqual(meta['aliases'], ['Alfa', 'Beta'])
        self.assertEqual(meta['tags'], 'a, b')

    def test_migration_rollback_on_failure(self):
        self.source('.claude/scripts/.state/flush-state.json', '{"status":"ok"}')
        self.source('.claude/scripts/.state/compile-state.json', '{"status":"ok"}')

        import shutil
        original_copyfile = shutil.copyfile

        def failing_copyfile(src, dst):
            original_copyfile(src, dst)
            if 'compile-state.json' in str(src):
                raise RuntimeError("Simulated disk full")

        shutil.copyfile = failing_copyfile
        try:
            with self.assertRaisesRegex(RuntimeError, "Simulated disk full"):
                self.m.migrate_v2(self.root, self.state)

            preserved = self.state / 'v2-preserved-state' / '.claude/scripts/.state'
            self.assertFalse((preserved / 'flush-state.json').exists())
            self.assertFalse((preserved / 'compile-state.json').exists())
            self.assertFalse((self.state / 'v2-migration.json').exists())
        finally:
            shutil.copyfile = original_copyfile


if __name__ == '__main__':
    unittest.main()
