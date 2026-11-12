"""Agent search_notes matches Turkish dotted/dotless i the way a reader expects.

Real temporary vault notes; no agent process, model or network.
Run from the repository root: python -m unittest discover -s tests -p v3_yakala_ajan_search_test.py -v
"""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
import beyin_v3_yakala as yakala  # noqa: E402


class AgentTurkishSearchTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yakala-ajan-arama-')
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / 'Örnek Vault'
        self.state = Path(temporary.name) / 'state'
        (self.vault / 'knowledge').mkdir(parents=True)
        (self.vault / 'knowledge' / 'şehir.md').write_text(
            '# Şehir\n\nİstanbul toplantısında KALDIK; Müşteri Arşiv notu ıhlamur.\n', encoding='utf-8')

    def paths(self, query):
        return [match['path'] for match in yakala._agent_note_search(self.vault, query, self.state)['matches']]

    def test_dotted_capital_i_matches_a_lowercase_query(self):
        self.assertEqual(self.paths('istanbul'), ['knowledge/şehir.md'])
        self.assertEqual(self.paths('İSTANBUL'), ['knowledge/şehir.md'])

    def test_dotless_i_and_ascii_i_meet_in_both_directions(self):
        self.assertEqual(self.paths('kaldık'), ['knowledge/şehir.md'])
        self.assertEqual(self.paths('IHLAMUR'), ['knowledge/şehir.md'])

    def test_snippet_stays_on_the_match_after_many_dotted_capitals(self):
        (self.vault / 'knowledge' / 'uzun.md').write_text('İ' * 200 + ' hedefkelime sonu\n', encoding='utf-8')
        match = yakala._agent_note_search(self.vault, 'hedefkelime', self.state)['matches'][0]
        self.assertEqual(match['path'], 'knowledge/uzun.md')
        self.assertIn('hedefkelime', match['snippet'])

    def test_capital_query_with_dotted_i_matches_lowercase_text(self):
        self.assertEqual(self.paths('ŞEHİR'), ['knowledge/şehir.md'])
        self.assertEqual(self.paths('MÜŞTERİ ARŞİV'), ['knowledge/şehir.md'])


if __name__ == '__main__':
    unittest.main()
