#!/usr/bin/env python3
"""#354: Note title and filename salience boost in lexical search and retrieval."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))
import beyin_v3  # noqa: E402


class TitleMatchSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="v3-title-match-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        self.vault.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        self.store = beyin_v3.MemoryStore(self.state, self.vault)
        self.addCleanup(self.store.close)

    def ingest(self, ident, text, source, title="", status="active", aliases=None):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        record = {
            "id": ident,
            "kind": "note",
            "status": status,
            "visibility": "internal",
            "text": text,
            "facts": {},
            "source": source,
            "title": title,
            "aliases": aliases or [],
            "updated_at": "2026-10-14T10:00:00Z"
        }
        self.store.ingest(record)

    def ids(self, query, limit=5, strict=False):
        res = self.store.retrieve(query, limit=limit, strict=strict)
        return [r["id"] for r in res.get("records", [])]

    def test_unmatched_filename_does_not_create_topic_candidate(self):
        self.ingest('actual', 'Toplanti icin not ve kararlar.', 'toplanti.md')
        self.ingest('other', 'Farkli konu ve icerik.', 'kisa-not.md')
        self.assertEqual(self.ids('notlar'), ['actual'])

    def test_unrelated_filename_lengths_do_not_change_existing_bm25_order(self):
        self.ingest('a', 'calculus geometry ' * 3, 'a.md')
        self.ingest('b', 'calculus geometry ' * 3, 'a-very-long-unrelated-filename.md')
        # Equal content keeps the existing stable id tiebreak, independent of path length.
        self.assertEqual(self.ids('calculus geometry'), ['b', 'a'])

    def test_generic_titles_do_not_receive_name_boost(self):
        for title in ('DURUM', 'Proje', 'Notlar', 'Kararlar'):
            record = {'title': title, 'source': title + '.md'}
            self.assertEqual(beyin_v3._name_match_score(record, beyin_v3._tokens('projelerin durumu kararlar notlar')), 0)
        self.ingest('generic', 'projelerin durumu', 'archive/DURUM.md', title='DURUM')
        self.ingest('current', 'projelerin durumu ' * 8, 'Guncel-Ozet.md', title='Guncel Ozet')
        self.assertEqual(self.ids('projelerin durumu')[0], 'current')

    def test_project_note_outranks_hub_with_frequent_mentions(self):
        """<A>/<A>.md outranks a MOC mentioning <A> and 'projesi' 30+ times (#354)."""
        self.ingest(
            "atlas-note",
            "Atlas projesi mimari detayları ve hedefleri.",
            "Atlas/Atlas.md",
            title="Atlas",
            status="rafta"
        )
        self.ingest(
            "projects-moc",
            ("Atlas projesi " * 20) + ("projesi genel durumu " * 30),
            "MOC/Projects.md",
            title="Projeler MOC"
        )
        # Query: Atlas projesi neydi
        results = self.ids("Atlas projesi neydi")
        self.assertIn("atlas-note", results)
        self.assertEqual(results[0], "atlas-note")

    def test_single_word_query_prefers_exact_title(self):
        """Single-word query '<A>' ranks note titled '<A>' at #1."""
        self.ingest(
            "atlas-note",
            "Atlas notu kısa içerik.",
            "Atlas/Atlas.md",
            title="Atlas"
        )
        self.ingest(
            "distractor-hub",
            "Atlas " * 50,
            "hub.md",
            title="Genel Hub"
        )
        results = self.ids("Atlas")
        self.assertEqual(results[0], "atlas-note")

    def test_abbreviation_name_beats_archive_distractor(self):
        """3-letter abbreviation project note beats archive note with repeated distractor words (#354)."""
        self.ingest(
            "kob-note",
            "KOB oyun motoru ve tasarımı.",
            "KOB/KOB.md",
            title="KOB",
            status="aktif"
        )
        self.ingest(
            "archive-distractor",
            ("oyunun durumu harika bir oyun ve oyun durumu " * 10),
            "900-Archive/eski-oyunlar.md",
            title="Eski Oyunlar",
            status="active"
        )
        # Query: KOB oyununun durumu
        results = self.ids("KOB oyununun durumu")
        self.assertIn("kob-note", results)
        self.assertEqual(results[0], "kob-note")

    def test_short_note_with_only_name_match_in_query(self):
        """Small 1.2 KB note (<C>/<C>.md) ranks #1 even if query contains extra terms (#354)."""
        self.ingest(
            "laya-note",
            "Laya mimari yapısı ve modülleri.",
            "Laya/Laya.md",
            title="Laya",
            status="pasif"
        )
        self.ingest(
            "planning-note",
            ("ne aşamada olduğu ve gelecek aşama planları " * 15),
            "Planning/Q3.md",
            title="Q3 Planlama"
        )
        results = self.ids("Laya ne aşamada")
        self.assertIn("laya-note", results)
        self.assertEqual(results[0], "laya-note")

    def test_numbered_prefix_filename_salience(self):
        """Files named '020-Projects/01-Falcon.md' match 'Falcon' query."""
        self.ingest(
            "falcon-note",
            "Falcon uçuş kontrol sistemi.",
            "020-Projects/01-Falcon.md",
            title=""  # empty title, relies on filename stem
        )
        self.ingest(
            "generic-doc",
            "Falcon kontrol sistemleri hakkında genel bir doküman.",
            "docs/systems.md",
            title="Sistemler"
        )
        results = self.ids("Falcon projesi neydi")
        self.assertEqual(results[0], "falcon-note")

    def test_unicode_dash_numbered_prefix_matches_ascii_candidates(self):
        expected = beyin_v3._name_candidates({'source': 'notes/001-claude-mem.md'})
        for dash in ('-', '\u2010', '\u2011', '\u2012', '\u2013', '\u2014', '\u2015', '\ufe63', '\uff0d'):
            with self.subTest(dash=dash):
                record = {'source': f'notes/001{dash}claude-mem.md'}
                self.assertEqual(beyin_v3._name_candidates(record), expected)
                self.assertEqual(beyin_v3._name_match_score(record, beyin_v3._tokens('claude-mem')), 2)

    def test_dash_normalization_does_not_expand_prefix_or_name_matching(self):
        for source in ('001.claude-mem.md', '001:claude-mem.md', '001\u2212claude-mem.md',
                       'v001-claude-mem.md', 'unrelated\u2014claude-mem.md'):
            with self.subTest(source=source):
                self.assertEqual(beyin_v3._name_match_score({'source': source},
                                 beyin_v3._tokens('claude-mem')), 0)
        self.assertEqual(beyin_v3._name_candidates({'source': 'notes/001-claude-mem.md'}),
                         ['001 claude mem', 'claude mem'])
        self.assertEqual(beyin_v3._name_candidates({'source': 'notes/claude-mem.md'}), ['claude mem'])
        self.assertEqual(beyin_v3._name_match_score({'source': '001\u2014claude-mem.md'},
                         beyin_v3._tokens('claude')), 0)

    def test_unicode_prefix_real_strict_retrieval_matches_ascii(self):
        for dash in ('-', '\u2010', '\u2011', '\u2013', '\u2014'):
            with self.subTest(dash=dash), tempfile.TemporaryDirectory(prefix='title-dash-') as td:
                root = Path(td); vault = root / 'vault'; vault.mkdir()
                store = beyin_v3.MemoryStore(root / 'state', vault)
                try:
                    for ident, source, text in (
                        ('target', f'notes/001{dash}claude-mem.md',
                         'Claude mem kurulumu için sentetik bakım kararı. ' + 'Başka açıklama. ' * 20),
                        ('other', 'notes/index.md', 'Claude mem kurulumu. ' * 8),
                    ):
                        path = vault / source; path.parent.mkdir(exist_ok=True)
                        path.write_text(text, encoding='utf-8')
                        store.ingest(dict(id=ident, source=source, text=text, title='', kind='note',
                                          status='active', visibility='internal',
                                          updated_at='2026-10-01T00:00:00Z'))
                    before = {p: p.read_bytes() for p in vault.rglob('*.md')}
                    results = store.retrieve('claude-mem', strict=True)
                    self.assertEqual([r['id'] for r in results['records']], ['target', 'other'])
                    context = store.context_for('claude', 'claude-mem', strict=True, budget_chars=2000)
                    self.assertEqual([r['id'] for r in context['records']], ['target', 'other'])
                    self.assertEqual(context['records'][0]['source'], f'notes/001{dash}claude-mem.md')
                    self.assertEqual(before, {p: p.read_bytes() for p in vault.rglob('*.md')})
                finally:
                    store.close()

    def test_more_specific_title_outranks_general_title(self):
        """More specific note title ('Atlas Projesi') ranks before shorter ('Atlas') for specific query."""
        self.ingest(
            "atlas-base",
            "Atlas temel kütüphanesi.",
            "Atlas.md",
            title="Atlas"
        )
        self.ingest(
            "atlas-project",
            "Atlas Projesi yol haritası ve takvimi.",
            "Atlas-Projesi.md",
            title="Atlas Projesi"
        )
        # Query matches both words of 'Atlas Projesi'
        results = self.ids("Atlas projesi neydi")
        self.assertEqual(results[0], "atlas-project")
        self.assertEqual(results[1], "atlas-base")

        # Single-word query matches only 'Atlas'
        results_single = self.ids("Atlas")
        self.assertEqual(results_single[0], "atlas-base")

    def test_alias_salience_boost(self):
        """Aliases provide the same salience boost as title."""
        self.ingest(
            "hermes-note",
            "Mesajlaşma omurgası.",
            "knowledge/hermes.md",
            title="Hermes İletişim Protokolü",
            aliases=["Hermes Core", "Hermes"]
        )
        self.ingest(
            "other-note",
            "Hermes protokolü burada da geçer.",
            "notes/other.md",
            title="Diğer Not"
        )
        results = self.ids("Hermes")
        self.assertEqual(results[0], "hermes-note")


if __name__ == "__main__":
    unittest.main()
