"""Client-unit rebudgeting must make bounded progress without tail clipping."""
from pathlib import Path
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_companion as companion
import beyin_v3_hook as hook


class RebudgetBoundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rebudget-bound-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.store = runtime.MemoryStore(root / 'state', self.vault)
        self.revisions = {}
        companion.save_context(self.store.state_dir, 9500)

    def write(self, name, text):
        source = 'Companion/' + name
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        record = dict(source=source, text=text, kind='note', status='active',
                      updated_at='2026-10-01T00:00:00Z')
        if source in self.revisions:
            saved = self.store.update_task(source, self.revisions[source], record)
        else:
            saved = self.store.ingest(dict(id=source, **record))
        self.revisions[source] = saved['revision']

    def run_opening(self, harness, renderer):
        calls = []
        original_guard = companion.fit_client
        # One initial render, one ratio retry, then an integer binary search.
        bound = 2 + companion.client_budget(harness, 9500).bit_length()

        def counted(store, budget, *args, **kwargs):
            calls.append(budget)
            if len(calls) > bound:
                raise AssertionError('opening exceeded its logarithmic render bound')
            return renderer(store, budget, *args, **kwargs)

        def guard(client, text):
            self.assertEqual(original_guard(client, text), text, 'final guard must not cut priorities')
            return text

        payload = dict(hook_event_name='SessionStart', session_id='rebudget-' + harness)
        output = io.StringIO()
        argv = ['hook', '--vault', str(self.vault), '--state', str(self.store.state_dir),
                '--harness', harness]
        with patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                patch.object(sys, 'stdout', output), patch.object(companion, 'context', side_effect=counted), \
                patch.object(companion, 'fit_client', side_effect=guard), \
                patch.dict(os.environ, inherited_env(BEYIN_V3_NO_SPAWN='1'), clear=True):
            hook.main()
        self.assertLessEqual(len(calls), bound)
        result = json.loads(output.getvalue())
        self.assertIn('hookSpecificOutput', result, result)
        text = result['hookSpecificOutput']['additionalContext']
        cap = companion.CLIENT_TEXT_LIMITS.get(harness)
        if cap:
            self.assertLessEqual(companion.client_size(harness, text), cap[0])
        return text, calls

    def test_unused_characters_do_not_count_as_byte_overflow_progress(self):
        text = 'ç' * 4000 + 'a' * 2001
        self.assertEqual(len(text), 6001)
        self.assertEqual(companion.client_size('codex', text), 10001)
        self.assertEqual(companion.client_rebudget('codex', 9500, text), 6000)

    def test_unused_characters_do_not_count_as_utf16_overflow_progress(self):
        text = '😀' * 5000 + 'a'
        self.assertEqual(len(text), 5001)
        self.assertEqual(companion.client_size('claude', text), 10001)
        self.assertEqual(companion.client_rebudget('claude', 9500, text), 5000)

    def test_real_short_render_one_byte_over_limit_converges(self):
        for name in ('Core.md', 'Soul.md', 'Last-Session.md', 'Threads.md', 'Journal.md'):
            self.write(name, '# Synthetic\nShort source.\n')
        base = '# Rules\n- ' + 'ç' * 4000 + '\n'
        self.write('Kurallar.md', base)
        session = hashlib.sha256(b'rebudget-codex').hexdigest()[:24]
        first = companion.context(self.store, 9500, session, 'codex')
        padding = 10001 - companion.client_size('codex', first)
        self.assertGreater(padding, 0)
        self.write('Kurallar.md', base + 'a' * padding)
        first = companion.context(self.store, 9500, session, 'codex')
        self.assertEqual(companion.client_size('codex', first), 10001)
        self.assertLess(len(first), 9500 - 1000)
        text, calls = self.run_opening('codex', companion.context)
        self.assertTrue(text)
        self.assertLessEqual(len(calls), 2)

    def test_changed_unicode_ratio_has_a_bounded_fitting_priority_render(self):
        required = 'MANDATORY_RULE_CANARY\nHANDOFF_CANARY\n'
        for harness, double in (('codex', 'ç'), ('claude', '😀')):
            with self.subTest(harness=harness):
                def renderer(store, budget, *args, **kwargs):
                    # Above 5,500 chars each render remains only one client unit
                    # too large, even after the previous budget was decremented.
                    if budget > 5500:
                        extra = 10001 - budget
                        return required + double * extra + 'a' * (budget - len(required) - extra)
                    return (required + 'a' * budget)[:budget]

                text, calls = self.run_opening(harness, renderer)
                self.assertIn(required, text)
                self.assertEqual(len(text), 5500)
                self.assertLessEqual(len(calls), 2 + (9500).bit_length())

    def test_uncapped_client_renders_only_once(self):
        text, calls = self.run_opening('hermes', lambda store, budget, *args, **kwargs: 'ç' * 7000)
        self.assertEqual(text, 'ç' * 7000)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
