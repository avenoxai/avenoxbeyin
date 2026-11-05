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
        # The selected primary passage is query-specific. A small complete
        # monthly source may also be delivered from spare room (#530).
        self.assertNotIn('Kobalt deneyi', result.sources[0]['text'])
        self.assertFalse(remote_allowed(result['records'][0]))
        self.assertFalse((self.vault / '.beyin-preferences.json').exists())

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


if __name__ == '__main__':
    unittest.main()
