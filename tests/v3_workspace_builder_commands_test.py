"""D's command boundary uses actual C1 authority and installed reader probes."""
import copy
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

import v3_workspace_builder_c1_test as c1_tests
from beyin_v3_parser_assets import managed_files
from beyin_v3_builder import execute, _failure
from beyin_v3_workspaces import validate_document
import beyin_v3_provision as provision


class BuilderCommandsTest(unittest.TestCase):
    def setUp(self):
        self.h = c1_tests.RealC1BuilderTest()
        self.h.setUp()
        self.addCleanup(self.h.tmp.cleanup)

    def install_assets(self):
        for name, raw in managed_files().items():
            path = self.h.vault / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)

    def call(self, action, payload):
        return execute(self.h.vault, self.h.state, action, payload)

    def test_plan_probes_real_readers_and_preview_is_read_only(self):
        self.install_assets()
        before = {str(p): p.read_bytes() for p in self.h.vault.rglob('*') if p.is_file()}
        result, code = self.call('plan', {'spec': self.h.spec, 'registry_id': 'fixture'})
        self.assertEqual(code, 0)
        self.assertEqual(result['capabilities']['pdf_text'], 'ready')
        self.assertEqual(result['capabilities']['docx_text'], 'ready')
        self.assertFalse(result['plan']['data']['unresolved'])
        preview, code = self.call('preview', {'plan': result['plan']})
        self.assertEqual(code, 0)
        self.assertEqual(preview, result['preview'])
        self.assertFalse(self.h.state.exists())
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.h.vault.rglob('*') if p.is_file()})

    def test_missing_dependency_stays_unresolved_and_cannot_be_forged(self):
        result, code = self.call('plan', {'spec': self.h.spec, 'registry_id': 'fixture'})
        self.assertEqual(code, 0)
        self.assertIn('missing_capability:pdf_text', result['plan']['data']['unresolved'])
        self.assertFalse(self.h.state.exists())
        error, code = self.call('plan', {'spec': self.h.spec, 'registry_id': 'fixture', 'capabilities': {'pdf_text': 'ready'}})
        self.assertEqual(code, 2)
        validate_document(error)
        self.assertFalse(self.h.state.exists())

    def test_apply_replay_and_revert_need_exact_c1_authority(self):
        payload = {'plan': self.h.plan, 'idempotency_key': 'command-test'}
        result, code = self.call('apply', payload)
        self.assertEqual(code, 4)
        self.assertEqual(result['data']['operation_status'], 'denied')
        self.h.approve()
        result, code = self.call('apply', payload)
        self.assertEqual(code, 5)
        self.assertEqual(result['data']['operation_status'], 'applied')
        result, code = self.call('apply', payload)
        self.assertEqual(code, 5)
        self.assertEqual(result['data']['operation_status'], 'replayed')
        result, code = self.call('revert', {'idempotency_key': 'command-test'})
        self.assertEqual(code, 4)
        self.h.auth.approve_revert(self.h.plan, evidence='Synthetic explicit undo scope approval')
        result, code = self.call('revert', {'idempotency_key': 'command-test'})
        self.assertEqual(code, 0)
        self.assertEqual(result['data']['workspace_health'], 'not_tested')
        self.assertFalse(self.h.registry.capture_files())

    def test_interruption_recovery_and_stale_preview_have_typed_results(self):
        self.h.approve()
        def crash(phase, index=None):
            if phase == 'after_replace' and index == 0:
                raise OSError('PRIVATE_SOURCE_PATH_AND_TEXT')
        with patch.object(provision, 'transaction_hook', crash):
            error, code = self.call('apply', {'plan': self.h.plan, 'idempotency_key': 'interrupted'})
        self.assertEqual(code, 5)
        self.assertNotIn('PRIVATE_SOURCE', str(error))
        result, code = self.call('recover', {'idempotency_key': 'interrupted'})
        self.assertEqual(code, 5)
        self.assertEqual(result['data']['operation_status'], 'applied')
        error, code = self.call('preview', {'plan': self.h.plan})
        self.assertEqual(code, 3)
        self.assertEqual(error['data']['issues'][0]['code'], 'preview_precondition_changed')

    def test_invalid_action_and_raw_exception_are_bounded(self):
        for action, payload in [('other', {}), ('recover', {}), ('plan', {'spec': {}, 'registry_id': 'fixture'})]:
            result, code = self.call(action, payload)
            self.assertEqual(code, 2)
            validate_document(result)
        error, code = _failure(ValueError('private/path SECRET raw document text'))
        self.assertEqual(code, 2)
        self.assertNotIn('SECRET', str(error))

    def test_actual_updater_lock_contention_is_retryable_not_invalid_input(self):
        self.h.approve()
        db = sqlite3.connect(self.h.state / 'update-lock.sqlite3')
        try:
            db.execute('BEGIN IMMEDIATE')
            result, code = self.call('apply', {'plan': self.h.plan, 'idempotency_key': 'busy-command'})
        finally:
            db.close()
        self.assertEqual(code, 3)
        self.assertEqual(result['data']['operation_status'], 'conflict')
        self.assertEqual(result['data']['issues'], [{'code': 'busy'}])
        self.assertFalse(self.h.registry.capture_files())
        self.assertFalse((self.h.state / 'workspace-provision/pending.json').exists())
