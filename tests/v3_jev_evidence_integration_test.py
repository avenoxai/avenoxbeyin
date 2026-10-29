"""Synthetic regressions for upstream #534 combined with evidence preservation."""
from v3_evidence_helpers import EvidenceFixture, jev, rt


class JevEvidenceIntegrationTest(EvidenceFixture):
    def test_long_matching_heading_keeps_body_and_other_selected_passage(self):
        marker = 'Restore the previous release.'
        text = '# ' + 'General background ' * 40 + '\n\nUnrelated furniture.\n\n'
        text += '## Rollback deploy\n\n' + marker
        self.note('guide', text)
        query = 'Quartz deploy steps and pricing'
        local = self.local(query)
        self.assertFalse(local['records'])
        result = self.judge(query, local, marker)
        card = self.calls[0]['state']['notes']['k0']['excerpt']
        self.assertIn('deploy', card)
        self.assertIn(marker, card)
        self.assertIn(marker, rt.render_context(result, 5000)[0])

    def test_short_root_card_and_rescue_keep_same_hash_bound_body(self):
        marker = 'Boy: 180 cm.'
        self.note('guide', '# General\n\n' + 'Unrelated furniture. ' * 60 + '\n\n## Details\n\n' + marker)
        query = 'quartz boyum pricing'
        local = self.local(query)
        self.assertFalse(local['records'])
        result = self.judge(query, local, marker)
        self.assertIn(marker, rt.render_context(result, 5000)[0])
        self.assertTrue(result.evidence['guide']['variants'])

    def test_card_scoring_preserves_project_root_exclusion(self):
        from beyin_v3_evidence import card_excerpt
        text = 'Ordinary furniture records. ' * 30 + 'Boy: 180 cm.'
        excerpt = card_excerpt('boyum', dict(text=text), project='boy')
        self.assertNotIn('180 cm', excerpt)

    def test_heading_overlap_does_not_displace_two_matching_body_passages(self):
        from beyin_v3_evidence import card_excerpt, loose_passages
        marker = 'Quartz deploy uses the copper switch.'
        self.note('guide', '# Quartz\n\nQuartz metadata.\n\n## Recovery\n\n'
                  'Quartz deploy overview.\n\n' + marker)
        record = self.store._records()[0]
        narrow, broad, ref = loose_passages(record, 'quartz deploy pricing')
        self.assertIn(marker, card_excerpt('quartz deploy pricing', narrow, broad, ref))

    def test_heading_only_sibling_does_not_dilute_the_primary_body_card(self):
        from beyin_v3_evidence import card_excerpt, loose_passages
        marker = 'Quartz rollback uses the copper switch.'
        self.note('guide', '# Manual\n\n' + 'Background detail. ' * 12 + marker +
                  '\n\n## Quartz rollback\n\n' + 'Unrelated furniture. ' * 50)
        record = self.store._records()[0]
        narrow, broad, ref = loose_passages(record, 'quartz rollback pricing')
        self.assertIsNone(broad)
        self.assertIn(marker, card_excerpt('quartz rollback pricing', narrow, broad, ref))

    def test_upstream_due_priority_survives_evidence_extensions(self):
        marker = 'Database migration rollback uses the amber switch.'
        for ident, due, updated in [('due', '2020-01-01', '2026-01-01'),
                                     ('future', '2099-01-01', '2026-10-01')]:
            self.note(ident, '# Guide\n\n' + marker, kind='task', due_at=due,
                      updated_at=updated, status='active')
        context = self.store.context_for('claude', 'database migration rollback', strict=True, limit=1)
        self.assertEqual([r['id'] for r in context['records']], ['due'])
        self.assertIn('due', context.evidence)

    def test_spare_room_delivers_source_condition_beyond_the_selected_passages(self):
        self.note('guide', '# Quartz Sapphire\n\n## Update\n\nTiny.\n\n'
                  '## Conditions\n\nThe prototype remains private until review.\n')
        context = self.local('quartz sapphire')
        self.assertEqual(len(context['records']), 1)
        text, _ = rt.render_context(context, 5000)
        self.assertIn('The prototype remains private until review.', text)
