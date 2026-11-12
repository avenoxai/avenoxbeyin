"""Agent hand-offs stay opt-in: the installed skill must not tell an agent to grant them itself.

Pure text checks on the skill `yakala kur` writes; no vault, process or network.
Run from the repository root: python -m unittest discover -s tests -p v3_yakala_ajan_consent_test.py -v
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
import beyin_v3_yakala as yakala  # noqa: E402


class SkillConsentTest(unittest.TestCase):
    def test_skill_enables_the_agent_only_on_an_explicit_user_request(self):
        skill = yakala.SKILL_MD
        self.assertNotIn('kullanici baska istemedikce `--ajan otomatik` kullanir', skill)
        self.assertIn('Arka plan ajanini yalniz kullanici acikca isterse ac', skill)
        self.assertIn('kendiliginden `--ajan`', skill)

    def test_skill_never_lets_an_agent_grant_send_batch_or_audio_permissions(self):
        skill = ' '.join(yakala.SKILL_MD.split())
        for flag in ('`ajan-gonder`', '`ajan-toplu --gonder --plan`', '`--ses-isle`', '`--tani-servise-gonder`'):
            self.assertIn(flag, skill)
        self.assertIn('kullanici istemeden bunlari calistirma veya ekleme', skill)
        self.assertIn('onizlemeyi kullaniciya goster', skill)


if __name__ == '__main__':
    unittest.main()
