"""Opt-in capture agent: durable requests, bounded launch and observable completion.

The tests use real temporary cards and job files, but no model, desktop notification,
network request or agent CLI is started. Most process/Win32 boundaries are stand-ins;
dedicated tests launch two real POSIX workers and, on Windows, check native argv/aliases.
Run from the repository root: python -m unittest discover -s tests -p v3_yakala_ajan_test.py -v
"""


import base64


import contextlib


import ctypes as C


import hashlib


import io


import json


import os


from pathlib import Path


import subprocess


import socket


import sys


import tempfile


import threading


import time


import types


import unittest


from unittest import mock


from unittest.mock import Mock, patch


import uuid


ROOT = Path(__file__).resolve().parents[1]


SCRIPTS = ROOT / 'template/.claude/scripts'


sys.path.insert(0, str(SCRIPTS))


import beyin_v3_yakala as yakala  # noqa: E402


def _host_lock_for_simulated_linux():
    """The popup is simulated as Linux; on a Windows host flock is missing, so a plain lock stands in."""
    if os.name != 'nt':
        return contextlib.nullcontext()
    locks, guard = {}, threading.Lock()

    class HostLock:
        def __init__(self, path):
            with guard:
                self.lock = locks.setdefault(str(path), threading.RLock())

        def __enter__(self):
            self.lock.acquire()
            return self

        def __exit__(self, *_):
            self.lock.release()

    enqueue = yakala.enqueue_agent

    def host_enqueue(*args, **kwargs):
        # Queue files are host files: a simulated Linux would fsync directories Windows refuses to open.
        with patch.object(yakala, 'os', os), patch.object(yakala, 'sys', sys):
            return enqueue(*args, **kwargs)
    stack = contextlib.ExitStack()
    stack.enter_context(patch.object(yakala, '_AgentFileLock', HostLock))
    stack.enter_context(patch.object(yakala, 'enqueue_agent', host_enqueue))
    return stack


class AgentManualProcessRaceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yakala-manual-agent-race-')
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / 'Örnek Vault'
        self.state = Path(temporary.name) / 'state'
        self.vault.mkdir()
        result = yakala.capture(self.vault, text='Original source text', why='Initial reason', state=self.state)
        self.card_id, self.card_path = result['id'], self.vault / result['path']
        self.raw_path = self.card_path.parent / '.ham' / (self.card_id + '.md')

    def claim(self):
        meta, body = yakala.read_card(self.card_path)
        meta.update(durum='ajanda', ajan_isi='8ed4d7b8-01bf-49fb-8d5b-b7e1eebc8ac6', ajan_asama='sirada')
        yakala.write_card(self.card_path, meta, body + '\n\n## New reason\nA newer captured request.\n')

    def test_claim_between_target_listing_and_snapshot_is_not_erased(self):
        original_find = yakala.find_card

        def claim_after_listing(vault, card_id, state=None):
            old_snapshot = original_find(vault, card_id, state)
            self.claim()
            return old_snapshot

        with patch.object(yakala, 'find_card', side_effect=claim_after_listing), \
                patch.object(yakala, 'tools', return_value={}), \
                patch.object(yakala, 'extract', return_value=('New extracted text', 'test', None)) as extract:
            result = yakala.process(self.vault, [self.card_id], state=self.state)
        meta, body = yakala.read_card(self.card_path)
        self.assertEqual(meta['durum'], 'ajanda')
        self.assertEqual(meta['ajan_isi'], '8ed4d7b8-01bf-49fb-8d5b-b7e1eebc8ac6')
        self.assertIn('A newer captured request.', body)
        self.assertEqual(result['kartlar'][0]['durum'], 'ajanda')
        extract.assert_not_called()
        self.assertFalse(self.raw_path.exists())

    def test_claim_during_extraction_keeps_both_card_and_existing_raw_source(self):
        self.raw_path.parent.mkdir(parents=True)
        original_raw = b'Existing source already available to the newly claimed background job.\n'
        self.raw_path.write_bytes(original_raw)

        def claim_during_extract(*args, **kwargs):
            self.claim()
            return 'A late extraction that may no longer commit', 'test', None

        with patch.object(yakala, 'tools', return_value={}), \
                patch.object(yakala, 'extract', side_effect=claim_during_extract):
            result = yakala.process(self.vault, [self.card_id], state=self.state)
        meta, body = yakala.read_card(self.card_path)
        self.assertEqual(meta['durum'], 'ajanda')
        self.assertEqual(meta['ajan_isi'], '8ed4d7b8-01bf-49fb-8d5b-b7e1eebc8ac6')
        self.assertIn('A newer captured request.', body)
        self.assertEqual(self.raw_path.read_bytes(), original_raw)
        self.assertEqual(result['kartlar'][0]['durum'], 'hata')
        self.assertIn('degisti', result['kartlar'][0]['not'])

    def test_unchanged_source_still_commits_raw_text_and_card(self):
        with patch.object(yakala, 'tools', return_value={}), \
                patch.object(yakala, 'extract', return_value=('Expected extracted source', 'test', None)):
            result = yakala.process(self.vault, [self.card_id], state=self.state)
        meta, _body = yakala.read_card(self.card_path)
        self.assertEqual(meta['durum'], 'cikarildi')
        self.assertEqual(result['kartlar'][0]['durum'], 'cikarildi')
        self.assertIn('Expected extracted source', self.raw_path.read_text(encoding='utf-8'))

    def test_snapshot_preserves_metadata_and_body_for_all_newlines(self):
        original = self.card_path.read_bytes()
        for newline in (b'\n', b'\r\n', b'\r', b'mixed'):
            with self.subTest(newline=newline):
                encoded = (b''.join(line + (b'\r', b'\r\n', b'\n')[i % 3]
                                    for i, line in enumerate(original.split(b'\n')))
                           if newline == b'mixed' else original.replace(b'\n', newline))
                self.card_path.write_bytes(encoded)
                before_meta, before_body = yakala.read_card(self.card_path)
                def extract(_vault, card, *_args):
                    self.assertEqual(card['meta'], before_meta)
                    self.assertEqual(card['body'], before_body)
                    return 'Expected extracted text', 'test', None
                with patch.object(yakala, 'tools', return_value={}), \
                        patch.object(yakala, 'extract', side_effect=extract):
                    result = yakala.process(self.vault, [self.card_id], state=self.state)
                after, body = yakala.read_card(self.card_path)
                for field in ('tur', 'baslik', 'visibility', 'kaynak_turu'):
                    self.assertEqual(after[field], before_meta[field])
                self.assertEqual(after['visibility'], 'private')
                self.assertEqual(yakala.read_card(self.raw_path)[0]['visibility'], 'private')
                self.assertIn('Original source text', body)
                self.assertIn('Initial reason', body)
                self.assertEqual(result['kartlar'][0]['durum'], 'cikarildi')

    def test_normalization_does_not_hide_a_concurrent_newline_only_edit(self):
        original = self.card_path.read_bytes()
        for newline, changed_newline in ((b'\n', b'\r'), (b'\r', b'\r\n'), (b'\r\n', b'\n')):
            with self.subTest(newline=newline):
                self.card_path.write_bytes(original.replace(b'\n', newline))
                changed = original.replace(b'\n', changed_newline)
                self.raw_path.parent.mkdir(parents=True, exist_ok=True)
                self.raw_path.write_bytes(b'Existing raw source')
                def extract(*_args):
                    self.card_path.write_bytes(changed)
                    return 'Late extraction', 'test', None
                with patch.object(yakala, 'tools', return_value={}), \
                        patch.object(yakala, 'extract', side_effect=extract):
                    result = yakala.process(self.vault, [self.card_id], state=self.state)
                self.assertEqual(result['kartlar'][0]['durum'], 'hata')
                self.assertIn('degisti', result['kartlar'][0]['not'])
                self.assertEqual(self.card_path.read_bytes(), changed)
                self.assertEqual(self.raw_path.read_bytes(), b'Existing raw source')


if __name__ == '__main__':
    unittest.main()
