"""The generated AGENTS.md block must separate direct user statements from agent inferences."""
import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

from v3_package_helpers import inherited_env

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
START, END = '<!-- beyin-v3:start -->', '<!-- beyin-v3:end -->'
ENGLISH = ('A preference, decision or fact the user states directly is not an inference; '
           'record it promptly without waiting to be asked.')
TURKISH = ('Kullanıcının doğrudan söylediği tercih, karar ve olgu çıkarım değildir; '
           'istenmesini beklemeden kaydedilir.')
# #375: "record it promptly" is about timing; the target of a correction is chosen first.
SCOPE_ENGLISH = ('Promptly is about timing, not target: a correction that belongs to one project or area goes to '
                 '`Kurallar.md` in the existing project folder (`🏰 300-Projects/<project>/` in the official template, or legacy `projects/<project>/`; do not create a second folder for the same project), leaving at most a one-line pointer in Kurallar.md.')
SCOPE_TURKISH = ('Beklemeden kaydetmek hedefi değiştirmez: tek bir projeye ya da alana ait düzeltme '
                 '`mevcut-proje-klasörü/Kurallar.md` (resmî şablonda `🏰 300-Projects/<proje>/Kurallar.md`, eski kasalarda `projects/<proje>/Kurallar.md`; aynı proje için ikinci klasör açma) dosyasına gider, Kurallar.md\'de en çok bir satırlık işaretçi kalır; '
                 'yalnız her işte geçerli kural Kurallar.md\'ye tam metin girer.')
CLAUSES = (ENGLISH, TURKISH, SCOPE_ENGLISH, SCOPE_TURKISH)
NOTE = '\nKullanıcının kendi notu: bu paragraf blok dışında ve korunmalı.\n'
RULES = '# Kurallarım\nUSER_RULE_SENTINEL: yalnız AGENTS.md içinde yaşar.\n'
CLAUDE = '# Claude kuralları\nBu dosya kendi talimatlarını taşır.\n'
IMPORT = b'@AGENTS.md\n'
STARTER_TEXTS = ('template/.agents/skills/beyin/SKILL.md', 'template/.agents/skills/beyin-doktor/SKILL.md',
                 'template/.agents/skills/beyin-guncelle/SKILL.md', 'template/CLAUDE.md')


def hard_wrapped_lines(text):
    """Lines that continue the paragraph or list item above them (#361); fences and tables are skipped."""
    if text.startswith('---\n'):
        text = text.split('\n---\n', 1)[1]
    found, chunk, fence = [], [], False

    def flush():
        if chunk and not chunk[0].startswith(('#', '|')):
            starts = re.compile(r'(?:[-*] |\d+\. |\*\*)') if re.match(r'(?:[-*] |\d+\. |\*\*)', chunk[0]) else None
            found.extend(line for line in chunk[1:] if starts is None or not starts.match(line))
        chunk.clear()
    for line in text.split('\n'):
        if line.startswith('```'):
            flush()
            fence = not fence
        elif not fence and line.strip():
            chunk.append(line)
        else:
            flush()
    flush()
    return found


class InstructionBlockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-instruction-block-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault = self.base / 'Örnek Beyin'
        self.vault.mkdir()
        self.state = self.base / 'state'
        self.env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')

    def run_cli(self, script, *args):
        result = subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True,
                                text=True, encoding='utf-8', env=self.env, cwd=self.vault, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def install(self, *extra):
        return self.run_cli(ROOT / 'scripts/install_v3.py', '--vault', self.vault, '--state', self.state, *extra)

    def install_conflict(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state)], capture_output=True, text=True, encoding='utf-8',
                                env=self.env, cwd=self.vault, timeout=120)
        self.assertEqual(result.returncode, 1, result.stdout)
        return json.loads(result.stderr)['message']

    def seed(self, name, text):
        (self.vault / name).write_bytes(text.encode())
        return text.encode()

    def manifest(self):
        return json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))

    def as_previous_install(self, original):
        """Leave CLAUDE.md and its manifest entry exactly as the pre-#84 installer did."""
        data = ((original or '').rstrip() + '\n\n' + START + self.block() + END + '\n').encode()
        (self.vault / 'CLAUDE.md').write_bytes(data)
        manifest = self.manifest()
        manifest['files']['CLAUDE.md'] = {
            'original': base64.b64encode(original.encode()).decode() if original is not None else None,
            'installed_hash': hashlib.sha256(data).hexdigest(), 'installed_content': base64.b64encode(data).decode()}
        (self.state / 'v3-install.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return data

    def update(self):
        package = self.base / 'upgrade.zip'
        if not package.exists():
            self.run_cli(ROOT / 'scripts/build_v3_release.py', '--output', package, '--version', '3.0.3')
        self.assertEqual(self.run_cli(self.vault / 'beyin.py', 'update', '--package', package)['version'], '3.0.3')

    def block(self, name='AGENTS.md'):
        text = (self.vault / name).read_text(encoding='utf-8')
        return text.split(START)[1].split(END)[0]

    def downgrade(self):
        """Rewrite both routers as a vault installed before this clause existed."""
        manifest_path = self.state / 'v3-install.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for name in ('AGENTS.md', 'CLAUDE.md'):
            path = self.vault / name
            text = path.read_text(encoding='utf-8')
            for sentence in CLAUSES:
                pattern = r'\s*' + r'\s+'.join(map(re.escape, sentence.split()))
                text, count = re.subn(pattern, '', text, count=1)
                self.assertEqual(count, 1, 'clause missing from generated ' + name)
            data = text.encode()
            manifest['files'][name] = dict(manifest['files'][name], installed_hash=hashlib.sha256(data).hexdigest(),
                                           installed_content=base64.b64encode(data).decode())
            # AGENTS.md also carries a paragraph the user added after that install, so its hash
            # cannot match and the reinstall gate has to fall through to the block comparison.
            path.write_text(text + NOTE if name == 'AGENTS.md' else text, encoding='utf-8')
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def downgrade_record_only(self):
        """Point the install record at the older block without touching the files."""
        manifest_path = self.state / 'v3-install.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for name in ('AGENTS.md', 'CLAUDE.md'):
            text = (self.vault / name).read_text(encoding='utf-8')
            for sentence in CLAUSES:
                text = re.sub(r'\s*' + r'\s+'.join(map(re.escape, sentence.split())), '', text, count=1)
            data = text.encode()
            manifest['files'][name] = dict(manifest['files'][name], installed_hash=hashlib.sha256(data).hexdigest(),
                                           installed_content=base64.b64encode(data).decode())
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def test_generated_block_records_direct_user_statements_in_both_languages(self):
        self.seed('CLAUDE.md', CLAUDE)
        self.install()
        for name in ('AGENTS.md', 'CLAUDE.md'):
            with self.subTest(file=name):
                block = self.block(name)
                self.assertIn(' '.join(ENGLISH.split()), ' '.join(block.split()))
                self.assertIn(' '.join(TURKISH.split()), ' '.join(block.split()))

    def test_correction_target_is_chosen_before_recording(self):
        # #375: the target of a user correction was fixed to Kurallar.md and scope was only a label,
        # so project rules piled up in the global file every session opens with.
        self.seed('CLAUDE.md', CLAUDE)
        self.install()
        for name in ('AGENTS.md', 'CLAUDE.md'):
            with self.subTest(file=name):
                block = ' '.join(self.block(name).split())
                for sentence in CLAUSES:
                    self.assertIn(' '.join(sentence.split()), block)
                self.assertNotIn("kural olarak Kurallar'a", block)
        scripts = str(ROOT / 'template/.claude/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import beyin_v3_companion as companion
        skill = ' '.join((ROOT / 'template/.agents/skills/beyin/SKILL.md').read_text(encoding='utf-8').split())
        self.assertIn('ama önce hedefini seç', skill)
        self.assertIn('`mevcut-proje-klasörü/Kurallar.md` (resmî şablonda `🏰 300-Projects/<proje>/Kurallar.md`, eski kasalarda `projects/<proje>/Kurallar.md`; aynı proje için ikinci klasör açma) dosyasına yaz', skill)
        # Document actual folder discovery and local project resolution (#456/#475).
        source = (ROOT / 'template/.claude/scripts/beyin_v3_companion.py').read_text(encoding='utf-8')
        folders = ast.literal_eval(re.search(r'for folder in (\([^)]*\))', source)[1])
        for folder in folders:
            self.assertIn('`' + folder + '/`', skill)
        self.assertIn('çalışma dizininden veya sorguda geçen tek bir bilinen projeden', skill)
        self.assertIn('belirsiz eşleşmede proje seçmez', skill)
        self.assertIn('arama kapsamını değiştirmez', skill)
        # The pointer the skill shows is a rule the hook really scopes.
        pointer = re.search(r'`(- \[kapsam: <proje>\][^`]*)`', skill)[1].replace('<proje>', 'alpha')
        self.assertIn('mevcut-proje-klasörü/Kurallar.md', pointer)
        self.assertIn('`🏰 300-Projects/`', skill)
        self.assertEqual(companion.rule_scopes(pointer), {'alpha'})
        self.assertEqual(companion.filter_scoped_rules(pointer, 'alpha'), pointer)
        self.assertEqual(companion.filter_scoped_rules(pointer, None), pointer)
        self.assertNotIn('alpha', companion.filter_scoped_rules(pointer, 'beta'))
        router = (ROOT / 'template/CLAUDE.md').read_text(encoding='utf-8')
        seeds = (companion.STARTERS['Kurallar.md'], (ROOT / 'template/🔮 850-Companion/Kurallar.md').read_text(encoding='utf-8'))
        for text in (skill, router) + seeds:
            self.assertIn('projects/<proje>/Kurallar.md', ' '.join(text.split()))
        for text in (skill, router, self.block()) + seeds:
            for forbidden in ('\u2014', '\u2013', '\u00e2'):  # em dash, en dash, circumflex a
                self.assertNotIn(forbidden, text)

    def test_generated_block_paragraphs_are_single_line_softwrapped(self):
        self.seed('CLAUDE.md', CLAUDE)
        self.install()
        block = self.block('AGENTS.md')
        paragraphs = [p.strip() for p in block.strip().split('\n\n') if p.strip()]
        for p in paragraphs:
            if p.startswith('##'):
                continue
            self.assertNotIn('\n', p, 'Paragraph in AGENTS.md block contains manual line break')

    def test_starter_skills_and_router_template_keep_paragraphs_on_one_line(self):
        # #361: a paragraph wrapped by hand near 80 columns splits a sentence in agent context.
        for name in STARTER_TEXTS:
            with self.subTest(file=name):
                self.assertEqual(hard_wrapped_lines((ROOT / name).read_text(encoding='utf-8')), [])

    def test_update_replaces_hard_wrapped_block_installed_by_older_release(self):
        # #361: releases up to 3.10.0 wrapped the block near 80 columns. An update finds the block
        # by its markers and swaps that text for the single-line one; it must not add a second block.
        self.seed('CLAUDE.md', CLAUDE)
        self.install()
        fresh = {name: self.block(name) for name in ('AGENTS.md', 'CLAUDE.md')}
        manifest_path = self.state / 'v3-install.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for name, block in fresh.items():
            wrapped = '\n\n'.join(part if part.lstrip().startswith('##') else
                                  textwrap.fill(part, 80, break_long_words=False, break_on_hyphens=False)
                                  for part in block.split('\n\n'))
            self.assertNotEqual(wrapped, block)
            path = self.vault / name
            data = path.read_text(encoding='utf-8').replace(block, wrapped).encode()
            manifest['files'][name] = dict(manifest['files'][name], installed_hash=hashlib.sha256(data).hexdigest(),
                                           installed_content=base64.b64encode(data).decode())
            path.write_bytes(data + (NOTE.encode() if name == 'AGENTS.md' else b''))
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self.update()
        for name, block in fresh.items():
            with self.subTest(file=name):
                text = (self.vault / name).read_text(encoding='utf-8')
                self.assertEqual((text.count(START), text.count(END)), (1, 1))
                self.assertEqual(self.block(name), block)
        self.assertIn(NOTE.strip(), (self.vault / 'AGENTS.md').read_text(encoding='utf-8'))

    def test_block_carries_no_machine_path(self):
        # #112: AGENTS.md may be synced between machines; the block must read the same on each.
        self.seed('CLAUDE.md', CLAUDE)
        self.install()
        other_vault, other_state = self.base / 'İkinci Makine' / 'Beyin', self.base / 'other-state'
        other_vault.mkdir(parents=True)
        (other_vault / 'CLAUDE.md').write_bytes(CLAUDE.encode())
        self.run_cli(ROOT / 'scripts/install_v3.py', '--vault', other_vault, '--state', other_state)
        for name in ('AGENTS.md', 'CLAUDE.md'):
            with self.subTest(file=name):
                self.assertEqual(self.block(name), (other_vault / name).read_text(encoding='utf-8').split(START)[1].split(END)[0])
                for machine in (str(self.vault), str(self.state), str(other_vault), sys.executable,
                                self.vault.as_posix(), '.claude/scripts/beyin_v3_cli.py'):
                    self.assertNotIn(machine, self.block(name))
                self.assertIn('beyin.py sync', self.block(name))

    def test_router_synced_from_a_machine_already_updated_does_not_conflict(self):
        # Machine A updated and pushed its routers; machine B pulled them before its own update.
        self.seed('CLAUDE.md', CLAUDE)
        self.install()
        current = {name: (self.vault / name).read_bytes() for name in ('AGENTS.md', 'CLAUDE.md')}
        self.downgrade()  # B's own install record is the older block
        for name, data in current.items():
            (self.vault / name).write_bytes(data + (NOTE.encode() if name == 'AGENTS.md' else b''))
        self.install()
        self.assertIn(NOTE.strip(), (self.vault / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertEqual((self.vault / 'CLAUDE.md').read_bytes(), current['CLAUDE.md'])
        # An edit inside the block is still the user's and still stops the installer.
        path = self.vault / 'AGENTS.md'
        path.write_text(path.read_text(encoding='utf-8').replace('beyin.py sync', 'beyin.py sync # benim'), encoding='utf-8')
        self.downgrade_record_only()
        self.assertIn('managed file changed', self.install_conflict())

    def test_reinstall_over_previous_block_adopts_the_clause_and_keeps_user_text(self):
        self.seed('CLAUDE.md', CLAUDE)
        self.install()
        self.downgrade()
        self.install()
        for name in ('AGENTS.md', 'CLAUDE.md'):
            for sentence in CLAUSES:
                self.assertIn(' '.join(sentence.split()), ' '.join(self.block(name).split()))
        self.assertIn(NOTE.strip(), (self.vault / 'AGENTS.md').read_text(encoding='utf-8'))

    def test_crlf_router_with_user_text_outside_the_block_still_reinstalls(self):
        self.seed('CLAUDE.md', CLAUDE)
        self.install()
        self.downgrade()
        path = self.vault / 'AGENTS.md'
        path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
        self.install()
        self.assertIn(' '.join(ENGLISH.split()), ' '.join(self.block().split()))
        self.assertIn(NOTE.strip(), path.read_text(encoding='utf-8'))

    def test_update_and_rollback_over_previous_block_keep_working(self):
        self.seed('CLAUDE.md', CLAUDE)
        self.install()
        self.downgrade()
        self.update()
        for name in ('AGENTS.md', 'CLAUDE.md'):
            text = (self.vault / name).read_text(encoding='utf-8')
            self.assertEqual((text.count(START), text.count(END)), (1, 1))
            for sentence in (TURKISH, SCOPE_TURKISH, SCOPE_ENGLISH):
                self.assertIn(' '.join(sentence.split()), ' '.join(self.block(name).split()))
        self.assertIn(NOTE.strip(), (self.vault / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertEqual(self.run_cli(self.vault / 'beyin.py', 'rollback')['status'], 'rolled_back')
        for name in ('AGENTS.md', 'CLAUDE.md'):
            for sentence in (TURKISH, SCOPE_TURKISH):
                self.assertNotIn(' '.join(sentence.split()), ' '.join(self.block(name).split()))
        self.assertIn(NOTE.strip(), (self.vault / 'AGENTS.md').read_text(encoding='utf-8'))

    def test_missing_claude_md_is_created_as_an_agents_md_import(self):
        rules = self.seed('AGENTS.md', RULES)
        self.install()
        self.assertEqual((self.vault / 'CLAUDE.md').read_bytes(), IMPORT)
        agents = (self.vault / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('USER_RULE_SENTINEL', agents)
        self.assertEqual(agents.count(START), 1)
        self.assertIsNone(self.manifest()['files']['CLAUDE.md']['original'])
        self.install()
        self.assertEqual((self.vault / 'CLAUDE.md').read_bytes(), IMPORT)
        self.install('--uninstall')
        self.assertFalse((self.vault / 'CLAUDE.md').exists())
        self.assertEqual((self.vault / 'AGENTS.md').read_bytes(), rules)

    def test_claude_md_that_imports_agents_md_is_not_given_a_second_block(self):
        rules, claude = self.seed('AGENTS.md', RULES), self.seed('CLAUDE.md', CLAUDE + '\n@AGENTS.md\n')
        self.install()
        self.assertEqual((self.vault / 'CLAUDE.md').read_bytes(), claude)
        self.assertNotIn('CLAUDE.md', self.manifest()['files'])
        self.assertEqual((self.vault / 'AGENTS.md').read_text(encoding='utf-8').count(START), 1)
        self.install()
        self.install('--uninstall')
        self.assertEqual([(self.vault / name).read_bytes() for name in ('AGENTS.md', 'CLAUDE.md')], [rules, claude])

    def test_claude_md_without_import_keeps_its_own_block(self):
        claude = self.seed('CLAUDE.md', CLAUDE)
        self.install()
        text = (self.vault / 'CLAUDE.md').read_text(encoding='utf-8')
        self.assertTrue(text.startswith(CLAUDE))
        self.assertEqual(text.count(START), 1)
        self.install('--uninstall')
        self.assertEqual((self.vault / 'CLAUDE.md').read_bytes(), claude)

    def test_symlinked_router_is_written_once(self):
        for link, target in (('CLAUDE.md', 'AGENTS.md'), ('AGENTS.md', 'CLAUDE.md')):
            with self.subTest(link=link):
                self.vault, self.state = self.base / ('vault-' + link), self.base / ('state-' + link)
                self.vault.mkdir()
                rules = self.seed(target, RULES)
                try: (self.vault / link).symlink_to(target)
                except OSError: self.skipTest('symlink unavailable on host')
                self.install()
                self.install()
                self.assertTrue((self.vault / link).is_symlink())
                self.assertEqual(list(self.manifest()['files']).count(target), 1)
                self.assertNotIn(link, self.manifest()['files'])
                self.assertEqual((self.vault / target).read_text(encoding='utf-8').count(START), 1)
                self.install('--uninstall')
                self.assertTrue((self.vault / link).is_symlink())
                self.assertEqual((self.vault / target).read_bytes(), rules)

    def test_update_migrates_block_only_claude_md_from_previous_install(self):
        rules = self.seed('AGENTS.md', RULES)
        self.install()
        self.as_previous_install(None)
        self.update()
        self.assertEqual((self.vault / 'CLAUDE.md').read_bytes(), IMPORT)
        agents = (self.vault / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('USER_RULE_SENTINEL', agents)
        self.assertEqual(agents.count(START), 1)
        self.install('--uninstall')
        self.assertFalse((self.vault / 'CLAUDE.md').exists())
        self.assertEqual((self.vault / 'AGENTS.md').read_bytes(), rules)

    def test_update_drops_duplicate_block_from_importing_claude_md(self):
        original = CLAUDE + '\n@./AGENTS.md\n'
        rules, claude = self.seed('AGENTS.md', RULES), self.seed('CLAUDE.md', original)
        self.install()
        self.as_previous_install(original)
        self.update()
        self.assertEqual((self.vault / 'CLAUDE.md').read_bytes(), claude)
        self.assertEqual((self.vault / 'AGENTS.md').read_text(encoding='utf-8').count(START), 1)
        self.install('--uninstall')
        self.assertEqual([(self.vault / name).read_bytes() for name in ('AGENTS.md', 'CLAUDE.md')], [rules, claude])

    def test_block_removed_by_hand_from_importing_claude_md_is_not_a_conflict(self):
        original = CLAUDE + '\n@AGENTS.md\n'
        self.seed('CLAUDE.md', original)
        self.install()
        self.as_previous_install(original)
        edited = self.seed('CLAUDE.md', original + '\nKullanıcı bloğu elle kaldırdı.\n')
        self.install()
        self.assertEqual((self.vault / 'CLAUDE.md').read_bytes(), edited)

    def test_user_edits_to_claude_md_still_conflict_or_survive(self):
        self.install()
        path = self.vault / 'CLAUDE.md'
        path.write_bytes(b'# Kendi Claude dosyam, import yok\n')
        self.assertIn('Reinstall conflict: managed file changed CLAUDE.md', self.install_conflict())
        created = self.as_previous_install(None)
        edited = created.replace(b'## V3 companion', b'## Edited companion')
        path.write_bytes(edited)
        self.assertIn('Reinstall conflict: managed file changed CLAUDE.md', self.install_conflict())
        self.assertEqual(path.read_bytes(), edited)
        # Text the user added around the block is theirs: the block is refreshed, the file is not replaced.
        path.write_bytes(created + NOTE.encode())
        self.install()
        text = path.read_text(encoding='utf-8')
        self.assertIn(NOTE.strip(), text)
        self.assertEqual(text.count(START), 1)
        importing = self.seed('CLAUDE.md', CLAUDE + '\n@AGENTS.md\n\n' + START + '\nkullanıcı düzenlemesi\n' + END + '\n')
        self.assertIn('Reinstall conflict: managed file changed CLAUDE.md', self.install_conflict())
        self.assertEqual(path.read_bytes(), importing)

    def test_managed_file_matching_planned_content_survives_reinstall(self):
        """#189: a managed file another machine already moved to this release is not a conflict.

        The record still names the older release this machine installed, so neither the hash nor
        the stored content matches the disk; only the planned bytes do. A CRLF checkout of those
        bytes is the same file. An edit on top of them, or of the older bytes, still conflicts.
        """
        names = ('.claude/scripts/beyin_v3_hook.py', '.agents/skills/beyin/SKILL.md', 'beyin.py')
        self.install()
        planned = {name: (self.vault / name).read_bytes() for name in names}
        older = {name: data + b'\n# older release\n' for name, data in planned.items()}
        manifest = self.manifest()
        for name in names:
            manifest['files'][name].update(installed_hash=hashlib.sha256(older[name]).hexdigest(),
                                           installed_content=base64.b64encode(older[name]).decode())
        (self.state / 'v3-install.json').write_text(json.dumps(manifest), encoding='utf-8')
        (self.vault / 'beyin.py').write_bytes(planned['beyin.py'].replace(b'\n', b'\r\n'))
        self.install()
        for name in names:
            self.assertEqual((self.vault / name).read_bytes(), planned[name])
            self.assertEqual(self.manifest()['files'][name]['installed_hash'], hashlib.sha256(planned[name]).hexdigest())
        manifest = self.manifest()
        hook = self.vault / names[0]
        for edited in (planned[names[0]] + b'\n# custom user edit\n', older[names[0]] + b'# custom user edit\n'):
            manifest['files'][names[0]].update(installed_hash=hashlib.sha256(older[names[0]]).hexdigest(),
                                               installed_content=base64.b64encode(older[names[0]]).decode())
            (self.state / 'v3-install.json').write_text(json.dumps(manifest), encoding='utf-8')
            hook.write_bytes(edited)
            self.assertIn('Reinstall conflict: managed file changed ' + names[0], self.install_conflict())
            self.assertEqual(hook.read_bytes(), edited)

    def test_generated_block_mandates_note_read_and_note_edit_for_human_notes(self):
        """#465: AGENTS.md closing protocol must instruct note-read and note-edit for existing human notes."""
        self.seed('CLAUDE.md', CLAUDE)
        self.install()
        for name in ('AGENTS.md', 'CLAUDE.md'):
            with self.subTest(file=name):
                block = self.block(name)
                self.assertIn('var olan insan notlarını `note-read` ile oku, `note-edit` ile düzenle', block)
                self.assertIn('Read and edit existing human notes with `note-read` and `note-edit`.', block)
        router = (ROOT / 'template/CLAUDE.md').read_text(encoding='utf-8')
        self.assertIn('var olan insan notlarını `note-read` ile oku, `note-edit` ile düzenle', router)

    def test_closing_protocol_indexes_concepts_before_final_receipt(self):
        self.seed('CLAUDE.md', CLAUDE)
        self.install()
        texts = [self.block(name) for name in ('AGENTS.md', 'CLAUDE.md')]
        texts.append((ROOT / 'template/CLAUDE.md').read_text(encoding='utf-8'))
        for text in texts:
            self.assertIn('Kavram yazınca `knowledge/index.md` kaydını tek satırla güncelle', text)
            self.assertIn('gerçek ilişki varsa `knowledge/connections/`', text)
            self.assertIn('receipt en son, kart ve Threads düzenlemelerinden sonra', text)

if __name__ == '__main__':
    unittest.main()
