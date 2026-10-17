"""Real committed C1 services with D planner/journal, not complete I1 acceptance."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_workspace_plan import plan_workspace, preview_workspace
from beyin_v3_provision import Provisioner, ProvisionConflict
import beyin_v3_provision as provision
import beyin_v3_workspaces as c1
import beyin_v3_builder as builder
from v3_workspace_spec_plan_test import fixture


def validate(document):
    c1.validate_document(document)  # C1 returns a document; D callback returns None.


class RealC1BuilderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'Vault'; self.state = self.root / 'State'
        self.source = self.root / 'Sources'; self.vault.mkdir(); self.source.mkdir()
        self.registry = c1.Registry(self.vault, self.state)
        self.auth = c1.LocalAuthority(self.registry)
        self.spec, _ = fixture()
        self.spec['data']['binding_requests'][0]['locator'] = str(self.source)
        self.plan = self.make_plan()
        self.engine = builder.provisioner(self.vault, self.state)

    def make_plan(self):
        return builder.plan(self.vault, self.state, self.spec, registry_id='fixture',
            capabilities={'pdf_text': 'ready', 'docx_text': 'ready'})[0]  # Synthetic; not C2 readiness.

    def approve(self):
        self.auth.record_approval('user-request', machine_id='fixture-machine', permissions=[{
            'workspace_id': 'custom', 'source_id': 'material', 'locator': str(self.source), 'access': 'read'}],
            evidence='Explicit synthetic grant')
        self.auth.approve_plan(self.plan, evidence='Synthetic consent to exact plan preview')

    def test_real_validation_approval_apply_replay_revert(self):
        self.assertFalse(self.state.exists())
        preview_workspace(self.plan, self.registry.capture_files(), validate_document=validate)
        self.assertFalse(self.state.exists())
        with self.assertRaises(c1.WorkspaceError): self.engine.apply(self.plan, 'real-c1')
        self.approve()
        result = self.engine.apply(self.plan, 'real-c1')
        self.assertEqual(result['data']['workspace_health'], 'verification_pending')
        self.assertEqual(self.registry.snapshot()['profiles'][0]['id'], 'custom')
        self.assertEqual(self.engine.apply(self.plan, 'real-c1')['data']['operation_status'], 'replayed')
        with self.assertRaisesRegex(c1.WorkspaceError, 'revert_authority_required'):
            self.engine.revert('real-c1')
        self.auth.approve_revert(self.plan, evidence='Synthetic explicit consent to old configuration/scope')
        self.engine.revert('real-c1')
        self.assertEqual(self.registry.capture_files(), {})

    def test_pending_reader_barrier_and_recovery_with_real_snapshot(self):
        self.approve()
        def crash(phase, index=None):
            if phase == 'after_replace' and index == 0: raise OSError('synthetic interruption')
        with patch.object(provision, 'transaction_hook', crash):
            with self.assertRaises(OSError): self.engine.apply(self.plan, 'pending-c1')
        with self.assertRaisesRegex(c1.WorkspaceError, 'operation_pending'): self.registry.snapshot()
        with self.assertRaisesRegex(c1.WorkspaceError, 'operation_pending'): self.registry.capture_files()
        self.assertEqual(self.engine.recover('pending-c1')['data']['workspace_health'], 'verification_pending')
        self.assertEqual(self.registry.snapshot()['profiles'][0]['id'], 'custom')

    def test_revoked_authority_and_unapproved_plan_change_cannot_write(self):
        self.approve()
        changed = copy.deepcopy(self.plan)
        changed['data']['verify_steps'].append('source_read')
        with self.assertRaisesRegex(c1.WorkspaceError, 'plan_changed'): self.engine.apply(changed, 'changed')
        self.auth.revoke('user-request')
        with self.assertRaises(c1.WorkspaceError): self.engine.apply(self.plan, 'denied')
        self.assertEqual(self.registry.capture_files(), {})

    def test_existing_profile_update_and_exact_before_snapshot(self):
        self.approve(); self.engine.apply(self.plan, 'first')
        self.spec['data']['desired_profile'].update(revision=2, purpose='Güncel amaç')
        self.plan = self.make_plan()
        self.auth.approve_plan(self.plan, evidence='Synthetic exact update preview')
        self.engine.apply(self.plan, 'second')
        self.assertEqual(self.registry.snapshot()['profiles'][0]['purpose'], 'Güncel amaç')
        self.auth.approve_revert(self.plan, evidence='Synthetic explicit consent to the previous profile')
        self.engine.revert('second')
        self.assertEqual(self.registry.snapshot()['profiles'][0]['revision'], 1)
