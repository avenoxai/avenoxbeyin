"""Synthetic evidence regressions."""
from v3_evidence_helpers import EvidenceFixture, rt, client, jev, continuity, os, patch


class JevEvidenceTest(EvidenceFixture):
    def test_rescued_long_match_uses_card_sized_window_from_spare_room(self):
        from beyin_v3_evidence import delivered_ranges
        opening = 'Restore mode uses the amber profile.'
        marker = 'Rollback uses the copper switch.'
        tail = 'Background detail. ' * 20 + marker + ' Adjacent explanation. ' * 28
        self.note('guide', opening + '\n\n' + 'Unrelated background. ' * 300
                  + '\n\n## Recovery\n\n' + tail)
        query = 'explain rollback restoration mechanism'
        local = self.local(query)
        self.assertFalse(local['records'])
        result = self.judge(query, local, marker)
        text, delivered = rt.render_context(result, 4500)
        self.assertLessEqual(len(text), 4500)
        self.assertIn(opening, text)
        self.assertIn(marker, text)
        record = delivered['records'][0]
        ranges = delivered_ranges(delivered.evidence[record['id']], record)
        self.assertEqual(len(ranges), 2)
        self.assertEqual(ranges[1][1] - ranges[1][0], 500)

    def test_rescue_reserves_primary_evidence_before_the_opening_fills_the_envelope(self):
        opening = 'Restore mode uses the amber profile.'
        marker = 'Rollback uses the copper switch.'
        condition = 'This is temporary; do not publish.'
        self.note('guide', opening + '\n\n' + 'Unrelated background. ' * 300
                  + '\n\n## Recovery\n\n' + condition + ' ' + marker + ' Adjacent explanation. ' * 30)
        query = 'explain rollback restoration mechanism'
        local = self.local(query)
        self.assertFalse(local['records'])
        result = self.judge(query, local, marker)
        text, delivered = rt.render_context(result, 4100)
        self.assertLessEqual(len(text), 4100)
        self.assertIn(opening, text)
        self.assertIn(marker, text)
        self.assertIn(condition, text)
        from beyin_v3_evidence import delivered_ranges
        ranges = delivered_ranges(delivered.evidence['guide'], delivered['records'][0])
        self.assertEqual(len(ranges), 2)

    def test_rescued_loose_source_keeps_opening_and_matching_tail(self):
        from beyin_v3_evidence import delivered_ranges
        opening = 'Restore mode uses the amber profile.'
        matching = 'Rollback uses the copper switch.'
        source = self.note('guide', opening + '\n\n' + ('Unrelated background. ' * 300)
                           + '\n\n## Recovery\n\n' + matching)
        query = 'explain rollback restoration mechanism'
        local = self.local(query)
        self.assertFalse(local['records'])
        result = self.judge(query, local, matching)
        text, delivered = rt.render_context(result, 5000)
        self.assertIn(opening, text)
        self.assertIn(matching, text)
        record = delivered['records'][0]
        ranges = delivered_ranges(delivered.evidence[record['id']], record)
        self.assertTrue(any(start == 0 for start, end in ranges))
        self.assertTrue(any(matching in source['text'][start:end] for start, end in ranges))
        continuity.remember(self.store, 'claude', 'dual-window', query, delivered, now=100)
        follow, inherited = continuity.resolve(self.store, 'claude', 'dual-window',
                                               'bunu detaylandır', rt.pack_context([]),
                                               budget_chars=5000, now=101)
        self.assertTrue(inherited)
        follow_text, _ = rt.render_context(follow, 5000)
        self.assertIn(opening, follow_text)
        self.assertIn(matching, follow_text)
        # A smaller final envelope must reconsider widening and only keep the
        # opening; the matching tail cannot displace the reserved prefix.
        text, medium = rt.render_context(result, 2400)
        self.assertLessEqual(len(text), 2400)
        self.assertIn(opening, text)
        self.assertNotIn(matching, text)
        self.assertLess(len(medium['records'][0]['text']), len(record['text']))
        # Remember only the opening characters that actually fit after clipping.
        text, delivered = rt.render_context(result, 1400)
        self.assertLessEqual(len(text), 1400)
        self.assertIn(opening, text)
        self.assertNotIn(matching, text)
        record = delivered['records'][0]
        ranges = delivered_ranges(delivered.evidence[record['id']], record)
        self.assertTrue(ranges)
        self.assertFalse(any(matching in source['text'][start:end] for start, end in ranges))
        self.assertTrue(any(start == 0 for start, end in ranges))

    def test_short_matching_sentence_keeps_surrounding_card_context(self):
        from beyin_v3_evidence import card_excerpt
        text = 'Background detail. ' * 40 + 'Quartz rollback switch. ' + 'Adjacent explanation. ' * 30
        excerpt = card_excerpt('quartz rollback switch', dict(text=text), limit=600)
        self.assertIn('Quartz rollback switch.', excerpt)
        self.assertEqual(len(excerpt), 600)
        self.assertIn(excerpt, text)

    def test_loose_tail_is_judged_and_the_same_passage_is_delivered(self):
        marker = 'Rollback uses the amber switch.'
        self.note('guide', '# Overview\n\n' + ('Unrelated background material. ' * 90) + '\n\n## Recovery\n\n' + marker)
        query = 'explain rollback restoration mechanism'
        local = self.local(query)
        self.assertFalse(local['records'])
        result = self.judge(query, local, marker)
        text, _ = rt.render_context(result, 5000)
        self.assertIn(marker, text)
        self.assertIn('Recovery', self.calls[0]['state']['notes']['k0']['excerpt'])

    def test_strict_long_block_card_centres_on_query_evidence(self):
        marker = 'Quartz rollback uses the amber switch.'
        self.note('guide', 'Background context. ' * 36 + marker)
        query = 'explain quartz rollback switch'
        local = self.local(query)
        self.assertTrue(any(marker in r['text'] for r in local['records']))
        result = self.judge(query, local, marker)
        self.assertIn(marker, rt.render_context(result, 5000)[0])

    def test_newly_selected_sensitive_tail_is_still_rejected_before_transport(self):
        secret = 'sk-' + 'x' * 40
        self.note('guide', 'Background. ' * 100 + '\n\nRollback access token: ' + secret)
        query = 'explain rollback restoration mechanism'
        local = self.local(query)
        client.set_mode(self.store.state_dir, 'on', enable=['auto_context'])
        with patch.dict(os.environ, {'TYPESAFE_API_KEY': 'synthetic'}, clear=True), \
                patch.object(client, '_transport', side_effect=AssertionError('sensitive input left the machine')) as transport:
            result = jev.auto_context(self.store, 'claude', query, local, budget_chars=5000)
        transport.assert_not_called()
        self.assertIs(result, local)

    def test_second_strict_passage_is_represented_on_card(self):
        marker = 'Quartz rollback switch is amber.'
        self.note('guide', '# Guide\n\nQuartz rollback instructions.\n\n## Switch\n\n' + marker)
        query = 'quartz rollback instructions switch'
        local = self.local(query)
        self.assertTrue(local.wider)
        result = self.judge(query, local, marker)
        self.assertIn('Quartz rollback instructions.', self.calls[0]['state']['notes']['k0']['excerpt'])
        self.assertIn(marker, rt.render_context(result, 5000)[0])
