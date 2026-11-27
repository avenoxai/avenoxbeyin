"""Opt-in conversation traces: synthetic metadata, real Markdown/search and installed hooks."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env, isolated_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
NOTICE = '[Sohbet izi]'
CANARY = 'SYNTHETIC_CHAT_TEXT_MUST_NOT_ENTER_STATE'


class TraceTest(unittest.TestCase):
    def setUp(self):
        import beyin_v3_chat_trace as trace
        self.trace = trace
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-chat-trace-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault, self.state = self.root / 'Sentetik Beyin', self.root / 'state'
        self.vault.mkdir()
        self.env = patch.dict(os.environ, inherited_env(CLAUDE_CODE_ENTRYPOINT='claude-desktop',
                              CLAUDE_CODE_SESSION_ATTENDED='1'), clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.session = hashlib.sha256(b'synthetic-chat').hexdigest()[:24]

    def payload(self, turn, **extra):
        return dict({'session_id': 'synthetic-chat', 'event_id': str(turn), 'prompt': CANARY}, **extra)

    def hint(self, turn, **extra):
        return self.trace.hint(self.state, 'claude', self.payload(turn, **extra))

    def enable(self):
        self.trace.configure(self.state, True)

    def write(self, text='Zirkon planı: robot kamerasını sabit ray üzerinde dene.'):
        return self.trace.write(self.vault, self.state, 'claude', self.session, text)

    def test_default_and_explicit_off_do_not_write_any_counter_or_note(self):
        self.assertFalse(self.trace.settings(self.state)['enabled'])
        self.assertFalse(self.state.exists())
        for n in range(5):
            self.assertEqual(self.hint(n), '')
        self.assertFalse(self.state.exists())
        self.trace.configure(self.state, False)
        for n in range(5):
            self.assertEqual(self.hint(n), '')
        self.assertEqual([p.name for p in self.state.iterdir()], ['chat-trace.json'])
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_43_human_prompts_nudge_only_at_2_10_18_without_persisting_prompt(self):
        self.enable()
        shown = [n for n in range(1, 44) if self.hint(n)]
        self.assertEqual(shown, [2, 10, 18])
        marker = next((self.state / self.trace.SESSIONS).glob('*.json')).read_text()
        self.assertNotIn(CANARY, marker)
        self.assertNotIn('synthetic-chat', marker)
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_unattended_unknown_and_synthetic_origins_stay_silent(self):
        self.enable()
        cases = ({}, {'CLAUDE_CODE_ENTRYPOINT': 'sdk-cli'}, {'CLAUDE_CODE_ENTRYPOINT': 'cli'},
                 {'CLAUDE_CODE_ENTRYPOINT': 'claude-desktop', 'CLAUDE_CODE_SESSION_ATTENDED': 'false'})
        for env in cases:
            with patch.dict(os.environ, env, clear=True):
                for n in range(3):
                    self.assertEqual(self.hint(n), '')
        for prompt in ('<task-notification>done', '<agent-message from="worker">done'):
            self.assertEqual(self.hint(1, prompt=prompt), '')
        self.assertEqual(self.hint(1, origin={'kind': 'peer'}), '')
        self.assertEqual(self.hint(1, agent_id='synthetic-worker'), '')
        self.assertEqual(self.hint(1, prompt=''), '')
        self.assertFalse((self.state / self.trace.SESSIONS).exists())

    def test_explicit_opt_out_survives_silent_origin_and_origin_change(self):
        self.enable()
        for entry in ('cli', 'sdk-cli', ''):
            with self.subTest(entry=entry):
                raw = 'silent-' + entry
                session = hashlib.sha256(raw.encode()).hexdigest()[:24]
                payload = self.payload(1, session_id=raw, prompt='Bu konuşma [kaydetme]')
                with patch.dict(os.environ, {'CLAUDE_CODE_ENTRYPOINT': entry}):
                    self.assertEqual(self.trace.hint(self.state, 'claude', payload), '')
                with self.assertRaisesRegex(ValueError, 'suppressed'):
                    self.trace.write(self.vault, self.state, 'claude', session, 'Sentetik özet')
                for turn in range(2, 5):
                    self.assertEqual(self.trace.hint(self.state, 'claude',
                        self.payload(turn, session_id=raw)), '')
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_agent_opt_out_text_cannot_suppress_human_session(self):
        self.enable()
        for extra in ({'origin': {'kind': 'peer'}}, {'origin': 'coordinator'},
                      {'agent_id': 'worker'}, {'agent_type': 'reviewer'},
                      {'no_memory': True}, {'prompt': '<task-notification>[kaydetme]'},
                      {'prompt': '<agent-message from="worker">[kaydetme]'}):
            payload = self.payload('peer', prompt='Alıntı: [kaydetme]')
            payload.update(extra)
            self.assertEqual(self.trace.hint(self.state, 'claude', payload), '')
            self.assertFalse((self.state / self.trace.SESSIONS).exists())
        self.assertEqual(self.hint(1), '')
        self.assertIn(NOTICE, self.hint(2))
        self.assertEqual(self.write()['status'], 'written')

    def test_codex_reads_only_matching_first_metadata_record(self):
        self.enable()
        log = self.root / 'synthetic-rollout.jsonl'
        meta = {'id': 'synthetic-chat', 'originator': 'codex_cli_rs', 'source': 'cli'}
        def save(**fields):
            log.write_text(json.dumps({'type': 'session_meta', 'payload': dict(meta, **fields)}) +
                           '\nTHIS IS NOT JSON AND MUST NEVER BE READ\n', encoding='utf-8')
        save()
        payload = self.payload(1, transcript_path=str(log))
        self.assertTrue(self.trace.human_turn('codex', payload))
        for change in ({'originator': 'codex_exec'}, {'source': 'exec'}, {'source': 'vscode'}, {'id': 'other'}):
            save(**change)
            self.assertFalse(self.trace.human_turn('codex', payload))
        save()
        self.assertFalse(self.trace.human_turn('codex', dict(payload, agent_id='worker')))
        self.assertFalse(self.trace.human_turn('codex', dict(payload, originator='codex_exec')))
        self.assertFalse(self.trace.human_turn('codex', self.payload(1)))
        log.write_text('x' * 65_537 + '\n', encoding='utf-8')
        self.assertFalse(self.trace.human_turn('codex', payload))
        self.assertFalse((self.state / self.trace.SESSIONS).exists())

    def test_repeat_event_does_not_count_twice_and_turn_id_is_supported(self):
        self.enable()
        self.assertEqual(self.hint(1), '')
        self.assertEqual(self.hint(1), '')
        self.assertIn(NOTICE, self.hint(2))
        self.assertEqual(self.hint(2), '')
        self.assertEqual(self.hint('', turn_id='next-turn'), '')
        self.assertEqual(self.hint('', turn_id='next-turn'), '')
        value = json.loads(next((self.state / self.trace.SESSIONS).glob('*.json')).read_text())
        self.assertEqual(value['turns'], 3)

    def test_kaydetme_suppresses_this_session_and_its_explicit_trace_only(self):
        self.enable()
        self.hint(1, prompt='Bu konuşma [kaydetme]')
        for n in range(2, 25):
            self.assertEqual(self.hint(n), '')
        with self.assertRaisesRegex(ValueError, 'suppressed'):
            self.write()
        other = self.payload(1, session_id='another-synthetic-chat')
        self.trace.hint(self.state, 'claude', other)
        self.assertIn(NOTICE, self.trace.hint(self.state, 'claude', dict(other, event_id='2')))

    def test_written_checkpoint_resets_gap_but_not_lifetime_notice_limit(self):
        self.enable()
        self.hint(1)
        self.assertIn(NOTICE, self.hint(2))
        self.assertEqual(self.write()['covered_turns'], 2)
        self.assertEqual(self.hint(3), '')
        self.assertIn(NOTICE, self.hint(4))
        self.write('Kobalt deneyi: ışık açısı otuz derece olarak denenecek.')
        self.assertEqual(self.hint(5), '')
        self.assertIn(NOTICE, self.hint(6))
        self.write('Lale malzemesi: yüzey örneği mat olarak karşılaştırılacak.')
        self.assertFalse(any(self.hint(n) for n in range(7, 25)))

    def test_real_receipt_closes_prior_counted_turns_for_same_session_only(self):
        from beyin_v3_sync import SyncEngine
        self.enable()
        self.hint(1)
        engine = SyncEngine(self.vault, self.state)
        ref = self.vault / 'notes' / 'synthetic.md'
        ref.parent.mkdir()
        ref.write_text('Zirkon deneyi tamamlandı.', encoding='utf-8')
        engine.receipt('another-result', 'Başka oturum.', ['notes/synthetic.md'],
                       'claude', session='0' * 24)
        self.assertIn(NOTICE, self.hint(2))
        engine.receipt('synthetic-result', 'Öğrenilen: mekanik kontrol.', ['notes/synthetic.md'],
                       'claude', session=self.session)
        self.assertEqual(self.hint(3), '')
        self.assertIn(NOTICE, self.hint(4))

    def test_monthly_paragraphs_are_separate_searchable_sources_of_context(self):
        from beyin_v3_sync import SyncEngine, parse
        from beyin_v3_passage import split_blocks
        from beyin_v3_jev import remote_allowed
        self.enable()
        first = self.write()
        second = self.write('Kobalt deneyi için ışık açısı otuz derece seçildi.')
        self.assertEqual(first['source'], second['source'])
        content = (self.vault / first['source']).read_text(encoding='utf-8')
        metadata, body = parse(content)
        self.assertEqual(metadata['visibility'], 'internal')
        self.assertFalse(metadata['remote_allowed'])
        paragraphs = [body[a:b] for _, a, b in split_blocks(body) if 'planı:' in body[a:b] or 'Kobalt' in body[a:b]]
        self.assertEqual(len(paragraphs), 2)
        self.assertTrue(all(len(p) <= 450 for p in paragraphs))
        engine = SyncEngine(self.vault, self.state)
        result = engine.store.context_for('claude', 'zirkon robot kamerası', strict=True)
        self.assertEqual(len(result['records']), 1)
        self.assertIn('sabit ray', result['records'][0]['text'])
        # A monthly trace keeps its paragraphs separate even with spare room (#530):
        # neither the whole note nor its one section is a delivery extension.
        self.assertNotIn('Kobalt deneyi', result['records'][0]['text'])
        self.assertNotIn('Kobalt deneyi', result.sources[0]['text'])
        self.assertFalse(remote_allowed(result['records'][0]))
        self.assertFalse((self.vault / '.beyin-preferences.json').exists())

    def test_monthly_trace_never_hands_a_prompt_other_sessions_summaries(self):
        from beyin_v3 import render_context
        from beyin_v3_sync import SyncEngine
        import beyin_v3_continuity as continuity
        self.enable()
        mine = 'Alfa projesi: zirkon robot kamerası sabit ray üzerinde denendi.'
        other = 'Beta projesi: kobalt sözleşmesi için sentetik fiyat 4200 konuşuldu.'
        self.write(mine)
        self.trace.write(self.vault, self.state, 'claude', hashlib.sha256(b'other').hexdigest()[:24], other)
        engine = SyncEngine(self.vault, self.state)
        engine.sync()
        context = engine.store.context_for('claude', 'zirkon robot kamerası', strict=True, budget_chars=8000)
        self.assertEqual(len(context['records']), 1)
        self.assertTrue(context['records'][0]['chat_trace'])
        self.assertEqual(getattr(context, 'extensions', {}), {})
        wire, delivered = render_context(context, 8000, prefix='Receipt session=synthetic\n')
        self.assertIn(mine, wire)
        self.assertNotIn('kobalt', wire.casefold())
        self.assertNotIn('4200', wire)
        continuity.remember(engine.store, 'claude', 'trace-session', 'zirkon robot kamerası', delivered)
        saved = json.loads(continuity._path(engine.store, 'claude', 'trace-session').read_text(encoding='utf-8'))
        record = next(r for r in engine.store._records() if r['id'] == context['records'][0]['id'])
        shown = ''.join(record['text'][start:end] for start, end in saved['refs'][0]['ranges'])
        self.assertIn(mine, shown)
        self.assertNotIn(other, shown)

    def test_explicit_write_replay_is_idempotent_and_long_text_is_rejected(self):
        self.enable()
        first = self.write()
        path = self.vault / first['source']
        before = path.read_bytes()
        self.assertEqual(self.write()['status'], 'unchanged')
        self.assertEqual(path.read_bytes(), before)
        for bad in ('', 'x' * 439, 'x' * 4097):
            with self.assertRaises(ValueError):
                self.write(bad)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.write('x' * 438)['paragraph_chars'], 450)

    def test_crlf_note_is_not_duplicated_on_replay_or_normalized_on_append(self):
        self.enable()
        first = self.write()
        path = self.vault / first['source']
        before = path.read_bytes().replace(b'\n', b'\r\n')
        path.write_bytes(before)
        self.assertEqual(self.write()['status'], 'unchanged')
        self.assertEqual(path.read_bytes(), before)
        self.write('Kobalt deneyi için açıyı sabit tut.')
        self.assertTrue(path.read_bytes().startswith(before))
        self.assertNotIn(b'\n', path.read_bytes().replace(b'\r\n', b''))

    def test_busy_lock_skips_hint_instead_of_waiting_or_changing_the_counter(self):
        self.enable()
        self.hint(1)
        marker = next((self.state / self.trace.SESSIONS).glob('*.json'))
        before = marker.read_bytes()
        with self.trace._lock(self.state):
            self.assertEqual(self.hint(2), '')
        self.assertEqual(marker.read_bytes(), before)
        self.assertIn(NOTICE, self.hint(2))

    def test_existing_foreign_monthly_source_is_preserved(self):
        from datetime import date
        self.enable()
        note = self.vault / 'knowledge/conversations' / (date.today().isoformat()[:7] + '.md')
        note.parent.mkdir(parents=True)
        note.write_text('Synthetic pre-existing monthly note.\n', encoding='utf-8')
        before = note.read_bytes()
        with self.assertRaisesRegex(ValueError, 'ownership/privacy'):
            self.write()
        self.assertEqual(note.read_bytes(), before)

    def test_unconfirmed_write_result_does_not_close_the_checkpoint(self):
        from beyin_v3_sync import SyncEngine
        from beyin_v3_notes import NoteConflict
        self.enable()
        self.hint(1)
        marker = next((self.state / self.trace.SESSIONS).glob('*.json'))
        before = marker.read_bytes()
        for status in ('conflict', 'error'):
            with patch.object(SyncEngine, 'note_edit', return_value={'status': status}):
                with self.assertRaisesRegex(NoteConflict, 'not confirmed'):
                    self.write()
            self.assertEqual(marker.read_bytes(), before)

    def test_external_edit_after_real_note_write_leaves_checkpoint_open(self):
        from beyin_v3_sync import SyncEngine
        from beyin_v3_notes import NoteConflict
        self.enable()
        self.hint(1)
        marker = next((self.state / self.trace.SESSIONS).glob('*.json'))
        before = marker.read_bytes()
        original = SyncEngine.note_edit
        replacement = 'Sentetik dış düzenleyici bu kaynağı değiştirdi.\n'
        changed = []
        def external_editor(engine, request):
            result = original(engine, request)  # real source and index transaction
            source = engine.root / request['source']
            source.write_text(replacement, encoding='utf-8')
            changed.append(source)
            return result
        with patch.object(SyncEngine, 'note_edit', side_effect=None, autospec=True) as edit:
            edit.side_effect = external_editor
            with self.assertRaisesRegex(NoteConflict, 'changed before acknowledgement'):
                self.write()
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(changed[0].read_text(encoding='utf-8'), replacement)

    def test_bad_settings_or_counter_fail_open_without_destroying_them(self):
        self.enable()
        config = self.state / 'chat-trace.json'
        config.write_text('{"schema":1,"enabled":"true"}', encoding='utf-8')
        self.assertFalse(self.trace.settings(self.state)['valid'])
        self.assertEqual(self.hint(1), '')
        self.enable()
        self.hint(1)
        marker = next((self.state / self.trace.SESSIONS).glob('*.json'))
        marker.write_text('broken', encoding='utf-8')
        self.assertEqual(self.hint(2), '')
        self.assertEqual(marker.read_text(), 'broken')

    def test_symlinked_settings_or_marker_folder_cannot_write_outside_state(self):
        target = self.root / 'elsewhere'
        target.mkdir()
        self.enable()
        link = self.state / self.trace.SESSIONS
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable on this platform')
        self.assertEqual(self.hint(1), '')
        self.assertEqual(list(target.iterdir()), [])
        link.unlink()
        config = self.state / 'chat-trace.json'
        config.unlink()
        external = target / 'settings.json'
        external.write_text('{"schema":1,"enabled":true}', encoding='utf-8')
        config.symlink_to(external)
        self.assertFalse(self.trace.settings(self.state)['enabled'])
        with self.assertRaises(ValueError):
            self.trace.configure(self.state, False)
        self.assertTrue(json.loads(external.read_text())['enabled'])

    def _ag_transcript(self, name, rows):
        path = (self.root / name).resolve()
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        return path

    def test_antigravity_human_turn_requires_explicit_user_transcript_metadata(self):
        self.enable()
        valid = self._ag_transcript('ag-human.jsonl', [
            {'step_index': 0, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'content': CANARY},
        ])
        base = {'session_id': 'synthetic-ag', 'prompt': CANARY, 'lastUserInput': CANARY,
                'initialNumSteps': 1, 'transcriptPath': str(valid)}
        self.assertTrue(self.trace.human_turn('antigravity', base))

        # Multi-turn human transcript ending on a fresh USER_INPUT step.
        multi = self._ag_transcript('ag-multi.jsonl', [
            {'step_index': 0, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'content': 'ilk'},
            {'step_index': 1, 'type': 'PLANNER_RESPONSE', 'source': 'MODEL', 'content': 'cevap'},
            {'step_index': 2, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'content': 'ikinci'},
        ])
        self.assertTrue(self.trace.human_turn('antigravity', dict(base, initialNumSteps=3, transcriptPath=str(multi))))

        # Large (> 64 KiB) transcript with USER_EXPLICIT in head passes without reading the whole file.
        big_head = (self.root / 'ag-big-head.jsonl').resolve()
        big_head.write_text(
            json.dumps({'step_index': 0, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT'}) + '\n'
            + 'THIS IS HUGE AND NOT JSON ' + ('x' * 70_000) + '\n',
            encoding='utf-8',
        )
        self.assertTrue(self.trace.human_turn('antigravity', {'session_id': 'synthetic-ag', 'prompt': CANARY,
                                                              'transcriptPath': str(big_head)}))

        # Large (> 64 KiB) transcript with USER_EXPLICIT only in bounded tail still verifies.
        big_tail = (self.root / 'ag-big-tail.jsonl').resolve()
        big_tail.write_text(
            json.dumps({'step_index': 0, 'type': 'PLANNER_RESPONSE', 'content': 'x' * 70_000}) + '\n'
            + json.dumps({'step_index': 1, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT'}) + '\n',
            encoding='utf-8',
        )
        self.assertTrue(self.trace.human_turn('antigravity', {'session_id': 'synthetic-ag', 'prompt': CANARY,
                                                              'transcriptPath': str(big_tail)}))

        # Missing, relative, UNC, non-.jsonl, non-canonical, and malformed transcript paths fail closed.
        non_jsonl = (self.root / 'ag-human.json').resolve()
        non_jsonl.write_text(valid.read_text(encoding='utf-8'), encoding='utf-8')
        sub = self.root / 'sub'
        sub.mkdir()
        non_canonical = str(sub / '..' / valid.name)
        for bad_path in (None, '', 'ag-human.jsonl', r'\\server\share\ag.jsonl', '//server/share/ag.jsonl',
                         str(non_jsonl), non_canonical, str(self.root / 'missing.jsonl'), str(valid) + '\0'):
            with self.subTest(bad_path=bad_path):
                self.assertFalse(self.trace.human_turn('antigravity', dict(base, transcriptPath=bad_path)))

        # Subagent / programmatic / malformed transcripts without USER_EXPLICIT fail closed.
        for label, content in (
            ('planner-only', json.dumps({'step_index': 0, 'type': 'PLANNER_RESPONSE', 'source': 'MODEL'}) + '\n'),
            ('system-source', json.dumps({'step_index': 0, 'type': 'USER_INPUT', 'source': 'SYSTEM'}) + '\n'),
            ('missing-source', json.dumps({'step_index': 0, 'type': 'USER_INPUT'}) + '\n'),
            ('malformed-json', 'not-json\n'),
            ('empty', ''),
        ):
            with self.subTest(transcript=label):
                bad = (self.root / f'ag-{label}.jsonl').resolve()
                bad.write_text(content, encoding='utf-8')
                self.assertFalse(self.trace.human_turn('antigravity', dict(base, transcriptPath=str(bad))))

        # Wakeup turn after PLANNER_RESPONSE (both with and without lastUserInput) is rejected.
        wakeup = self._ag_transcript('ag-wakeup.jsonl', [
            {'step_index': 0, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'content': 'eski'},
            {'step_index': 1, 'type': 'PLANNER_RESPONSE', 'source': 'MODEL', 'content': 'sonuc'},
        ])
        self.assertFalse(self.trace.human_turn('antigravity', dict(base, initialNumSteps=2, transcriptPath=str(wakeup))))
        without_last = {'session_id': 'synthetic-ag', 'prompt': CANARY,
                        'initialNumSteps': 2, 'transcriptPath': str(wakeup)}
        self.assertFalse(self.trace.human_turn('antigravity', without_last))

        # Delegated / synthetic / no_memory signals still fail closed.
        self.assertFalse(self.trace.human_turn('antigravity', dict(base, agent_id='worker')))
        self.assertFalse(self.trace.human_turn('antigravity', dict(base, no_memory=True)))
        self.assertFalse(self.trace.human_turn('antigravity', dict(base, prompt='<task-notification>done')))

        # Reparse point / symlinked transcript is rejected (real symlink if supported, plus stat guard).
        import stat
        real_stat = valid.lstat()
        fake_reparse = type('FakeStat', (), {
            'st_mode': real_stat.st_mode,
            'st_size': real_stat.st_size,
            'st_reparse_tag': 0xA000000C,
            'st_file_attributes': getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400),
        })()
        with patch.object(Path, 'lstat', return_value=fake_reparse):
            self.assertFalse(self.trace.human_turn('antigravity', base))

        link = (self.root / 'ag-link.jsonl').resolve()
        try:
            link.symlink_to(valid)
        except OSError:
            pass
        else:
            self.assertFalse(self.trace.human_turn('antigravity', dict(base, transcriptPath=str(link))))

        self.assertFalse((self.state / self.trace.SESSIONS).exists())

    def test_antigravity_hint_dedupes_steps_resets_on_receipt_and_suppresses(self):
        from beyin_v3_sync import SyncEngine
        self.enable()
        raw_session = 'synthetic-ag-chat'
        ag_session = hashlib.sha256(raw_session.encode()).hexdigest()[:24]
        transcript = self._ag_transcript('ag-session.jsonl', [
            {'step_index': 0, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'content': CANARY},
            {'step_index': 1, 'type': 'PLANNER_RESPONSE', 'source': 'MODEL'},
            {'step_index': 2, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'content': CANARY},
            {'step_index': 3, 'type': 'PLANNER_RESPONSE', 'source': 'MODEL'},
            {'step_index': 4, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'content': CANARY},
            {'step_index': 5, 'type': 'PLANNER_RESPONSE', 'source': 'MODEL'},
            {'step_index': 6, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'content': CANARY},
        ])

        def ag_payload(steps, **extra):
            return dict({'session_id': raw_session, 'prompt': CANARY, 'lastUserInput': CANARY,
                         'initialNumSteps': steps, 'transcriptPath': str(transcript)}, **extra)

        # Turn 1 (step 1) and duplicate invocation at step 1 do not double-count.
        self.assertEqual(self.trace.hint(self.state, 'antigravity', ag_payload(1)), '')
        self.assertEqual(self.trace.hint(self.state, 'antigravity', ag_payload(1)), '')
        marker_path = self.state / self.trace.SESSIONS / ('antigravity-' + ag_session + '.json')
        self.assertEqual(json.loads(marker_path.read_text(encoding='utf-8'))['turns'], 1)

        # Turn 2 (step 3) reaches next_due == 2 and emits hint naming --harness antigravity.
        nudge = self.trace.hint(self.state, 'antigravity', ag_payload(3))
        self.assertIn(NOTICE, nudge)
        self.assertIn('--harness antigravity --session ' + ag_session, nudge)
        # Repeated same initialNumSteps does not count again.
        self.assertEqual(self.trace.hint(self.state, 'antigravity', ag_payload(3)), '')
        raw_marker = marker_path.read_text(encoding='utf-8')
        self.assertEqual(json.loads(raw_marker)['turns'], 2)
        self.assertNotIn(CANARY, raw_marker)
        self.assertNotIn(raw_session, raw_marker)

        # Real receipt for this antigravity session resets next_due to turns + 2.
        engine = SyncEngine(self.vault, self.state)
        ref = self.vault / 'notes' / 'synthetic-ag.md'
        ref.parent.mkdir()
        ref.write_text('Antigravity sentetik notu.', encoding='utf-8')
        engine.receipt('ag-receipt-1', 'Öğrenilen: adım sayacı kontrolü.', ['notes/synthetic-ag.md'],
                       'antigravity', session=ag_session)
        self.assertEqual(self.trace.hint(self.state, 'antigravity', ag_payload(5)), '')
        self.assertIn(NOTICE, self.trace.hint(self.state, 'antigravity', ag_payload(7)))

        # [kaydetme] suppresses a session (even without transcriptPath) and blocks explicit write.
        opt_raw = 'synthetic-ag-optout'
        opt_session = hashlib.sha256(opt_raw.encode()).hexdigest()[:24]
        self.assertEqual(self.trace.hint(self.state, 'antigravity',
                                         {'session_id': opt_raw, 'prompt': 'Bunu [kaydetme]'}), '')
        self.assertEqual(self.trace.hint(self.state, 'antigravity', ag_payload(1, session_id=opt_raw)), '')
        self.assertEqual(self.trace.hint(self.state, 'antigravity', ag_payload(3, session_id=opt_raw)), '')
        with self.assertRaisesRegex(ValueError, 'suppressed'):
            self.trace.write(self.vault, self.state, 'antigravity', opt_session, 'Sentetik özet')

    def test_antigravity_write_creates_and_replays_monthly_trace(self):
        self.enable()
        first = self.trace.write(self.vault, self.state, 'antigravity', self.session,
                                 'Antigravity zirkon planı: sabit ray denendi.')
        self.assertEqual(first['status'], 'written')
        self.assertFalse(first['remote_allowed'])
        self.assertEqual(self.trace.write(self.vault, self.state, 'antigravity', self.session,
                                          'Antigravity zirkon planı: sabit ray denendi.')['status'], 'unchanged')
        self.assertTrue((self.state / self.trace.SESSIONS / ('antigravity-' + self.session + '.json')).is_file())


class InstalledTraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-installed-trace-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault, self.state = self.root / 'Sentetik Vault', self.root / 'runtime'
        self.vault.mkdir()
        self.env = inherited_env(**isolated_env(self.root / 'home'), CLAUDE_CODE_ENTRYPOINT='claude-desktop',
                                 CLAUDE_CODE_SESSION_ATTENDED='true')
        done = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault', str(self.vault),
                               '--state', str(self.state)], capture_output=True, text=True, encoding='utf-8',
                              env=self.env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def hook(self, turn, *extra, **fields):
        done = subprocess.run([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'),
                               '--vault', str(self.vault), '--state', str(self.state), '--harness', 'claude', *extra],
                              input=json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': 'synthetic-chat',
                                                'event_id': str(turn), 'prompt': 'Sentetik zirkon planı konuşalım.', **fields}),
                              capture_output=True, text=True, encoding='utf-8', env=self.env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_second_human_prompt_gets_soft_hint_without_file_edits(self):
        # Deliberately enable by file: this same scenario runs against pre-feature
        # code and fails on the missing hint, not on a missing module or CLI command.
        (self.state / 'chat-trace.json').write_text('{"schema":1,"enabled":true}', encoding='utf-8')
        self.assertNotIn(NOTICE, json.dumps(self.hook(1)))
        output = self.hook(2)
        self.assertIn(NOTICE, output.get('hookSpecificOutput', {}).get('additionalContext', ''))
        self.assertNotIn('decision', output)
        self.assertFalse((self.vault / 'knowledge/conversations').exists())

    def test_installed_peer_opt_out_does_not_disable_human_trace(self):
        (self.state / 'chat-trace.json').write_text('{"schema":1,"enabled":true}', encoding='utf-8')
        self.hook('peer', origin={'kind': 'peer'}, prompt='Alıntı: [kaydetme]')
        self.assertNotIn(NOTICE, json.dumps(self.hook(1)))
        self.assertIn(NOTICE, json.dumps(self.hook(2)))
        session = hashlib.sha256(b'synthetic-chat').hexdigest()[:24]
        done = subprocess.run([sys.executable, str(self.vault / 'beyin.py'),
                               'chat-trace', 'write', '--harness', 'claude', '--session', session,
                               '--text', 'Sentetik insan oturumu özeti.'],
                              capture_output=True, text=True, encoding='utf-8', env=self.env,
                              cwd=self.vault, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_metadata_only_and_disabled_configuration_keep_output_unchanged(self):
        baseline = self.hook(1)
        (self.state / 'chat-trace.json').write_text('{"schema":1,"enabled":false}', encoding='utf-8')
        self.assertEqual(self.hook(2), baseline)
        (self.state / 'chat-trace.json').write_text('{"schema":1,"enabled":true}', encoding='utf-8')
        self.assertEqual(self.hook(3, '--metadata-only'), {})
        self.assertFalse((self.state / 'chat-trace-sessions').exists())

    def test_installed_entrypoint_exposes_opt_in_and_explicit_write(self):
        session = hashlib.sha256(b'synthetic-chat').hexdigest()[:24]
        for arguments in (['chat-trace', 'on'],
                          ['chat-trace', 'write', '--harness', 'claude', '--session', session,
                           '--text', 'Zirkon örneği düz ışık altında incelendi.'], ['chat-trace', 'off']):
            done = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *arguments],
                                  capture_output=True, text=True, encoding='utf-8', env=self.env,
                                  cwd=self.vault, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertFalse((self.vault / '.beyin-preferences.json').exists())
        self.assertEqual(len(list((self.vault / 'knowledge/conversations').glob('*.md'))), 1)

    def test_installed_antigravity_cli_write_and_preinvocation_hint(self):
        conversation = 'synthetic-ag-installed'
        session = hashlib.sha256(conversation.encode()).hexdigest()[:24]
        transcript = (self.root / 'ag-installed.jsonl').resolve()
        rows = [
            {'step_index': 0, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'content': 'ilk'},
            {'step_index': 1, 'type': 'PLANNER_RESPONSE', 'source': 'MODEL'},
            {'step_index': 2, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'content': 'ikinci'},
            {'step_index': 3, 'type': 'PLANNER_RESPONSE', 'source': 'MODEL'},
            {'step_index': 4, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'content': 'ucuncu'},
            {'step_index': 5, 'type': 'PLANNER_RESPONSE', 'source': 'MODEL'},
        ]
        transcript.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')

        def ag_hook(event, **fields):
            done = subprocess.run(
                [sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'),
                 '--vault', str(self.vault), '--state', str(self.state),
                 '--harness', 'antigravity', '--event', event],
                input=json.dumps({'hookEventName': event, 'conversationId': conversation,
                                  'cwd': str(self.vault), 'transcriptPath': str(transcript), **fields}),
                capture_output=True, text=True, encoding='utf-8', env=self.env, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            return json.loads(done.stdout)

        # Turn chat-trace on via installed CLI.
        on_res = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'chat-trace', 'on'],
                                capture_output=True, text=True, encoding='utf-8', env=self.env,
                                cwd=self.vault, timeout=30)
        self.assertEqual(on_res.returncode, 0, on_res.stderr + on_res.stdout)

        # SessionStart answers the first prompt; its matching PreInvocation is quiet.
        ag_hook('SessionStart', lastUserInput='ilk soru')
        self.assertEqual(ag_hook('PreInvocation', invocationNum=0, initialNumSteps=1, lastUserInput='ilk soru'), {})

        # Turn 2 (1st UserPromptSubmit): counted once, duplicate step does not double-count.
        t2 = ag_hook('PreInvocation', invocationNum=0, initialNumSteps=3, lastUserInput='ikinci soru')
        self.assertNotIn(NOTICE, json.dumps(t2))
        t2_dup = ag_hook('PreInvocation', invocationNum=0, initialNumSteps=3, lastUserInput='ikinci soru')
        self.assertNotIn(NOTICE, json.dumps(t2_dup))

        # Turn 3 (2nd UserPromptSubmit): emits [Sohbet izi] hint with --harness antigravity.
        t3 = ag_hook('PreInvocation', invocationNum=0, initialNumSteps=5, lastUserInput='ucuncu soru')
        ephemeral = t3.get('injectSteps', [{}])[0].get('ephemeralMessage', '')
        self.assertIn(NOTICE, ephemeral)
        self.assertIn('--harness antigravity --session ' + session, ephemeral)

        # Wakeup turn after PLANNER_RESPONSE stays quiet.
        self.assertEqual(ag_hook('PreInvocation', invocationNum=0, initialNumSteps=6, lastUserInput='ucuncu soru'), {})

        # CLI chat-trace write --harness antigravity succeeds.
        write_res = subprocess.run([sys.executable, str(self.vault / 'beyin.py'),
                                    'chat-trace', 'write', '--harness', 'antigravity',
                                    '--session', session, '--text', 'Antigravity CLI özeti.'],
                                   capture_output=True, text=True, encoding='utf-8', env=self.env,
                                   cwd=self.vault, timeout=30)
        self.assertEqual(write_res.returncode, 0, write_res.stderr + write_res.stdout)
        self.assertEqual(len(list((self.vault / 'knowledge/conversations').glob('*.md'))), 1)

        # Backfill remains Claude Code only even with antigravity accepted on --harness.
        backfill_res = subprocess.run([sys.executable, str(self.vault / 'beyin.py'),
                                       'chat-trace', 'backfill', '--harness', 'antigravity'],
                                      capture_output=True, text=True, encoding='utf-8', env=self.env,
                                      cwd=self.vault, timeout=30)
        self.assertEqual(backfill_res.returncode, 1, backfill_res.stdout)
        self.assertIn('Claude Code transcripts only', backfill_res.stderr)


if __name__ == '__main__':
    unittest.main()
