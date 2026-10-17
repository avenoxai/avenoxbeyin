#!/usr/bin/env python3
"""Session-scoped context deduplication contracts (#363)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from v3_package_helpers import isolated_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
HOOK = SCRIPTS / 'beyin_v3_hook.py'
spec = importlib.util.spec_from_file_location('v3_dedupe_evaluator', ROOT / 'scripts/evaluate_v3.py')
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)

NOTE_A = 'Quartz observatory calibration: the GoPlus quota is wasted because every candidate is rescanned.'
NOTE_B = 'PostgreSQL performance indexing: btree indexes are ideal for equality and range queries.'


class ContextDedupeHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-dedupe-hook-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.state = root / 'state'
        home = root / 'home'
        home.mkdir()
        self.env = {'HOME': str(home), 'USERPROFILE': str(home), 'APPDATA': str(home / 'appdata'),
                    'LOCALAPPDATA': str(home / 'localappdata'), 'TEMP': str(root), 'TMP': str(root),
                    'PATH': isolated_path(), 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1',
                    'BEYIN_V3_NO_SPAWN': '1'}
        for key in ('SYSTEMROOT', 'WINDIR'):
            if key in os.environ:
                self.env[key] = os.environ[key]
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        from beyin_v3_sync import SyncEngine
        self.sync_engine = SyncEngine
        self.note_a_path = self.vault / 'knowledge/goplus-quota.md'
        self.note_a_path.parent.mkdir(parents=True)
        self.note_a_path.write_text(NOTE_A, encoding='utf-8')
        self.note_b_path = self.vault / 'knowledge/postgres-index.md'
        self.note_b_path.write_text(NOTE_B, encoding='utf-8')
        self.sync_engine(self.vault, self.state).sync()

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
                                 '--vault', str(self.vault), '--state', str(self.state), *args],
                                capture_output=True, text=True, encoding='utf-8', env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def invoke(self, event, session_id='test-session', prompt=None, source=None, extra_env=None):
        payload = {'hook_event_name': event, 'session_id': session_id,
                   'event_id': f'evt-{event}', 'cwd': str(self.vault)}
        if prompt is not None:
            payload['prompt'] = prompt
        if source is not None:
            payload['source'] = source
        env = dict(self.env, **(extra_env or {}))
        result = subprocess.run([sys.executable, str(HOOK), '--vault', str(self.vault), '--state', str(self.state),
                                 '--harness', 'claude'], input=json.dumps(payload), capture_output=True,
                                text=True, env=env, check=True)
        return json.loads(result.stdout)

    def test_dedupe_off_by_default_preserves_legacy_repeated_delivery(self):
        # Default behavior: dedupe is off, both turns deliver Note A
        res1 = self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted')
        self.assertIn('knowledge/goplus-quota.md', res1.get('hookSpecificOutput', {}).get('additionalContext', ''))
        res2 = self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted')
        self.assertIn('knowledge/goplus-quota.md', res2.get('hookSpecificOutput', {}).get('additionalContext', ''))

    def test_dedupe_on_suppresses_duplicate_injections_in_same_session(self):
        self.cli('preferences', '--context-dedupe', 'on')
        # Turn 1: delivers Note A
        res1 = self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted')
        self.assertIn('knowledge/goplus-quota.md', res1.get('hookSpecificOutput', {}).get('additionalContext', ''))
        # Turn 2: identical match in same session -> suppressed completely!
        res2 = self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted')
        self.assertEqual(res2, {})

    def test_dedupe_mixed_novel_and_duplicate_delivers_only_novel(self):
        self.cli('preferences', '--context-dedupe', 'on')
        # Turn 1: query matches Note A
        res1 = self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted')
        self.assertIn('knowledge/goplus-quota.md', res1.get('hookSpecificOutput', {}).get('additionalContext', ''))
        self.assertNotIn('knowledge/postgres-index.md', res1.get('hookSpecificOutput', {}).get('additionalContext', ''))

        # Turn 2: query matches both Note A and Note B
        res2 = self.invoke('UserPromptSubmit', prompt='GoPlus quota and postgres performance indexing')
        ctx2 = res2.get('hookSpecificOutput', {}).get('additionalContext', '')
        # Note A was already delivered, so only Note B is novel!
        self.assertNotIn('knowledge/goplus-quota.md', ctx2)
        self.assertIn('knowledge/postgres-index.md', ctx2)

    def test_dedupe_reinjects_when_source_sha256_changes(self):
        self.cli('preferences', '--context-dedupe', 'on')
        # Turn 1: delivers Note A
        res1 = self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted')
        self.assertIn('knowledge/goplus-quota.md', res1.get('hookSpecificOutput', {}).get('additionalContext', ''))

        # User edits Note A on disk and syncs
        self.note_a_path.write_text(NOTE_A + '\nUpdated with fresh benchmarks.', encoding='utf-8')
        self.sync_engine(self.vault, self.state).sync()

        # Turn 2: same prompt now delivers Note A because its source_sha256 changed!
        res2 = self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted')
        ctx2 = res2.get('hookSpecificOutput', {}).get('additionalContext', '')
        self.assertIn('knowledge/goplus-quota.md', ctx2)
        self.assertIn('Updated with fresh benchmarks', ctx2)

    def test_dedupe_resets_on_pre_compact(self):
        self.cli('preferences', '--context-dedupe', 'on')
        # Turn 1: delivers Note A
        res1 = self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted')
        self.assertIn('knowledge/goplus-quota.md', res1.get('hookSpecificOutput', {}).get('additionalContext', ''))

        # Turn 2: suppressed
        res2 = self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted')
        self.assertEqual(res2, {})

        # Compaction occurs
        self.invoke('PreCompact')

        # Turn 3: Note A is delivered again into the compacted context!
        res3 = self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted')
        self.assertIn('knowledge/goplus-quota.md', res3.get('hookSpecificOutput', {}).get('additionalContext', ''))

    def test_dedupe_resets_on_session_start_compact(self):
        self.cli('preferences', '--context-dedupe', 'on')
        # Turn 1: delivers Note A
        res1 = self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted')
        self.assertIn('knowledge/goplus-quota.md', res1.get('hookSpecificOutput', {}).get('additionalContext', ''))

        # Claude Code emits SessionStart with source: compact
        self.invoke('SessionStart', source='compact')

        # Turn 2: Note A is delivered again
        res2 = self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted')
        self.assertIn('knowledge/goplus-quota.md', res2.get('hookSpecificOutput', {}).get('additionalContext', ''))

    def test_dedupe_isolated_per_session(self):
        self.cli('preferences', '--context-dedupe', 'on')
        # Session 1: delivers Note A
        res1 = self.invoke('UserPromptSubmit', session_id='session-1', prompt='why is the GoPlus quota wasted')
        self.assertIn('knowledge/goplus-quota.md', res1.get('hookSpecificOutput', {}).get('additionalContext', ''))

        # Session 2: also delivers Note A (isolated per session)
        res2 = self.invoke('UserPromptSubmit', session_id='session-2', prompt='why is the GoPlus quota wasted')
        self.assertIn('knowledge/goplus-quota.md', res2.get('hookSpecificOutput', {}).get('additionalContext', ''))

    def test_dedupe_session_end_cleans_up(self):
        self.cli('preferences', '--context-dedupe', 'on')
        self.invoke('UserPromptSubmit', session_id='ephemeral-sess', prompt='why is the GoPlus quota wasted')
        import beyin_v3_dedupe as dedupe
        file_path = dedupe._session_file(self.state, 'claude', 'ephemeral-sess')
        self.assertTrue(file_path.is_file())

        self.invoke('SessionEnd', session_id='ephemeral-sess')
        self.assertFalse(file_path.exists())

    def test_dedupe_preferences_and_doctor_roundtrip(self):
        # 1. Default preferences: context_dedupe is off
        prefs_default = self.cli('preferences')
        self.assertEqual(prefs_default.get('context_dedupe'), 'off')
        self.assertFalse((self.vault / '.beyin-preferences.json').exists())

        # 2. Enable context_dedupe
        saved = self.cli('preferences', '--context-dedupe', 'on')
        self.assertEqual(saved['status'], 'saved')
        self.assertEqual(saved['context_dedupe'], 'on')
        self.assertTrue((self.state / 'context-dedupe.json').exists())
        self.assertFalse((self.vault / '.beyin-preferences.json').exists(), 'must not modify vault preference schema')

        # 3. Doctor reports context_dedupe enabled
        doc = self.cli('doctor')
        self.assertEqual(doc.get('context_dedupe', {}).get('enabled'), True)

        # 4. Human readable output
        sys.path.insert(0, str(ROOT / 'scripts'))
        import beyin_entry
        human_prefs = beyin_entry.human_result(saved, 'preferences')
        self.assertIn('Oturum ici tekrar onleme: acik', human_prefs)
        human_doc = beyin_entry.human_result(doc, 'doctor')
        self.assertIn('Oturum ici tekrar onleme: acik', human_doc)

        # 5. Disable context_dedupe
        disabled = self.cli('preferences', '--context-dedupe', 'off')
        self.assertEqual(disabled['context_dedupe'], 'off')

    def test_dedupe_fail_open_on_damaged_ledger(self):
        self.cli('preferences', '--context-dedupe', 'on')
        # Corrupt the session delivery ledger file
        import beyin_v3_dedupe as dedupe
        path = dedupe._session_file(self.state, 'claude', 'damaged-session', create=True)
        path.write_text('{invalid-json-content', encoding='utf-8')

        # Hook must fail open and deliver context normally without crashing
        res = self.invoke('UserPromptSubmit', session_id='damaged-session', prompt='why is the GoPlus quota wasted')
        self.assertIn('knowledge/goplus-quota.md', res.get('hookSpecificOutput', {}).get('additionalContext', ''))

    def test_all_duplicate_turn_keeps_the_continuity_anchor(self):
        # Maintainer change: a turn whose records were all held back still anchors the topic, so a
        # vague follow-up after compaction can inherit it instead of losing the thread.
        self.cli('preferences', '--context-dedupe', 'on')
        from beyin_v3 import MemoryStore
        from beyin_v3_continuity import _path
        anchor = _path(MemoryStore(self.state, self.vault), 'claude', 'test-session')
        self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted')
        self.assertTrue(anchor.is_file())
        self.assertEqual(self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted'), {})
        self.assertTrue(anchor.is_file(), 'an all-duplicate turn must not erase the topic anchor')

    def test_missing_session_id_never_dedupes(self):
        self.cli('preferences', '--context-dedupe', 'on')
        for _ in range(2):
            res = self.invoke('UserPromptSubmit', session_id='unknown', prompt='why is the GoPlus quota wasted')
            self.assertIn('knowledge/goplus-quota.md', res.get('hookSpecificOutput', {}).get('additionalContext', ''))
        self.assertFalse((self.state / 'session-delivery').exists())

    # The latest receipt block appended after the records (#363, reported by grogureis) follows
    # the same ledger: once per session, again when a new receipt is written or after compaction.

    @staticmethod
    def context_of(result):
        return result.get('hookSpecificOutput', {}).get('additionalContext', '')

    def write_receipt(self, event_id, summary):
        handoff = self.vault / 'knowledge/handoff.md'
        if not handoff.exists():
            handoff.write_text('Lighthouse ferry handoff log.', encoding='utf-8')
        saved = self.cli('receipt', '--harness', 'claude', '--event-id', event_id,
                         '--summary', summary, '--ref', 'knowledge/handoff.md')
        self.assertEqual(saved['status'], 'succeeded')

    def assert_turn(self, result, records=(), receipt=None):
        text = self.context_of(result)
        for source in ('knowledge/goplus-quota.md', 'knowledge/postgres-index.md'):
            (self.assertIn if source in records else self.assertNotIn)(source, text)
        if receipt is None:
            self.assertNotIn('Latest receipt (', text)
        else:
            self.assertIn('Latest receipt (', text)
            self.assertIn(receipt, text)
        return text

    def test_receipt_is_delivered_once_while_new_records_keep_coming(self):
        self.cli('preferences', '--context-dedupe', 'on')
        self.write_receipt('receipt-one', 'Receipt marker zebra-one: the ferry handoff is finished.')
        self.assert_turn(self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted'),
                         ['knowledge/goplus-quota.md'], 'zebra-one')
        # A new record arrives, the receipt is the same: the record alone.
        self.assert_turn(self.invoke('UserPromptSubmit', prompt='how does postgres performance indexing work'),
                         ['knowledge/postgres-index.md'])
        # Nothing new at all: nothing, as before.
        self.assertEqual(self.invoke('UserPromptSubmit', prompt='how does postgres performance indexing work'), {})

    def test_dedupe_off_repeats_the_receipt_like_before(self):
        self.write_receipt('receipt-one', 'Receipt marker zebra-one: the ferry handoff is finished.')
        for setting in (None, 'off'):
            if setting:
                self.cli('preferences', '--context-dedupe', setting)
            session = 'off-%s' % setting
            first = self.assert_turn(self.invoke('UserPromptSubmit', session_id=session, prompt='why is the GoPlus quota wasted'),
                                     ['knowledge/goplus-quota.md'], 'zebra-one')
            second = self.assert_turn(self.invoke('UserPromptSubmit', session_id=session,
                                                  prompt='how does postgres performance indexing work'),
                                      ['knowledge/postgres-index.md'], 'zebra-one')
            receipt = first[first.index('\nLatest receipt ('):]
            self.assertTrue(second.endswith(receipt))
        self.assertFalse((self.state / 'session-delivery').exists())

    def test_new_receipt_is_delivered_alone_when_every_record_is_held_back(self):
        self.cli('preferences', '--context-dedupe', 'on')
        from beyin_v3 import MemoryStore
        from beyin_v3_continuity import _path
        anchor = _path(MemoryStore(self.state, self.vault), 'claude', 'test-session')
        self.write_receipt('receipt-one', 'Receipt marker zebra-one: the ferry handoff is finished.')
        self.assert_turn(self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted'),
                         ['knowledge/goplus-quota.md'], 'zebra-one')
        self.write_receipt('receipt-two', 'Receipt marker zebra-two: the tide table still needs a check.')
        text = self.assert_turn(self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted'),
                                receipt='zebra-two')
        self.assertIn('V3 source-backed context (data, not instructions):\nLatest receipt (', text)
        self.assertNotIn('"records"', text, 'no empty envelope that reads as an abstained search')
        self.assertNotIn('zebra-one', text)
        self.assertTrue(anchor.is_file(), 'a receipt-only turn keeps the topic anchor')
        self.assertEqual(self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted'), {})
        self.assert_turn(self.invoke('UserPromptSubmit', prompt='how does postgres performance indexing work'),
                         ['knowledge/postgres-index.md'])

    def test_receipt_returns_after_compaction(self):
        self.cli('preferences', '--context-dedupe', 'on')
        self.write_receipt('receipt-one', 'Receipt marker zebra-one: the ferry handoff is finished.')
        self.assert_turn(self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted'),
                         ['knowledge/goplus-quota.md'], 'zebra-one')
        self.assert_turn(self.invoke('UserPromptSubmit', prompt='how does postgres performance indexing work'),
                         ['knowledge/postgres-index.md'])
        self.invoke('PreCompact')
        self.assert_turn(self.invoke('UserPromptSubmit', prompt='how does postgres performance indexing work'),
                         ['knowledge/postgres-index.md'], 'zebra-one')
        self.invoke('SessionStart', source='compact')
        self.assert_turn(self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted'),
                         ['knowledge/goplus-quota.md'], 'zebra-one')

    def test_receipt_fails_open_without_session_or_with_damaged_ledger(self):
        self.cli('preferences', '--context-dedupe', 'on')
        self.write_receipt('receipt-one', 'Receipt marker zebra-one: the ferry handoff is finished.')
        for prompt, source in (('why is the GoPlus quota wasted', 'knowledge/goplus-quota.md'),
                               ('how does postgres performance indexing work', 'knowledge/postgres-index.md')):
            self.assert_turn(self.invoke('UserPromptSubmit', session_id='unknown', prompt=prompt), [source], 'zebra-one')
        import beyin_v3_dedupe as dedupe
        damaged = dedupe._session_file(self.state, 'claude', 'damaged-session', create=True)
        damaged.write_text('{invalid-json-content', encoding='utf-8')
        self.assert_turn(self.invoke('UserPromptSubmit', session_id='damaged-session', prompt='why is the GoPlus quota wasted'),
                         ['knowledge/goplus-quota.md'], 'zebra-one')
        # The damaged ledger was replaced by a sound one: the next turn holds the receipt back.
        self.assert_turn(self.invoke('UserPromptSubmit', session_id='damaged-session',
                                     prompt='how does postgres performance indexing work'),
                         ['knowledge/postgres-index.md'])

    def test_receipt_that_did_not_fit_is_not_counted_as_delivered(self):
        # render_context drops a receipt that does not fit rather than cutting it; a dropped
        # receipt was never shown, so a later turn with room still carries it.
        self.cli('preferences', '--context-dedupe', 'on')
        self.cli('preferences', '--context-chars', '1000')
        self.write_receipt('receipt-long', 'Receipt marker zebra-long: ' + 'the ferry handoff needs a tide check. ' * 28)
        self.assert_turn(self.invoke('UserPromptSubmit', prompt='why is the GoPlus quota wasted'),
                         ['knowledge/goplus-quota.md'])
        self.cli('preferences', '--context-chars', '5000')
        self.assert_turn(self.invoke('UserPromptSubmit', prompt='how does postgres performance indexing work'),
                         ['knowledge/postgres-index.md'], 'zebra-long')


class DedupeLedgerTest(unittest.TestCase):
    """Maintainer contracts for the ledger itself: excerpts, size cap and pruning."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-dedupe-ledger-')
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        import beyin_v3_dedupe
        self.dedupe = beyin_v3_dedupe
        self.dedupe.save_settings(self.state, True)

    @staticmethod
    def record(rid, text='x' * 40, sha='a' * 64):
        return {'id': rid, 'source_sha256': sha, 'text': text, 'source': 'knowledge/%s.md' % rid}

    def test_another_window_of_a_seen_note_is_still_delivered(self):
        # Turn context is passage based (#83): one id answers different prompts with different windows.
        d = self.dedupe
        first = 'Heading\n\n' + 'alpha section ' * 40
        other = 'Heading\n\n' + 'omega section ' * 40
        d.record_delivered(self.state, 'claude', 's', [self.record('a', first)])
        novel, dup = d.filter_novel(self.state, 'claude', 's', [self.record('a', other)])
        self.assertEqual([r['id'] for r in novel], ['a'])
        clipped = first[:200] + ' [truncated]'
        novel, dup = d.filter_novel(self.state, 'claude', 's', [self.record('a', clipped)])
        self.assertEqual(novel, [], 'the same window clipped shorter is still a repeat')
        d.record_delivered(self.state, 'claude', 's', [self.record('a', other)])
        self.assertEqual(d.filter_novel(self.state, 'claude', 's', [self.record('a', first)])[0], [])
        self.assertEqual(d.filter_novel(self.state, 'claude', 's', [self.record('a', other)])[0], [])

    def test_held_back_records_return_their_budget_to_the_new_ones(self):
        from beyin_v3 import pack_context, render_context
        d = self.dedupe
        old, new = self.record('a', 'old note ' * 300), self.record('b', 'new note ' * 300)
        d.record_delivered(self.state, 'claude', 's', [old])
        context = pack_context([old, new], 5, 2400)
        clipped = next(r for r in context['records'] if r['id'] == 'b')
        kept, dup = d.drop_delivered(self.state, 'claude', 's', context)
        self.assertEqual([r['id'] for r in dup], ['a'])
        _text, delivered = render_context(kept, 2400)
        self.assertEqual([r['id'] for r in delivered['records']], ['b'])
        self.assertGreater(len(delivered['records'][0]['text']), len(clipped['text']),
                           'the new record is repacked from its original, not from the earlier clip')

    def test_ledger_is_capped_below_the_read_limit(self):
        d = self.dedupe
        many = [self.record('md-%04d-%s' % (i, 'q' * 24)) for i in range(d.MAX_RECORDS + 200)]
        d.record_delivered(self.state, 'claude', 's', many)
        path = d._session_file(self.state, 'claude', 's')
        self.assertLess(path.stat().st_size, d.MAX_FILE_BYTES)
        stored = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(len(stored['records']), d.MAX_RECORDS)
        self.assertNotIn('session', stored, 'the raw session id is not stored')
        self.assertEqual(d.filter_novel(self.state, 'claude', 's', [many[-1]])[0], [])
        self.assertEqual(d.filter_novel(self.state, 'claude', 's', [many[0]])[0], [many[0]])

    def test_old_session_ledgers_are_pruned_on_write(self):
        d = self.dedupe
        d.record_delivered(self.state, 'claude', 'old', [self.record('a')])
        old = d._session_file(self.state, 'claude', 'old')
        stale = time.time() - d.MAX_AGE_SECONDS - 60
        os.utime(old, (stale, stale))
        d.record_delivered(self.state, 'claude', 'new', [self.record('a')])
        self.assertFalse(old.exists())
        self.assertTrue(d._session_file(self.state, 'claude', 'new').is_file())

    def test_receipt_key_lives_in_the_same_ledger(self):
        import hashlib
        d = self.dedupe
        one, two = '\nLatest receipt (receipts/one.md; ...):\nbody one', '\nLatest receipt (receipts/two.md; ...):\nbody two'
        self.assertFalse(d.receipt_delivered(self.state, 'claude', 's', one), 'no ledger yet')
        d.record_delivered(self.state, 'claude', 's', [self.record('a')], receipt=one)
        self.assertTrue(d.receipt_delivered(self.state, 'claude', 's', one))
        self.assertFalse(d.receipt_delivered(self.state, 'claude', 's', two))
        self.assertFalse(d.receipt_delivered(self.state, 'claude', 'other', one), 'per session')
        self.assertFalse(d.receipt_delivered(self.state, 'codex', 's', one), 'per harness')
        # A turn that delivered records but no receipt keeps the receipt already said.
        d.record_delivered(self.state, 'claude', 's', [self.record('b')])
        self.assertTrue(d.receipt_delivered(self.state, 'claude', 's', one))
        # A receipt-only turn records the new receipt and keeps the records.
        d.record_delivered(self.state, 'claude', 's', [], receipt=two)
        self.assertTrue(d.receipt_delivered(self.state, 'claude', 's', two))
        self.assertFalse(d.receipt_delivered(self.state, 'claude', 's', one))
        self.assertEqual(d.filter_novel(self.state, 'claude', 's', [self.record('a'), self.record('b')])[0], [])
        stored = json.loads(d._session_file(self.state, 'claude', 's').read_text(encoding='utf-8'))
        self.assertEqual(stored['version'], 1, 'an older release still reads the records')
        self.assertEqual(stored['receipt'], hashlib.sha256(two.encode('utf-8')).hexdigest())
        self.assertNotIn('body two', json.dumps(stored), 'only a hash of the receipt is kept')
        d.save_settings(self.state, False)
        self.assertFalse(d.receipt_delivered(self.state, 'claude', 's', two), 'dedupe off: always new')
        d.save_settings(self.state, True)
        d.reset_session(self.state, 'claude', 's')
        self.assertFalse(d.receipt_delivered(self.state, 'claude', 's', two), 'compaction clears it')
        d.record_delivered(self.state, 'claude', 'unknown', [], receipt=one)
        self.assertFalse(d.receipt_delivered(self.state, 'claude', 'unknown', one))
        self.assertEqual(list((self.state / d.DELIVERY_DIR).glob('*.json')), [], 'no ledger without a session id')

    def test_damaged_receipt_value_fails_open(self):
        d = self.dedupe
        path = d._session_file(self.state, 'claude', 's', create=True)
        path.write_text(json.dumps({'version': 1, 'harness': 'claude', 'at': time.time(), 'receipt': 7,
                                    'records': {'a': {'sha': 'a' * 64, 'windows': [d._window(self.record('a'))]}}}),
                        encoding='utf-8')
        self.assertFalse(d.receipt_delivered(self.state, 'claude', 's', '\nLatest receipt (x):\n7'))
        self.assertEqual(d.filter_novel(self.state, 'claude', 's', [self.record('a')])[0], [],
                         'a bad receipt value does not discard the records')


if __name__ == '__main__':
    unittest.main()
