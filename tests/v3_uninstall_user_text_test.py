"""Uninstall takes out only what V3 manages. Text the user added to a managed file stays, also after a
reinstall or update recorded that text as installed content of a file V3 created."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
START, END = '<!-- beyin-v3:start -->', '<!-- beyin-v3:end -->'
NOTE = 'Kullanıcının kendi notu: kurulumdan sonra eklendi ve korunmalı.\n'
RULES = '# Kurallarım\nUSER_RULE_SENTINEL: yalnız benim.\n'
CLAUDE = '# Claude kurallarım\nBu dosya kendi talimatlarını taşır.\n'
IMPORT = '@AGENTS.md\n'
CREATED = ('AGENTS.md', 'CLAUDE.md', '.claude/settings.local.json', '.codex/hooks.json',
           '.agents/hooks.json', '.codex/config.toml')
CONFLICT = 'Uninstall conflict: managed file changed; preserve and reconcile '


class UninstallKeepsUserTextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-uninstall-user-text-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        self.count = 0
        self.fresh()

    def fresh(self):
        self.count += 1
        self.vault = self.base / ('Örnek Beyin %d' % self.count)
        self.vault.mkdir()
        self.state = self.base / ('state-%d' % self.count)

    def run_cli(self, script, *args):
        return subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True,
                              encoding='utf-8', env=self.env, cwd=self.vault, timeout=120)

    def ok(self, script, *args):
        result = self.run_cli(script, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def install(self, *extra):
        return self.ok(ROOT / 'scripts/install_v3.py', '--vault', self.vault, '--state', self.state, *extra)

    def uninstall(self):
        return self.install('--uninstall')

    def uninstall_conflict(self):
        result = self.run_cli(ROOT / 'scripts/install_v3.py', '--vault', self.vault, '--state', self.state, '--uninstall')
        self.assertEqual(result.returncode, 1, result.stdout)
        return json.loads(result.stderr)['message']

    def refresh(self, via):
        """Run the step that rewrites the install record over the user's edit."""
        if via == 'reinstall':
            self.install()
        elif via == 'update':
            package = self.base / 'upgrade.zip'
            if not package.exists():
                self.ok(ROOT / 'scripts/build_v3_release.py', '--output', package, '--version', '3.0.3')
            self.assertEqual(self.ok(self.vault / 'beyin.py', 'update', '--package', package)['version'], '3.0.3')

    def path(self, name):
        return self.vault / name

    def append(self, name, text):
        self.path(name).write_bytes(self.path(name).read_bytes() + text.encode('utf-8'))

    def seed(self, name, text):
        self.path(name).parent.mkdir(parents=True, exist_ok=True)
        self.path(name).write_bytes(text.encode('utf-8'))
        return text.encode('utf-8')

    def read_json(self, name):
        return json.loads(self.path(name).read_text(encoding='utf-8'))

    def edit_json(self, name, change):
        data = self.read_json(name)
        change(data)
        self.path(name).write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')

    def record(self, name):
        return json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))['files'][name]

    def snapshot(self):
        return {p.relative_to(self.vault).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.vault.rglob('*')) if p.is_file()}

    def test_created_routers_keep_text_added_after_install(self):
        for via in ('none', 'reinstall', 'update'):
            with self.subTest(via=via):
                self.fresh()
                self.install()
                self.assertIsNone(self.record('AGENTS.md')['original'])
                self.append('AGENTS.md', NOTE)
                self.append('CLAUDE.md', NOTE)
                self.refresh(via)
                if via != 'none':
                    # The record now claims the user's text as V3's own, in a file V3 created.
                    for name in ('AGENTS.md', 'CLAUDE.md'):
                        record = self.record(name)
                        self.assertIsNone(record['original'])
                        self.assertIn(NOTE, base64.b64decode(record['installed_content']).decode('utf-8'))
                result = self.uninstall()
                self.assertEqual(self.path('AGENTS.md').read_text(encoding='utf-8'), NOTE)
                # The import line written with the file stays: the user's text may rely on it.
                self.assertEqual(self.path('CLAUDE.md').read_text(encoding='utf-8'), IMPORT + NOTE)
                self.assertEqual(result.get('kept_user_text'), ['AGENTS.md', 'CLAUDE.md'])

    def test_existing_routers_get_original_back_with_later_text(self):
        for crlf in (False, True):
            with self.subTest(crlf=crlf):
                self.fresh()
                self.seed('AGENTS.md', RULES)
                self.seed('CLAUDE.md', CLAUDE)
                self.install()
                for name in ('AGENTS.md', 'CLAUDE.md'):
                    self.append(name, NOTE)
                    if crlf:
                        self.path(name).write_bytes(self.path(name).read_bytes().replace(b'\n', b'\r\n'))
                self.install()
                if crlf:
                    for name in ('AGENTS.md', 'CLAUDE.md'):
                        self.path(name).write_bytes(self.path(name).read_bytes().replace(b'\n', b'\r\n'))
                self.uninstall()
                for name, original in (('AGENTS.md', RULES), ('CLAUDE.md', CLAUDE)):
                    expected = (original.rstrip() + '\n\n' + NOTE).encode('utf-8')
                    self.assertEqual(self.path(name).read_bytes(), expected.replace(b'\n', b'\r\n') if crlf else expected)

    def test_router_with_byte_order_mark_keeps_user_text(self):
        self.install()
        self.path('AGENTS.md').write_bytes(b'\xef\xbb\xbf' + self.path('AGENTS.md').read_bytes() + NOTE.encode('utf-8'))
        self.install()
        self.uninstall()
        self.assertEqual(self.path('AGENTS.md').read_bytes(), b'\xef\xbb\xbf' + NOTE.encode('utf-8'))

    def test_symlinked_router_keeps_user_text(self):
        rules = self.seed('AGENTS.md', RULES)
        try:
            self.path('CLAUDE.md').symlink_to('AGENTS.md')
        except OSError:
            self.skipTest('symlink unavailable on host')
        self.install()
        self.append('CLAUDE.md', NOTE)
        self.install()
        self.uninstall()
        self.assertTrue(self.path('CLAUDE.md').is_symlink())
        self.assertEqual(self.path('AGENTS.md').read_bytes(), rules.rstrip() + b'\n\n' + NOTE.encode('utf-8'))

    def test_created_hook_files_keep_user_entries(self):
        own_hook = {'type': 'command', 'command': 'echo kendi-hook'}
        self.install()
        self.edit_json('.claude/settings.local.json', lambda d: d.setdefault('permissions', {'allow': ['Bash(git status)']}))
        self.edit_json('.codex/hooks.json', lambda d: d['hooks']['SessionStart'].append({'hooks': [own_hook]}))
        self.edit_json('.agents/hooks.json', lambda d: d.update(kendi={'Stop': [own_hook]}))
        self.install()
        result = self.uninstall()
        self.assertEqual(self.read_json('.claude/settings.local.json'), {'permissions': {'allow': ['Bash(git status)']}})
        self.assertEqual(self.read_json('.codex/hooks.json'), {'hooks': {'SessionStart': [{'hooks': [own_hook]}]}})
        self.assertEqual(self.read_json('.agents/hooks.json'), {'kendi': {'Stop': [own_hook]}})
        self.assertEqual(result.get('kept_user_text'), ['.agents/hooks.json', '.claude/settings.local.json', '.codex/hooks.json'])

    def test_hook_files_with_a_byte_order_mark_keep_user_entries(self):
        # Windows PowerShell 5.1 (Set-Content -Encoding UTF8) and many editors save JSON with a
        # UTF-8 BOM; install and update read these files as plain UTF-8 and stopped with
        # "Unexpected UTF-8 BOM (decode using utf-8-sig)".
        own_hook = {'type': 'command', 'command': 'echo kendi-hook'}
        bom = b'\xef\xbb\xbf'
        plain = {name: json.dumps({'hooks': {'Stop': [{'hooks': [own_hook]}]}}, indent=2).encode('utf-8')
                 for name in ('.claude/settings.json', '.claude/settings.local.json', '.codex/hooks.json')}
        plain['.agents/hooks.json'] = json.dumps({'kendi': {'Stop': [own_hook]}}, indent=2).encode('utf-8')
        for via in ('none', 'update'):
            with self.subTest(via=via):
                self.fresh()
                # Such an editor saves the user's files before install, or V3's between install and update.
                seeded = {name: (bom if via == 'none' else b'') + data for name, data in plain.items()}
                for name, data in seeded.items():
                    self.path(name).parent.mkdir(parents=True, exist_ok=True)
                    self.path(name).write_bytes(data)
                self.install()
                if via == 'update':
                    for name in seeded:
                        self.path(name).write_bytes(bom + self.path(name).read_bytes())
                    self.refresh(via)
                for name in seeded:
                    data = json.loads(self.path(name).read_bytes())
                    if name == '.agents/hooks.json':
                        self.assertEqual(data['kendi'], {'Stop': [own_hook]})
                        self.assertEqual(set(data['beyin-v3']), {'PreInvocation', 'Stop'})
                        continue
                    handlers = [h for group in data['hooks']['Stop'] for h in group['hooks']]
                    self.assertIn(own_hook, handlers, name)
                    if name != '.claude/settings.json':  # V3 adds its own hooks to the other two
                        self.assertEqual(len(handlers), 2, name)
                        self.assertEqual(set(data['hooks']), {'SessionStart', 'UserPromptSubmit', 'Stop',
                                                              'PostToolUse', 'PreCompact', 'SessionEnd'}, name)
                self.uninstall()
                for name, data in seeded.items():
                    self.assertEqual(self.path(name).read_bytes(), data, name)

    def test_existing_hook_files_get_original_back_with_later_entries(self):
        untouched = self.seed('.codex/hooks.json', json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo dur'}]}]}}))
        # A V2 entry install retires comes back next to the permission added after install.
        legacy = {'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': '"$CLAUDE_PROJECT_DIR/.claude/hooks/session-start.sh"', 'timeout': 15}]}]}}
        self.seed('.claude/settings.local.json', json.dumps(dict(legacy, permissions={'allow': ['Bash(ls)']})))
        self.install()
        self.assertNotIn('session-start.sh', self.path('.claude/settings.local.json').read_text(encoding='utf-8'))
        self.edit_json('.claude/settings.local.json', lambda d: d['permissions']['allow'].append('Bash(git status)'))
        self.install()
        self.uninstall()
        self.assertEqual(self.path('.codex/hooks.json').read_bytes(), untouched)
        self.assertEqual(self.read_json('.claude/settings.local.json'),
                         dict(legacy, permissions={'allow': ['Bash(ls)', 'Bash(git status)']}))

    def test_codex_config_keeps_user_tables(self):
        profile = '\n[profiles.kendi]\nmodel = "x"\n'
        self.install()
        self.append('.codex/config.toml', profile)
        self.install()
        self.uninstall()
        text = self.path('.codex/config.toml').read_text(encoding='utf-8')
        self.assertEqual(text.strip(), profile.strip())
        # An existing [features] table gets its own hooks value back; untouched files come back byte for byte.
        self.fresh()
        original = self.seed('.codex/config.toml', '[features]\nhooks = false\nweb_search = true\n')
        self.install()
        self.assertIn('hooks = true', self.path('.codex/config.toml').read_text(encoding='utf-8'))
        self.append('.codex/config.toml', profile)
        self.install()
        self.uninstall()
        self.assertEqual(self.path('.codex/config.toml').read_bytes(), original + profile.encode('utf-8'))
        self.fresh()
        original = self.seed('.codex/config.toml', '[features]\nweb_search = true\n')
        self.install()
        self.install()
        self.uninstall()
        self.assertEqual(self.path('.codex/config.toml').read_bytes(), original)

    def test_untouched_install_still_removes_every_created_file(self):
        self.install()
        self.install()
        result = self.uninstall()
        self.assertNotIn('kept_user_text', result)
        for name in CREATED:
            self.assertFalse(self.path(name).exists(), name)

    def test_changed_managed_part_still_conflicts_without_writing(self):
        self.install()
        self.append('AGENTS.md', NOTE)
        self.install()
        agents = self.path('AGENTS.md')
        cases = (
            ('AGENTS.md', lambda: agents.write_text(agents.read_text(encoding='utf-8').replace(
                'beyin.py sync', 'beyin.py sync # benim'), encoding='utf-8')),
            ('.claude/settings.local.json', lambda: self.edit_json('.claude/settings.local.json',
                                                                   lambda d: d['hooks'].pop('Stop'))),
            ('.kiro/skills/beyin/SKILL.md', lambda: self.append('.kiro/skills/beyin/SKILL.md', NOTE)),
        )
        for name, change in cases:
            with self.subTest(file=name):
                saved = self.path(name).read_bytes()
                change()
                before = self.snapshot()
                self.assertIn(CONFLICT + name, self.uninstall_conflict())
                self.assertEqual(self.snapshot(), before)
                self.path(name).write_bytes(saved)
        self.uninstall()
        self.assertEqual(agents.read_text(encoding='utf-8'), NOTE)


if __name__ == '__main__':
    unittest.main()
