"""Unit tests for advisory memory auditor (Issue #372 & RFC #368)."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE_SCRIPTS = str(ROOT / 'template/.claude/scripts')
if _TEMPLATE_SCRIPTS not in sys.path:
    sys.path.insert(0, _TEMPLATE_SCRIPTS)
_SCRIPTS = str(ROOT / 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.append(_SCRIPTS)

import beyin_v3_audit as audit_module
from beyin_entry import human_result
from tests.v3_package_helpers import inherited_env


class MemoryAuditorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-audit-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = (Path(self.tmp.name) / 'vault').resolve()
        self.vault.mkdir()
        self.state = (Path(self.tmp.name) / 'state').resolve()
        self.state.mkdir()

        # Initialize mock runtime for beyin.py entry tests
        (self.vault / '.beyin-runtime.json').write_text(
            json.dumps({'state': str(self.state), 'schema': 1}), encoding='utf-8'
        )
        (self.vault / '.beyin-version').write_text('3.11.0\n', encoding='utf-8')
        shutil.copy2(ROOT / 'scripts/beyin_entry.py', self.vault / 'beyin.py')

        claude_scripts = self.vault / '.claude' / 'scripts'
        claude_scripts.mkdir(parents=True)
        shutil.copy2(ROOT / 'scripts/beyin_v3.py', claude_scripts / 'beyin_v3_cli.py')
        for f in (ROOT / 'template/.claude/scripts').glob('*.py'):
            shutil.copy2(f, claude_scripts / f.name)

        self.knowledge = self.vault / 'knowledge'
        self.knowledge.mkdir()

    def test_near_duplicate_detection(self):
        """Two notes on the same topic sharing > 85% vocabulary are flagged as near-duplicate."""
        note1 = self.knowledge / 'docker-setup.md'
        note1.write_text(
            "---\ntitle: Docker Kurulumu ve Ayarlari\ncreated: 2026-09-01\n---\n"
            "# Docker Kurulumu\nDocker engine kurulumu ubuntu linux sunucusunda basariyla tamamlandi. "
            "Compose dosyalari ile servisler ayaga kaldirildi. Port yonlendirmeleri ve network yapilandirmasi hazir.",
            encoding='utf-8'
        )

        note2 = self.knowledge / 'docker-setup-yeni.md'
        note2.write_text(
            "---\ntitle: Docker Kurulumu ve Yapilandirma\ncreated: 2026-10-01\n---\n"
            "# Docker Kurulumu\nDocker engine kurulumu ubuntu linux sunucusunda basariyla tamamlandi. "
            "Compose dosyalari ile servisler ayaga kaldirildi. Port yonlendirmeleri ve network yapilandirmasi hazir.",
            encoding='utf-8'
        )

        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60)
        self.assertEqual(report['notes_scanned'], 2)
        self.assertEqual(len(report['findings']), 1)
        finding = report['findings'][0]
        self.assertEqual(finding['type'], 'near_duplicate')
        self.assertGreaterEqual(finding['overlap'], 0.85)
        self.assertEqual(finding['severity'], 'warning')
        self.assertIn('Ayri not acmak yerine', finding['recommendation'])

    def test_status_contradiction_detection(self):
        """Detect temporal conflict when older note states unverified/failed and newer note states verified/active."""
        note_old = self.knowledge / 'postgres-baglanti-v1.md'
        note_old.write_text(
            "---\ntitle: PostgreSQL Baglanti Durumu\nupdated_at: 2026-09-10\nstatus: unverified\n---\n"
            "# PostgreSQL Baglanti\nVeritabani baglantisi henuz dogrulanmadi. "
            "Port 5432 erisilemiyor ve socket yanit vermiyor. Baglanti testi basarisiz oldu.",
            encoding='utf-8'
        )

        note_new = self.knowledge / 'postgres-baglanti-v2.md'
        note_new.write_text(
            "---\ntitle: PostgreSQL Baglanti Durumu\nupdated_at: 2026-10-15\nstatus: verified\n---\n"
            "# PostgreSQL Baglanti\nVeritabani baglantisi basariyla dogrulandi. "
            "Port 5432 uzerinden socket baglantisi kuruldu ve sorgular calisiyor. Baglanti testi basarili.",
            encoding='utf-8'
        )

        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.50, min_shared=3)
        self.assertEqual(len(report['findings']), 1)
        finding = report['findings'][0]
        self.assertEqual(finding['type'], 'status_contradiction')
        self.assertEqual(finding['severity'], 'warning')
        self.assertIn('conflict', finding)
        conf = finding['conflict']
        self.assertIn(conf['older_term'], ('dogrulanmadi', 'unverified', 'basarisiz'))
        self.assertIn(conf['newer_term'], ('dogrulandi', 'verified', 'basarili'))
        self.assertIn('tarihsel', finding['recommendation'])

    def test_superseded_notes_are_not_flagged_as_open_conflicts(self):
        """When newer note explicitly includes supersedes: [older-note], no open conflict is raised."""
        note_old = self.knowledge / 'redis-cluster.md'
        note_old.write_text(
            "---\ntitle: Redis Cluster Mimarisi\nupdated_at: 2026-08-01\nstatus: calismiyor\n---\n"
            "Redis cluster dugumleri arasinda iletisim koptu, cluster calismiyor.",
            encoding='utf-8'
        )

        note_new = self.knowledge / 'redis-cluster-v2.md'
        note_new.write_text(
            "---\ntitle: Redis Cluster Mimarisi\nupdated_at: 2026-10-01\nsupersedes: [knowledge/redis-cluster.md]\nstatus: calisiyor\n---\n"
            "Redis cluster dugumleri sentinel ile yapilandirildi, cluster aktif calisiyor.",
            encoding='utf-8'
        )

        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.50, min_shared=3)
        self.assertEqual(len(report['findings']), 0)

    def test_linked_summary_and_detail_notes_are_not_flagged_as_near_duplicate(self):
        """Already linked summary-detail pairs stay out of near_duplicate and substantial_overlap."""
        concepts = self.knowledge / 'concepts'
        concepts.mkdir()
        body = (
            "Konteyner orkestrasyonu katmaninda servis kesfi, saglik yoklamasi, "
            "gunluk dondurme politikasi ve olcekleme esikleri belgelendi."
        )
        summary_md = concepts / 'ornek-mimari.md'
        summary_md.write_text(
            "---\ntitle: Ornek Mimari Ozeti\ncreated: 2026-10-01\n---\n"
            f"# Ornek Mimari\n{body}\nTam teknik ayriintilar: [detay](../ornek-mimari-detay.md).\n",
            encoding='utf-8',
        )
        detail_md = self.knowledge / 'ornek-mimari-detay.md'
        detail_md.write_text(
            "---\ntitle: Ornek Mimari Detay\ncreated: 2026-10-01\n---\n"
            f"# Ornek Mimari Detay\n{body} Ek parametre tablolarini ve hata kodlarini icerir.\n",
            encoding='utf-8',
        )

        summary_wiki = concepts / 'ornek-plan.md'
        summary_wiki.write_text(
            "---\ntitle: Ornek Plan Ozeti\ncreated: 2026-10-02\n---\n"
            "# Ornek Plan\nYedekleme penceresi, replikasyon gecikmesi, geri yukleme tatbikati ve "
            "kota denetimi adimlari standartlastirildi. Tam dokum: [[ornek-plan-detay]].\n",
            encoding='utf-8',
        )
        detail_wiki = self.knowledge / 'ornek-plan-detay.md'
        detail_wiki.write_text(
            "---\ntitle: Ornek Plan Detay\ncreated: 2026-10-02\n---\n"
            "# Ornek Plan Detay\nYedekleme penceresi, replikasyon gecikmesi, geri yukleme tatbikati ve "
            "kota denetimi adimlari komut ornekleriyle belgeye islendi.\n",
            encoding='utf-8',
        )

        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
        flagged = [
            f for f in report['findings']
            if {f['older_note'], f['newer_note']} in (
                {'knowledge/concepts/ornek-mimari.md', 'knowledge/ornek-mimari-detay.md'},
                {'knowledge/concepts/ornek-plan.md', 'knowledge/ornek-plan-detay.md'},
            )
        ]
        self.assertEqual(flagged, [], flagged)

    def test_linked_notes_with_status_contradiction_are_still_reported(self):
        """A direct link between two notes must not suppress a real status contradiction."""
        old_note = self.knowledge / 'servis-v1.md'
        old_note.write_text(
            "---\ntitle: Servis Durumu\nupdated_at: 2026-09-01\nstatus: unverified\n---\n"
            "Baglanti havuzu henuz dogrulanmadi, servis yanit vermiyor ve test basarisiz.\n"
            "Yeni not: [[servis-v2]].\n",
            encoding='utf-8',
        )
        new_note = self.knowledge / 'servis-v2.md'
        new_note.write_text(
            "---\ntitle: Servis Durumu Guncel\nupdated_at: 2026-10-01\nstatus: verified\n---\n"
            "Baglanti havuzu testi dogrulandi, servis yanit veriyor ve test basarili.\n",
            encoding='utf-8',
        )

        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.50, min_shared=3)
        self.assertEqual(len(report['findings']), 1)
        self.assertEqual(report['findings'][0]['type'], 'status_contradiction')

    _PAIR_TEXT = ("Yedekleme penceresi, replikasyon gecikmesi, geri yukleme tatbikati ve "
                  "kota denetimi adimlari standartlastirildi.")

    def _note(self, rel, title, extra=''):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(f"---\ntitle: {title}\ncreated: 2026-10-02\n---\n"
                         f"# {title}\n{self._PAIR_TEXT} {extra}\n")
        return path

    def _overlap_paths(self, report):
        return [{f['older_note'], f['newer_note']} for f in report['findings']
                if f['type'] in ('near_duplicate', 'substantial_overlap')]

    def test_linked_pairs_are_counted_in_json_and_human_report(self):
        """A silenced linked pair leaves a count, so the report does not hide it entirely."""
        self._note('knowledge/ornek-plan.md', 'Ornek Plan Ozeti', 'Tam dokum: [[ornek-plan-detay]].')
        self._note('knowledge/ornek-plan-detay.md', 'Ornek Plan Detay', 'Komut ornekleri eklendi.')
        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['linked_pairs_skipped'], 1)
        text = audit_module.format_audit_human(report)
        self.assertIn('Birbirine bagli oldugu icin raporlanmayan ortusme: 1 cift', text)

        target = audit_module.audit_memory(self.vault, state=self.state, target_file='knowledge/ornek-plan-detay.md',
                                           threshold=0.60, min_shared=5)
        self.assertEqual((target['findings'], target['linked_pairs_skipped']), ([], 1))

    def test_ambiguous_wikilink_name_does_not_silence_either_same_named_note(self):
        """[[name]] that two notes share links neither, so the real overlap is still reported."""
        self._note('knowledge/ornek-plan.md', 'Ornek Plan Ozeti', 'Tam dokum: [[detay]].')
        self._note('projects/alfa/detay.md', 'Alfa Detay', 'Komut ornekleri eklendi.')
        self._note('projects/beta/detay.md', 'Beta Detay', 'Komut ornekleri eklendi.')
        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
        found = self._overlap_paths(report)
        self.assertIn({'knowledge/ornek-plan.md', 'projects/alfa/detay.md'}, found)
        self.assertIn({'knowledge/ornek-plan.md', 'projects/beta/detay.md'}, found)
        self.assertEqual(report['linked_pairs_skipped'], 0)

    def test_folder_wikilink_resolves_by_unique_path_suffix(self):
        """[[concepts/ornek-plan]] names knowledge/concepts/ornek-plan.md the way Obsidian does."""
        self._note('knowledge/concepts/ornek-plan.md', 'Ornek Plan Ozeti')
        self._note('knowledge/ornek-plan-detay.md', 'Ornek Plan Detay', 'Ozet: [[concepts/ornek-plan|ozet]].')
        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
        self.assertEqual(self._overlap_paths(report), [])
        self.assertEqual(report['linked_pairs_skipped'], 1)

    def test_links_in_code_or_outside_the_vault_do_not_count(self):
        """A link quoted in inline/fenced code, or a Markdown path above the vault root, links nothing."""
        self._note('ornek-plan-detay.md', 'Ornek Plan Detay', 'Komut ornekleri eklendi.')
        self._note('knowledge/ornek-plan.md', 'Ornek Plan Ozeti',
                   'Ornek: `[[ornek-plan-detay]]`\n\n````md\n[[ornek-plan-detay]]\n````\n'
                   'Dis kopya: [dis](../../ornek-plan-detay.md).')
        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
        self.assertEqual(self._overlap_paths(report), [{'knowledge/ornek-plan.md', 'ornek-plan-detay.md'}])
        self.assertEqual(report['linked_pairs_skipped'], 0)

    def test_link_in_crlf_fenced_code_does_not_count(self):
        """A fenced block with CRLF line endings still hides the link it quotes.

        File reads normalise newlines, so this pins the helper for callers that pass raw text.
        """
        body = "Ozet\r\n```\r\n[[kod-ornegi]]\r\n```\r\nDetay: [[ornek-plan-detay]]\r\n"
        self.assertEqual(audit_module._link_keys_from_body(body, 'knowledge/ornek-plan.md'),
                         ((True, 'ornek-plan-detay'),))

    def test_nfc_wikilink_matches_nfd_file_name(self):
        """A macOS/iCloud NFD file name still matches the composed [[Şube Özeti]] a person typed."""
        import unicodedata
        self._note(unicodedata.normalize('NFD', 'knowledge/Şube Özeti.md'), 'Sube Ozeti')
        self._note('knowledge/sube-detay.md', 'Sube Detay',
                   'Ozet: [[' + unicodedata.normalize('NFC', 'Şube Özeti') + ']].')
        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
        self.assertEqual(report['notes_scanned'], 2)
        self.assertEqual(self._overlap_paths(report), [])
        self.assertEqual(report['linked_pairs_skipped'], 1)

    def test_privacy_and_trust_boundary_excluded(self):
        """Notes with visibility: private, sensitive directory, or untrusted status are never audited."""
        # 1. Private note sharing content with public note
        private_note = self.knowledge / 'private-strategy.md'
        private_note.write_text(
            "---\ntitle: Gizli Strateji\nvisibility: private\n---\n"
            "Yapay zeka modelleri optimizasyon stratejisi ve butce planlamasi.",
            encoding='utf-8'
        )

        # 2. Sensitive directory note
        kasa_dir = self.vault / '🔐 Kasa'
        kasa_dir.mkdir()
        kasa_note = kasa_dir / 'finans.md'
        kasa_note.write_text(
            "---\ntitle: Finans\n---\n"
            "Yapay zeka modelleri optimizasyon stratejisi ve butce planlamasi.",
            encoding='utf-8'
        )

        # 3. Untrusted note
        untrusted_note = self.knowledge / 'untrusted-note.md'
        untrusted_note.write_text(
            "---\ntitle: Guvensiz Not\ntrust: untrusted\n---\n"
            "Yapay zeka modelleri optimizasyon stratejisi ve butce planlamasi.",
            encoding='utf-8'
        )

        # 4. Standard public note
        public_note = self.knowledge / 'ai-strateji.md'
        public_note.write_text(
            "---\ntitle: AI Stratejisi\nvisibility: internal\n---\n"
            "Yapay zeka modelleri optimizasyon stratejisi ve butce planlamasi.",
            encoding='utf-8'
        )

        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.50, min_shared=3)
        # Only public_note should be scanned as candidate; cannot pair with private/sensitive/untrusted
        self.assertEqual(report['notes_scanned'], 1)
        self.assertEqual(len(report['findings']), 0)

    def test_negative_control_unrelated_notes(self):
        """Unrelated notes sharing no topical terms produce 0 findings."""
        note1 = self.knowledge / 'yemek-tarifi.md'
        note1.write_text(
            "---\ntitle: Menemen Tarifi\n---\n"
            "Domates, biber, yumurta ve tereyagi ile yapilan lezzetli bir kahvalti yemegi.",
            encoding='utf-8'
        )

        note2 = self.knowledge / 'kuantum-fizigi.md'
        note2.write_text(
            "---\ntitle: Kuantum Dolanillik\n---\n"
            "Fotonlar ve atomalti parcaciklarin dalga fonksiyonu cokmesi ve spin olcumu.",
            encoding='utf-8'
        )

        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.50, min_shared=3)
        self.assertEqual(report['notes_scanned'], 2)
        self.assertEqual(len(report['findings']), 0)

    def test_target_file_specific_audit(self):
        """When target_file is supplied, only findings involving target are returned."""
        n1 = self.knowledge / 'target-test.md'
        n1.write_text(
            "---\ntitle: Test Mimarisi\n---\n"
            "Birim testleri, entegrasyon testleri ve uctan uca test kapsami otomasyonu.",
            encoding='utf-8'
        )

        n2 = self.knowledge / 'diger-test.md'
        n2.write_text(
            "---\ntitle: Test Kapsami\n---\n"
            "Birim testleri, entegrasyon testleri ve uctan uca test kapsami otomasyonu.",
            encoding='utf-8'
        )

        n3 = self.knowledge / 'alakasiz.md'
        n3.write_text(
            "---\ntitle: Bahce Bakimi\n---\n"
            "Cim bicme, sulama sistemleri ve gubreleme yontemleri.",
            encoding='utf-8'
        )

        report = audit_module.audit_memory(self.vault, state=self.state, target_file='knowledge/target-test.md', threshold=0.60, min_shared=3)
        self.assertEqual(report['target'], 'knowledge/target-test.md')
        self.assertEqual(len(report['findings']), 1)
        self.assertIn('knowledge/target-test.md', (report['findings'][0]['older_note'], report['findings'][0]['newer_note']))

    def test_cli_audit_json_and_human(self):
        """CLI invocation via beyin.py audit supports both --json and human-readable output."""
        note1 = self.knowledge / 'kavram-a.md'
        note1.write_text(
            "---\ntitle: Kavram A\nupdated_at: 2026-09-01\n---\n"
            "Mikroservis mimarisinde kafka event bus entegrasyonu ve mesaj kuyrugu yapilandirmasi.",
            encoding='utf-8'
        )

        note2 = self.knowledge / 'kavram-b.md'
        note2.write_text(
            "---\ntitle: Kavram B\nupdated_at: 2026-10-01\n---\n"
            "Mikroservis mimarisinde kafka event bus entegrasyonu ve mesaj kuyrugu yapilandirmasi.",
            encoding='utf-8'
        )

        # 1. Test CLI with --json
        cmd_json = [
            sys.executable,
            str(self.vault / '.claude/scripts/beyin_v3_cli.py'),
            '--vault', str(self.vault),
            '--state', str(self.state),
            'audit',
            '--threshold', '0.50',
            '--min-shared', '3'
        ]
        res_json = subprocess.run(cmd_json, capture_output=True, text=True, env=inherited_env())
        self.assertEqual(res_json.returncode, 0, res_json.stderr)
        data = json.loads(res_json.stdout)
        self.assertEqual(data['schema'], 1)
        self.assertEqual(len(data['findings']), 1)

        # 2. Test entrypoint with human output
        cmd_human = [
            sys.executable,
            str(self.vault / 'beyin.py'),
            'audit',
            '--threshold', '0.50',
            '--min-shared', '3',
            '--human'
        ]
        res_human = subprocess.run(cmd_human, capture_output=True, text=True, env=inherited_env())
        self.assertEqual(res_human.returncode, 0, res_human.stderr)
        self.assertIn("Ikinci Beyin Hafiza Denetim Raporu", res_human.stdout)
        self.assertIn("Yuksek Tekrar ve Ortusme", res_human.stdout)
        self.assertIn("Denetci Siniri", res_human.stdout)


    SHARED = ("Kuyruk servisi yeniden deneme politikasi bekleme suresi zaman asimi "
              "yapilandirmasi uretim ortami izleme panosu alarm esigi.\n")

    def _public_twin(self):
        (self.knowledge / 'kuyruk.md').write_text(
            "---\ntitle: Kuyruk\nupdated_at: 2026-09-01\n---\n" + self.SHARED, encoding='utf-8')

    def test_private_or_kasa_target_is_refused_without_printing_its_terms(self):
        """An explicit target passes the same boundary as the scan; nothing of it reaches the report."""
        self._public_twin()
        (self.knowledge / 'ozel-plan.md').write_text(
            "---\nvisibility: private\nupdated_at: 2026-10-01\n---\n" + self.SHARED + "zzgizlikelime\n",
            encoding='utf-8')
        kasa = self.vault / 'Kasa'
        kasa.mkdir()
        (kasa / 'not.md').write_text(
            "---\nupdated_at: 2026-10-02\n---\n" + self.SHARED + "zzkasakelime\n", encoding='utf-8')
        outside = Path(self.tmp.name) / 'outside.md'
        outside.write_text(self.SHARED + "zzdisarikelime\n", encoding='utf-8')
        for target in ('knowledge/ozel-plan.md', 'Kasa/not.md', str(outside)):
            with self.subTest(target=target), self.assertRaises(ValueError) as caught:
                audit_module.audit_memory(self.vault, state=self.state, target_file=target, min_shared=3)
            self.assertNotIn('zz', str(caught.exception))
        result, code = self._cli_result(['audit', 'knowledge/ozel-plan.md', '--min-shared', '3'])
        self.assertEqual(code, 1)
        self.assertNotIn('zzgizli', json.dumps(result))

    def test_symlinked_note_is_not_read(self):
        """A note-shaped link in a public folder must not carry a kasa note into the report."""
        self._public_twin()
        kasa = self.vault / 'Kasa'
        kasa.mkdir()
        secret = kasa / 'gizli.md'
        secret.write_text("---\nupdated_at: 2026-10-02\n---\n" + self.SHARED + "zzkasakelime\n", encoding='utf-8')
        try:
            os.symlink(secret, self.knowledge / 'alias.md')
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are not available')
        report = audit_module.audit_memory(self.vault, state=self.state, min_shared=3)
        self.assertEqual(report['notes_scanned'], 1)
        self.assertNotIn('zzkasa', json.dumps(report))
        with self.assertRaises(ValueError):
            audit_module.audit_memory(self.vault, state=self.state, target_file='knowledge/alias.md', min_shared=3)

    def test_bom_and_yaml_false_keep_private_and_untrusted_notes_out(self):
        """The engine's parser reads a BOM frontmatter and `trusted: false`; the audit must agree."""
        self._public_twin()
        (self.knowledge / 'windows.md').write_bytes(
            b'\xef\xbb\xbf---\r\nvisibility: private\r\n---\r\n' + self.SHARED.encode() + b'zzbomkelime\r\n')
        (self.knowledge / 'dis-kaynak.md').write_text(
            "---\ntrusted: false\n---\n" + self.SHARED + "zzguvensiz\n", encoding='utf-8')
        report = audit_module.audit_memory(self.vault, state=self.state, min_shared=3)
        self.assertEqual(report['notes_scanned'], 1)
        self.assertEqual(report['findings'], [])

    def test_turkish_status_flip_is_found_in_stemmed_text(self):
        """The #372 example in real Turkish spelling, with no frontmatter status to lean on."""
        (self.knowledge / 'baglanti-1.md').write_text(
            "---\nupdated_at: 2026-09-01\n---\n# Bağlantı\nVeritabanı bağlantısı henüz doğrulanmadı. " + self.SHARED,
            encoding='utf-8')
        (self.knowledge / 'baglanti-2.md').write_text(
            "---\nupdated_at: 2026-10-01\n---\n# Bağlantı\nVeritabanı bağlantısı doğrulandı. " + self.SHARED,
            encoding='utf-8')
        findings = audit_module.audit_memory(self.vault, state=self.state, min_shared=3)['findings']
        self.assertEqual([f['type'] for f in findings], ['status_contradiction'])
        self.assertEqual(findings[0]['conflict'], {'older_term': 'dogrulanmadi', 'newer_term': 'dogrulandi',
                                                   'direction': 'negative_to_positive'})

    def test_both_sides_or_prose_words_are_not_a_status_flip(self):
        """Two test logs that both say "passed" and "failed", or "açık kaynak" against "çalışmıyor"."""
        for old_tail, new_tail in (("12 passed, 0 failed.", "14 passed, 0 failed."),
                                   ("Açık kaynak kütüphane.", "Alarm servisi çalışmıyor.")):
            with self.subTest(old=old_tail):
                (self.knowledge / 'rapor-1.md').write_text(
                    "---\nupdated_at: 2026-09-01\n---\n" + self.SHARED + old_tail, encoding='utf-8')
                (self.knowledge / 'rapor-2.md').write_text(
                    "---\nupdated_at: 2026-10-01\n---\n" + self.SHARED + new_tail, encoding='utf-8')
                findings = audit_module.audit_memory(self.vault, state=self.state, min_shared=3)['findings']
                self.assertEqual(len(findings), 1)
                self.assertNotEqual(findings[0]['type'], 'status_contradiction')

    def test_retired_receipt_and_generated_notes_stay_out(self):
        """Notes the context never delivers do not fill the report (archived notes were 18 of 20)."""
        self._public_twin()
        archive = self.vault / '📦 900-Archive'
        archive.mkdir()
        for folder, name, front in ((archive, 'eski.md', 'status: Arşivlendi'),
                                    (self.knowledge, 'arsivli.md', 'status: archived'),
                                    (self.knowledge, 'makbuz.md', 'kind: receipt'),
                                    (self.knowledge, 'gorunum.md', 'generated: true')):
            (folder / name).write_text("---\n" + front + "\n---\n" + self.SHARED, encoding='utf-8')
        report = audit_module.audit_memory(self.vault, state=self.state, min_shared=3)
        self.assertEqual(report['notes_scanned'], 1)
        self.assertEqual(report['findings'], [])

    def test_supersedes_resolves_like_the_core(self):
        """A record id from note-create, an Obsidian list and a [[link]] all retire the old note."""
        (self.knowledge / 'deploy-v1.md').write_text(
            "---\nid: deployment-v1\nupdated_at: 2026-09-01\nstatus: failed\n---\n" + self.SHARED,
            encoding='utf-8')
        for supersedes in ("supersedes:\n  - deployment-v1", 'supersedes: ["[[deploy-v1]]"]'):
            with self.subTest(supersedes=supersedes):
                (self.knowledge / 'deploy-v2.md').write_text(
                    "---\nupdated_at: 2026-10-01\nstatus: passed\n" + supersedes + "\n---\n" + self.SHARED,
                    encoding='utf-8')
                report = audit_module.audit_memory(self.vault, state=self.state, min_shared=3)
                self.assertEqual(report['findings'], [])
                self.assertEqual(report['notes_scanned'], 1)

    def test_target_cannot_bypass_retirement_or_generated_note_filters(self):
        self._public_twin()
        target = self.knowledge / 'hedef.md'
        for front in ('status: archived', 'status: Arşivlendi', 'kind: receipt', 'generated: true'):
            with self.subTest(front=front):
                target.write_text('---\n' + front + '\n---\n' + self.SHARED, encoding='utf-8')
                with self.assertRaises(ValueError):
                    audit_module.audit_memory(self.vault, state=self.state,
                                              target_file='knowledge/hedef.md', min_shared=3)

    def test_superseded_target_does_not_reappear_in_findings(self):
        self._public_twin()
        (self.knowledge / 'yeni.md').write_text(
            '---\nsupersedes: [knowledge/kuyruk.md]\n---\n' + self.SHARED, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'retired'):
            audit_module.audit_memory(self.vault, state=self.state,
                                      target_file='knowledge/kuyruk.md', min_shared=3)

    def test_negative_limit_is_rejected_and_zero_limit_is_empty(self):
        self._public_twin()
        (self.knowledge / 'yeni.md').write_text(self.SHARED, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'limit'):
            audit_module.audit_memory(self.vault, state=self.state, limit=-1)
        report = audit_module.audit_memory(self.vault, state=self.state, limit=0)
        self.assertEqual(report['findings'], [])
        self.assertGreater(report['pairs_compared'], 0)

    def test_created_dates_determine_status_order_despite_rewritten_mtime(self):
        old = self.knowledge / 'eski.md'
        new = self.knowledge / 'yeni.md'
        old.write_text('---\ncreated: 2026-09-01\nstatus: failed\n---\n' + self.SHARED, encoding='utf-8')
        new.write_text('---\ncreated: 2026-10-01\nstatus: passed\n---\n' + self.SHARED, encoding='utf-8')
        os.utime(old, (2000000000, 2000000000))
        os.utime(new, (1000000000, 1000000000))
        finding = audit_module.audit_memory(self.vault, state=self.state)['findings'][0]
        self.assertEqual(finding['older_note'], 'knowledge/eski.md')
        self.assertEqual(finding['conflict']['direction'], 'negative_to_positive')

    def test_unknown_or_equal_dates_do_not_claim_a_status_change(self):
        for front in ('', 'created: 2026-10-01\n'):
            with self.subTest(front=front):
                (self.knowledge / 'a.md').write_text(
                    '---\n' + front + 'status: failed\n---\n' + self.SHARED, encoding='utf-8')
                (self.knowledge / 'b.md').write_text(
                    '---\n' + front + 'status: passed\n---\n' + self.SHARED, encoding='utf-8')
                findings = audit_module.audit_memory(self.vault, state=self.state)['findings']
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0]['type'], 'near_duplicate')

    def test_timestamp_parser_preserves_offsets_and_fractional_seconds(self):
        expected = datetime(2026, 11, 11, 7, 0, 0, 250000, tzinfo=timezone.utc).timestamp()
        for stamp in ('2026-11-11T07:00:00.250000Z', '2026-11-11T10:00:00.25+03:00',
                      '2026-11-11 01:30:00.25-05:30', '2026-11-11T07:00:00.25'):
            with self.subTest(stamp=stamp):
                self.assertEqual(audit_module.parse_date_to_timestamp(stamp), expected)
        midnight = datetime(2026, 11, 11, tzinfo=timezone.utc).timestamp()
        for stamp in ('2026-11-11', '2026-11-11 00:00', '2026-11-11T00:00',
                      '2026-11-11 00:00:00', '2026-11-11T00:00:00'):
            with self.subTest(stamp=stamp):
                self.assertEqual(audit_module.parse_date_to_timestamp(stamp), midnight)

    def test_invalid_timestamp_suffix_does_not_establish_chronology(self):
        for stamp in ('2026-11-11T07:00:00invalid', '2026-11-11T07:00:00+25:00',
                      '2026-11-11T07:00:00+03:99', '2026-11-11T07:00:00.',
                      '2026-02-30', '2026-11-11T25:00:00Z', '', None, 20261111):
            with self.subTest(stamp=stamp):
                self.assertIsNone(audit_module.parse_date_to_timestamp(stamp))

    def _dated_pair(self, first, second, key='updated_at'):
        for name, stamp, status in (('a', first, 'failed'), ('b', second, 'passed')):
            (self.knowledge / (name + '.md')).write_text(
                f'---\ntitle: Kuyruk\n{key}: {stamp}\nstatus: {status}\n---\n' + self.SHARED,
                encoding='utf-8')

    def test_offsets_order_actual_notes_and_installed_cli_by_instant(self):
        self._dated_pair('2026-11-11T10:00:00+03:00', '2026-11-11T08:00:00Z')
        before = {p.name: p.read_bytes() for p in self.knowledge.glob('*.md')}
        direct = audit_module.audit_memory(self.vault, state=self.state)
        result, code = self._cli_result(['audit'])
        self.assertEqual(code, 0)
        for report in (direct, result):
            finding = report['findings'][0]
            self.assertEqual(finding['older_note'], 'knowledge/a.md')
            self.assertEqual(finding['newer_note'], 'knowledge/b.md')
            self.assertEqual(finding['conflict']['direction'], 'negative_to_positive')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.knowledge.glob('*.md')})

    def test_equal_instants_with_different_offsets_are_not_status_changes(self):
        for key in ('created', 'updated_at', 'modified'):
            with self.subTest(key=key):
                self._dated_pair('2026-11-11T10:00:00+03:00', '2026-11-11T07:00:00Z', key)
                finding = audit_module.audit_memory(self.vault, state=self.state)['findings'][0]
                self.assertEqual(finding['type'], 'near_duplicate')
                self.assertNotIn('conflict', finding)

    def test_fractional_seconds_establish_real_chronology(self):
        self._dated_pair('2026-11-11T07:00:00.100Z', '2026-11-11T07:00:00.900Z')
        finding = audit_module.audit_memory(self.vault, state=self.state)['findings'][0]
        self.assertEqual(finding['type'], 'status_contradiction')
        self.assertEqual(finding['conflict']['direction'], 'negative_to_positive')

    def test_invalid_date_tail_is_unknown_not_a_status_change(self):
        self._dated_pair('2026-11-11T07:00:00invalid', '2026-11-11T08:00:00Z')
        finding = audit_module.audit_memory(self.vault, state=self.state)['findings'][0]
        self.assertEqual(finding['type'], 'near_duplicate')
        self.assertNotIn('conflict', finding)

    def test_asymmetric_size_notes_do_not_produce_false_duplicate_or_body_conflict(self):
        """Asymmetric pair (large document vs tiny note) must not be flagged as near-duplicate

        or status contradiction from stray prose words (#466).
        """
        vocabulary = ("architecture service configuration system infrastructure parameter analysis "
                      "model data pipeline testing database transaction concurrency reliability durability "
                      "latency throughput partition sharding replication consistency consensus protocol "
                      "transport network router gateway address subnet packet buffer socket process thread "
                      "scheduler memory storage volume filesystem directory permission ownership encryption "
                      "authentication authorization credential certificate signature checksum compression "
                      "serialization validation migration rollback deployment release version package "
                      "dependency registry container orchestration cluster monitoring tracing logging metrics "
                      "alert incident recovery backup restore snapshot retention archive inventory catalog "
                      "document record entity relation index query filter ranking relevance retrieval "
                      "precision recall benchmark evaluation experiment hypothesis evidence research "
                      "discovery inference reasoning context continuity session receipt capture synthesis").split()
        large_body = " ".join(vocabulary) + " tamamlandi"
        short_body = " ".join(vocabulary[:8]) + " bekliyor"
        for name, body, date in [('buyuk-not.md', large_body, '2026-08-01'),
                                 ('kucuk-not.md', short_body, '2026-10-01')]:
            (self.knowledge / name).write_text(
                f"---\ntitle: Shared Topic\nupdated_at: {date}\n---\n{body}\n", encoding='utf-8')
        first, second = audit_module.scan_audit_candidates(self.vault, self.state)
        shared = len(first['tokens'] & second['tokens'])
        # Ensure this reaches the changed gate; repeated numbered filler does not.
        self.assertGreaterEqual(shared / min(len(first['tokens']), len(second['tokens'])), 0.80)
        self.assertLess(shared / len(first['tokens'] | second['tokens']), 0.20)

        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
        # Must not report near_duplicate or status_contradiction for asymmetric pair
        self.assertEqual(len(report['findings']), 0)

    def test_explicit_frontmatter_status_contradiction_still_detected(self):
        """When frontmatter explicitly declares contradicting status, conflict is flagged."""
        (self.knowledge / 'gorev-eski.md').write_text(
            "---\ntitle: Gorev\nstatus: failed\nupdated_at: 2026-09-01\n---\n" + self.SHARED,
            encoding='utf-8'
        )
        (self.knowledge / 'gorev-yeni.md').write_text(
            "---\ntitle: Gorev\nstatus: passed\nupdated_at: 2026-10-01\n---\n" + self.SHARED,
            encoding='utf-8'
        )
        report = audit_module.audit_memory(self.vault, state=self.state, min_shared=3)
        self.assertEqual(len(report['findings']), 1)
        self.assertEqual(report['findings'][0]['type'], 'status_contradiction')

    def _cli_result(self, args):
        cmd = [sys.executable, str(self.vault / '.claude/scripts/beyin_v3_cli.py'),
               '--vault', str(self.vault), '--state', str(self.state)] + args
        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', env=inherited_env())
        text = res.stdout if res.returncode == 0 else res.stderr
        return json.loads(text), res.returncode

    def test_audit_scale_10000_candidates_and_massive_string(self):
        # Prove no OOM with massive strings for candidate's word count and re.findall usage
        body = "word " * 100000
        meta = {'title': 'Massive Note'}
        path = self.vault / 'massive.md'
        candidate = audit_module._candidate(path, 'massive.md', meta, body)
        self.assertEqual(candidate['words_count'], 100000)

    def _write_card(self, rel: str, text: str) -> Path:
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def _customer_card(
        self,
        title: str,
        legal_name: str,
        city: str,
        sector: str,
        *,
        website: str = '',
        tax_number: str = '',
        contact: str = '',
        phone: str = '',
        email: str = '',
        products: str = '',
        payment: str = '',
        notes: str = '',
        frontmatter: str = '',
    ) -> str:
        head = f"---\n{frontmatter.strip()}\n---\n" if frontmatter.strip() else ''
        h1 = f"# {title}\n\n" if title else ''
        return (
            f"{head}{h1}"
            "## Company\n"
            f"- Legal name: {legal_name}\n"
            f"- City: {city}\n"
            f"- Sector: {sector}\n"
            f"- Website: {website}\n"
            f"- Tax number: {tax_number}\n"
            "- Delivery address:\n\n"
            "## Contact\n"
            f"- Contact person: {contact}\n"
            f"- Phone: {phone}\n"
            f"- E-mail: {email}\n\n"
            "## Trade\n"
            f"- Products bought: {products}\n"
            f"- Payment terms: {payment}\n\n"
            "## Notes\n"
            f"{notes}\n"
        )

    def test_template_sibling_cards_with_distinct_values_are_not_flagged(self):
        """Issue #953: sibling cards sharing template headings and field labels with distinct values are skipped."""
        self._write_card(
            'notes/customers/north-bakery.md',
            self._customer_card(
                'North Bakery Ltd',
                'North Bakery Ltd',
                'Leeds',
                'food',
                frontmatter='title: North Bakery Ltd\ncreated: 2026-10-01',
            ),
        )
        self._write_card(
            'notes/customers/harbour-paints.md',
            self._customer_card(
                'Harbour Paints GmbH',
                'Harbour Paints GmbH',
                'Hamburg',
                'coatings',
                frontmatter='title: Harbour Paints GmbH\ncreated: 2026-10-02',
            ),
        )

        # Verify raw lexical overlap would otherwise cross near_duplicate (>= 0.80, jaccard >= 0.65).
        first, second = audit_module.scan_audit_candidates(self.vault, self.state)
        raw_shared = len(first['tokens'] & second['tokens'])
        self.assertGreaterEqual(raw_shared / min(len(first['tokens']), len(second['tokens'])), 0.80)
        self.assertGreaterEqual(raw_shared / len(first['tokens'] | second['tokens']), 0.65)

        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
        self.assertEqual(report['findings'], [], report['findings'])
        self.assertEqual(report['template_pairs_skipped'], 1)
        self.assertIn('1 cift', audit_module.format_audit_human(report))

        target = audit_module.audit_memory(
            self.vault, state=self.state, target_file='notes/customers/north-bakery.md',
            threshold=0.60, min_shared=5,
        )
        self.assertEqual((target['findings'], target['template_pairs_skipped']), ([], 1))

    def test_template_cards_with_duplicate_values_are_still_flagged(self):
        """Genuine duplicate customer cards sharing both template AND actual values still flag near_duplicate."""
        for label, extra in (
            ('filled', dict(
                website='https://northbakery.example',
                contact='Clara Bennett',
                phone='+44 113 555 0101',
                email='clara@northbakery.example',
                products='Industrial ovens',
                payment='Net 30',
                notes='Prefers morning deliveries.',
            )),
            ('sparse', {}),
        ):
            with self.subTest(case=label):
                self._write_card(
                    'notes/customers/north-bakery.md',
                    self._customer_card(
                        'North Bakery Ltd', 'North Bakery Ltd', 'Leeds', 'food',
                        frontmatter='title: North Bakery Ltd\ncreated: 2026-10-01',
                        **extra,
                    ),
                )
                self._write_card(
                    'notes/customers/north-bakery-copy.md',
                    self._customer_card(
                        'North Bakery Ltd', 'North Bakery Ltd', 'Leeds', 'food',
                        frontmatter='title: North Bakery Ltd\ncreated: 2026-10-05',
                        **extra,
                    ),
                )
                report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
                self.assertEqual(len(report['findings']), 1, report)
                self.assertEqual(report['findings'][0]['type'], 'near_duplicate')
                self.assertEqual(report['template_pairs_skipped'], 0)

    def test_empty_duplicate_template_copies_are_still_flagged(self):
        """Two empty template copies with no substantive values must not be silenced as distinct cards."""
        for label, h1 in (('no-heading', ''), ('shared-heading', 'Customer Card')):
            with self.subTest(case=label):
                self._write_card('notes/customers/draft-one.md', self._customer_card(h1, '', '', ''))
                self._write_card('notes/customers/draft-two.md', self._customer_card(h1, '', '', ''))
                report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
                self.assertEqual(len(report['findings']), 1, report)
                self.assertEqual(report['findings'][0]['type'], 'near_duplicate')
                self.assertEqual(report['template_pairs_skipped'], 0)

    def test_plain_prose_duplicates_in_same_folder_are_still_flagged(self):
        """Plain prose duplicate notes in the same folder (with 1 shared H1 or none) still flag near_duplicate."""
        prose = (
            "Konteyner orkestrasyonu katmaninda servis kesfi, saglik yoklamasi, "
            "gunluk dondurme politikasi ve olcekleme esikleri belgelendi."
        )
        for label, heading in (('single-h1', '# Mimari Notu\n\n'), ('no-heading', '')):
            with self.subTest(case=label):
                self._write_card('notes/prose/not-a.md', f"{heading}{prose}\n")
                self._write_card('notes/prose/not-b.md', f"{heading}{prose}\n")
                report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
                self.assertEqual(len(report['findings']), 1, report)
                self.assertEqual(report['findings'][0]['type'], 'near_duplicate')
                self.assertEqual(report['template_pairs_skipped'], 0)

    def test_turkish_bold_template_field_labels_are_discounted_cleanly(self):
        """Turkish bold field labels (- **Adı:** / - **Şehir**:) separate sibling cards while keeping real dupes."""
        def tr_card(name: str, city: str, sector: str) -> str:
            return (
                f"---\ntitle: {name}\n---\n"
                f"# {name}\n\n"
                "## Sirket Bilgileri\n"
                f"- **Adı:** {name}\n"
                f"- **Şehir**: {city}\n"
                f"- **Sektör:** {sector}\n"
                "- **Web Sitesi:**\n"
                "- **Vergi Numarasi**:\n\n"
                "## Iletisim Bilgileri\n"
                "- **İletişim Kişisi:**\n"
                "- **Telefon:**\n"
                "- **E-posta:**\n\n"
                "## Ticari Bilgiler\n"
                "- **Alınan Ürünler:**\n"
                "- **Ödeme Koşulları:**\n\n"
                "## Notlar\n"
            )

        self._write_card('notes/firmalar/kuzey-firin.md', tr_card('Kuzey Firin', 'Izmir', 'Gida'))
        self._write_card('notes/firmalar/liman-boya.md', tr_card('Liman Boya', 'Bursa', 'Kimya'))
        distinct = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
        self.assertEqual(distinct['notes_scanned'], 2)
        self.assertEqual(distinct['findings'], [], distinct['findings'])
        self.assertEqual(distinct['template_pairs_skipped'], 1)

        self._write_card('notes/firmalar/kuzey-firin-kopya.md', tr_card('Kuzey Firin', 'Izmir', 'Gida'))
        with_dupe = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
        self.assertEqual(with_dupe['notes_scanned'], 3)
        self.assertEqual(len(with_dupe['findings']), 1, with_dupe['findings'])
        self.assertEqual(with_dupe['findings'][0]['type'], 'near_duplicate')
        self.assertEqual(
            {with_dupe['findings'][0]['older_note'], with_dupe['findings'][0]['newer_note']},
            {'notes/firmalar/kuzey-firin.md', 'notes/firmalar/kuzey-firin-kopya.md'},
        )
        self.assertEqual(with_dupe['template_pairs_skipped'], 2)

    def test_template_siblings_with_different_statuses_do_not_raise_false_contradiction(self):
        """Distinct template cards with different statuses are skipped, while same-entity flips still report."""
        self._write_card(
            'notes/customers/north-bakery.md',
            self._customer_card(
                'North Bakery Ltd', 'North Bakery Ltd', 'Leeds', 'food',
                frontmatter='title: North Bakery Ltd\nupdated_at: 2026-09-01\nstatus: active',
            ),
        )
        self._write_card(
            'notes/customers/harbour-paints.md',
            self._customer_card(
                'Harbour Paints GmbH', 'Harbour Paints GmbH', 'Hamburg', 'coatings',
                frontmatter='title: Harbour Paints GmbH\nupdated_at: 2026-10-01\nstatus: inactive',
            ),
        )
        distinct = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
        self.assertEqual(distinct['findings'], [], distinct['findings'])
        self.assertEqual(distinct['template_pairs_skipped'], 1)

        self._write_card(
            'notes/customers/north-bakery-v2.md',
            self._customer_card(
                'North Bakery Ltd', 'North Bakery Ltd', 'Leeds', 'food',
                frontmatter='title: North Bakery Ltd\nupdated_at: 2026-10-15\nstatus: inactive',
            ),
        )
        same_entity = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
        self.assertEqual(len(same_entity['findings']), 1, same_entity['findings'])
        self.assertEqual(same_entity['findings'][0]['type'], 'status_contradiction')
        self.assertEqual(
            {same_entity['findings'][0]['older_note'], same_entity['findings'][0]['newer_note']},
            {'notes/customers/north-bakery.md', 'notes/customers/north-bakery-v2.md'},
        )

    def test_revised_same_record_card_still_reports_status_contradiction(self):
        """A same-record card whose contact, trade and notes were revised is not a template sibling."""
        old = dict(website='https://northbakery.example', contact='Clara Bennett', phone='+44 113 555 0101',
                   email='clara@northbakery.example', products='Industrial ovens', payment='Net 30',
                   notes='Prefers morning deliveries.')
        new = dict(website='https://northbakery.example', contact='John Carter', phone='+44 113 555 0199',
                   email='john@northbakery.example', products='Commercial dough mixers', payment='Prepaid',
                   notes='Account closed after the October price increase.')
        layouts = {
            # The record name in the title (front matter and H1) and in the Legal name field.
            'titled': (('north-bakery.md', 'North Bakery Ltd', 'title: North Bakery Ltd\n'),
                       ('north-bakery-2026.md', 'North Bakery Ltd', 'title: North Bakery Ltd\n')),
            # No title at all: Obsidian shows the file name, and a duplicated file gets " 2".
            'file_name': (('North Bakery Ltd.md', '', ''), ('North Bakery Ltd 2.md', '', '')),
        }
        for layout, ((old_name, old_h1, old_fm), (new_name, new_h1, new_fm)) in layouts.items():
            with self.subTest(layout=layout):
                folder = self.vault / 'notes' / layout
                self._write_card(f'notes/{layout}/{old_name}', self._customer_card(
                    old_h1, 'North Bakery Ltd', 'Leeds', 'food',
                    frontmatter=f'{old_fm}updated_at: 2026-09-01\nstatus: active', **old))
                self._write_card(f'notes/{layout}/{new_name}', self._customer_card(
                    new_h1, 'North Bakery Ltd', 'Leeds', 'food',
                    frontmatter=f'{new_fm}updated_at: 2026-10-15\nstatus: inactive', **new))
                report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
                self.assertEqual(report['template_pairs_skipped'], 0, report)
                self.assertEqual([f['type'] for f in report['findings']], ['status_contradiction'], report)
                self.assertEqual(report['findings'][0]['conflict']['newer_term'], 'inactive')
                shutil.rmtree(folder)

    def test_fenced_code_key_values_are_not_discounted_as_template_fields(self):
        """YAML/config lines inside fenced code blocks stay substantive content, never template scaffold."""
        self._write_card(
            'knowledge/cfg-a.md',
            "---\ntitle: Config A\n---\n```yaml\nhost: app-one\nport: 8080\nmode: cluster\n"
            "region: eu-west\nstorage: nvme\nworkers: four\n```\n",
        )
        self._write_card(
            'knowledge/cfg-b.md',
            "---\ntitle: Config B\n---\n```yaml\nhost: app-two\nport: 9090\nmode: cluster\n"
            "region: eu-west\nstorage: nvme\nworkers: four\n```\n",
        )
        report = audit_module.audit_memory(self.vault, state=self.state, threshold=0.60, min_shared=5)
        self.assertEqual(len(report['findings']), 1, report)
        self.assertEqual(report['template_pairs_skipped'], 0)


class TemplatePairInvariantTest(unittest.TestCase):
    """#953 as a class: the template rule may only quiet pairs of different records.

    Every case is audited twice, with the rule and with `_template_only_overlap` switched off.
    Two kinds of pair must give the identical report: copies of one record, i.e. notes whose
    titles (file names when untitled) name the same record, whatever share of the values was
    revised and whether the status changed; and pairs whose values are a subset of the other
    note's (same card saved twice, a copy filled in later or given extra notes). Notes with
    fewer than three headings/field labels must too. Only sibling cards for different records
    may disappear, and then they are counted in `template_pairs_skipped`.
    """

    LAYOUTS = {
        'en': (('Company', ('Legal name', 'City', 'Sector', 'Website')),
               ('Contact', ('Contact person', 'Phone', 'E-mail')),
               ('Trade', ('Products bought', 'Payment terms'))),
        'tr': (('Şirket Bilgileri', ('Ünvan', 'Şehir', 'Sektör', 'Web sitesi')),
               ('İletişim', ('İletişim Kişisi', 'Telefon', 'E-posta')),
               ('Ticari Bilgiler', ('Alınan Ürünler', 'Ödeme Koşulları'))),
    }
    RECORDS = {
        'en': ({'Legal name': 'North Bakery Ltd', 'City': 'Leeds', 'Sector': 'food', 'Website': 'northbakery.example',
                'Contact person': 'Clara Bennett', 'Phone': '+44 113 555 0101', 'E-mail': 'clara@northbakery.example',
                'Products bought': 'Industrial ovens', 'Payment terms': 'Net 30'},
               {'Legal name': 'Harbour Paints GmbH', 'City': 'Hamburg', 'Sector': 'coatings',
                'Website': 'harbourpaints.example', 'Contact person': 'Jonas Weber', 'Phone': '+49 40 555 0199',
                'E-mail': 'jonas@harbourpaints.example', 'Products bought': 'Marine primers', 'Payment terms': 'Net 60'}),
        'tr': ({'Ünvan': 'Kuzey Fırın AŞ', 'Şehir': 'İzmir', 'Sektör': 'gıda', 'Web sitesi': 'kuzeyfirin.example',
                'İletişim Kişisi': 'Ayşe Çelik', 'Telefon': '0232 555 0101', 'E-posta': 'ayse@kuzeyfirin.example',
                'Alınan Ürünler': 'Endüstriyel fırın', 'Ödeme Koşulları': 'Otuz gün vadeli'},
               {'Ünvan': 'Liman Boya Ltd', 'Şehir': 'Bursa', 'Sektör': 'kimya', 'Web sitesi': 'limanboya.example',
                'İletişim Kişisi': 'Oğuz Güneş', 'Telefon': '0224 555 0199', 'E-posta': 'oguz@limanboya.example',
                'Alınan Ürünler': 'Gemi astar boyası', 'Ödeme Koşulları': 'Peşin ödeme'}),
    }
    STYLES = {
        'dash': ('##', '- {label}: {value}'),
        'bold': ('##', '**{label}:** {value}'),
        'dash_bold': ('###', '- **{label}**: {value}'),
        'dataview': ('##', '{label}:: {value}'),
    }
    # A later revision of the first record: new values, applied in this order (last revised first).
    REVISIONS = {
        'en': (('Contact person', 'John Carter'), ('Phone', '+44 113 555 0199'),
               ('E-mail', 'john@northbakery.example'), ('Products bought', 'Commercial dough mixers'),
               ('Payment terms', 'Prepaid'), ('Website', 'northbakery-shop.example'), ('Sector', 'catering'),
               ('City', 'York')),
        'tr': (('İletişim Kişisi', 'Oğuz Güneş'), ('Telefon', '0232 555 0199'),
               ('E-posta', 'oguz@kuzeyfirin.example'), ('Alınan Ürünler', 'Hamur yoğurma makinesi'),
               ('Ödeme Koşulları', 'Peşin ödeme'), ('Web sitesi', 'kuzeyfirin-magaza.example'),
               ('Sektör', 'hazır yemek'), ('Şehir', 'Manisa')),
    }
    # Where the record name sits besides the first field: front matter title + H1, H1 only, or
    # nowhere but the file name (Obsidian shows it; a duplicated file gets " 2", a copy "-2026").
    # 'h1_only' leaves the name field blank, so the name is in the H1 and nowhere else.
    NAMINGS = ('title', 'h1', 'h1_only', 'file')
    GENERIC_TITLES = {'en': ('Weekly sync', 'Customer card'), 'tr': ('Haftalık toplantı', 'Müşteri kartı')}
    SPARSE = 3  # the first three fields (name, city, sector) are filled, the rest left blank
    EXTRA = ('ziyaret bakim sozlesmesi yenileme sezon tahmini teslimat saati fatura itirazi satin alma '
             'muduru teklif talebi dolap uretim hatti kapasite planlamasi numune onayi kalite raporu '
             'nakliye sigortasi depo kirasi yillik indirim paketi ihracat belgesi gumruk beyannamesi '
             'odeme hatirlatmasi')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-audit-template-')
        self.addCleanup(self.tmp.cleanup)

    def card(self, lang, style, record, filled, *, notes='', status='', created='2026-10-01', naming='title'):
        heading, line = self.STYLES[style]
        fields = [label for _head, labels in self.LAYOUTS[lang] for label in labels]
        keep = set(fields[:filled])
        name = record[fields[0]] if fields[0] in keep else ''
        if naming == 'h1_only':
            keep.discard(fields[0])
        title = f"title: {name or 'Kart'}\n" if naming == 'title' else ''
        head = f"---\n{title}created: {created}\n" + (f"status: {status}\n" if status else '') + "---\n"
        body = f"# {name or 'Kart'}\n\n" if naming in ('title', 'h1', 'h1_only') else ''
        for section, labels in self.LAYOUTS[lang]:
            body += f"{heading} {section}\n"
            body += ''.join(line.format(label=label, value=record[label] if label in keep else '').rstrip() + '\n'
                            for label in labels) + '\n'
        return head + body + f"{heading} Notes\n{notes}\n"

    def audit_both(self, files):
        root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        vault, state = root / 'vault', root / 'state'
        state.mkdir(parents=True)
        for rel, text in files.items():
            path = vault / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')
        report = audit_module.audit_memory(vault, state=state)
        with mock.patch.object(audit_module, '_template_only_overlap', return_value=False):
            baseline = audit_module.audit_memory(vault, state=state)
        self.last_vault, self.last_state = vault, state
        return report, baseline

    def paths(self, lang, record, other=None):
        """File names carrying the record name: the first card, then its copy (or `other`'s card)."""
        def slug(rec):
            return audit_module.fold(rec[self.LAYOUTS[lang][0][1][0]]).replace(' ', '-')
        copy = f'{slug(other)}.md' if other else (f'{slug(record)}-2026.md' if lang == 'en' else f'{slug(record)} 2.md')
        return f'notes/kart/{slug(record)}.md', f'notes/kart/{copy}'

    def quieted_by_values_alone(self):
        """Would the values test alone (titles ignored) have dropped the last audited pair?"""
        a, b = audit_module.scan_audit_candidates(self.last_vault, self.last_state)
        if not a['template'] or not b['template']:
            return False
        a = {**a, 'template': {**a['template'], 'record_words': frozenset({'a'})}}
        b = {**b, 'template': {**b['template'], 'record_words': frozenset({'b'})}}
        return audit_module._template_only_overlap(a, b, 0.60)

    def assert_unchanged(self, report, baseline, expected_type=None):
        self.assertEqual(report['template_pairs_skipped'], 0, report)
        self.assertEqual(json.dumps(report, sort_keys=True), json.dumps(baseline, sort_keys=True))
        self.assertEqual(len(report['findings']), 1, report)  # the case really is a reported pair
        if expected_type:
            self.assertEqual(report['findings'][0]['type'], expected_type)

    def test_same_record_copies_always_report_as_without_the_rule(self):
        """Copies whose values are a subset of the other's report unchanged, also when nothing in
        the titles ties them together ('file': untitled a.md and b.md, so the values decide)."""
        full = sum(len(labels) for _head, labels in self.LAYOUTS['en'])
        cases = 0
        for lang in self.LAYOUTS:
            record = self.RECORDS[lang][0]
            for style in self.STYLES:
                for naming in ('title', 'file'):
                    for filled in (self.SPARSE, full):
                        def card(filled_, **kw):
                            return self.card(lang, style, record, filled_, naming=naming, **kw)
                        first = card(filled)
                        variants = {
                            'identical': card(filled, created='2026-10-05'),
                            'filled_later': card(full, created='2026-10-05'),
                            'notes_12': card(filled, created='2026-10-05', notes=' '.join(self.EXTRA.split()[:12])),
                            'notes_30': card(filled, created='2026-10-05', notes=self.EXTRA),
                            'filled_later_with_notes': card(full, created='2026-10-05', notes=self.EXTRA),
                        }
                        for variant, second in variants.items():
                            with self.subTest(lang=lang, style=style, naming=naming, filled=filled, variant=variant):
                                self.assert_unchanged(*self.audit_both({'notes/kart/a.md': first,
                                                                        'notes/kart/b.md': second}))
                                cases += 1
                        with self.subTest(lang=lang, style=style, naming=naming, filled=filled, variant='status_flip'):
                            report, baseline = self.audit_both({
                                'notes/kart/a.md': card(filled, status='active'),
                                'notes/kart/b.md': card(filled, status='inactive', created='2026-10-05'),
                            })
                            self.assert_unchanged(report, baseline, 'status_contradiction')
                            cases += 1
        self.assertEqual(cases, 2 * 4 * 2 * 2 * 6)

    def test_revised_same_record_reports_as_without_the_rule(self):
        """A later card for the same record, with 1..all values revised and with or without a
        status flip, under any naming, reports exactly as without the template rule."""
        full = sum(len(labels) for _head, labels in self.LAYOUTS['en'])
        cases = rescued = 0
        for lang in self.LAYOUTS:
            record = self.RECORDS[lang][0]
            first_path, copy_path = self.paths(lang, record)
            for style in self.STYLES:
                for naming in self.NAMINGS:
                    for flip in (False, True):
                        first = self.card(lang, style, record, full, naming=naming,
                                          status='active' if flip else '')
                        for revised in range(1, len(self.REVISIONS[lang]) + 1):
                            later = {**record, **dict(self.REVISIONS[lang][:revised])}
                            second = self.card(lang, style, later, full, naming=naming, created='2026-10-05',
                                               status='inactive' if flip else '')
                            with self.subTest(lang=lang, style=style, naming=naming, flip=flip, revised=revised):
                                report, baseline = self.audit_both({first_path: first, copy_path: second})
                                self.assertEqual(report['template_pairs_skipped'], 0, report)
                                self.assertEqual(json.dumps(report, sort_keys=True),
                                                 json.dumps(baseline, sort_keys=True))
                                if revised == 1:
                                    self.assertEqual(len(baseline['findings']), 1, baseline)
                                if baseline['findings']:
                                    self.assertIn(baseline['findings'][0]['type'],
                                                  {'status_contradiction'} if flip
                                                  else {'near_duplicate', 'substantial_overlap'})
                                    rescued += self.quieted_by_values_alone()
                            cases += 1
        self.assertEqual(cases, 2 * 4 * 4 * 2 * 8)
        # The grid really holds reported revisions that the values test alone would have dropped.
        self.assertGreaterEqual(rescued, 2 * 4 * 4 * 2)

    def test_sibling_cards_for_different_records_are_counted_not_reported(self):
        for lang in self.LAYOUTS:
            ours, theirs = self.RECORDS[lang]
            for style in self.STYLES:
                for naming in self.NAMINGS:
                    ours_path, theirs_path = self.paths(lang, ours, theirs)
                    for filled in (self.SPARSE, 9):
                        with self.subTest(lang=lang, style=style, naming=naming, filled=filled):
                            report, baseline = self.audit_both({
                                ours_path: self.card(lang, style, ours, filled, status='active', naming=naming),
                                theirs_path: self.card(lang, style, theirs, filled, status='inactive',
                                                       created='2026-10-05', naming=naming),
                            })
                            self.assertEqual(report['findings'], [], report)
                            self.assertEqual(report['template_pairs_skipped'], len(baseline['findings']))
                            if filled == self.SPARSE:
                                self.assertEqual(len(baseline['findings']), 1, baseline)  # 3.18.0 reported it

    def test_same_generic_title_reports_as_without_the_rule(self):
        """Different records under one generic title ("Weekly sync") report as without the rule.

        The title gives no sign that they are different records, so the rule stays out, although
        the values alone would have quieted every one of these pairs.
        """
        for lang, generics in self.GENERIC_TITLES.items():
            ours, theirs = self.RECORDS[lang]
            name = self.LAYOUTS[lang][0][1][0]
            for generic in generics:
                for style in self.STYLES:
                    with self.subTest(lang=lang, generic=generic, style=style):
                        # Front matter title and H1 carry the generic title; the name field keeps the record.
                        first = self.card(lang, style, ours, self.SPARSE, status='active').replace(
                            ours[name], generic, 2)
                        second = self.card(lang, style, theirs, self.SPARSE, status='inactive',
                                           created='2026-10-05').replace(theirs[name], generic, 2)
                        report, baseline = self.audit_both({'notes/kart/a.md': first, 'notes/kart/b.md': second})
                        self.assert_unchanged(report, baseline, 'status_contradiction')
                        self.assertTrue(self.quieted_by_values_alone())

    def test_notes_without_three_template_slots_report_as_without_the_rule(self):
        prose = ('Konteyner orkestrasyonu katmaninda servis kesfi, saglik yoklamasi, gunluk dondurme politikasi '
                 've olcekleme esikleri belgelendi. Yuk dengeleyici ayarlari ve sertifika yenileme takvimi eklendi.')
        shapes = {
            'plain': prose,
            'h1': '# Mimari Notu\n\n' + prose,
            'two_headings': '# Mimari Notu\n\n## Ozet\n\n' + prose,
            'heading_and_field': '# Mimari Notu\n\nDurum: tamamlandı\n\n' + prose,
            'two_fields': 'Sonuç: kabul edildi\nKaynak: toplanti\n\n' + prose,
            'colons_in_prose': 'Saat 10:30 toplantisinda https://ornek.example adresi konusuldu.\n\n' + prose,
        }
        for shape, body in shapes.items():
            with self.subTest(shape=shape):
                files = {'notes/prose/a.md': f"---\ntitle: Mimari\ncreated: 2026-10-01\n---\n{body}\n",
                         'notes/prose/b.md': f"---\ntitle: Mimari\ncreated: 2026-10-05\n---\n{body}\n"}
                report, baseline = self.audit_both(files)
                self.assert_unchanged(report, baseline, 'near_duplicate')
                meta, text = audit_module.parse_frontmatter_and_body(files['notes/prose/a.md'])
                self.assertIsNone(audit_module._extract_template_structure(text, 'Mimari', set()))


if __name__ == '__main__':
    unittest.main()
