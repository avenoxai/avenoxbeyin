"""Tests for context-fill reminder (issue #685)."""
import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
import beyin_v3_context_fill as cf


class ContextFillTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

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


if __name__ == '__main__':
    unittest.main()