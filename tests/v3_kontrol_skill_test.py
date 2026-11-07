"""beyin-kontrol (#589): a read-only memory audit skill the user starts by name.

The skill ships inside the checksum-listed installer, because released updaters accept only the
three older starter SKILL.md members. These tests pin the package shape, the install and rollback
of the mirrored files, a user's own same-named skill, and the read-only promise of the commands
the skill tells the agent to run.
"""
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

from v3_package_helpers import ROOT, build_package, inherited_env, run_python, snapshot

SKILL = ROOT / 'template/.agents/skills/beyin-kontrol/SKILL.md'
ROOTS = ('.agents', '.claude', '.kiro')
STARTERS = ('beyin', 'beyin-doktor', 'beyin-guncelle', 'beyin-kontrol')
# beyin_v3_update.allowed() as released up to 3.12.0. Those updaters install every later
# release, so a package member outside this list would stop them before the new installer runs.
RELEASED_ALLOWLIST = re.compile(r'scripts/(?:install_v3|beyin_v3|beyin_entry)\.py|'
                                r'template/\.claude/scripts/beyin_v3(?:_[a-z]+)*\.py|'
                                r'template/\.agents/skills/(?:beyin|beyin-doktor|beyin-guncelle)/SKILL\.md')


def installer():
    spec = importlib.util.spec_from_file_location('beyin_kontrol_installer', ROOT / 'scripts/install_v3.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class KontrolSkillTextTest(unittest.TestCase):
    def test_installer_carries_the_template_bytes(self):
        self.assertEqual(installer().AUDIT_SKILL_TEXT.encode('utf-8'), SKILL.read_bytes())

    def test_skill_is_user_invoked_read_only_and_names_real_commands(self):
        text = SKILL.read_text(encoding='utf-8')
        front = text.split('\n---\n', 1)[0]
        self.assertIn('name: beyin-kontrol', front)
        self.assertIn('Yalnız kullanıcı açıkça', front)
        self.assertIn('kendiliğinden çalıştırma', front)
        self.assertIn('Denetim salt okunurdur.', text)
        self.assertIn('Hiçbir düzeltme onaylanmadıysa hiçbir şey yazma', text)
        for character in ('—', '–', 'â'):
            self.assertNotIn(character, text)
        source = (ROOT / 'scripts/beyin_v3.py').read_text(encoding='utf-8')
        commands = set(re.findall(r'add_parser\("([a-z-]+)"', source))
        used = set(re.findall(r'`python3 beyin\.py ([a-z-]+)', text))
        self.assertTrue({'doctor', 'triage', 'audit', 'recap'} <= used, used)
        self.assertLessEqual(used, commands)
        for name in ('note-read', 'note-edit', 'task-update', 'companion-compact'):
            self.assertIn('`' + name + '`', text)
            self.assertIn(name, commands)


class KontrolSkillInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-kontrol-skill-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault = self.base / 'Örnek Kasa'
        self.vault.mkdir()
        self.state = self.base / 'state'
        home = self.base / 'home'
        home.mkdir()
        self.env = inherited_env(HOME=str(home), USERPROFILE=str(home), BEYIN_V3_NO_SPAWN='1',
                                 BEYIN_UPDATES_OFF='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')

    def run_ok(self, script, *args, cwd=None):
        result = run_python(script, args, cwd or ROOT, self.env)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        return json.loads(result.stdout)

    def install(self, *args):
        return self.run_ok(ROOT / 'scripts/install_v3.py', '--vault', self.vault, '--state', self.state, *args)

    def beyin(self, *args):
        return self.run_ok(self.vault / 'beyin.py', *args, cwd=self.vault)

    def manifest(self):
        return json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))

    def skill_paths(self, name='beyin-kontrol'):
        return [self.vault / root / 'skills' / name / 'SKILL.md' for root in ROOTS]

    def test_package_keeps_released_member_list_and_installs_every_mirror(self):
        package = build_package(self.base / 'release.zip', '3.13.0', self.env)
        extracted = self.base / 'package'
        with zipfile.ZipFile(package) as archive:
            names = [name for name in archive.namelist() if name != 'manifest.json']
            archive.extractall(extracted)
        self.assertEqual([name for name in names if not RELEASED_ALLOWLIST.fullmatch(name)], [])
        self.assertNotIn('template/.agents/skills/beyin-kontrol/SKILL.md', names)
        result = self.run_ok(extracted / 'scripts/install_v3.py', '--vault', self.vault, '--state', self.state,
                             cwd=self.vault)
        self.assertIn('beyin-kontrol', result['skills']['synced'])
        self.assertNotIn('kept_user_skills', result['skills'])
        for path in self.skill_paths():
            self.assertEqual(path.read_bytes(), SKILL.read_bytes(), path)
        files = self.manifest()['files']
        for root in ROOTS:
            self.assertIn(root + '/skills/beyin-kontrol/SKILL.md', files)
        self.assertEqual(self.beyin('skill-sync')['conflicts'], [])
        references = self.beyin('doctor')['instruction_references']
        self.assertEqual(references['dead'], [])

    def test_existing_user_skill_with_the_same_name_is_kept_through_install_and_reinstall(self):
        own = self.vault / '.agents/skills/beyin-kontrol/SKILL.md'
        own.parent.mkdir(parents=True)
        own.write_text('---\nname: beyin-kontrol\ndescription: Kendi denetimim.\n---\n\nKendi akışım.\n',
                       encoding='utf-8')
        before = own.read_bytes()
        for _ in range(2):
            result = self.install()
            self.assertEqual(result['skills']['kept_user_skills'], ['beyin-kontrol'])
            self.assertNotIn('beyin-kontrol', result['skills']['synced'])
            self.assertEqual(own.read_bytes(), before)
            self.assertFalse((self.vault / '.claude/skills/beyin-kontrol').exists())
            self.assertFalse(any('beyin-kontrol' in name for name in self.manifest()['files']))
        self.assertEqual(self.beyin('skill-sync')['conflicts'], [])
        self.assertEqual(own.read_bytes(), before)

    def test_rollback_past_the_install_that_added_skills_leaves_no_empty_skill_folder(self):
        # The earlier install has no skills, like a release before beyin-kontrol; the next one
        # adds them, skill sync records them, and rollback removes them again.
        self.install('--exclude-component', 'skills')
        for name in STARTERS:
            self.assertFalse((self.vault / '.agents/skills' / name).exists(), name)
        self.install('--include-component', 'skills')
        self.assertEqual(self.beyin('skill-sync')['synced'], list(STARTERS))
        keep = self.vault / '.claude/skills/beyin-doktor/NOTLARIM.md'
        keep.write_text('Kullanıcının kendi dosyası.\n', encoding='utf-8')
        self.assertEqual(self.beyin('rollback')['status'], 'rolled_back')
        for name in STARTERS:
            for root in ROOTS:
                folder = self.vault / root / 'skills' / name
                self.assertFalse((folder / 'SKILL.md').exists(), folder)
                if folder != keep.parent:
                    self.assertFalse(folder.exists(), folder)
        self.assertEqual(keep.read_text(encoding='utf-8'), 'Kullanıcının kendi dosyası.\n')
        conflicts = self.beyin('skill-sync')['conflicts']
        # The folder that still holds the user's file stays theirs to resolve; no other skill is.
        self.assertEqual(conflicts, ['beyin-doktor'])

    def test_excluding_skills_after_sync_leaves_no_empty_skill_folder(self):
        self.install()
        self.assertEqual(self.beyin('skill-sync')['conflicts'], [])
        result = self.install('--exclude-component', 'skills')
        self.assertEqual(len(result['removed']), len(STARTERS) * len(ROOTS))
        for name in STARTERS:
            for root in ROOTS:
                self.assertFalse((self.vault / root / 'skills' / name).exists(), (root, name))
        self.assertEqual(self.beyin('skill-sync')['conflicts'], [])
        self.install('--include-component', 'skills')
        for path in self.skill_paths():
            self.assertEqual(path.read_bytes(), SKILL.read_bytes())
        self.assertEqual(self.beyin('skill-sync')['conflicts'], [])

    def test_collection_commands_named_by_the_skill_do_not_write_the_vault(self):
        self.install()
        companion = next(p for p in self.vault.iterdir() if p.name.endswith('850-Companion'))
        (companion / 'Threads.md').write_text(
            '# Threads\n\n## Active Threads\n\n### Site geliştirmesi (2026-01-21, KULLANICI TESTİ BEKLİYOR)\n'
            '- Dev kopyasında, canlı yok.\n\n### Tarihsiz konu\n- Sonraki adım belirsiz.\n\n## Closed Threads\n',
            encoding='utf-8')
        self.beyin('sync')
        before = snapshot(self.vault)
        triage = self.beyin('triage')
        self.assertEqual([row['title'] for row in triage['threads']['undated_threads']], ['Tarihsiz konu'])
        self.assertEqual(len(triage['threads']['stale_threads']), 1)
        self.assertIn('findings', self.beyin('audit'))
        self.assertIn('instruction_references', self.beyin('doctor'))
        self.assertEqual(self.beyin('recap', '--days', '14')['status'], 'ok')
        self.assertEqual(snapshot(self.vault), before)


if __name__ == '__main__':
    unittest.main()
