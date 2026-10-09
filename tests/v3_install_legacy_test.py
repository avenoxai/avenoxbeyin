"""A vault whose state manifest is gone must still reinstall, and a customized legacy runner
must be retirable through one named path instead of a blanket override."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import ROOT, build_package, isolated_env, run_python, snapshot

RELEASED = Path(__file__).resolve().parent / 'fixtures/v3/released-skills'
TAGS = ('v3.0.0', 'v3.0.1', 'v3.0.2')
SKILLS = ('beyin', 'beyin-doktor', 'beyin-guncelle')
ROOTS = ('.agents', '.claude')
CUSTOM_RUNNER = b'# kullanicinin kendi flush surumu\nprint("synthetic customized legacy runner")\n'
CUSTOM_HOOK = b'#!/bin/sh\n# kullanicinin kendi session-end kancasi\nexit 0\n'


# Digests of the V2 hooks every V3 release retires. The installer recognizes an untouched V2
# hook only by these bytes, so editing template/.claude/hooks turns every stock V2 vault into
# "Customized legacy runner requires review" (#155). Fix the V3 engine instead.
RELEASED_V2_HOOKS = {
    'session-start.sh': '92dd2ce61777cc80034894fc8e0e053cebcec3bfc3c2455e5c1783b7bbae4088',
    'session-start.ps1': 'c90f93b3bd21deae6289504646b56b0aeb91a5ba38401b8188053d73eb1edcee',
    'session-end.sh': '3f079c8b64a907fc81b58187d32317b1db182e14190d600498d6cd48b1fbb92a',
    'session-end.ps1': '8fbf8f92572d01eb5b71582ae4e05df392c0aa69e60cfc57d5f3686c5251471c',
    'pre-compact.sh': '19e39f72f431203374fe3f26b94c4ceae0f031949617d8a5bfdcb9b93964ffd8',
    'pre-compact.ps1': '1af3b730efbe4148444cf9e6ea0421ca03b49d8588a43c7ed1760c1c5712c835',
    'prompt-counter.sh': '3494e2a7282c0278372496c08a51c0e605879d16146ffe184a71e218721a40c0',
    'prompt-counter.ps1': 'bcb6375090dcbf716c62e0232f61772a69d83ced289ad5ed7d37126246237cde',
}


class ReleasedLegacyHooksTest(unittest.TestCase):
    def test_legacy_hook_templates_keep_their_released_bytes(self):
        for name, expected in RELEASED_V2_HOOKS.items():
            with self.subTest(name=name):
                data = (ROOT / 'template/.claude/hooks' / name).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected)


class InstallLegacyExemptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-install-legacy-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.env = isolated_env(self.base / 'home')
        spec = importlib.util.spec_from_file_location('beyin_legacy_installer', ROOT / 'scripts/install_v3.py')
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        self.new_vault()

    def new_vault(self, label='vault'):
        self.vault = self.base / ('Örnek Beyin ' + label)
        self.vault.mkdir()
        self.state = self.base / ('state-' + label)
        return self.vault

    def seed(self, name, data):
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'),
                               '--vault', str(self.vault), '--state', str(self.state), *args],
                              cwd=ROOT, env=self.env, capture_output=True, timeout=120)

    def test_released_starter_skills_at_both_roots_install_without_a_manifest(self):
        for tag in TAGS:
            with self.subTest(tag=tag):
                self.new_vault(tag)
                seeded = {}
                for name in SKILLS:
                    data = (RELEASED / tag / (name + '.md')).read_bytes()
                    for root in ROOTS:
                        seeded[root + '/skills/' + name + '/SKILL.md'] = self.seed(
                            root + '/skills/' + name + '/SKILL.md', data)
                self.installer.install(self.vault, self.state)
                for relative, path in seeded.items():
                    skill = relative.split('/')[2]
                    self.assertEqual(path.read_bytes(),
                                     (ROOT / 'template/.agents/skills' / skill / 'SKILL.md').read_bytes(),
                                     relative)

    def test_released_package_carries_the_exemption_for_both_roots(self):
        import zipfile
        extracted = self.base / 'extracted'
        with zipfile.ZipFile(build_package(self.base / 'release.zip', '3.0.1', self.env)) as archive:
            # The pre-#40 validator only accepts this key (issue #73).
            self.assertEqual(set(json.loads(archive.read('manifest.json'))['legacy_skill_hashes']),
                             {'.claude/skills/beyin-doktor/SKILL.md'})
            archive.extractall(extracted)
        for tag in TAGS:
            with self.subTest(tag=tag):
                self.new_vault('package-' + tag)
                seeded = {}
                for name in SKILLS:
                    data = (RELEASED / tag / (name + '.md')).read_bytes()
                    for root in ROOTS:
                        relative = root + '/skills/' + name + '/SKILL.md'
                        seeded[relative] = self.seed(relative, data)
                result = run_python(extracted / 'scripts/install_v3.py',
                                    ['--vault', self.vault, '--state', self.state], extracted, self.env)
                self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
                for relative, path in seeded.items():
                    self.assertEqual(path.read_bytes(),
                                     (ROOT / 'template/.agents/skills' / relative.split('/')[2] / 'SKILL.md').read_bytes())

    def test_legacy_wire_subset_preserves_custom_skill_conflict(self):
        doctor = '.claude/skills/beyin-doktor/SKILL.md'
        subset = {doctor: self.installer.default_skill_hashes()[doctor]}
        for root in ROOTS:
            for name in SKILLS:
                with self.subTest(root=root, skill=name):
                    relative = root + '/skills/' + name + '/SKILL.md'
                    path = self.seed(relative, (RELEASED / 'v3.0.2' / (name + '.md')).read_bytes()
                                     + b'\nUser customization.\n')
                    before = snapshot(self.vault)
                    with self.assertRaisesRegex(ValueError, 'Unmanaged file conflict'):
                        self.installer.install(self.vault, self.state, plan_only=True,
                                               legacy_skill_hashes=subset)
                    self.assertEqual(snapshot(self.vault), before)
                    path.unlink()

    def test_stock_claude_doctor_skill_stays_exempt(self):
        path = self.seed('.claude/skills/beyin-doktor/SKILL.md',
                         (RELEASED / 'stock-claude-doctor.md').read_bytes())
        self.installer.install(self.vault, self.state)
        self.assertEqual(path.read_bytes(),
                         (ROOT / 'template/.agents/skills/beyin-doktor/SKILL.md').read_bytes())

    def test_windows_v2_copied_stock_doctor_skill_stays_exempt_at_both_roots(self):
        # V2's scripts/install.ps1 copied .claude\skills\* into .agents\skills instead of
        # linking it, so a Windows V2 vault holds the stock doctor bytes at both roots.
        stock = (RELEASED / 'stock-claude-doctor.md').read_bytes()
        paths = [self.seed(root + '/skills/beyin-doktor/SKILL.md', stock) for root in ROOTS]
        self.installer.install(self.vault, self.state)
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(path.read_bytes(),
                                 (ROOT / 'template/.agents/skills/beyin-doktor/SKILL.md').read_bytes())

    def test_windows_edition_doctor_skill_stays_exempt_at_both_roots(self):
        # From v3.0.2 on install.ps1 writes scripts/skill.beyin-doktor.windows.md to .claude
        # and then copies .claude\skills\* to .agents\skills: those bytes sit at both roots.
        windows = (RELEASED / 'windows-doctor.md').read_bytes()
        paths = [self.seed(root + '/skills/beyin-doktor/SKILL.md', windows) for root in ROOTS]
        self.installer.install(self.vault, self.state)
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(path.read_bytes(),
                                 (ROOT / 'template/.agents/skills/beyin-doktor/SKILL.md').read_bytes())

    def test_shipped_windows_doctor_bytes_are_exempt(self):
        # install.ps1 still ships; editing the Windows doctor without pinning its new
        # digest would strand every Windows vault installed after that edit.
        current = self.installer.digest((ROOT / 'scripts/skill.beyin-doktor.windows.md').read_bytes())
        hashes = self.installer.default_skill_hashes()
        for root in ROOTS:
            with self.subTest(root=root):
                self.assertIn(current, hashes[root + '/skills/beyin-doktor/SKILL.md'])

    def test_unknown_skill_content_is_still_an_unmanaged_conflict(self):
        for root in ROOTS:
            for name in SKILLS:
                with self.subTest(root=root, skill=name):
                    relative = root + '/skills/' + name + '/SKILL.md'
                    path = self.seed(relative, b'Kullanicinin kendi ' + name.encode() + b' skilli.\n')
                    with self.assertRaises(ValueError) as raised:
                        self.installer.install(self.vault, self.state, plan_only=True)
                    self.assertEqual(str(raised.exception), 'Unmanaged file conflict ' + relative)
                    path.unlink()

    def test_customized_legacy_runner_needs_its_own_named_acceptance(self):
        runner = self.seed('.claude/scripts/flush.py', CUSTOM_RUNNER)
        with self.assertRaises(ValueError) as raised:
            self.installer.install(self.vault, self.state, plan_only=True)
        self.assertIn('Customized legacy runner requires review .claude/scripts/flush.py',
                      str(raised.exception))
        self.assertEqual(runner.read_bytes(), CUSTOM_RUNNER)
        result = self.cli('--accept-customized-legacy', '.claude/scripts/flush.py')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertIn(b'BEYIN_V3_LEGACY_RETIRED', runner.read_bytes())
        uninstall = self.cli('--uninstall')
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(runner.read_bytes(), CUSTOM_RUNNER)

    def test_accepting_one_runner_does_not_exempt_another(self):
        self.seed('.claude/scripts/flush.py', CUSTOM_RUNNER)
        self.seed('.claude/scripts/compile.py', CUSTOM_RUNNER)
        with self.assertRaises(ValueError) as raised:
            self.installer.install(self.vault, self.state, plan_only=True,
                                   accept_customized=('.claude/scripts/flush.py',))
        self.assertIn('Customized legacy runner requires review .claude/scripts/compile.py',
                      str(raised.exception))

    def test_acceptance_outside_the_legacy_runner_set_is_rejected(self):
        self.seed('notes/rapor.md', b'Kullanici notu.\n')
        for name in ('notes/rapor.md', '.claude/skills/beyin/SKILL.md', '../escape.py',
                     '.claude/scripts/beyin_v3_cli.py'):
            with self.subTest(path=name):
                with self.assertRaises(ValueError) as raised:
                    self.installer.install(self.vault, self.state, plan_only=True,
                                           accept_customized=(name,))
                self.assertIn('unsupported legacy managed path', str(raised.exception))
        result = self.cli('--accept-customized-legacy', 'notes/rapor.md')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.vault / 'beyin.py').exists())

    def test_plan_reports_the_retirement_and_writes_nothing(self):
        runner = self.seed('.claude/scripts/flush.py', CUSTOM_RUNNER)
        self.state.mkdir(parents=True, exist_ok=True)
        before_vault, before_state = snapshot(self.vault), snapshot(self.state)
        result = self.cli('--plan', '--accept-customized-legacy', '.claude/scripts/flush.py')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        plan = json.loads(result.stdout)
        self.assertEqual(plan['status'], 'plan')
        self.assertEqual(plan['retire'], ['.claude/scripts/flush.py'])
        self.assertIn('.claude/scripts/flush.py', plan['preserve'])
        self.assertIn('beyin.py', plan['write'])
        self.assertNotIn('.claude/scripts/flush.py', plan['write'])
        self.assertEqual(snapshot(self.vault), before_vault)
        self.assertEqual(snapshot(self.state), before_state)
        self.assertEqual(runner.read_bytes(), CUSTOM_RUNNER)
        self.assertFalse((self.state / 'v3-install.json').exists())

    def test_kept_customized_runner_is_untouched_unplanned_and_unmanaged(self):
        runner = self.seed('.claude/scripts/flush.py', CUSTOM_RUNNER)
        result = self.cli('--keep-customized-legacy', '.claude\\scripts\\flush.py')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(runner.read_bytes(), CUSTOM_RUNNER)
        self.assertEqual(json.loads(result.stdout)['kept_legacy'], ['.claude/scripts/flush.py'])
        manifest = json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['kept_legacy'], ['.claude/scripts/flush.py'])
        self.assertNotIn('.claude/scripts/flush.py', manifest['files'])

    def test_install_reports_flush_sentinels_whose_writer_died(self):
        self.seed('.claude/scripts/flush.py', CUSTOM_RUNNER)
        sentinel = self.seed('.claude/scripts/.state/flush-example.json', b'{"status":"inflight"}')
        self.seed('.claude/scripts/.state/flush-example.lock', b'')
        result = self.cli('--keep-customized-legacy', '.claude/scripts/flush.py')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        orphaned = ['.claude/scripts/.state/flush-example.json']
        self.assertEqual(json.loads(result.stdout)['orphaned_legacy_state'], orphaned)
        self.assertEqual(sentinel.read_bytes(), b'{"status":"inflight"}')
        receipt = json.loads((self.state / 'v2-migration.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['orphaned_legacy_state'], orphaned)

    def test_keeping_one_runner_does_not_exempt_another(self):
        self.seed('.claude/scripts/flush.py', CUSTOM_RUNNER)
        self.seed('.claude/scripts/compile.py', CUSTOM_RUNNER)
        with self.assertRaises(ValueError) as raised:
            self.installer.install(self.vault, self.state, plan_only=True,
                                   keep_customized=('.claude/scripts/flush.py',))
        self.assertIn('Customized legacy runner requires review .claude/scripts/compile.py',
                      str(raised.exception))

    def test_keeping_a_path_outside_the_legacy_runner_set_is_rejected(self):
        self.seed('notes/rapor.md', b'Kullanici notu.\n')
        for name in ('notes/rapor.md', '.claude/skills/beyin/SKILL.md', '../escape.py',
                     '.claude/scripts/beyin_v3_cli.py'):
            with self.subTest(path=name):
                with self.assertRaises(ValueError) as raised:
                    self.installer.install(self.vault, self.state, plan_only=True,
                                           keep_customized=(name,))
                self.assertIn('unsupported legacy managed path', str(raised.exception))
        result = self.cli('--keep-customized-legacy', 'notes/rapor.md')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.vault / 'beyin.py').exists())

    def test_one_runner_cannot_be_both_kept_and_retired(self):
        runner = self.seed('.claude/scripts/flush.py', CUSTOM_RUNNER)
        with self.assertRaises(ValueError) as raised:
            self.installer.install(self.vault, self.state, plan_only=True,
                                   accept_customized=('.claude/scripts/flush.py',),
                                   keep_customized=('.claude/scripts/flush.py',))
        self.assertIn('both kept and retired', str(raised.exception))
        result = self.cli('--accept-customized-legacy', '.claude/scripts/flush.py',
                          '--keep-customized-legacy', '.claude/scripts/flush.py')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(runner.read_bytes(), CUSTOM_RUNNER)

    def test_plan_reports_the_kept_runner_outside_write_retire_and_preserve(self):
        runner = self.seed('.claude/scripts/flush.py', CUSTOM_RUNNER)
        self.state.mkdir(parents=True, exist_ok=True)
        before_vault, before_state = snapshot(self.vault), snapshot(self.state)
        result = self.cli('--plan', '--keep-customized-legacy', '.claude/scripts/flush.py')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        plan = json.loads(result.stdout)
        self.assertEqual(plan['status'], 'plan')
        self.assertEqual(plan['keep'], ['.claude/scripts/flush.py'])
        for section in ('write', 'retire', 'preserve'):
            self.assertNotIn('.claude/scripts/flush.py', plan[section], section)
        self.assertIn('beyin.py', plan['write'])
        self.assertEqual(snapshot(self.vault), before_vault)
        self.assertEqual(snapshot(self.state), before_state)
        self.assertEqual(runner.read_bytes(), CUSTOM_RUNNER)

    def test_hook_entries_of_a_kept_hook_survive_while_others_are_stripped(self):
        hook = self.seed('.claude/hooks/session-end.sh', CUSTOM_HOOK)
        entries = {'hooks': {'SessionEnd': [{'hooks': [
            {'type': 'command', 'command': '"$CLAUDE_PROJECT_DIR/.claude/hooks/session-end.sh"'},
            {'type': 'command', 'command': '"$CLAUDE_PROJECT_DIR/.claude/hooks/session-start.sh"'},
            {'type': 'command', 'command': 'synthetic-custom-command'}]}]}}
        names = ('.claude/settings.json', '.claude/settings.local.json', '.codex/hooks.json')
        for name in names:
            self.seed(name, json.dumps(entries).encode('utf-8'))
        result = self.cli('--keep-customized-legacy', '.claude/hooks/session-end.sh')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(hook.read_bytes(), CUSTOM_HOOK)
        for name in names:
            with self.subTest(settings=name):
                text = (self.vault / name).read_text(encoding='utf-8')
                self.assertIn('.claude/hooks/session-end.sh', text)
                self.assertNotIn('session-start.sh', text)
                self.assertIn('synthetic-custom-command', text)

    def test_update_without_the_flag_honours_the_kept_runner_from_the_manifest(self):
        runner = self.seed('.claude/scripts/flush.py', CUSTOM_RUNNER)
        result = self.cli('--keep-customized-legacy', '.claude/scripts/flush.py')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        package = build_package(self.base / 'upgrade.zip', '3.0.1', self.env)
        update = run_python(self.vault / 'beyin.py', ['update', '--package', package], self.vault, self.env)
        self.assertEqual(update.returncode, 0, update.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(runner.read_bytes(), CUSTOM_RUNNER)
        self.assertEqual((self.vault / '.beyin-version').read_text(encoding='utf-8').strip(), '3.0.1')
        manifest = json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['kept_legacy'], ['.claude/scripts/flush.py'])

    def test_uninstall_leaves_the_kept_runner_and_its_hook_entry_alone(self):
        runner = self.seed('.claude/scripts/flush.py', CUSTOM_RUNNER)
        hook = self.seed('.claude/hooks/session-end.sh', CUSTOM_HOOK)
        settings = self.seed('.claude/settings.json', json.dumps({'hooks': {'SessionEnd': [{'hooks': [
            {'type': 'command', 'command': '"$CLAUDE_PROJECT_DIR/.claude/hooks/session-end.sh"'}]}]}}).encode('utf-8'))
        result = self.cli('--keep-customized-legacy', '.claude/scripts/flush.py',
                          '--keep-customized-legacy', '.claude/hooks/session-end.sh')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        uninstall = self.cli('--uninstall')
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(runner.read_bytes(), CUSTOM_RUNNER)
        self.assertEqual(hook.read_bytes(), CUSTOM_HOOK)
        self.assertIn('.claude/hooks/session-end.sh', settings.read_text(encoding='utf-8'))

    def test_doctor_reports_the_kept_legacy_runners(self):
        self.seed('.claude/scripts/flush.py', CUSTOM_RUNNER)
        result = self.cli('--keep-customized-legacy', '.claude/scripts/flush.py')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        doctor = run_python(self.vault / 'beyin.py', ['doctor'], self.vault, self.env)
        self.assertEqual(doctor.returncode, 0, doctor.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(json.loads(doctor.stdout)['kept_legacy_runners'], ['.claude/scripts/flush.py'])
        self.new_vault('plain')
        plain = self.cli()
        self.assertEqual(plain.returncode, 0, plain.stderr.decode('utf-8', errors='replace'))
        plain_doctor = run_python(self.vault / 'beyin.py', ['doctor'], self.vault, self.env)
        self.assertEqual(plain_doctor.returncode, 0, plain_doctor.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(json.loads(plain_doctor.stdout)['kept_legacy_runners'], [])

    def test_editing_or_removing_the_kept_hook_entry_is_no_reinstall_or_update_conflict(self):
        self.seed('.claude/hooks/session-end.sh', CUSTOM_HOOK)
        settings = self.seed('.claude/settings.local.json', json.dumps({'hooks': {'SessionEnd': [{'hooks': [
            {'type': 'command', 'command': '"$CLAUDE_PROJECT_DIR/.claude/hooks/session-end.sh"'}]}]}}).encode('utf-8'))
        result = self.cli('--keep-customized-legacy', '.claude/hooks/session-end.sh')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        def edit(change):
            data = json.loads(settings.read_text(encoding='utf-8'))
            for groups in data['hooks'].values():
                for group in groups:
                    change(group)
            settings.write_text(json.dumps(data), encoding='utf-8')
        kept = lambda group: 'session-end.sh' in json.dumps(group)
        edit(lambda group: [h.update(timeout=30) for h in group['hooks']] if kept(group) else None)
        reinstall = self.cli()
        self.assertEqual(reinstall.returncode, 0, reinstall.stderr.decode('utf-8', errors='replace'))
        self.assertIn('"timeout": 30', settings.read_text(encoding='utf-8'))
        edit(lambda group: group['hooks'].clear() if kept(group) else None)
        package = build_package(self.base / 'upgrade.zip', '3.0.1', self.env)
        update = run_python(self.vault / 'beyin.py', ['update', '--package', package], self.vault, self.env)
        self.assertEqual(update.returncode, 0, update.stderr.decode('utf-8', errors='replace'))
        self.assertNotIn('session-end.sh', settings.read_text(encoding='utf-8'))
        # V3's own entries; on Windows the command is an encoded PowerShell launcher, so select by exclusion.
        edit(lambda group: [h.update(timeout=99) for h in group['hooks'] if 'session-end.sh' not in json.dumps(h)])
        with self.assertRaisesRegex(ValueError, 'Reinstall conflict: managed file changed .claude/settings.local.json'):
            self.installer.install(self.vault, self.state, plan_only=True)

    def test_accepting_a_previously_kept_runner_retires_it_and_ends_the_keep(self):
        runner = self.seed('.claude/scripts/flush.py', CUSTOM_RUNNER)
        hook = self.seed('.claude/hooks/session-end.sh', CUSTOM_HOOK)
        settings = self.seed('.claude/settings.json', json.dumps({'hooks': {'SessionEnd': [{'hooks': [
            {'type': 'command', 'command': '"$CLAUDE_PROJECT_DIR/.claude/hooks/session-end.sh"'}]}]}}).encode('utf-8'))
        result = self.cli('--keep-customized-legacy', '.claude/scripts/flush.py',
                          '--keep-customized-legacy', '.claude/hooks/session-end.sh')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        accept = self.cli('--accept-customized-legacy', '.claude/hooks/session-end.sh')
        self.assertEqual(accept.returncode, 0, accept.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(json.loads(accept.stdout)['kept_legacy'], ['.claude/scripts/flush.py'])
        self.assertIn(b'BEYIN_V3_LEGACY_RETIRED', hook.read_bytes())
        self.assertNotIn('session-end.sh', settings.read_text(encoding='utf-8'))
        self.assertEqual(runner.read_bytes(), CUSTOM_RUNNER)
        manifest = json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['kept_legacy'], ['.claude/scripts/flush.py'])
        self.assertIn('.claude/hooks/session-end.sh', manifest['files'])
        last = self.cli('--accept-customized-legacy', '.claude/scripts/flush.py')
        self.assertEqual(last.returncode, 0, last.stderr.decode('utf-8', errors='replace'))
        self.assertIn(b'BEYIN_V3_LEGACY_RETIRED', runner.read_bytes())
        self.assertNotIn('kept_legacy', json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8')))
        uninstall = self.cli('--uninstall')
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(runner.read_bytes(), CUSTOM_RUNNER)
        self.assertEqual(hook.read_bytes(), CUSTOM_HOOK)
        self.assertIn('session-end.sh', settings.read_text(encoding='utf-8'))

    def test_keeping_a_claude_hook_keeps_only_entries_for_that_exact_path(self):
        self.seed('.claude/hooks/session-end.sh', CUSTOM_HOOK)
        self.seed('.codex/hooks.json', json.dumps({'hooks': {'SessionEnd': [{'hooks': [
            {'type': 'command', 'command': '"$CLAUDE_PROJECT_DIR/' + root + '/hooks/session-end.sh"'}
            for root in ('.claude', '.codex', '.agents')]}]}}).encode('utf-8'))
        plan = self.installer.install(self.vault, self.state, plan_only=True,
                                      keep_customized=('.claude/hooks/session-end.sh',))
        groups = json.loads(plan['planned']['.codex/hooks.json'])['hooks'].values()
        self.assertEqual([h['command'] for group in sum(groups, []) for h in group['hooks'] if 'session-end.sh' in h['command']],
                         ['"$CLAUDE_PROJECT_DIR/.claude/hooks/session-end.sh"'])

    def test_keeping_a_missing_or_already_retired_runner_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'kept legacy runner not found .claude/scripts/flush.py'):
            self.installer.install(self.vault, self.state, plan_only=True,
                                   keep_customized=('.claude/scripts/flush.py',))
        runner = self.seed('.claude/scripts/flush.py', CUSTOM_RUNNER)
        result = self.cli('--accept-customized-legacy', '.claude/scripts/flush.py')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        with self.assertRaisesRegex(ValueError, 'legacy runner already retired'):
            self.installer.install(self.vault, self.state, plan_only=True,
                                   keep_customized=('.claude/scripts/flush.py',))
        keep = self.cli('--keep-customized-legacy', '.claude/scripts/flush.py')
        self.assertNotEqual(keep.returncode, 0)
        self.assertIn(b'BEYIN_V3_LEGACY_RETIRED', runner.read_bytes())
        self.assertNotIn('kept_legacy', json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8')))


if __name__ == '__main__':
    unittest.main()
