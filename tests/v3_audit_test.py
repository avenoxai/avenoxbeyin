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

    def test_asymmetric_size_notes_do_not_produce_false_duplicate_or_body_conflict(self):
        """Asymmetric pair (large document vs tiny note) must not be flagged as near-duplicate

        or status contradiction from stray prose words (#466).
        """
        # Note A: Large document with 800 words, including passing mention of "tamamlandi"
        words = [
            f"kavram_{i} mimari servis yapilandirma sistem altyapi parametre analiz model veri akis test"
            for i in range(50)
        ]
        large_body = " ".join(words) + " Proje asamasi tamamlandi ve arsivlendi."
        (self.knowledge / 'buyuk-not.md').write_text(
            f"---\ntitle: Buyuk Sistem Raporu\nupdated_at: 2026-08-01\n---\n# Sistem\n{large_body}\n",
            encoding='utf-8'
        )

        # Note B: Short scratch note of 15 words whose vocabulary is contained in Note A,
        # but with stray word "bekliyor". Jaccard is low, vocab ratio is > 10.
        short_body = "servis yapilandirma altyapi test akis dogrulama bekliyor."
        (self.knowledge / 'kucuk-not.md').write_text(
            f"---\ntitle: Kisa Not\nupdated_at: 2026-10-01\n---\n# Kisa\n{short_body}\n",
            encoding='utf-8'
        )

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

if __name__ == '__main__':
    unittest.main()
