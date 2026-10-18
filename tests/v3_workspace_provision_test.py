"""Actual config IO/faults against service doubles. Not C1/C3/C4 or I1 acceptance."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_provision as provision
import beyin_v3_update as updater
from beyin_v3_workspace_plan import canonical_bytes, content_hash, plan_workspace
from v3_workspace_spec_plan_test import fixture, snapshots


class Harness:
    def __init__(self, root, initialize=True):
        self.root = Path(root)
        self.vault, self.state = self.root / 'Türkçe Vault', self.root / 'Yerel durum'
        self.vault.mkdir(exist_ok=True); self.state.mkdir(exist_ok=True)
        self.spec, self.originals = fixture()
        if initialize:
            for (scope, name), raw in self.originals.items():
                path = (self.vault if scope == 'vault' else self.state) / name
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        registry, binding_hash = snapshots(self.originals)
        self.plan = plan_workspace(self.spec, registry, binding_hash, self.originals,
            validate_document=self.validate, capabilities={'pdf_text': 'ready', 'docx_text': 'ready'})
        self.revoked = False
        self.revert_allowed = True
        self.actions = []
        self.engine = provision.Provisioner(self.vault, self.state,
            validate_document=self.validate, check_authority=self.authority, check_snapshot=self.snapshot)

    def validate(self, document):
        if document.get('contract_version') != 'workspace/0.1.0' or not isinstance(document.get('data'), dict):
            raise ValueError('mock_invalid_schema')

    def authority(self, document, action):
        self.actions.append(action)
        if (self.revoked or document['data']['plan_sha256'] != self.plan['data']['plan_sha256']
                or action == 'revert' and not self.revert_allowed):
            raise PermissionError('scope_denied')

    def read_files(self):
        files = {}
        for scope, root in [('vault', self.vault), ('state', self.state)]:
            for path in root.rglob('*'):
                if path.is_file() and (scope == 'vault' or path.relative_to(root).as_posix().startswith('workspace-bindings/')):
                    files[(scope, path.relative_to(root).as_posix())] = path.read_bytes()
        return files

    def snapshot(self, document, observations):
        # C1 boundary double: normalize already-owned writes to the initial snapshot.
        files = self.read_files()
        for item in observations:
            target = item['target']; key = (target['root'], target['relative_path'])
            if item['current_sha256'] == item['after_sha256']:
                if key in self.originals: files[key] = self.originals[key]
                else: files.pop(key, None)
        registry, binding_hash = snapshots(files)
        if (registry['data']['snapshot_sha256'] != document['data']['snapshot_sha256']
                or binding_hash != document['data']['binding_snapshot_sha256']):
            raise provision.ProvisionConflict('snapshot_conflict')

    def assert_applied(self, test):
        for item in self.plan['data']['operations']:
            root = self.vault if item['target']['root'] == 'vault' else self.state
            test.assertEqual(content_hash((root / item['target']['relative_path']).read_bytes()), item['after_sha256'])
        test.assertEqual((self.vault / 'Core.md').read_bytes(), b'UNCHANGED_USER_IDENTITY')
        test.assertEqual((self.vault / 'notes/user.md').read_bytes(), b'user prose')
        test.assertFalse((self.state / 'workspace-provision/pending.json').exists())

    def child(self, phase, index, action='apply', *, crash=True):
        code = '''
import os,sys
sys.path.insert(0, sys.argv[1])
from v3_workspace_provision_test import Harness, provision
h=Harness(sys.argv[2],initialize=False)
phase=sys.argv[3]; index=None if sys.argv[4]=='none' else int(sys.argv[4])
def kill(actual, step=None):
    if actual==phase and step==index: os._exit(93)
provision.transaction_hook=kill
action=sys.argv[5]
if action=='apply': h.engine.apply(h.plan,'same-operation')
elif action=='recover': h.engine.recover('same-operation')
else: h.engine.revert('same-operation')
'''
        result = subprocess.run([sys.executable, '-B', '-c', code, str(ROOT / 'tests'), str(self.root),
            phase, 'none' if index is None else str(index), action], capture_output=True, timeout=30)
        if crash:
            if result.returncode != 93:
                raise AssertionError(result.stderr.decode(errors='replace'))
        elif result.returncode:
            raise AssertionError(result.stderr.decode(errors='replace'))
        return result


class ProvisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.h = Harness(self.tmp.name)

    def apply(self):
        return self.h.engine.apply(self.h.plan, 'same-operation')

    def test_apply_replay_revert_preserve_user_data_and_pending_health(self):
        result = self.apply()
        self.assertEqual(result['data']['operation_status'], 'applied')
        self.assertEqual(result['data']['workspace_health'], 'verification_pending')
        self.h.assert_applied(self)
        self.assertEqual(self.apply()['data']['operation_status'], 'replayed')
        self.assertEqual(len(list((self.h.state / 'workspace-provision').glob('*.json'))), 1)
        reverted = self.h.engine.revert('same-operation')
        self.assertEqual(reverted['data']['workspace_health'], 'not_tested')
        self.assertEqual(self.h.read_files(), self.h.originals)
        self.assertEqual(self.h.engine.revert('same-operation')['data']['operation_status'], 'replayed')
        with self.assertRaisesRegex(provision.ProvisionConflict, 'operation_already_reverted'):
            self.apply()

    def test_update_revert_restores_exact_crlf_bytes_and_mode(self):
        self.apply()
        profile = self.h.vault / '.beyin-workspaces/workspaces/custom.json'
        old = profile.read_bytes().replace(b'\n', b'\r\n')
        profile.write_bytes(old)
        profile.chmod(0o640)
        self.h.originals = self.h.read_files()
        self.h.spec['data']['desired_profile'].update(revision=2, name='Geliştirilmiş alan')
        registry, binding_hash = snapshots(self.h.originals)
        self.h.plan = plan_workspace(self.h.spec, registry, binding_hash, self.h.originals,
            validate_document=self.h.validate, capabilities={'pdf_text': 'ready', 'docx_text': 'ready'})
        result = self.h.engine.apply(self.h.plan, 'update-operation')
        self.assertEqual(result['data']['operation_status'], 'applied')
        self.assertNotEqual(profile.read_bytes(), old)
        self.h.engine.revert('update-operation')
        self.assertEqual(profile.read_bytes(), old)
        if os.name != 'nt':
            self.assertEqual(profile.stat().st_mode & 0o777, 0o640)

    def test_revert_rechecks_complete_snapshot_before_removing_shared_config(self):
        self.apply()
        from beyin_v3_workspace_plan import _document_bytes, _envelope
        path = self.h.vault / '.beyin-workspaces/sources/added-later.json'
        path.write_bytes(_document_bytes(_envelope('SourceDescriptor',
            {'id': 'added-later', 'kind': 'directory', 'label': 'Later user connection'})))
        before = self.h.read_files()
        with self.assertRaisesRegex(provision.ProvisionConflict, 'snapshot_conflict'):
            self.h.engine.revert('same-operation')
        self.assertEqual(self.h.read_files(), before)

    def test_corrupt_journal_mode_or_checkpoints_fail_before_revert_writes(self):
        self.apply()
        path = next((self.h.state / 'workspace-provision').glob('*.json'))
        original = json.loads(path.read_bytes())
        before = self.h.read_files()
        for mutation in ('mode', 'intended'):
            document = copy.deepcopy(original)
            if mutation == 'mode':
                document['originals'][self.h.plan['data']['operations'][0]['id']]['mode'] = 'invalid'
            else:
                document['intended'].append('unknown-operation')
            path.write_bytes(canonical_bytes(document))
            with self.assertRaisesRegex(provision.ProvisionConflict, 'invalid_provision_journal'):
                self.h.engine.revert('same-operation')
            self.assertEqual(self.h.read_files(), before)

    @unittest.skipIf(os.name == 'nt', 'POSIX mode CAS; native Windows attributes are separate')
    def test_later_user_chmod_is_preserved_on_revert(self):
        self.apply()
        path = self.h.vault / '.beyin-workspaces/workspaces/custom.json'
        path.chmod(0o640)
        before = self.h.read_files()
        with self.assertRaisesRegex(provision.ProvisionConflict, 'revert_user_mode_conflict'):
            self.h.engine.revert('same-operation')
        self.assertEqual(self.h.read_files(), before)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_fresh_process_kill_and_recover_at_every_durable_apply_step(self):
        phases = [('journal_created', None), ('pending_published', None),
                  ('applied_checkpoint', None), ('pending_cleared', None)]
        phases += [(phase, index) for index in range(3) for phase in ('step_intent', 'after_replace', 'step_checkpoint')]
        for number, (phase, index) in enumerate(phases):
            with self.subTest(phase=phase, index=index):
                root = self.h.root / str(number); root.mkdir()
                harness = Harness(root)
                harness.child(phase, index)
                harness.child('never', None, 'recover', crash=False)
                harness.assert_applied(self)

    def test_fresh_process_revert_restart_at_each_checkpoint(self):
        phases = [('revert_started', None), ('reverted_checkpoint', None)]
        phases += [(phase, index) for index in range(3) for phase in ('revert_intent', 'revert_replace')]
        for number, (phase, index) in enumerate(phases):
            with self.subTest(phase=phase, index=index):
                root = self.h.root / ('revert-' + str(number)); root.mkdir()
                harness = Harness(root)
                harness.engine.apply(harness.plan, 'same-operation')
                harness.child(phase, index, 'revert')
                harness.child('never', None, 'revert', crash=False)
                self.assertEqual(harness.read_files(), harness.originals)
                self.assertFalse((harness.state / 'workspace-provision/pending.json').exists())

    def test_changed_plan_snapshot_and_unapproved_authority_do_not_apply(self):
        mutated = copy.deepcopy(self.h.plan)
        mutated['data']['operations'][0]['content_utf8'] += ' '
        with self.assertRaisesRegex(provision.ProvisionConflict, 'plan_changed'):
            self.h.engine.apply(mutated, 'same-operation')
        self.h.revoked = True
        with self.assertRaises(PermissionError): self.apply()
        self.assertFalse((self.h.state / 'workspace-provision').exists())
        self.h.revoked = False
        path = self.h.vault / '.beyin-workspaces/format.json'; path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(provision.ProvisionConflict, 'snapshot_conflict'): self.apply()

    def test_idempotency_collision_and_replay_never_reuse_old_health(self):
        self.apply()
        other = copy.deepcopy(self.h.plan); other['data']['required_authority'].append('different-scope')
        other['data']['plan_sha256'] = content_hash(canonical_bytes({k:v for k,v in other['data'].items()
            if k not in ('plan_id', 'plan_sha256')}))
        with self.assertRaisesRegex(provision.ProvisionConflict, 'idempotency_collision'):
            self.h.engine.apply(other, 'same-operation')
        operation = self.h.plan['data']['operations'][-1]
        path = self.h.vault / operation['target']['relative_path']; path.write_bytes(b'USER EDIT')
        with self.assertRaisesRegex(provision.ProvisionConflict, 'replay_source_changed'): self.apply()
        with self.assertRaisesRegex(provision.ProvisionConflict, 'revert_user_edit_conflict'):
            self.h.engine.revert('same-operation')
        self.assertEqual(path.read_bytes(), b'USER EDIT')

    def test_edit_between_intent_and_replace_is_preserved(self):
        operation = self.h.plan['data']['operations'][0]
        path = self.h.vault / operation['target']['relative_path']
        def edit(phase, index=None):
            if phase == 'before_replace' and index == 0:
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'USER EDIT')
        with patch.object(provision, 'transaction_hook', edit):
            with self.assertRaisesRegex(provision.ProvisionConflict, 'source_hash_conflict'): self.apply()
        self.assertEqual(path.read_bytes(), b'USER EDIT')

    def test_identical_future_file_without_durable_intent_is_not_owned(self):
        def crash(phase, index=None):
            if phase == 'journal_created': raise OSError('synthetic interruption')
        with patch.object(provision, 'transaction_hook', crash):
            with self.assertRaises(OSError): self.apply()
        operation = self.h.plan['data']['operations'][0]
        path = self.h.vault / operation['target']['relative_path']
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(operation['content_utf8'])
        with self.assertRaises(provision.ProvisionConflict): self.h.engine.recover('same-operation')
        with self.assertRaises(provision.ProvisionConflict): self.h.engine.revert('same-operation')
        self.assertTrue(path.exists())

    def test_disk_and_permission_failures_leave_recoverable_separate_journal(self):
        original_atomic = updater.atomic
        for exception in (OSError('synthetic disk full'), PermissionError('synthetic denied')):
            root = self.h.root / type(exception).__name__; root.mkdir()
            harness = Harness(root)
            def fail(path, raw):
                if Path(path).parts[-2:] == ('sources', 'material.json'): raise exception
                return original_atomic(path, raw)
            with patch.object(updater, 'atomic', fail):
                with self.assertRaises(type(exception)):
                    harness.engine.apply(harness.plan, 'same-operation')
            self.assertTrue((harness.state / 'workspace-provision/pending.json').exists())
            self.assertFalse((harness.state / 'update-journal.json').exists())
            harness.engine.recover('same-operation'); harness.assert_applied(self)

    def test_other_builder_updater_and_pending_barrier(self):
        def crash(phase, index=None):
            if phase == 'pending_published': raise OSError('synthetic interruption')
        with patch.object(provision, 'transaction_hook', crash):
            with self.assertRaises(OSError): self.apply()
        with self.assertRaisesRegex(provision.ProvisionConflict, 'operation_pending'):
            self.h.engine.apply(self.h.plan, 'other-operation')
        with self.assertRaisesRegex(ValueError, 'workspace provisioning pending'):
            with updater.locked(self.h.vault, self.h.state): pass
        self.h.engine.recover('same-operation')
        with updater.locked(self.h.vault, self.h.state): pass

    def test_updater_journal_blocks_new_provisioning(self):
        (self.h.state / 'update-journal.json').write_text('{}')
        with self.assertRaisesRegex(provision.ProvisionConflict, 'updater_operation_pending'): self.apply()
        self.assertFalse((self.h.state / 'workspace-provision').exists())

    def test_revoked_authority_blocks_recover_and_revert(self):
        self.apply(); self.h.revoked = True
        with self.assertRaises(PermissionError): self.h.engine.recover('same-operation')
        with self.assertRaises(PermissionError): self.h.engine.revert('same-operation')
        self.h.revoked = False; self.h.revert_allowed = False
        with self.assertRaises(PermissionError): self.h.engine.revert('same-operation')
        self.h.assert_applied(self)

    def test_redirected_config_target_is_rejected(self):
        outside = self.h.root / 'outside'; outside.mkdir()
        link = self.h.vault / '.beyin-workspaces/sources'
        try: link.symlink_to(outside, target_is_directory=True)
        except OSError: self.skipTest('symlink unavailable')
        with self.assertRaisesRegex(provision.ProvisionConflict, 'redirected_provision_target'): self.apply()
        self.assertFalse(list(outside.iterdir()))

    def test_service_false_is_denial_and_corrupt_backup_is_preserved(self):
        engine = provision.Provisioner(self.h.vault, self.h.state, validate_document=self.h.validate,
            check_authority=lambda *args: False, check_snapshot=self.h.snapshot)
        with self.assertRaises(PermissionError): engine.apply(self.h.plan, 'no-authority')
        self.apply()
        journal = next(path for path in (self.h.state / 'workspace-provision').glob('*.json') if path.name != 'pending.json')
        content = json.loads(journal.read_bytes())
        content['originals']['source-material']['content'] = base64.b64encode(b'tampered').decode()
        journal.write_text(json.dumps(content))
        before = self.h.read_files()
        with self.assertRaisesRegex(provision.ProvisionConflict, 'journal_backup_changed'):
            self.h.engine.revert('same-operation')
        self.assertEqual(self.h.read_files(), before)

    def test_real_second_process_updater_lock_serializes_builder(self):
        ready = self.h.root / 'holder-ready'
        code = '''
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import beyin_v3_update as updater
with updater.locked(Path(sys.argv[2]),Path(sys.argv[3])):
    Path(sys.argv[4]).write_text('ready')
    sys.stdin.read()
'''
        holder = subprocess.Popen([sys.executable, '-B', '-c', code, str(ROOT / 'template/.claude/scripts'),
            str(self.h.vault), str(self.h.state), str(ready)], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            with self.assertRaisesRegex(ValueError, 'busy'): self.apply()
            self.assertFalse((self.h.state / 'workspace-provision').exists())
        finally:
            holder.communicate(timeout=10)


if __name__ == '__main__':
    unittest.main()
