"""Tests-first installation acceptance for the user-facing V3 product package."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from v3_package_helpers import ROOT, install, isolated_env, run_python, snapshot


class ProductInstallationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-product-test-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault = self.base / 'Synthetic Beyin Ölçüm'
        self.vault.mkdir()
        self.state = self.base / 'state'
        self.env = isolated_env(self.base / 'home')

    def installed(self):
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        return result

    def command(self, *args, payload=None):
        entry = self.vault / 'beyin.py'
        self.assertTrue(entry.is_file(), 'Product entry point not implemented')
        result = run_python(entry, args, self.vault, self.env, payload)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        return json.loads(result.stdout)

    def test_plan_manifest_uses_portable_paths_under_windows_path_semantics(self):
        import importlib.util
        from pathlib import PureWindowsPath
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('product_windows_plan', ROOT / 'scripts/install_v3.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        original_relative = Path.relative_to
        def windows_relative(path, *args, **kwargs):
            return PureWindowsPath(*original_relative(path, *args, **kwargs).parts)
        # Keep actual filesystem operations native; emulate Windows serialization
        # only at relative-path boundaries, which feed the portable manifest.
        with patch.object(Path, 'relative_to', windows_relative):
            plan = installer.install(self.vault, self.state, plan_only=True)
        self.assertIn('.claude/settings.local.json', plan['planned'])
        self.assertIn('.codex/hooks.json', plan['manifest']['files'])
        self.assertTrue(all('\\' not in name for name in plan['planned']))
        seed = installer.PERSON_TEMPLATE_PATH
        self.assertIn(seed, plan['planned'])
        self.assertNotIn(seed, plan['manifest']['files'])
        self.assertEqual(set(plan['planned']) - {seed}, set(plan['manifest']['files']))

    def test_installer_retry_recovers_before_root_entrypoint_exists(self):
        import importlib.util
        from importlib.machinery import SourceFileLoader
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('product_interrupted_install', ROOT / 'scripts/install_v3.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        original_exec = SourceFileLoader.exec_module
        def crash(phase, index=None):
            if phase == 'after_replace' and index == 0:
                raise OSError('Synthetic first install interruption')
        def load_with_fault(loader, module):
            original_exec(loader, module)
            if module.__name__ == 'beyin_install_transaction':
                module.transaction_hook = crash
        with patch.object(SourceFileLoader, 'exec_module', load_with_fault):
            with self.assertRaises(OSError):
                installer.install(self.vault, self.state)
        self.assertFalse((self.vault / 'beyin.py').exists())
        self.assertTrue((self.state / 'update-journal.json').exists())
        note = self.vault / 'user-after-interruption.md'
        note.write_text('Synthetic note created while installation was interrupted.')
        installer.install(self.vault, self.state)
        self.assertEqual((self.vault / '.beyin-version').read_text().strip(),
                         (ROOT / 'VERSION').read_text(encoding='utf-8-sig').strip())
        self.assertTrue((self.vault / 'beyin.py').is_file())
        self.assertFalse((self.state / 'update-journal.json').exists())
        self.assertEqual(note.read_text(), 'Synthetic note created while installation was interrupted.')

    def test_extracted_release_installer_uses_manifest_version(self):
        import zipfile
        from v3_package_helpers import build_package
        package = build_package(self.base / 'release.zip', '3.0.1', self.env)
        extracted = self.base / 'extracted'
        with zipfile.ZipFile(package) as archive:
            archive.extractall(extracted)
        result = run_python(extracted / 'scripts/install_v3.py',
                            ['--vault', self.vault, '--state', self.state], extracted, self.env)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertEqual((self.vault / '.beyin-version').read_text().strip(), '3.0.1')

    def test_release_builder_refreshes_matching_checksum(self):
        from v3_package_helpers import build_package
        package = build_package(self.base / 'release.zip', '3.0.1', self.env)
        checksum = package.with_name(package.name + '.sha256')
        expected = hashlib.sha256(package.read_bytes()).hexdigest()
        self.assertEqual(checksum.read_text(encoding='utf-8'), f'{expected}  {package.name}\n')

    def test_extracted_release_installer_retires_stock_legacy_workers(self):
        import zipfile
        from v3_package_helpers import build_package
        old_scripts = self.vault / '.claude/scripts'
        old_scripts.mkdir(parents=True)
        for name in ('flush.py', 'compile.py'):
            (old_scripts / name).write_bytes((ROOT / 'template/.claude/scripts' / name).read_bytes())
        (self.vault / '.beyin-version').write_text('2.3.0')
        package = build_package(self.base / 'release.zip', '3.0.0', self.env)
        extracted = self.base / 'extracted'
        with zipfile.ZipFile(package) as archive:
            archive.extractall(extracted)
        result = run_python(extracted / 'scripts/install_v3.py',
                            ['--vault', self.vault, '--state', self.state], extracted, self.env)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        for name in ('flush.py', 'compile.py'):
            self.assertIn(b'BEYIN_V3_LEGACY_RETIRED', (old_scripts / name).read_bytes())
        self.assertEqual((self.vault / '.beyin-version').read_text().strip(), '3.0.0')

    def test_extracted_release_accepts_current_stock_v23_doctor_skill(self):
        import zipfile
        from v3_package_helpers import build_package
        skill = self.vault / '.claude/skills/beyin-doktor/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_bytes((ROOT/'template/.claude/skills/beyin-doktor/SKILL.md').read_bytes())
        package = build_package(self.base/'release.zip', '3.0.0', self.env)
        extracted = self.base/'extracted'
        with zipfile.ZipFile(package) as archive:
            archive.extractall(extracted)
        result = run_python(extracted/'scripts/install_v3.py',
                            ['--vault', self.vault, '--state', self.state], extracted, self.env)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(skill.read_bytes(), (self.vault/'.agents/skills/beyin-doktor/SKILL.md').read_bytes())

    def test_clean_install_entrypoint_version_skills_and_doctor(self):
        self.installed()
        self.assertTrue((self.vault / 'beyin.py').is_file())
        self.assertEqual((self.vault / '.beyin-version').read_text(encoding='utf-8').strip(), '3.0.0')
        for name in ('beyin', 'beyin-doktor', 'beyin-guncelle'):
            canonical = self.vault / '.agents/skills' / name / 'SKILL.md'
            mirror = self.vault / '.claude/skills' / name / 'SKILL.md'
            self.assertTrue(canonical.is_file(), name)
            self.assertTrue(mirror.is_file(), name)
            self.assertEqual(canonical.read_bytes(), mirror.read_bytes())
            self.assertIn('beyin.py', canonical.read_text(encoding='utf-8'))
        doctor = self.command('doctor')
        self.assertIsInstance(doctor, dict)
        self.assertIn('pending_events', doctor)
        instructions = (self.vault / 'AGENTS.md').read_text(encoding='utf-8')
        for name in ('beyin-doktor', 'beyin-guncelle'):
            self.assertIn(name, instructions)

    def test_custom_sources_settings_skills_preserved_on_install_and_reinstall(self):
        note = self.vault / 'custom.md';note.write_text('Synthetic personal source retained.\n')
        skill = self.vault / '.agents/skills/custom/SKILL.md';skill.parent.mkdir(parents=True)
        skill.write_text('Synthetic custom skill retained.\n')
        config = self.vault / '.claude/settings.local.json';config.parent.mkdir(parents=True)
        custom = {'permissions': {'allow': ['Read']}, 'synthetic': 'ölçüm'}
        config.write_text(json.dumps(custom, ensure_ascii=False), encoding='utf-8')
        self.installed()
        new = json.loads(config.read_text(encoding='utf-8'))
        extra = {'hooks': [{'type': 'command', 'command': 'synthetic-user-added-handler'}]}
        new['hooks'].setdefault('SessionStart', []).append(extra)
        config.write_text(json.dumps(new, ensure_ascii=False), encoding='utf-8')
        self.installed()
        final = json.loads(config.read_text(encoding='utf-8'))
        self.assertEqual(final['permissions'], custom['permissions'])
        self.assertEqual(final['synthetic'], custom['synthetic'])
        self.assertIn(extra, final['hooks']['SessionStart'])
        self.assertEqual(note.read_text(), 'Synthetic personal source retained.\n')
        self.assertEqual(skill.read_text(), 'Synthetic custom skill retained.\n')

    def test_bundled_skill_name_collision_preserves_user_content(self):
        skill = self.vault / '.agents/skills/beyin/SKILL.md';skill.parent.mkdir(parents=True)
        skill.write_text('User-owned existing beyin skill.\n')
        result = install(self.vault, self.state, self.env)
        self.assertNotEqual(result.returncode, 0, 'Same-name user skill must be an explicit conflict')
        self.assertEqual(skill.read_text(), 'User-owned existing beyin skill.\n')
        self.assertFalse((self.vault / '.beyin-version').exists(), 'Conflicted install cannot stamp success')

    def test_installed_first_note_task_receipt_workflow(self):
        self.installed()
        note = self.vault / 'task.md'
        note.write_text('---\n' + json.dumps({'id': 'first-task', 'kind': 'task', 'revision': 1, 'status': 'active', 'project': 'demo'}) +
                        '\n---\nDemo spectroscope next step is calibration.\n', encoding='utf-8')
        self.command('sync')
        context = self.command('context', 'Demo spectroscope', '--project', 'demo')
        self.assertIn('first-task', [r['id'] for r in context['records']])
        self.command('task-update', payload={'id': 'first-task', 'expected_revision': 1, 'changes': {'status': 'done'}})
        self.assertIn('"done"', note.read_text())
        receipt = self.command('receipt', payload={'event_id': 'product-first-run', 'summary': 'Synthetic calibration marked done.', 'refs': ['task.md']})
        self.assertTrue((self.vault / receipt['source']).is_file())

    def test_context_no_sync_reads_existing_index_without_mutation(self):
        self.installed()
        note = self.vault / 'indexed.md'
        note.write_text('---\n' + json.dumps({'id': 'indexed-note', 'project': 'demo'}) +
                        '\n---\nQuartz read-only context remains source-backed.\n', encoding='utf-8')
        self.command('sync')
        before_vault, before_state = snapshot(self.vault), snapshot(self.state)
        context = self.command('context', 'Quartz read-only context', '--project', 'demo', '--no-sync')
        self.assertEqual([r['id'] for r in context['records']], ['indexed-note'])
        self.assertEqual(context['source_sync'], {'status': 'skipped', 'reason': 'explicit_no_sync'})
        self.assertEqual(snapshot(self.vault), before_vault)
        self.assertEqual(snapshot(self.state), before_state)

        note.write_text(note.read_text(encoding='utf-8').replace('remains', 'changed'), encoding='utf-8')
        before_vault, before_state = snapshot(self.vault), snapshot(self.state)
        stale = self.command('context', 'Quartz read-only context', '--project', 'demo', '--no-sync')
        self.assertEqual(stale['records'], [])
        self.assertEqual(stale['stale_count'], 1)
        self.assertEqual(snapshot(self.vault), before_vault)
        self.assertEqual(snapshot(self.state), before_state)

        note.unlink()
        before_vault, before_state = snapshot(self.vault), snapshot(self.state)
        deleted = self.command('context', 'Quartz read-only context', '--project', 'demo', '--no-sync')
        self.assertEqual(deleted['records'], [])
        self.assertEqual(deleted['stale_count'], 1)
        self.assertEqual(snapshot(self.vault), before_vault)
        self.assertEqual(snapshot(self.state), before_state)

    def test_v2_sources_retained_and_old_writer_handlers_retired(self):
        sources = {'daily/2026-01-01.md': 'Legacy starlight daily source.',
                   'knowledge/legacy.md': 'Legacy starlight knowledge source.',
                   '🔮 850-Companion/Last-Session.md': '# Son Oturum\nLegacy starlight companion source.'}
        for name, body in sources.items():
            path = self.vault / name;path.parent.mkdir(parents=True, exist_ok=True);path.write_text(body, encoding='utf-8')
        (self.vault / '.beyin-version').write_text('2.3.0\n')
        config = self.vault / '.claude/settings.json';config.parent.mkdir()
        config.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [
            {'type': 'command', 'command': '"${CLAUDE_PROJECT_DIR}/.claude/hooks/session-start.sh"'},
            {'type': 'command', 'command': 'synthetic-custom-command'}]}]}}))
        codex = self.vault / '.codex/hooks.json'; codex.parent.mkdir()
        codex.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [
            {'type': 'command', 'command': "bash '/synthetic/vault/.codex/hooks/session-start.sh'"},
            {'type': 'command', 'command': 'synthetic-codex-command'}]}]}}))
        self.installed()
        for name, body in sources.items():
            self.assertEqual((self.vault / name).read_text(encoding='utf-8'), body)
        serialized = codex.read_text(encoding='utf-8')
        self.assertNotIn('.codex/hooks/session-start.sh', serialized)
        self.assertIn('synthetic-codex-command', serialized)
        self.command('sync')
        context = self.command('context', 'Legacy starlight')
        self.assertTrue(set(sources).issubset({r['source'] for r in context['records']}))
        handlers = json.loads(config.read_text())['hooks']['SessionStart']
        text = json.dumps(handlers)
        self.assertNotIn('session-start.sh', text)
        self.assertIn('synthetic-custom-command', text)


    def test_initial_v2_upgrade_rollback_restores_legacy_system_and_keeps_new_note(self):
        version = self.vault / '.beyin-version';version.write_text('2.3.0\n')
        config = self.vault / '.claude/settings.json';config.parent.mkdir()
        original_config = json.dumps({'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': '"${CLAUDE_PROJECT_DIR}/.claude/hooks/session-start.sh"'}]}]}}).encode()
        config.write_bytes(original_config)
        legacy = self.vault / '.claude/scripts/flush.py';legacy.parent.mkdir()
        original_runner = (ROOT / 'template/.claude/scripts/flush.py').read_bytes()
        legacy.write_bytes(original_runner)
        self.installed()
        new_note = self.vault / 'created-after-upgrade.md';new_note.write_text('New user note must survive rollback.\n')
        result = self.command('rollback')
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(version.read_text(), '2.3.0\n')
        self.assertEqual(config.read_bytes(), original_config)
        self.assertEqual(legacy.read_bytes(), original_runner)
        self.assertEqual(new_note.read_text(), 'New user note must survive rollback.\n')

    def test_fresh_install_rollback_is_honest_uninstall_not_invented_prior_version(self):
        self.installed()
        note = self.vault / 'new-note.md';note.write_text('Fresh user note retained.\n')
        result = self.command('rollback')
        self.assertEqual(result['status'], 'uninstalled')
        self.assertFalse((self.vault / '.beyin-version').exists())
        self.assertFalse((self.vault / 'beyin.py').exists())
        self.assertEqual(note.read_text(), 'Fresh user note retained.\n')


    def test_cached_stock_legacy_worker_paths_are_inert_after_cutover(self):
        (self.vault / '.beyin-version').write_text('2.3.0\n')
        directory = self.vault / '.claude/scripts';directory.mkdir(parents=True)
        cached_commands = []
        for name in ('flush.py', 'compile.py', '_portalock.py'):
            destination = directory / name
            destination.write_bytes((ROOT / 'template/.claude/scripts' / name).read_bytes())
            if name != '_portalock.py':
                cached_commands.append(destination)
        self.installed()
        before_vault, before_state = snapshot(self.vault), snapshot(self.state)
        for command in cached_commands:
            args = ['--hook-input', 'missing-synthetic-input.json', '--reason', 'sessionend'] if command.name == 'flush.py' else ['--dry-run']
            result = run_python(command, args, self.vault, self.env)
            self.assertEqual(result.returncode, 0, 'Cached known legacy path should retire cleanly')
            self.assertIn(b'BEYIN_V3_LEGACY_RETIRED', command.read_bytes())
        self.assertEqual(snapshot(self.vault), before_vault)
        self.assertEqual(snapshot(self.state), before_state)


if __name__ == '__main__':
    unittest.main()
