"""Retrieval without a query term reads no note text through the tokeniser. Synthetic and offline.

Snapshots, scoped listings and the advisor's eligibility pass (beyin_v3_jev._eligible, twice per
auto_context turn) score every record 0, so token counts cannot change what they return. The
oracle is the same call with one word that no record contains: it scores and orders like an
empty query but still tokenises every note, as the empty query used to.
"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_jev as advisor

ABSENT = 'zzqxvterm'


class EmptyQueryTokensTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.store = runtime.MemoryStore(root / 'state', self.vault)
        self.texts = []
        self.add('current', 'Quartz observatory calibration awaits review.')
        self.add('waiting', 'Telescope mirror order is waiting on the supplier.', status='waiting')
        self.add('old', 'The deploy runbook lives in the wiki.')
        self.add('new', 'The deploy runbook moved into the repository.', supersedes=['old'])
        self.add('retired', 'Legacy backup schedule, nightly at two.', status='archived')
        self.add('untrusted', 'Imported claim from a pasted page.', trust='untrusted')
        self.add('private', 'Personal reminder kept out of shared context.', visibility='private')
        self.add('public', 'Public release notes for the quartz site.', visibility='public')
        self.add('beta', 'Another project tracks its own milestones.', project='beta')
        self.add('long', 'Calibration log entry with repeated detail. ' * 400)
        self.add('stale', 'This source changes after it was indexed.')
        (self.vault / 'notes/stale.md').write_text('Edited outside the index.', encoding='utf-8')

    def add(self, ident, text, **extra):
        path = self.vault / 'notes' / (ident + '.md')
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding='utf-8')
        self.texts.append(text)
        self.store.ingest(dict(id=ident, title=ident, text=text, project=extra.pop('project', 'alpha'),
                               source=path.relative_to(self.vault).as_posix(),
                               updated_at='2026-09-%02dT10:00:00Z' % len(self.texts), **extra))

    def tokenised(self, call):
        """call()'s result and how many note texts it passed to the tokeniser."""
        seen, real = [], runtime._token_counts

        def spy(text):
            seen.append(text)
            return real(text)
        with patch.object(runtime, '_token_counts', spy):
            result = call()
        return result, sum(1 for text in seen if any(note in text for note in self.texts))

    def test_no_query_term_tokenises_no_note(self):
        calls = {
            'snapshot': lambda: self.store.snapshot_context(project='alpha'),
            'eligibility': lambda: advisor._eligible(self.store, 'alpha'),
            'unscoped eligibility': lambda: advisor._eligible(self.store, None),
            'scoped listing': lambda: self.store.retrieve('alpha', project='alpha'),
            'stopwords only': lambda: self.store.candidates('what is the status'),
        }
        for name, call in calls.items():
            with self.subTest(name):
                _, notes = self.tokenised(call)
                self.assertEqual(notes, 0)
        self.assertTrue(self.store.retrieve('alpha', project='alpha')['records'])

    def test_result_is_what_the_tokenising_path_returns(self):
        for project in (None, 'alpha'):
            for audience in ('public', 'internal', 'private'):
                for statuses in (None, ('active', 'waiting')):
                    for limit, budget in ((100000, 10000000), (3, 1200)):
                        params = dict(project=project, audience=audience, statuses=statuses,
                                      limit=limit, budget_chars=budget, snapshot=True)
                        with self.subTest(**params):
                            fast, notes = self.tokenised(lambda: self.store._retrieve('', **params))
                            # The oracle deliberately tokenises on every call, even when a
                            # previous subtest has populated the persistent count cache.
                            with patch.object(runtime._CountCache, 'counts',
                                              lambda _, text: runtime._token_counts(text)):
                                oracle, oracle_notes = self.tokenised(lambda: self.store._retrieve(ABSENT, **params))
                            self.assertEqual(notes, 0)
                            self.assertGreater(oracle_notes, 0)
                            self.assertEqual(fast, oracle)
                            self.assertEqual(fast.sources, oracle.sources)

    def test_advisor_eligibility_keeps_every_gate(self):
        oracle = self.store._retrieve(ABSENT, project='alpha', audience='internal', limit=100000,
                                      budget_chars=10000000, snapshot=True)['records']
        eligible = advisor._eligible(self.store, 'alpha')
        self.assertEqual(eligible, {r['id']: r for r in oracle if not r.get('text_truncated')})
        self.assertEqual(sorted(eligible), ['current', 'long', 'new', 'public', 'waiting'])


if __name__ == '__main__':
    unittest.main()
