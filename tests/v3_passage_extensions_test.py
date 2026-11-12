"""Synthetic regressions for spare-room context; no personal vault fixtures."""
import unittest
from unittest.mock import patch
from v3_evidence_helpers import EvidenceFixture, rt, jev, client, os
from beyin_v3_evidence import card_excerpt, delivered_ranges
import beyin_v3_passage as passage
import beyin_v3_dedupe as dedupe


class PassageExtensionsTest(EvidenceFixture):
    def note(self, ident, text, **fields):
        path = self.vault / (ident + '.md')
        path.write_text(text, encoding='utf-8')
        return self.store.ingest(dict(id=ident, title=fields.pop('title', ident),
                                      source=path.name, text=text, **fields))

    def guide(self):
        return self.note('guide', '# Quartz Sapphire\n\n## Update\n\nTiny.\n\n'
                         '## Conditions\n\nThe prototype remains private until review.\n',
                         title='Quartz Sapphire')

    def test_whole_source_fits_without_changing_the_card(self):
        source = self.guide()
        local = self.local('quartz sapphire')
        self.assertEqual(len(local['records']), 1)
        narrow = jev._unclipped(local)[0]
        excerpt = card_excerpt('quartz sapphire', narrow, local.wider.get('guide'), local.evidence['guide'])
        self.assertNotIn('remains private', excerpt)
        text, delivered = rt.render_context(local, 5000)
        self.assertIn('remains private until review', text)
        record = delivered['records'][0]
        self.assertEqual(record['text'], source['text'])
        self.assertEqual(delivered_ranges(delivered.evidence['guide'], record), [[0, len(source['text'])]])

    def test_existing_second_passages_are_reserved_before_extensions(self):
        base = [dict(id='a', source='a.md', text='A' * 100),
                dict(id='b', source='b.md', text='B' * 100)]
        wider = {'b': dict(base[1], text='B' * 600)}
        # Enough for either a large first-source extension or b's second passage,
        # but not both. The old passage reservation has priority across sources.
        protected = rt.pack_widened(base, wider, budget_chars=1300)
        expanded = rt.pack_widened(base, wider, budget_chars=1300,
                                  extensions={'a': [dict(base[0], text='A' * 900)]})
        self.assertEqual(expanded['records'], protected['records'])
        self.assertEqual(expanded['records'][1]['text'], 'B' * 600)

    def test_final_repack_reconsiders_the_complete_extension(self):
        self.guide()
        local = self.local('quartz sapphire')
        text, full = rt.render_context(local, 5000)
        self.assertIn('remains private until review', text)
        # Find a smaller real envelope that fits the original selected passage
        # but not the full source. A re-render must not clip the full source.
        for budget in range(200, len(text)):
            shown, delivered = rt.render_context(full, budget)
            if delivered['records'] and 'Tiny.' in shown and 'remains private' not in shown:
                self.assertLessEqual(len(shown), budget)
                record = delivered['records'][0]
                ranges = delivered_ranges(delivered.evidence['guide'], record)
                self.assertTrue(ranges)
                self.assertFalse(any(start == 0 for start, end in ranges))
                break
        else:
            self.fail('no smaller envelope kept the original selected passage')

    def test_jev_repack_preserves_extensions_and_unchanged_card_evidence(self):
        self.guide()
        self.note('other', 'Quartz sapphire unrelated pricing.', title='Other')
        query = 'quartz sapphire'
        local = self.local(query)
        result = self.judge(query, local, 'Tiny.')
        self.assertIn('guide', result.extensions)
        self.assertIn('remains private until review', rt.render_context(result, 5000)[0])
        guide_card = next(c for c in self.calls[0]['state']['notes'].values() if c['title'] == 'Quartz Sapphire')
        self.assertNotIn('remains private', guide_card['excerpt'])

    def test_whole_sections_can_fit_when_the_full_source_cannot(self):
        source = self.note('section', '# Quartz Sapphire\n\n## Update\n\nTiny.\n\n'
                           'Private until review.\n\n## Appendix\n\n' + 'Unrelated background. ' * 400,
                           title='Quartz Sapphire')
        local = self.local('quartz sapphire')
        text, delivered = rt.render_context(local, 1800)
        self.assertIn('Private until review.', text)
        self.assertNotIn('Unrelated background.', text)
        ranges = delivered_ranges(delivered.evidence['section'], delivered['records'][0])
        self.assertTrue(any('Private until review.' in source['text'][s:e] for s,e in ranges))

    def test_policy_exclusions_still_prevent_extensions(self):
        self.guide()
        (self.store.state_dir / passage.CONFIG_NAME).write_text('{"strict_exclude":["guide.md"]}', encoding='utf-8')
        local = self.local('quartz sapphire')
        self.assertFalse(local['records'])
        self.assertFalse(local.extensions)

    def test_dedupe_keeps_only_surviving_source_extensions(self):
        self.guide()
        self.note('other', 'Quartz sapphire unrelated pricing.')
        local = self.local('quartz sapphire')
        guide = next(r for r in local['records'] if r['id'] == 'guide')
        others = [r for r in local['records'] if r['id'] != 'guide']
        with patch.object(dedupe, 'filter_novel', return_value=([guide], others)):
            kept, _ = dedupe.drop_delivered(self.store.state_dir, 'claude', 'synthetic', local)
        self.assertEqual(set(kept.extensions), {'guide'})
        self.assertIn('remains private until review', rt.render_context(kept, 5000)[0])


if __name__ == '__main__':
    unittest.main()
