"""Doctor lists in-vault links in instruction and skill files that point nowhere (#94, item 5).

Information only: no model call, no write, and the doctor status is not raised by it."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unicodedata
import unittest

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
SCRIPTS = ROOT / 'template/.claude/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import beyin_v3_references as references  # noqa: E402

COMPANION = '🔮 850-Companion'


class ReferencesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-references-')
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name) / 'Örnek Beyin'
        self.write('knowledge/concepts/Var Olan Not.md', '# Var olan not\n')
        self.write('knowledge/concepts/ek.png', 'png')
        self.write(COMPANION + '/Core.md', '# Kimlik\n')

    def write(self, name, text):
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
        return path

    def dead(self):
        return [(entry['file'], entry['line'], entry['target']) for entry in references.check(self.vault)['dead']]

    def test_live_links_in_every_form_are_not_reported(self):
        self.write('AGENTS.md', '\n'.join([
            '[[Var Olan Not]] [[var olan not|takma ad]] [[Var Olan Not#Başlık]] [[Var Olan Not\\|tablo]]',
            '[[knowledge/concepts/Var Olan Not]] ![[ek.png]] [[#yalnız başlık]]',
            '[not](knowledge/concepts/Var%20Olan%20Not.md) [not](<knowledge/concepts/Var Olan Not.md>)',
            '[kök](/knowledge/concepts/Var Olan Not.md#bolum) [dış](https://example.com/x.md) [posta](mailto:a@b.c)',
            '[çapa](#bolum) [dışarı](../baska-vault/not.md)', '']))
        self.assertEqual(self.dead(), [])

    def test_dead_links_are_listed_with_file_line_and_target(self):
        self.write('AGENTS.md', '# Talimat\n\nÖnce [[Silinmiş Not]] oku.\n[rehber](docs/yok.md)\n')
        self.write('.agents/skills/ornek/SKILL.md', '---\nname: ornek\n---\n\n[[knowledge/concepts/Taşınmış]]\n')
        self.write(COMPANION + '/Kurallar.md', '# Kurallar\n- [[Eski Kural Notu]] geçerli.\n')
        self.assertEqual(sorted(self.dead()), [
            ('.agents/skills/ornek/SKILL.md', 5, 'knowledge/concepts/Taşınmış'),
            ('AGENTS.md', 3, 'Silinmiş Not'),
            ('AGENTS.md', 4, 'docs/yok.md'),
            (COMPANION + '/Kurallar.md', 2, 'Eski Kural Notu'),
        ])

    def test_code_examples_and_placeholders_are_not_links(self):
        self.write('.claude/skills/ornek/SKILL.md', '\n'.join([
            '```markdown', '[[Örnek Not]] ve [bağlantı](yok.md)', '```',
            'Satır içi `[[Kod Notu]]` sayılmaz.', '[[<not-adı>]] ve [x]({yol}) de sayılmaz.',
            '~~~', '[[Başka Örnek]]', '~~~', '[[Gerçek Kırık]]', '']))
        self.assertEqual(self.dead(), [('.claude/skills/ornek/SKILL.md', 9, 'Gerçek Kırık')])

    def test_links_resolve_like_obsidian_on_every_file_system(self):
        self.write(unicodedata.normalize('NFD', 'notlar/Kaşık Ğöz.md'), 'x')
        self.write('Notlar2/v3.2 Plan.md', 'x')
        self.write('ekler/Not (1).md', 'x')
        self.write('.agents/skills/ornek/SKILL.md', '# skill\n')
        self.write('.agents/skills/ornek/references/a.md', '[b](references/b.md)\n')
        self.write('.agents/skills/ornek/references/b.md', 'x')
        self.write('AGENTS.md', '\n'.join([
            '[[Kaşık Ğöz]] [[notlar/kaşık ğöz]] [[concepts/Var Olan Not]] [[notlar2/V3.2 PLAN]]',
            '[x](notlar2/v3.2%20plan) [x](ekler/Not%20(1).md) [x](concepts/Var%20Olan%20Not.md)',
            '<!-- beyin-v3:start -->', '<!-- [[Yorumdaki Not]] -->', '%% [[Obsidian Yorumu]] %%',
            '````markdown', '```', '[[İç İçe Örnek]]', '```', '````', '```', '[[Kapanmamış]]', '']))
        self.assertEqual(self.dead(), [])

    def test_each_dead_link_is_reported_once_and_bounded(self):
        for root in ('.agents', '.claude'):
            self.write(root + '/skills/ornek/SKILL.md', '[[Kopyada Kırık]]\n')
        self.write('AGENTS.md', '[[' + 'u' * 500 + ']]\n')
        self.assertEqual(sorted(self.dead()), [('.agents/skills/ornek/SKILL.md', 1, 'Kopyada Kırık'),
                                               ('AGENTS.md', 1, 'u' * 200)])

    def test_hidden_folders_do_not_satisfy_a_bare_wikilink(self):
        self.write('.obsidian/Gizli.md', '# gizli\n')
        self.write('AGENTS.md', '[[Gizli]]\n')
        self.assertEqual(self.dead(), [('AGENTS.md', 1, 'Gizli')])

    def test_report_is_capped_and_says_so(self):
        self.write('AGENTS.md', ''.join('[[Yok %d]]\n' % number for number in range(25)))
        report = references.check(self.vault)
        self.assertEqual((report['dead_count'], len(report['dead']), report['truncated']), (25, 20, True))


class DoctorIntegrationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-references-doctor-')
        self.addCleanup(tmp.cleanup)
        self.vault, self.state = Path(tmp.name) / 'Örnek Beyin', Path(tmp.name) / 'state'
        self.vault.mkdir()
        (self.vault / 'AGENTS.md').write_bytes('Bkz. [[Silinmiş Not]]\n'.encode('utf-8'))
        self.env = dict(os.environ, BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')

    def test_doctor_reports_dead_links_without_raising_status(self):
        before = sorted(path.relative_to(self.vault).as_posix() for path in self.vault.rglob('*'))
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'doctor'], capture_output=True, text=True,
                                encoding='utf-8', env=self.env, cwd=self.vault, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        doctor = json.loads(result.stdout)
        self.assertEqual(doctor['instruction_references']['dead'],
                         [{'file': 'AGENTS.md', 'line': 1, 'target': 'Silinmiş Not'}])
        self.assertNotEqual(doctor['status'], 'needs_attention')
        self.assertEqual(sorted(path.relative_to(self.vault).as_posix() for path in self.vault.rglob('*')), before)

    def test_human_output_names_the_first_links(self):
        spec = importlib.util.spec_from_file_location('references_entry', ROOT / 'scripts/beyin_entry.py')
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        dead = [{'file': 'AGENTS.md', 'line': number, 'target': 'Yok %d' % number} for number in range(1, 6)]
        text = entry.human_result({'status': 'observed_metadata', 'instruction_references': {
            'checked_files': 1, 'dead_count': 5, 'dead': dead, 'truncated': False}}, 'doctor', '3.4.0')
        self.assertIn('Talimat ve skill dosyalarinda kirik baglanti (bilgi): AGENTS.md:1 -> Yok 1, '
                      'AGENTS.md:2 -> Yok 2, AGENTS.md:3 -> Yok 3 ve 2 tane daha.', text)
        quiet = entry.human_result({'status': 'observed_metadata', 'instruction_references': {
            'checked_files': 1, 'dead_count': 0, 'dead': [], 'truncated': False}}, 'doctor', '3.4.0')
        self.assertNotIn('kirik baglanti', quiet)


if __name__ == '__main__':
    unittest.main()
