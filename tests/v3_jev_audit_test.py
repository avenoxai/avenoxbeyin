"""Auto_context per-note audit logging tests."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))

import beyin_v3 as runtime
import beyin_v3_jev as advisor
import beyin_v3_jev_client as client

spec_cli = importlib.util.spec_from_file_location('beyin_v3_cli', str(ROOT / 'scripts/beyin_v3.py'))
beyin_cli = importlib.util.module_from_spec(spec_cli)
spec_cli.loader.exec_module(beyin_cli)

spec_entry = importlib.util.spec_from_file_location('beyin_entry_cli', str(ROOT / 'scripts/beyin_entry.py'))
entry = importlib.util.module_from_spec(spec_entry)
spec_entry.loader.exec_module(entry)


class JevAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.store = runtime.MemoryStore(root / 'state', self.vault)
        self.state = self.store.state_dir
        self.env = patch.dict(os.environ, {'TYPESAFE_API_KEY': 'synthetic-key'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.record('pricing', 'Quartz pricing: deploy the yearly plan at ninety dollars.')
        self.record('deploy', 'Quartz deploy steps: push to main, the site server pulls.')
        self.record('rollback', 'Undo a release: revert the previous commit, then deploy again.')

    def record(self, ident, text, **extra):
        path = self.vault / extra.pop('source', ident + '.md')
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return self.store.ingest(dict(id=ident, title=ident, text=text, source=path.relative_to(self.vault).as_posix(), **extra))

    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                res, code = beyin_cli.main(['--vault', str(self.vault), '--state', str(self.state), *args], return_result=True)
            except SystemExit as e:
                res, code = e.code, 1
        return res, buf.getvalue()

    def test_default_audit_config_and_status(self):
        cfg = advisor.load_audit_config(self.state)
        self.assertFalse(cfg['enabled'])
        self.assertTrue(cfg['include_scores'])
        self.assertTrue(cfg['include_topical'])

        res, out = self.run_cli('jev-audit', 'status')
        self.assertEqual(res['audit']['enabled'], False)
        self.assertIn('Jev auto_context denetimi: kapali', entry.human_result(res, 'jev-audit'))

    def test_audit_configuration_toggles(self):
        res, _ = self.run_cli('jev-audit', 'on', '--no-include-scores', '--no-include-topical')
        self.assertTrue(res['audit']['enabled'])
        self.assertFalse(res['audit']['include_scores'])
        self.assertFalse(res['audit']['include_topical'])

        res, out = self.run_cli('jev-audit', 'off')
        self.assertFalse(res['audit']['enabled'])
        self.assertIn('Jev auto_context denetimi devre disi birakildi', entry.human_result(res, 'jev-audit'))

    def test_audit_logging_behavior(self):
        # 1. Audit disabled -> no log created
        client.set_mode(self.state, 'on', enable=['auto_context'])
        answers = {'topical': 0.9, 'deploy': 0.9, 'pricing': 0.05, 'rollback': 0.9}
        def transport(url, body, key, timeout):
            notes = body['state']['notes']
            out = {'topical': {'type': 'noul', 'noul': answers['topical']}}
            for k, note in notes.items():
                out[k] = {'type': 'noul', 'noul': answers.get(note['title'], 0.9)}
            return {'answers': out, 'usage': {'input_tokens': 100}}

        query = 'how do we deploy the quartz site'
        ctx = self.store.context_for('claude', query, strict=True, budget_chars=6000)
        with patch('beyin_v3_jev_client._transport', transport):
            advisor.auto_context(self.store, 'claude', query, ctx, budget_chars=6000, transport=transport)
        log_path = advisor._audit_log_path(self.state)
        self.assertFalse(log_path.exists())

        # 2. Audit enabled -> writes jsonl with required fields (kept, dropped, rescued), omits prompt/body
        self.run_cli('jev-audit', 'on')
        shutil.rmtree(self.state / '.cache', ignore_errors=True)
        ctx = self.store.context_for('claude', query, strict=True, budget_chars=6000)
        with patch('beyin_v3_jev_client._transport', transport):
            advisor.auto_context(self.store, 'claude', query, ctx, budget_chars=6000, transport=transport)

        self.assertTrue(log_path.exists())
        lines = log_path.read_text(encoding='utf-8').strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry_data = json.loads(lines[0])

        self.assertIn('at', entry_data)
        self.assertIn('query_hash', entry_data)
        self.assertEqual(entry_data['mode'], 'on')
        self.assertTrue(entry_data['gate_passed'])
        self.assertIsNotNone(entry_data['topical_score'])
        self.assertIn('notes', entry_data)
        self.assertGreater(len(entry_data['notes']), 0)
        outcomes = {n['source_id']: n['outcome'] for n in entry_data['notes']}
        self.assertEqual(outcomes, {'deploy': 'kept', 'pricing': 'dropped', 'rollback': 'rescued'})

        for n in entry_data['notes']:
            self.assertIn('score', n)
            self.assertIn('kept', n)
            self.assertIn('outcome', n)

        # Ensure no query text or note body is leaked in the log entry
        raw_log = lines[0]
        self.assertNotIn('how do we deploy the quartz site', raw_log)
        self.assertNotIn('Quartz deploy steps', raw_log)
        self.assertNotIn('Quartz pricing', raw_log)


if __name__ == '__main__':
    unittest.main()
