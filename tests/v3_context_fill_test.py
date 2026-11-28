"""Tests for context-fill reminder (issue #685)."""
import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
import beyin_v3_context_fill as cf
from v3_package_helpers import clean_environ


class ContextFillTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        # The reminder reads BEYIN_V3_NO_RECEIPT_REMINDER from os.environ in process (#887).
        environment = clean_environ()
        environment.start()
        self.addCleanup(environment.stop)

    def test_default_config(self):
        cfg = cf.load_config(self.state)
        self.assertEqual(cfg, {'enabled': False, 'first_threshold': 150000, 'repeat_threshold': 75000})

    def test_save_and_load_config(self):
        cfg = {'enabled': True, 'first_threshold': 200000, 'repeat_threshold': 100000}
        cf.save_config(self.state, cfg)
        loaded = cf.load_config(self.state)
        self.assertEqual(loaded, cfg)

    def test_config_invalid_type_fallbacks(self):
        # Invalid enabled type should fall back to default
        (self.state / 'context-fill.json').write_text(json.dumps({'enabled': 'yes'}))
        loaded = cf.load_config(self.state)
        self.assertFalse(loaded['enabled'])

    def test_session_state(self):
        session_digest = cf._session_digest('test-session')
        st = cf.load_session_state(self.state, session_digest)
        self.assertEqual(st, {'tokens': 0, 'last_reminded_at': 0, 'reminders_sent': 0})

        st['tokens'] = 100000
        st['reminders_sent'] = 1
        cf.save_session_state(self.state, session_digest, st)
        loaded = cf.load_session_state(self.state, session_digest)
        self.assertEqual(loaded['tokens'], 100000)
        self.assertEqual(loaded['reminders_sent'], 1)

    def test_extract_context_tokens(self):
        # From transcript
        class Payload:
            def get(self, key, default=None):
                if key == 'transcript':
                    return [
                        {'role': 'user', 'content': 'test'},
                        {'role': 'assistant', 'usage': {'input_tokens': 50000, 'cache_creation_input_tokens': 30000, 'cache_read_input_tokens': 20000}}
                    ]
                return default
        tokens = cf._extract_context_tokens(Payload())
        self.assertEqual(tokens, 100000)

        # From top-level usage
        class Payload2:
            def get(self, key, default=None):
                if key == 'usage':
                    return {'input_tokens': 60000, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}
                return default
        tokens = cf._extract_context_tokens(Payload2())
        self.assertEqual(tokens, 60000)

    def test_should_remind_first_threshold(self):
        cfg = {'enabled': True, 'first_threshold': 150000, 'repeat_threshold': 75000}
        state = {'tokens': 200000, 'reminders_sent': 0}
        remind, new_state, reason = cf.should_remind(self.state, 'session1', cfg, state)
        self.assertTrue(remind)
        self.assertEqual(reason, 'context_fill_threshold')
        self.assertEqual(new_state['reminders_sent'], 1)

    def test_should_remind_repeat_threshold(self):
        cfg = {'enabled': True, 'first_threshold': 150000, 'repeat_threshold': 75000}
        state = {'tokens': 250000, 'reminders_sent': 1}
        remind, new_state, reason = cf.should_remind(self.state, 'session1', cfg, state)
        self.assertTrue(remind)
        self.assertEqual(new_state['reminders_sent'], 2)

    def test_should_not_remind_below_threshold(self):
        cfg = {'enabled': True, 'first_threshold': 150000, 'repeat_threshold': 75000}
        state = {'tokens': 100000, 'reminders_sent': 0}
        remind, _, _ = cf.should_remind(self.state, 'session1', cfg, state)
        self.assertFalse(remind)

    def test_should_not_remind_when_disabled(self):
        cfg = {'enabled': False, 'first_threshold': 150000, 'repeat_threshold': 75000}
        state = {'tokens': 200000, 'reminders_sent': 0}
        remind, _, _ = cf.should_remind(self.state, 'session1', cfg, state)
        self.assertFalse(remind)

    def test_context_fill_reminder_basic(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            cfg = cf.load_config(state)
            cfg['enabled'] = True
            cfg['first_threshold'] = 150000
            cfg['repeat_threshold'] = 75000
            cf.save_config(state, cfg)

            class MockPayload:
                def __init__(self, tokens):
                    self.session_id = 'test-session'
                    self.stop_hook_active = False
                    self.tokens = tokens
                def get(self, key, default=None):
                    if key == 'session_id': return self.session_id
                    if key == 'stop_hook_active': return self.stop_hook_active
                    if key == 'prompt': return ''
                    if key == 'transcript':
                        return [{'role': 'assistant', 'usage': {'input_tokens': self.tokens, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}}]
                    return default

            # Below threshold
            payload = MockPayload(100000)
            result = cf.context_fill_reminder(payload, tmp, 'claude', 'Stop')
            self.assertIsNone(result)

            # Above first threshold
            payload = MockPayload(200000)
            result = cf.context_fill_reminder(payload, tmp, 'claude', 'Stop')
            self.assertIsNotNone(result)
            self.assertEqual(result['decision'], 'block')
            self.assertIn('devir kartını güncelle', result['reason'])

    def test_reminder_reason_reports_tokens_and_crossed_threshold(self):
        # A 1M-window session reminded at 164.601 tokens read "doluluk eşiği aşıldı" while the
        # client showed 17% used; the reason must say what was reached, not that context is full.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(cf.load_config(Path(tmp)), enabled=True,
                       first_threshold=150000, repeat_threshold=75000)
            cf.save_config(Path(tmp), cfg)

            def payload(tokens):
                return {'session_id': 'test-session', 'stop_hook_active': False, 'prompt': '',
                        'transcript': [{'role': 'assistant', 'usage': {'input_tokens': tokens}}]}

            first = cf.context_fill_reminder(payload(164601), tmp, 'claude', 'Stop')
            self.assertIn('164.601', first['reason'])
            self.assertIn('150.000', first['reason'])
            self.assertNotIn('doluluk', first['reason'])
            self.assertIn('sıkıştırma', first['reason'])
            self.assertIn('devir kartını güncelle', first['reason'])

            repeat = cf.context_fill_reminder(payload(250000), tmp, 'claude', 'Stop')
            self.assertIn('250.000', repeat['reason'])
            self.assertIn('239.601', repeat['reason'])  # last reminder + repeat interval

    def test_silencing_stop_hook_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            cfg = cf.load_config(state)
            cfg['enabled'] = True
            cfg['first_threshold'] = 150000
            cfg['repeat_threshold'] = 75000
            cf.save_config(state, cfg)

            class MockPayload:
                def __init__(self):
                    self.session_id = 'test-session'
                    self.stop_hook_active = True
                def get(self, key, default=None):
                    if key == 'session_id': return self.session_id
                    if key == 'stop_hook_active': return self.stop_hook_active
                    if key == 'prompt': return ''
                    if key == 'transcript':
                        return [{'role': 'assistant', 'usage': {'input_tokens': 200000}}]
                    return default

            payload = MockPayload()
            result = cf.context_fill_reminder(payload, tmp, 'claude', 'Stop')
            self.assertIsNone(result)  # Should be silenced

    def test_installed_stop_and_global_bridge_read_only_their_own_main_chain(self):
        from v3_package_helpers import install, isolated_env, run_python
        base = self.state
        vault, project, state = base / 'vault', base / 'project', base / 'runtime'
        vault.mkdir()
        project.mkdir()
        env = isolated_env(base / 'home')
        self.assertEqual(install(vault, state, env).returncode, 0)
        configured = run_python(vault / 'beyin.py', ['preferences', '--context-fill', 'on'],
                                vault, env)
        self.assertEqual(configured.returncode, 0, configured.stderr)
        transcript = base / 'session.jsonl'
        def append(message, **fields):
            row = dict(sessionId='session', type=message['role'], message=message)
            with transcript.open('a', encoding='utf-8') as out:
                out.write(json.dumps(dict(row, **fields)) + chr(10))
        def send(hook, cwd, event):
            script = vault / '.claude/scripts' / ('beyin_v3_' + hook + '.py')
            args = ['--vault', vault, '--state', state, '--harness', 'claude']
            if hook == 'bridge':
                args.extend(['--project-root', project])
            result = run_python(script, args, cwd, env, dict(cwd=str(cwd), session_id='session',
                hook_event_name=event, transcript_path=str(transcript)))
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout).get('decision')
        for hook, cwd in [('hook', vault), ('bridge', project)]:
            transcript.write_text('', encoding='utf-8')
            append({'role': 'user', 'content': 'first turn'})
            append({'role': 'assistant', 'usage': {'input_tokens': 10000, 'cache_read_input_tokens': 140001}})
            append({'role': 'assistant', 'usage': {'input_tokens': 1}}, isSidechain=True)
            append({'role': 'assistant', 'usage': {'input_tokens': 1}}, sessionId='another')
            self.assertEqual(send(hook, cwd, 'Stop'), 'block')
            self.assertIsNone(send(hook, cwd, 'Stop'))
            append({'role': 'assistant', 'usage': {'input_tokens': 225001}})
            self.assertEqual(send(hook, cwd, 'Stop'), 'block')
            append({'role': 'user', 'content': '[kaydetme]'})
            append({'role': 'assistant', 'usage': {'input_tokens': 400000}})
            self.assertIsNone(send(hook, cwd, 'Stop'))
            send(hook, cwd, 'PreCompact')
            append({'role': 'user', 'content': 'new turn'})
            append({'role': 'assistant', 'usage': {'input_tokens': 150001}})
            self.assertEqual(send(hook, cwd, 'Stop'), 'block')
            send(hook, cwd, 'SessionEnd')
        self.assertEqual(list((state / 'context-fill').glob('*.json')), [])

    def test_concurrent_stops_claim_a_threshold_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        cf.save_config(self.state, dict(enabled=True, first_threshold=150000, repeat_threshold=75000))
        payload = dict(session_id='same', usage={'input_tokens': 175000})
        barrier = Barrier(8)
        def stop(_):
            barrier.wait()
            return cf.context_fill_reminder(payload, self.state, 'claude', 'Stop')
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(stop, range(8)))
        self.assertEqual(sum(bool(response) for response in responses), 1)

    def test_disabled_configuration_does_not_open_transcript_or_create_state(self):
        from unittest.mock import patch
        with patch.object(cf, '_extract_context_tokens', side_effect=AssertionError) as reader:
            self.assertIsNone(cf.context_fill_reminder({'session_id': 'session'}, self.state, 'claude', 'Stop'))
        reader.assert_not_called()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_reset_session_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            cfg = cf.load_config(state)
            cfg['enabled'] = True
            cf.save_config(state, cfg)

            session_digest = cf._session_digest('test-session')
            st = cf.load_session_state(state, session_digest)
            st['tokens'] = 200000
            st['reminders_sent'] = 2
            cf.save_session_state(state, session_digest, st)

            cf.reset_session_state(state, 'test-session')
            reset_st = cf.load_session_state(state, session_digest)
            self.assertEqual(reset_st['tokens'], 0)
            self.assertEqual(reset_st['reminders_sent'], 0)

    def _write_ag_steps(self, path, rows):
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8', newline='\n')

    def test_antigravity_stop_reminds_with_continue_and_respects_stop_hook_active(self):
        cf.save_config(self.state, dict(enabled=True, first_threshold=150000, repeat_threshold=75000))
        transcript = (self.state / 'agy.jsonl').resolve()
        self._write_ag_steps(transcript, [
            {'step_index': 0, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE', 'content': 'analyze'},
            {'step_index': 1, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
             'input_tokens': 10000, 'cache_read_tokens': 20000},
            {'step_index': 2, 'source': 'SYSTEM', 'type': 'GENERIC', 'status': 'DONE'},
            {'step_index': 3, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
             'input_tokens': 7949, 'cache_read_tokens': 507670, 'output_tokens': 1652},
        ])
        payload = {'session_id': 'agy-session-1', 'transcriptPath': str(transcript), 'lastUserInput': 'analyze'}
        first = cf.context_fill_reminder(payload, self.state, 'antigravity', 'Stop')
        self.assertIsNotNone(first)
        self.assertEqual(first['decision'], 'continue')
        self.assertIn('515.619', first['reason'])
        self.assertIn('150.000', first['reason'])
        # Repeat at same token count stays silent (below 515619 + 75000).
        self.assertIsNone(cf.context_fill_reminder(payload, self.state, 'antigravity', 'Stop'))
        # stop_hook_active, continuationActive and executionNum > 0 all silence even a fresh session.
        for extra in ({'stop_hook_active': True}, {'continuationActive': True}, {'executionNum': 1}):
            with self.subTest(extra=extra):
                suppressed = dict(payload, session_id='agy-suppressed', **extra)
                self.assertIsNone(cf.context_fill_reminder(suppressed, self.state, 'antigravity', 'Stop'))
        # Non-int / bool / negative token fields are ignored cleanly.
        self._write_ag_steps(transcript, [
            {'step_index': 0, 'type': 'USER_INPUT', 'content': 'go'},
            {'step_index': 1, 'type': 'PLANNER_RESPONSE', 'input_tokens': True, 'cache_read_tokens': -500000},
            {'step_index': 2, 'type': 'PLANNER_RESPONSE', 'input_tokens': '200000', 'cache_read_tokens': None},
        ])
        self.assertIsNone(cf.context_fill_reminder(
            dict(payload, session_id='agy-bad-types'), self.state, 'antigravity', 'Stop'))

    def test_antigravity_checkpoint_stops_backward_scan_and_resets_reminders(self):
        cf.save_config(self.state, dict(enabled=True, first_threshold=150000, repeat_threshold=75000))
        transcript = (self.state / 'agy-compact.jsonl').resolve()
        rows = [
            {'step_index': 0, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'content': 'turn 1'},
            {'step_index': 1, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE',
             'input_tokens': 10000, 'cache_read_tokens': 190000},
        ]
        self._write_ag_steps(transcript, rows)
        payload = {'session_id': 'agy-compact-session', 'transcriptPath': str(transcript), 'lastUserInput': 'turn 1'}
        first = cf.context_fill_reminder(payload, self.state, 'antigravity', 'Stop')
        self.assertEqual(first['decision'], 'continue')

        # CHECKPOINT arrives with no post-compaction PLANNER_RESPONSE yet:
        # pre-compaction 200k tokens must NOT trigger reminder, and reminders_sent resets.
        rows.append({'step_index': 328, 'source': 'SYSTEM', 'type': 'CHECKPOINT', 'status': 'DONE'})
        self._write_ag_steps(transcript, rows)
        self.assertIsNone(cf.context_fill_reminder(payload, self.state, 'antigravity', 'Stop'))
        saved = cf.load_session_state(self.state, cf._session_digest('agy-compact-session'))
        self.assertEqual(saved['reminders_sent'], 0)
        self.assertEqual(saved.get('checkpoint_step'), 328)

        # Post-compaction turn 1 below threshold -> silent.
        rows.extend([
            {'step_index': 329, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'content': 'turn 2'},
            {'step_index': 330, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE',
             'input_tokens': 6395, 'cache_read_tokens': 16763},
        ])
        self._write_ag_steps(transcript, rows)
        self.assertIsNone(cf.context_fill_reminder(
            dict(payload, lastUserInput='turn 2'), self.state, 'antigravity', 'Stop'))

        # Post-compaction turn 2 grows past first_threshold (150k) -> reminds again with first_threshold!
        rows.extend([
            {'step_index': 331, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'content': 'turn 3'},
            {'step_index': 332, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE',
             'input_tokens': 10000, 'cache_read_tokens': 150000},
        ])
        self._write_ag_steps(transcript, rows)
        second = cf.context_fill_reminder(
            dict(payload, lastUserInput='turn 3'), self.state, 'antigravity', 'Stop')
        self.assertIsNotNone(second)
        self.assertEqual(second['decision'], 'continue')
        self.assertIn('160.000', second['reason'])
        self.assertIn('150.000', second['reason'])

        # Same CHECKPOINT still in tail on next turn below repeat threshold -> must NOT re-reset!
        rows.extend([
            {'step_index': 333, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'content': 'turn 4'},
            {'step_index': 334, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE',
             'input_tokens': 10000, 'cache_read_tokens': 160000},
        ])
        self._write_ag_steps(transcript, rows)
        self.assertIsNone(cf.context_fill_reminder(
            dict(payload, lastUserInput='turn 4'), self.state, 'antigravity', 'Stop'))

        # Second CHECKPOINT followed directly by two turns reaching 155k (no intermediate Stop) resets cleanly.
        rows.extend([
            {'step_index': 600, 'source': 'SYSTEM', 'type': 'CHECKPOINT', 'status': 'DONE'},
            {'step_index': 601, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'content': 'turn 5'},
            {'step_index': 602, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE',
             'input_tokens': 5000, 'cache_read_tokens': 150000},
        ])
        self._write_ag_steps(transcript, rows)
        third = cf.context_fill_reminder(
            dict(payload, lastUserInput='turn 5'), self.state, 'antigravity', 'Stop')
        self.assertIsNotNone(third)
        self.assertEqual(third['decision'], 'continue')
        self.assertIn('155.000', third['reason'])
        self.assertIn('150.000', third['reason'])

    def test_antigravity_kaydetme_opt_out_suppresses_reminder(self):
        cf.save_config(self.state, dict(enabled=True, first_threshold=150000, repeat_threshold=75000))
        transcript = (self.state / 'agy-optout.jsonl').resolve()
        # Case-insensitive [KAYDETME] in USER_INPUT content suppresses reminder.
        self._write_ag_steps(transcript, [
            {'step_index': 0, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'content': 'Bunu [KAYDETME] lutfen'},
            {'step_index': 1, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE',
             'input_tokens': 200000, 'cache_read_tokens': 0},
        ])
        payload = {'session_id': 'agy-optout-1', 'transcriptPath': str(transcript), 'lastUserInput': 'plain'}
        self.assertIsNone(cf.context_fill_reminder(payload, self.state, 'antigravity', 'Stop'))
        self.assertFalse((self.state / 'context-fill').exists())

        # [kaydetme] in payload lastUserInput also suppresses even without opt-out in transcript.
        self._write_ag_steps(transcript, [
            {'step_index': 0, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'content': 'normal'},
            {'step_index': 1, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE',
             'input_tokens': 200000, 'cache_read_tokens': 0},
        ])
        payload2 = {'session_id': 'agy-optout-2', 'transcriptPath': str(transcript),
                    'lastUserInput': 'sadece bak [Kaydetme]'}
        self.assertIsNone(cf.context_fill_reminder(payload2, self.state, 'antigravity', 'Stop'))
        # Same transcript without [kaydetme] reminds normally (proving suppression came from opt-out).
        normal = cf.context_fill_reminder(
            {'session_id': 'agy-optout-3', 'transcriptPath': str(transcript), 'lastUserInput': 'normal'},
            self.state, 'antigravity', 'Stop')
        self.assertEqual(normal['decision'], 'continue')

    def test_antigravity_unsafe_transcript_paths_fail_open(self):
        cf.save_config(self.state, dict(enabled=True, first_threshold=150000, repeat_threshold=75000))
        real = (self.state / 'real.jsonl').resolve()
        self._write_ag_steps(real, [
            {'step_index': 0, 'type': 'USER_INPUT', 'content': 'go'},
            {'step_index': 1, 'type': 'PLANNER_RESPONSE', 'input_tokens': 200000, 'cache_read_tokens': 0},
        ])
        wrong_ext = (self.state / 'real.txt').resolve()
        wrong_ext.write_text(real.read_text(encoding='utf-8'), encoding='utf-8')
        folder = (self.state / 'dir.jsonl').resolve()
        folder.mkdir()

        unsafe = [
            None, 123, '', 'relative.jsonl', str(wrong_ext), str(folder),
            str(real) + '\0.jsonl', '//server/share/transcript.jsonl', r'\\server\share\transcript.jsonl',
            str((self.state / 'missing.jsonl').resolve()),
        ]
        for bad in unsafe:
            with self.subTest(path=bad):
                res = cf.context_fill_reminder(
                    {'session_id': 'agy-unsafe', 'transcriptPath': bad}, self.state, 'antigravity', 'Stop')
                self.assertIsNone(res)

        link = self.state.resolve() / 'link.jsonl'
        try:
            link.symlink_to(real)
        except OSError:
            pass
        else:
            res = cf.context_fill_reminder(
                {'session_id': 'agy-link', 'transcriptPath': str(link)}, self.state, 'antigravity', 'Stop')
            self.assertIsNone(res)

        # Positive control: the real regular .jsonl path succeeds.
        ok = cf.context_fill_reminder(
            {'session_id': 'agy-safe', 'transcriptPath': str(real)}, self.state, 'antigravity', 'Stop')
        self.assertEqual(ok['decision'], 'continue')

    def test_antigravity_bounded_tail_reads_only_last_limit_bytes(self):
        from unittest.mock import patch
        import beyin_v3_hook as hook
        cf.save_config(self.state, dict(enabled=True, first_threshold=150000, repeat_threshold=75000))
        transcript = (self.state / 'agy-big.jsonl').resolve()
        rows = [
            {'step_index': 0, 'type': 'PLANNER_RESPONSE', 'input_tokens': 500000, 'cache_read_tokens': 0,
             'padding': 'x' * 1500},
            {'step_index': 1, 'source': 'SYSTEM', 'type': 'CHECKPOINT'},
            {'step_index': 2, 'type': 'USER_INPUT', 'content': 'after'},
            {'step_index': 3, 'type': 'PLANNER_RESPONSE', 'input_tokens': 160000, 'cache_read_tokens': 0},
        ]
        self._write_ag_steps(transcript, rows)
        with patch.object(hook, 'ANTIGRAVITY_TRANSCRIPT_LIMIT', 512):
            res = cf.context_fill_reminder(
                {'session_id': 'agy-big', 'transcriptPath': str(transcript)}, self.state, 'antigravity', 'Stop')
        self.assertIsNotNone(res)
        self.assertEqual(res['decision'], 'continue')
        self.assertIn('160.000', res['reason'])


if __name__ == '__main__':
    unittest.main()