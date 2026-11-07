"""Skill frontmatter stays loadable, and starter skills only route to skills a V3 install ships.

Agents pick a skill from its one-line `description` (#598). Claude Code, Codex and Kiro read
it as YAML, so a `Tetik: ...` style colon or a ` #` silently changes or truncates the value. The
release ZIP carries only the starter skills; a boundary that sends the agent to a V2-only
skill must say that skill may be missing.
"""
import importlib.util
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
STARTER_ROOT = ROOT / 'template/.agents/skills'
# Every SKILL.md this repository ships through any installer, V2 editions included.
SHIPPED = sorted([*ROOT.glob('template/*/skills/*/SKILL.md'), ROOT / 'scripts/skill.beyin-doktor.windows.md'])
NAME = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')
# YAML plain scalars may not start with an indicator, and ': ' or ' #' inside one ends the
# value or turns the line into a mapping/comment.
YAML_INDICATORS = tuple('-?:,[]{}#&*!|>\'"%@`')


def installer():
    spec = importlib.util.spec_from_file_location('beyin_skill_description_installer', ROOT / 'scripts/install_v3.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frontmatter(path):
    lines = path.read_text(encoding='utf-8').split('\n')
    if lines[0] != '---' or '---' not in lines[1:]:
        raise AssertionError(f'{path}: frontmatter must open and close with ---')
    fields = {}
    for line in lines[1:lines.index('---', 1)]:
        key, separator, value = line.partition(': ')
        if not separator or not NAME.fullmatch(key):
            raise AssertionError(f'{path}: unexpected frontmatter line {line!r}')
        fields[key] = value
    return fields


def mentions(text, name):
    return re.finditer(r'(?<![\w-])' + re.escape(name) + r'(?![\w-])', text)


class SkillDescriptionTest(unittest.TestCase):
    def test_shipped_skill_frontmatter_is_a_single_line_plain_yaml_scalar(self):
        self.assertTrue(SHIPPED)
        for path in SHIPPED:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                fields = frontmatter(path)
                self.assertEqual(set(fields), {'name', 'description'})
                self.assertTrue(NAME.fullmatch(fields['name']))
                self.assertLessEqual(len(fields['name']), 64)
                if path.name == 'SKILL.md':
                    self.assertEqual(fields['name'], path.parent.name)
                description = fields['description']
                self.assertTrue(0 < len(description) <= 1024, len(description))
                self.assertEqual(description, description.strip())
                self.assertFalse(description.startswith(YAML_INDICATORS), description[:20])
                self.assertNotIn(': ', description)
                self.assertNotIn(' #', description)
                self.assertNotIn('\t', description)
                self.assertFalse(description.endswith(':'))

    def test_starter_boundaries_name_only_shipped_skills_or_say_they_may_be_absent(self):
        starters = set(installer().STARTER_SKILLS)
        self.assertEqual(starters, {p.parent.name for p in STARTER_ROOT.glob('*/SKILL.md')})
        others = sorted({p.parent.name for p in ROOT.glob('template/*/skills/*/SKILL.md')} - starters)
        self.assertIn('gecmis-import', others)
        for name in sorted(starters):
            description = frontmatter(STARTER_ROOT / name / 'SKILL.md')['description']
            groups = [(m.start(), m.end()) for m in re.finditer(r'\([^()]*\)', description)]
            for other in others:
                for mention in mentions(description, other):
                    with self.subTest(skill=name, mentions=other):
                        group = next((description[a:b] for a, b in groups if a < mention.start() < b), '')
                        # "(kasada varsa gecmis-import)": V3 never installs it, a V2 vault may keep it.
                        self.assertIn('varsa', group, description)


if __name__ == '__main__':
    unittest.main()
