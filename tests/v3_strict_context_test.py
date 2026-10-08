#!/usr/bin/env python3
"""Strict automatic context: per-turn injection only on meaningful lexical matches."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
HOOK = SCRIPTS / 'beyin_v3_hook.py'
spec = importlib.util.spec_from_file_location('v3_strict_evaluator', ROOT / 'scripts/evaluate_v3.py')
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)

LONG_LOG = ('Session log. ' + ' '.join(f'filler{i}' for i in range(400)) +
            ' The team discussed the screenshot problem and the deploy calendar and the budget.')
SHORT_NOTE = 'Quartz observatory calibration: the GoPlus quota is wasted because every candidate is rescanned.'
OTHER_NOTE = 'Grocery list for the weekend: apples, bread, olive oil.'


class StrictRetrieveTest(unittest.TestCase):
    def setUp(self):
        self.module = evaluator.load_runtime()
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-strict-test-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.store = self.module.MemoryStore(root / 'runtime', self.vault)
        self.addCleanup(lambda: evaluator.close_store(self.store))
        self.ingest('daily-log', LONG_LOG, 'daily/2026-09-11.md')
        self.ingest('quota-note', SHORT_NOTE, 'knowledge/goplus-quota.md')
        self.ingest('grocery', OTHER_NOTE, 'notes/grocery.md')

    def ingest(self, id, text, source):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        self.store.ingest({'id': id, 'kind': 'note', 'status': 'active', 'visibility': 'internal',
                           'text': text, 'facts': {}, 'source': source, 'updated_at': '2026-09-18T10:00:00Z'})

    def sources(self, query, **kwargs):
        return [record['source'] for record in self.store.retrieve(query, **kwargs)['records']]

    def test_single_shared_word_is_not_a_match_in_strict_mode(self):
        self.assertEqual(self.sources('is the budget approved', strict=False), ['daily/2026-09-11.md'])
        self.assertEqual(self.sources('is the budget approved', strict=True), [])

    def test_daily_logs_are_excluded_from_strict_context_only(self):
        query = 'screenshot problem and deploy calendar'
        self.assertIn('daily/2026-09-11.md', self.sources(query, strict=False))
        self.assertEqual(self.sources(query, strict=True), [])

    def test_meaningful_match_survives_strict_mode(self):
        self.assertEqual(self.sources('why is the GoPlus quota wasted', strict=True), ['knowledge/goplus-quota.md'])

    def test_empty_vocabulary_note_is_cleanly_handled(self):
        """Notes with empty vocabulary (or only stopwords) do not cause math errors in _strict_rank (PR #213/#214)."""
        self.ingest('empty-note', '   ', 'notes/empty.md')
        self.ingest('stopword-note', 'and the or is', 'notes/stopwords.md')
        # Querying should evaluate without raising ZeroDivisionError or ValueError
        results = self.sources('quota wasted', strict=True)
        self.assertIn('knowledge/goplus-quota.md', results)
        self.assertNotIn('notes/empty.md', results)
        self.assertNotIn('notes/stopwords.md', results)

    def test_strict_abstains_when_nothing_relates(self):
        result = self.store.retrieve('thanks that was great', strict=True)
        self.assertEqual(result['records'], [])
        self.assertTrue(result['abstained'])

    def test_explicit_retrieve_keeps_default_behaviour(self):
        self.assertEqual(self.sources('is the budget approved'), ['daily/2026-09-11.md'])


class StrictHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-strict-hook-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.state = root / 'state'
        home = root / 'home'
        home.mkdir()
        self.env = {'HOME': str(home), 'USERPROFILE': str(home), 'APPDATA': str(home / 'appdata'),
                    'LOCALAPPDATA': str(home / 'localappdata'), 'TEMP': str(root), 'TMP': str(root),
                    'PATH': os.defpath, 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1',
                    'BEYIN_V3_NO_SPAWN': '1'}
        for key in ('SYSTEMROOT', 'WINDIR'):
            if key in os.environ:
                self.env[key] = os.environ[key]
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        from beyin_v3_sync import SyncEngine
        note = self.vault / 'knowledge/goplus-quota.md'
        note.parent.mkdir(parents=True)
        note.write_text(SHORT_NOTE, encoding='utf-8')
        log = self.vault / 'daily/2026-09-11.md'
        log.parent.mkdir(parents=True)
        log.write_text(LONG_LOG, encoding='utf-8')
        SyncEngine(self.vault, self.state).sync()

    def invoke(self, prompt):
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'strict-session',
                   'event_id': 'strict-' + prompt[:8], 'cwd': str(self.vault), 'prompt': prompt}
        result = subprocess.run([sys.executable, str(HOOK), '--vault', str(self.vault), '--state', str(self.state),
                                 '--harness', 'claude'], input=json.dumps(payload), capture_output=True,
                                text=True, env=self.env, check=True)
        return json.loads(result.stdout)

    def test_prompt_without_meaningful_match_injects_nothing(self):
        self.assertEqual(self.invoke('thanks that was great'), {})
        self.assertEqual(self.invoke('is the budget approved'), {})

    def test_prompt_with_meaningful_match_injects_source_backed_context(self):
        context = self.invoke('why is the GoPlus quota wasted')['hookSpecificOutput']['additionalContext']
        self.assertIn('knowledge/goplus-quota.md', context)
        self.assertNotIn('daily/2026-09-11.md', context)


if __name__ == '__main__':
    unittest.main()
