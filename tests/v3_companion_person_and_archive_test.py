"""Unit tests for companion person notes and archive continuity (Issue #288 / Package A)."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_companion as companion
import beyin_v3 as runtime
from beyin_v3_sync import SyncEngine

COMPANION = '🔮 850-Companion'


class CompanionPersonAndArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-person-archive-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        (self.vault / COMPANION).mkdir(parents=True)
        self.state = Path(self.tmp.name) / 'state'
        self.state.mkdir()
        self.store = runtime.MemoryStore(self.state, self.vault)

    def seed(self, last_session=None):
        notes = {
            'Core.md': '# Core\n- Kullanıcı: Baki\n- Eşi: Betül\n',
            'Last-Session.md': last_session or '## 2026-10-18 10:00 · etiket · abcd1234\nDevir kartı.\n',
            'Threads.md': '## Active Threads\n### Thread: T1\n**Status:** active\n',
            'Journal.md': '# Journal\n## 2026-10-18\nNot.\n',
            'Kurallar.md': '# Kurallar\n- Genel kural bir.\n',
        }
        for name, body in notes.items():
            source = f'{COMPANION}/{name}'
            (self.vault / source).write_text(body, encoding='utf-8')
            self.store.ingest({'id': name, 'kind': 'note', 'status': 'active', 'text': body,
                               'source': source, 'updated_at': '2026-10-18T00:00:00Z'})

    def test_last_session_excerpt_preserves_archive_pointer(self):
        text = (
            "## 2026-10-18 10:00 · latest · 12345678\n"
            "- **Ne yapıldı:** En son çalışma yapıldı.\n\n"
            "## Önceki oturumlar\n"
            "Arşivlenen metin: `🔮 850-Companion/Arşiv/Last-Session-2026-10.md`\n"
        )
        excerpt = companion.excerpt('Last-Session.md', text)
        self.assertIn("## Önceki oturumlar", excerpt)
        self.assertIn("Arşivlenen metin: `🔮 850-Companion/Arşiv/Last-Session-2026-10.md`", excerpt)
        self.assertIn("latest", excerpt)

    def test_last_session_excerpt_discards_legacy_historical_cards(self):
        text = (
            "## 2026-10-18 10:00 · latest · 12345678\n"
            "- **Ne yapıldı:** En son çalışma yapıldı.\n\n"
            "## Önceki oturumlar\n"
            "## 2026-10-10 12:00 · old · 87654321\n"
            "- Eski oturum gövdesi uzun metin.\n"
        )
        excerpt = companion.excerpt('Last-Session.md', text)
        self.assertIn("latest", excerpt)
        self.assertNotIn("old", excerpt)
        self.assertNotIn("Eski oturum gövdesi", excerpt)

    def test_person_notes_loaded_into_companion_context(self):
        self.seed()
        # Create person note in notes/kisiler/betul.md
        p_dir = self.vault / 'notes' / 'kisiler'
        p_dir.mkdir(parents=True)
        person_text = (
            "---\n"
            "kind: person\n"
            "title: Betül\n"
            "facts:\n"
            "  iliski: eş\n"
            "  meslek: Doktor\n"
            "---\n"
            "# Betül\n\n"
            "## Beyan\n"
            "- 2026-10-12 (kullanıcı): Eşim. Doktor.\n\n"
            "## Temaslar\n"
            "- 2026-10-17: Akşam yemeği.\n"
        )
        (p_dir / 'betul.md').write_text(person_text, encoding='utf-8')
        self.store.ingest({
            'id': 'betul', 'kind': 'person', 'status': 'active', 'source': 'notes/kisiler/betul.md',
            'text': person_text, 'visibility': 'internal', 'updated_at': '2026-10-18T00:00:00Z',
            'facts': {'iliski': 'eş', 'meslek': 'Doktor'}
        })

        ctx = companion.context(self.store, 8000, 'session-1', 'claude')
        self.assertIn('[notes/kisiler/betul.md]', ctx)
        self.assertIn('# Betül', ctx)
        self.assertIn('Eşim. Doktor.', ctx)
        self.assertIn('[Bilgiler: iliski: eş · meslek: Doktor]', ctx)

    def test_private_person_note_never_loaded_into_companion_context(self):
        self.seed()
        p_dir = self.vault / 'notes' / 'kisiler'
        p_dir.mkdir(parents=True)
        private_text = "# Gizli Kişi\n\n## Beyan\n- Özel temas.\n"
        (p_dir / 'gizli.md').write_text(private_text, encoding='utf-8')
        self.store.ingest({
            'id': 'gizli', 'kind': 'person', 'status': 'active', 'source': 'notes/kisiler/gizli.md',
            'text': private_text, 'visibility': 'private', 'updated_at': '2026-10-18T00:00:00Z'
        })

        ctx = companion.context(self.store, 8000, 'session-1', 'claude')
        self.assertNotIn('Gizli Kişi', ctx)
        self.assertNotIn('notes/kisiler/gizli.md', ctx)

    def test_private_candidates_do_not_hide_visible_person(self):
        self.seed()
        folder = self.vault / 'notes/kisiler'
        folder.mkdir(parents=True)
        for i in range(6):
            visibility = 'private' if i < 5 else 'internal'
            (folder / f'person-{i}.md').write_text(
                f'---\nid: person-{i}\nkind: person\nvisibility: {visibility}\n---\n'
                f'# Person {i}\nVisible identity {i}.\n', encoding='utf-8')
        result = SyncEngine(self.vault, self.state).sync()
        self.assertEqual(result['status'], 'succeeded')
        ctx = companion.context(self.store, 8000, 'session-1', 'claude')
        self.assertIn('[notes/kisiler/person-5.md]', ctx)
        for i in range(5):
            self.assertNotIn(f'Person {i}', ctx)
            self.assertNotIn(f'person-{i}.md', ctx)

    def test_query_reaches_a_person_note_beyond_the_shown_five(self):
        self.seed()
        folder = self.vault / 'notes/kisiler'
        folder.mkdir(parents=True)
        for i in range(5):
            (folder / f'person-{i}.md').write_text(
                f'---\nid: person-{i}\nkind: person\n---\n# Person {i}\nFriend {i}.\n', encoding='utf-8')
        (folder / 'person-5.md').write_text(
            '---\nid: person-5\nkind: person\n---\n# Zümrüt Kayalı\nTez danışmanı.\n', encoding='utf-8')
        (folder / 'person-6.md').write_text(
            '---\nid: person-6\nkind: person\nvisibility: private\n---\n# Zümrüt Kayalıgil\nGizli kişi.\n',
            encoding='utf-8')
        self.assertEqual(SyncEngine(self.vault, self.state).sync()['status'], 'succeeded')
        ambient = companion.context(self.store, 8000, 'session-1', 'claude')
        self.assertNotIn('person-5.md', ambient)
        asked = companion.context(self.store, 8000, 'session-1', 'claude', query='Zümrüt Kayalı kimdi')
        self.assertIn('[Related source: notes/kisiler/person-5.md]', asked)
        self.assertEqual(asked.count('person-5.md'), 1)
        self.assertNotIn('person-6.md', asked)
        self.assertNotIn('Gizli kişi', asked)

    def test_person_facts_share_the_identity_limit(self):
        self.seed()
        folder = self.vault / 'notes/kisiler'
        folder.mkdir(parents=True)
        (folder / 'bounded.md').write_text(
            '---\nid: bounded-person\nkind: person\nfacts:\n  detail: '
            + 'x' * 1800 + '\n---\n# Bounded Person\n', encoding='utf-8')
        self.assertEqual(SyncEngine(self.vault, self.state).sync()['status'], 'succeeded')
        ctx = companion.context(self.store, 10000, 'session-1', 'claude')
        label = '[notes/kisiler/bounded.md]\n'
        self.assertIn(label, ctx)
        self.assertNotIn('x' * 401, ctx)
        self.assertLessEqual(len(ctx), 10000)

    def test_person_sources_preserves_same_basename_in_distinct_folders(self):
        for folder in ('notes/kisiler', 'kisiler'):
            directory = self.vault / folder
            directory.mkdir(parents=True)
            (directory / 'identity.md').write_text('# Identity\n', encoding='utf-8')
        sources = companion.person_sources(self.vault)
        self.assertEqual(len(sources), 2)

    def test_excerpt_person_strips_empty_placeholders(self):
        sample = (
            "# Ayşe Yılmaz\n\n"
            "## Beyan\n"
            "- 2026-10-12 (kullanıcı): Danışman hocam.\n\n"
            "## Çıkarım (onay bekliyor)\n"
            "<!-- Boş tahminler -->\n\n"
            "## Temaslar\n"
            "- 2026-10-15: Tez toplantısı.\n\n"
            "## Kaynaklar\n"
            "<!-- Kaynak bağlantıları -->\n"
        )
        cleaned = companion.excerpt_person(sample)
        self.assertIn('# Ayşe Yılmaz', cleaned)
        self.assertIn('Danışman hocam.', cleaned)
        self.assertIn('Tez toplantısı.', cleaned)
        self.assertNotIn('Çıkarım (onay bekliyor)', cleaned)
        self.assertNotIn('Kaynaklar', cleaned)
        self.assertNotIn('<!--', cleaned)

    def test_person_notes_bounded_and_does_not_starve_rules(self):
        self.seed()
        p_dir = self.vault / 'notes' / 'kisiler'
        p_dir.mkdir(parents=True)
        # Create 8 person notes
        for i in range(1, 9):
            name = f'kisi_{i:02d}'
            text = f'# Kişi {i}\n\n## Beyan\n- Arkadaş {i}.\n'
            (p_dir / f'{name}.md').write_text(text, encoding='utf-8')
            self.store.ingest({
                'id': name, 'kind': 'person', 'status': 'active', 'source': f'notes/kisiler/{name}.md',
                'text': text, 'visibility': 'internal', 'updated_at': '2026-10-18T00:00:00Z'
            })

        ctx = companion.context(self.store, 8000, 'session-1', 'claude')
        # Core rules must still be present
        self.assertIn('Genel kural bir', ctx)
        # At most 5 person notes should be present
        count = sum(1 for i in range(1, 9) if f'notes/kisiler/kisi_{i:02d}.md' in ctx)
        self.assertLessEqual(count, 5)
        self.assertGreaterEqual(count, 1)

    def test_person_sources_ignores_directory_symlink_escaping_vault(self):
        p_dir = self.vault / 'notes' / 'kisiler'
        p_dir.mkdir(parents=True)
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (outside / 'hacker.md').write_text('# Hacker\n', encoding='utf-8')
        try:
            (p_dir / 'evil_link').symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))

        sources = companion.person_sources(self.vault)
        self.assertNotIn('hacker.md', [p.name for p in sources])


if __name__ == '__main__':
    unittest.main()
