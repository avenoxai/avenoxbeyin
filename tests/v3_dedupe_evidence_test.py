"""Synthetic evidence regressions."""
from v3_evidence_helpers import EvidenceFixture, rt, dedupe


class DedupeEvidenceTest(EvidenceFixture):
    def test_dedupe_preserves_wider_for_the_final_envelope(self):
        a = self.note('seen', 'Previously delivered synthetic material.')
        b = self.note('new', 'Quartz rollback instructions.')
        wide = dict(b, text=b['text'] + '\n\nThe switch is amber.')
        context = rt.pack_widened([a, b], {'new': wide}, budget_chars=5000)
        (self.store.state_dir / dedupe.SETTINGS_FILE).write_text('{"enabled": true}', encoding='utf-8')
        dedupe.record_delivered(self.store.state_dir, 'claude', 'session', [a], now=100)
        novel, duplicates = dedupe.drop_delivered(self.store.state_dir, 'claude', 'session', context, now=101)
        self.assertEqual([r['id'] for r in duplicates], ['seen'])
        self.assertIn('The switch is amber.', rt.render_context(novel, 5000)[0])
