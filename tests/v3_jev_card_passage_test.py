#!/usr/bin/env python3
"""Tests for Jev auto_context card passage extraction (Issue #507)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from beyin_v3 import MemoryStore
import beyin_v3_jev as advisor
import beyin_v3_jev_client as client


class JevCardPassageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-jev-card-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.store = MemoryStore(self.state, self.vault)
        self.addCleanup(lambda: getattr(self.store, 'close', lambda: None)())
        self.env = patch.dict(os.environ, {'TYPESAFE_API_KEY': 'synthetic-key'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls = []

    def record(self, ident, text, **extra):
        path = self.vault / extra.pop('source', ident + '.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return self.store.ingest(dict(id=ident, title=ident, text=text,
                                      source=path.relative_to(self.vault).as_posix(),
                                      visibility='internal', status='active', **extra))

    def transport(self, url, body, key, timeout):
        self.calls.append((body, timeout))
        notes = body['state']['notes']
        answers = {'topical': {'type': 'noul', 'noul': 0.9}}
        for key_, note in notes.items():
            answers[key_] = {'type': 'noul', 'noul': 0.85}
        return {'answers': answers, 'usage': {'input_tokens': 120}}

    def test_card_excerpt_extracts_matching_passage_from_long_note(self):
        filler = "Giris ve alakasiz aciklama satirlari.\n" * 40  # ~1500 chars of unrelated text
        target_section = (
            "## Postgres WAL Yapilandirmasi\n\n"
            "Postgres WAL streaming replikasyonu saniyede bir tetiklenir. "
            "KMS uzerinden anahtar rotasyonu guvenceye alinmistir.\n"
        )
        trailing = "Diger alakasiz notlar ve arsiv maddeleri.\n" * 30
        full_text = f"# Sistem Karar Gunlugu\n\n{filler}\n{target_section}\n{trailing}"

        rec = {'id': 'sys-decisions', 'text': full_text}
        query = 'Postgres WAL replikasyonu'

        excerpt = advisor.card_excerpt(rec, query)
        self.assertIn('Postgres WAL Yapilandirmasi', excerpt)
        self.assertNotIn('Giris ve alakasiz aciklama', excerpt)

    def test_auto_context_sends_matching_passage_for_loose_candidate_to_jev(self):
        client.set_mode(self.state, 'on', enable=['auto_context'])

        # Multiword query where this note shares only one term ('deploy'), placing it into loose pool
        query = 'Quartz deploy steps and pricing'

        filler = "Alakasiz ofis masasi ve sandalye alimlari.\n" * 45
        target = "## Geri Alma ve Dagitim\nUndo a release: revert the previous commit, then deploy again.\n"
        full_text = f"# Ofis Notlari\n\n{filler}\n{target}"
        self.record('rollback', full_text)

        context = self.store.context_for('claude', query, strict=True)
        # Verify note is not in strict results
        self.assertEqual(len(context['records']), 0)

        # Run auto_context
        advisor.auto_context(self.store, 'claude', query, context,
                             budget_chars=4000, transport=self.transport)

        self.assertEqual(len(self.calls), 1)
        notes = self.calls[0][0]['state']['notes']
        self.assertEqual(len(notes), 1)
        card = list(notes.values())[0]

        # The card excerpt delivered to Jev must show the deploy passage, not office furniture
        self.assertIn('Geri Alma ve Dagitim', card['excerpt'])
        self.assertIn('deploy again', card['excerpt'])
        self.assertNotIn('ofis masasi ve sandalye', card['excerpt'])

    def test_card_excerpt_preserves_short_note_and_strict_passage(self):
        # Short note
        short_rec = {'id': 'short', 'text': 'Kisa metin.', 'text_truncated': False}
        self.assertEqual(advisor.card_excerpt(short_rec, 'herhangi bir sorgu'), 'Kisa metin.')

        # Record with text_truncated=True already carries passage excerpt
        strict_rec = {'id': 'strict', 'text': '## Hedef Bolum\n\nPasaj metni.', 'text_truncated': True}
        self.assertEqual(advisor.card_excerpt(strict_rec, 'farkli sorgu'), '## Hedef Bolum\n\nPasaj metni.')


if __name__ == '__main__':
    unittest.main()
