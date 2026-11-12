"""Evidence transport regressions. Entirely synthetic and offline."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as rt
import beyin_v3_continuity as continuity
import beyin_v3_dedupe as dedupe
import beyin_v3_jev as jev
import beyin_v3_jev_client as client


class EvidenceFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='v3-evidence-')
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / 'vault'
        self.vault.mkdir()
        self.store = rt.MemoryStore(Path(temporary.name) / 'state', self.vault)
        self.calls = []

    def note(self, ident, text, **fields):
        path = self.vault / fields.pop('source', ident + '.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return self.store.ingest(dict(id=ident, title=ident, source=path.relative_to(self.vault).as_posix(), text=text, **fields))

    def local(self, query, budget=5000):
        return self.store.context_for('claude', query, strict=True, budget_chars=budget)

    def judge(self, query, local, marker):
        client.set_mode(self.store.state_dir, 'on', enable=['auto_context'])
        def transport(url, body, key, timeout):
            self.calls.append(body)
            answers = {'topical': {'type': 'noul', 'noul': .99}}
            for key, note in body['state']['notes'].items():
                answers[key] = {'type': 'noul', 'noul': .99 if marker in note['excerpt'] else .01}
            return {'answers': answers}
        with patch.dict(os.environ, {'TYPESAFE_API_KEY': 'synthetic'}, clear=True):
            result = jev.auto_context(self.store, 'claude', query, local, budget_chars=5000, transport=transport)
        self.assertTrue(self.calls)
        for note in self.calls[-1]['state']['notes'].values():
            self.assertLessEqual(len(note['excerpt']), 600)
        return result
