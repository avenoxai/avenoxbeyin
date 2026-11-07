"""Synthetic evidence regressions."""
from v3_evidence_helpers import EvidenceFixture, rt, continuity, json


class ContinuityEvidenceTest(EvidenceFixture):
    def test_continuation_preserves_only_delivered_ranges_and_invalidates_changes(self):
        marker = 'Quartz rollback uses the amber switch.'
        self.note('guide', '# Overview\n\n' + 'Unrelated background. ' * 90 + '\n\n## Recovery\n\n' + marker)
        query = 'quartz rollback switch'
        text, delivered = rt.render_context(self.local(query), 1800)
        self.assertIn(marker, text)
        continuity.remember(self.store, 'claude', 'session', query, delivered, now=100)
        empty = rt.pack_context([])
        follow, inherited = continuity.resolve(self.store, 'claude', 'session', 'bunu detaylandır', empty, budget_chars=1800, now=101)
        self.assertTrue(inherited)
        self.assertIn(marker, rt.render_context(follow, 1800)[0])
        self.assertNotIn('Unrelated background.', rt.render_context(follow, 1800)[0])
        (self.vault / 'guide.md').write_text('Quartz rollback has changed.', encoding='utf-8')
        self.assertFalse(continuity.resolve(self.store, 'claude', 'session', 'bunu detaylandır', empty, budget_chars=1800, now=102)[1])
        self.store.update_task('guide', 1, {'text': 'Quartz rollback has changed.'})
        self.assertFalse(continuity.resolve(self.store, 'claude', 'session', 'bunu detaylandır', empty, budget_chars=1800, now=103)[1])

    def test_continuation_rechecks_current_exclusions(self):
        self.note('guide', 'Quartz rollback switch is amber.', source='archive/guide.md')
        query = 'quartz rollback switch'
        _, delivered = rt.render_context(self.local(query), 3000)
        continuity.remember(self.store, 'claude', 'session', query, delivered, now=100)
        (self.store.state_dir / 'retrieval.json').write_text(json.dumps({'strict_exclude': ['ARCHIVE/']}), encoding='utf-8')
        self.assertFalse(continuity.resolve(self.store, 'claude', 'session', 'bunu detaylandır', rt.pack_context([]), budget_chars=3000, now=101)[1])

    def test_clipped_delivery_does_not_anchor_unseen_text_or_store_content(self):
        self.note('guide', '# Manual\n\n' + 'Quartz rollback switch. ' * 100)
        query = 'quartz rollback switch'
        _, delivered = rt.render_context(self.local(query), 1100)
        self.assertTrue(delivered['records'])
        self.assertTrue(delivered['records'][0]['text'].endswith(rt.CONTEXT_MARKER))
        continuity.remember(self.store, 'claude', 'session', query, delivered, now=100)
        path = continuity._path(self.store, 'claude', 'session')
        saved = path.read_text(encoding='utf-8')
        for text in ('Quartz', 'Manual', query):
            self.assertNotIn(text, saved)
        follow, inherited = continuity.resolve(self.store, 'claude', 'session', 'bunu detaylandır', rt.pack_context([]), budget_chars=5000, now=101)
        self.assertTrue(inherited)
        # More room in the follow-up cannot reveal characters omitted on the first turn.
        self.assertEqual(follow['records'][0]['text'], delivered['records'][0]['text'][:-len(rt.CONTEXT_MARKER)])
        self.assertNotIn('variants', rt.render_context(follow, 5000)[0])

    def test_legacy_and_malformed_ranges_fall_back_to_local_context(self):
        self.note('guide', 'Quartz rollback switch is amber.')
        query = 'quartz rollback switch'
        _, delivered = rt.render_context(self.local(query), 3000)
        continuity.remember(self.store, 'claude', 'session', query, delivered, now=100)
        path = continuity._path(self.store, 'claude', 'session')
        original = json.loads(path.read_text(encoding='utf-8'))
        for ranges in ([[-1, 4]], [[0, 99999]], [[True, 4]], [[5, 3]], [[0, 4], [2, 6]], []):
            value = json.loads(json.dumps(original))
            value['refs'][0]['ranges'] = ranges
            path.write_text(json.dumps(value), encoding='utf-8')
            local = rt.pack_context([])
            result, inherited = continuity.resolve(self.store, 'claude', 'session', 'bunu detaylandır', local, budget_chars=3000, now=101)
            self.assertIs(result, local)
            self.assertFalse(inherited)
        original['version'] = 1
        path.write_text(json.dumps(original), encoding='utf-8')
        self.assertFalse(continuity.resolve(self.store, 'claude', 'session', 'bunu detaylandır', rt.pack_context([]), budget_chars=3000, now=101)[1])
