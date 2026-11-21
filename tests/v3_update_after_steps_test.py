"""Tests for condition-filtered after-update steps (#784)."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

from v3_package_helpers import ROOT, build_package, install, isolated_env, rewrite_zip, snapshot

MODULE = ROOT / 'template/.claude/scripts/beyin_v3_update.py'


def load_updater():
    spec = importlib.util.spec_from_file_location('beyin_v3_update_after_test_subject', MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class UpdateAfterStepsTest(unittest.TestCase):
    def setUp(self):
        self.module = load_updater()
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-after-test-')
        self.base = Path(self.tmp.name)
        self.vault = self.base / 'Vault'
        self.vault.mkdir(parents=True, exist_ok=True)
        self.state = self.base / 'runtime'
        self.env = isolated_env(self.base / 'home')
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.package = build_package(self.base / 'release.zip', '3.0.1', self.env)

    def tearDown(self):
        self.tmp.cleanup()

    def test_after_update_steps_filtered_by_conditions(self):
        steps = [
            {'id': 'step-always', 'text': 'Always run step', 'condition': 'always'},
            {'id': 'step-yakala', 'text': 'Yakala installed step', 'condition': 'yakala_installed'},
            {'id': 'step-git', 'text': 'Vault in git step', 'condition': 'vault_in_git'},
            {'id': 'step-trust', 'text': 'Trust review required step', 'condition': 'trust_review_required'}
        ]
        def add_after_update(files):
            files['after-update.json'] = json.dumps(steps).encode()
            metadata = json.loads(files['manifest.json'])
            metadata['files']['after-update.json'] = hashlib.sha256(files['after-update.json']).hexdigest()
            files['manifest.json'] = json.dumps(metadata).encode()

        pkg = rewrite_zip(self.package, self.base / 'after-update-release.zip', add_after_update)

        res = self.module.update(self.vault, self.state, pkg)
        self.assertEqual(res['status'], 'updated')
        self.assertIn('after_update_steps', res)
        matching = res['after_update_steps']
        self.assertEqual([s['id'] for s in matching], ['step-always'])

    def test_after_update_steps_yakala_and_git_matching(self):
        steps = [
            {'id': 'step-always', 'text': 'Always run step', 'condition': 'always'},
            {'id': 'step-yakala', 'text': 'Yakala installed step', 'condition': 'yakala_installed'},
            {'id': 'step-git', 'text': 'Vault in git step', 'condition': 'vault_in_git'}
        ]
        def add_after_update(files):
            files['after-update.json'] = json.dumps(steps).encode()
            metadata = json.loads(files['manifest.json'])
            metadata['files']['after-update.json'] = hashlib.sha256(files['after-update.json']).hexdigest()
            files['manifest.json'] = json.dumps(metadata).encode()

        pkg = rewrite_zip(self.package, self.base / 'after-update-release-2.zip', add_after_update)

        (self.state / 'yakala.json').write_text('{}', encoding='utf-8')
        (self.vault / '.git').mkdir()

        res = self.module.update(self.vault, self.state, pkg)
        self.assertEqual(res['status'], 'updated')
        matching = res['after_update_steps']
        self.assertEqual(set(s['id'] for s in matching), {'step-always', 'step-yakala', 'step-git'})


    def test_human_result_formatting_for_after_update_steps(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import beyin_entry
        steps = [
            {'id': 'step-1', 'text': 'Run migration command', 'command': 'python3 migrate.py'},
            {'id': 'step-2', 'text': 'Check docs'}
        ]
        result = {'status': 'updated', 'from_version': '3.0.0', 'version': '3.0.1', 'after_update_steps': steps}
        output = beyin_entry.human_result(result, 'update')
        self.assertIn('Guncelleme sonrasi adimlar:', output)
        self.assertIn('Run migration command (python3 migrate.py)', output)
        self.assertIn('Check docs', output)

    def test_file_exists_condition_path_traversal_rejected(self):
        steps = [
            {'id': 'bad-1', 'text': 'Traverse up', 'condition': 'file_exists:../secret.txt'},
            {'id': 'bad-2', 'text': 'Absolute path', 'condition': 'file_exists:/etc/passwd'},
            {'id': 'bad-3', 'text': 'Backslash', 'condition': 'file_exists:foo\\bar'},
            {'id': 'good-1', 'text': 'Normal file', 'condition': 'file_exists:user-note.md'}
        ]
        def add_after_update(files):
            files['after-update.json'] = json.dumps(steps).encode()
            metadata = json.loads(files['manifest.json'])
            metadata['files']['after-update.json'] = hashlib.sha256(files['after-update.json']).hexdigest()
            files['manifest.json'] = json.dumps(metadata).encode()

        pkg = rewrite_zip(self.package, self.base / 'traversal-release.zip', add_after_update)
        (self.vault / 'user-note.md').write_text('exists', encoding='utf-8')

        res = self.module.update(self.vault, self.state, pkg)
        self.assertEqual(res['status'], 'updated')
        matching = res['after_update_steps']
        # Only good-1 should match
        self.assertEqual([s['id'] for s in matching], ['good-1'])

    def test_legacy_package_without_after_update_updates_cleanly(self):
        res = self.module.update(self.vault, self.state, self.package)
        self.assertEqual(res['status'], 'updated')
        self.assertNotIn('after_update_steps', res)

if __name__ == '__main__':
    unittest.main()
