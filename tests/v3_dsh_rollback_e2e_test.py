"""Gerçek eski sürüm kurulumu -> aday DSH güncellemesi -> dsh receipt -> rollback -> eski sync zinciri.

PR #560 incelemesinde elle koşulan zincir: eski kurulum, aday paketle güncelleme,
DSH makbuzu, rollback ve eski sürümle sync. Eski test yeni sürümün iki dosyasını silerek eski
kurulumu taklit ediyordu; bu test gerçek eski kodu (RELEASED etiketinin worktree'si) kurar.
Taban hareketli main değil sabit etikettir: bu dal merge olunca main dsh'yi tanır ve eski
doğrulayıcı olmaktan çıkar. Ağdan sürüm
indirilmez: aday paket çalışma ağacındaki dosyalardan, eski sürüm yerel git ref'inden üretilir.

Rollback sonrası iki ayrı makine durumu kanıtlanır: (1) rollback'in kendi state dizini, aday
sürümün yazdığı makbuzu `invalid harness` demeden indeksler; (2) boş, yeni bir state dizini
(ikinci makine) aynı vault'u eski kodla tarar, `succeeded` döner ve makbuzu `manual` olarak
indeksler. Yani dosya biçimi eski doğrulayıcıyla uyumludur; indeks eski makinede de kurulur.

Beklenen kırmızı: aday sürüm `harness: dsh` yazarsa eski sync `invalid harness: dsh` uyarısıyla
degraded döner ve makbuz indekslenmez. Düzeltme indiğinde aynı test yeşile döner.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from v3_package_helpers import inherited_env


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts/build_v3_release.py"
DSH_ADAPTER = ".claude/scripts/beyin_v3_dsh.py"
INVALID_HARNESS = "invalid harness: dsh"
RECEIPT_ID = "dsh-rollback-e2e"


def git(*args, cwd=ROOT):
    return subprocess.run(["git", *list(args)], cwd=str(cwd), text=True, encoding="utf-8",
                          errors="replace", capture_output=True, timeout=180)


# Rollback hedefi: dsh'yi tanımayan son yayın. Sabittir; yeni yayın bunu değiştirmez.
RELEASED = "v3.12.0"


def failures(result):
    return [item.get("reason", "") for item in (result.get("warnings") or []) + (result.get("conflicts") or [])]


def next_patch(value):
    major, minor, patch = (int(part) for part in value.split("."))
    return f"{major}.{minor}.{patch + 1}"


def skip_or_fail(test, reason):
    """Yerelde eksik ref atlanır; CI'da eksik ref kanıt üretmez, açık hata olur."""
    if os.environ.get("CI"):
        test.fail(reason + "; CI tam geçmişi çekmeli (actions/checkout fetch-depth: 0)")
    test.skipTest(reason)


class DshRollbackEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="beyin-dsh-rollback-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.state = self.root / "state"
        self.tmpdir = self.root / "tmp"
        self.tmpdir.mkdir()
        self.base = self.root / "released-tree"
        self.worktree = None
        # HOME/state/temp tamamen geçici: gerçek ~/.dsh, ~/.claude, ~/.codex ve vault'a dokunulmaz.
        self.env = inherited_env(
            HOME=str(self.home), USERPROFILE=str(self.home),
            APPDATA=str(self.home / "appdata"), LOCALAPPDATA=str(self.home / "localappdata"),
            TMPDIR=str(self.tmpdir), TEMP=str(self.tmpdir), TMP=str(self.tmpdir),
            PYTHONDONTWRITEBYTECODE="1", BEYIN_V3_NO_SPAWN="1", BEYIN_UPDATES_OFF="1",
        )
        self._checkout_released()

    def _checkout_released(self):
        if shutil.which("git") is None:
            skip_or_fail(self, "git yok; gerçek main worktree'si kurulamaz")
        if git("rev-parse", "--git-dir").returncode:
            skip_or_fail(self, "çalışma dizini git deposu değil")
        # Sabit yayın etiketi: hareketli main, bu dal merge olunca dsh'yi tanır ve eski doğrulayıcı olmaktan çıkar.
        base_ref = RELEASED
        if git("rev-parse", "--verify", "--quiet", base_ref + "^{commit}").returncode:
            skip_or_fail(self, base_ref + " etiketi bu klonda yok")
        added = git("worktree", "add", "--detach", str(self.base), base_ref)
        if added.returncode:
            skip_or_fail(self, "git worktree add başarısız: " + added.stderr.strip()[-300:])
        self.worktree = self.base
        self.addCleanup(self._remove_worktree)

    def _remove_worktree(self):
        if self.worktree is None or not self.worktree.exists():
            return
        git("worktree", "remove", "--force", str(self.worktree))
        git("worktree", "prune")

    def run_python(self, script, *args, cwd=None, expect=0):
        result = subprocess.run([sys.executable, str(script), *map(str, args)],
                                cwd=str(cwd or self.root), env=self.env, text=True,
                                encoding="utf-8", capture_output=True, timeout=300)
        if expect is not None:
            self.assertEqual(result.returncode, expect,
                             f"{Path(script).name} {' '.join(map(str, args))} çıkışı {result.returncode}: "
                             + (result.stderr.strip()[-1500:] or result.stdout.strip()[-1500:]))
        return result

    def cli(self, *args):
        return json.loads(self.run_python(self.vault / "beyin.py", *args).stdout)

    def harnesses(self, script):
        """Kurulu sürümün kabul ettiği harness listesi: rollback'in gerçekten eski koda döndüğünün kanıtı."""
        text = Path(script).read_text(encoding="utf-8")
        match = re.search(r"^HARNESSES = \((.*?)\)", text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, f"{script} içinde HARNESSES bulunamadı")
        return [part.strip().strip("\"'") for part in match.group(1).split(",") if part.strip()]

    def test_main_install_update_dsh_receipt_rollback_then_old_sync(self):
        # 1. Eski sürüm: RELEASED etiketinin worktree'sinden gerçek kurulum.
        installed = json.loads(self.run_python(self.base / "scripts/install_v3.py",
                                               "--vault", self.vault, "--state", self.state,
                                               cwd=self.base).stdout)
        self.assertEqual(installed.get("status"), "installed", "eski sürüm kurulumu tamamlanmadı: " + json.dumps(installed))
        old_version = (self.vault / ".beyin-version").read_text(encoding="utf-8").strip()
        self.assertRegex(old_version, r"^\d+\.\d+\.\d+$", "kurulum sürüm damgası okunamadı")
        self.assertNotIn("dsh", self.harnesses(self.vault / ".claude/scripts/beyin_v3.py"),
                         "kurulan eski sürüm dsh harness'ını zaten tanıyor; test eski kodu kurmuyor")
        self.assertFalse((self.vault / DSH_ADAPTER).exists(), "eski kurulumda dsh adaptörü olmamalı")

        # 2. Aday: bu çalışma ağacının (değişiklikler dahil) paketiyle yerel güncelleme.
        candidate_version = next_patch(old_version)
        package = self.root / f"beyin-v3-{candidate_version}.zip"
        built = json.loads(self.run_python(BUILDER, "--output", package, "--version", candidate_version).stdout)
        self.assertEqual(built.get("status"), "built", "aday paket üretilemedi: " + json.dumps(built))
        with zipfile.ZipFile(package) as archive:
            for name in ("scripts/beyin_v3.py", "template/.claude/scripts/beyin_v3.py",
                         "template/.claude/scripts/beyin_v3_update.py"):
                self.assertEqual(archive.read(name), (ROOT / name).read_bytes(),
                                 f"{name} pakete çalışma ağacındaki haliyle girmeli")

        updated = self.cli("update", "--package", package, "--json")
        self.assertEqual(updated.get("status"), "updated", "aday paket uygulanamadı: " + json.dumps(updated))
        self.assertEqual(updated.get("from_version"), old_version)
        self.assertEqual(updated.get("version"), candidate_version)
        self.assertEqual((self.vault / ".beyin-version").read_text(encoding="utf-8").strip(), candidate_version)
        self.assertIn("dsh", self.harnesses(self.vault / ".claude/scripts/beyin_v3.py"),
                      "aday sürüm dsh harness'ını tanımalı")
        self.assertTrue((self.vault / DSH_ADAPTER).is_file(), "aday sürüm dsh adaptörünü kurmalı")

        # 3. Aday kurulumda CLI ile dsh makbuzu.
        note = self.vault / "notes" / "rollback-e2e.md"
        note.parent.mkdir(exist_ok=True)
        note.write_text("# rollback e2e\n\nDSH rollback zinciri.\n", encoding="utf-8")
        receipt = self.cli("receipt", "--harness", "dsh", "--event-id", RECEIPT_ID,
                           "--summary", "DSH rollback e2e", "--ref", "notes/rollback-e2e.md",
                           "--session", RECEIPT_ID, "--json")
        self.assertEqual(receipt.get("status"), "succeeded", "dsh makbuzu yazılamadı: " + json.dumps(receipt))
        receipt_path = self.vault / receipt["source"]
        self.assertTrue(receipt_path.is_file(), "makbuz dosyası oluşmadı")
        # Aday sürüm dsh'ı eski doğrulayıcıyla uyumlu yazabilir (harness: manual + client: dsh);
        # önemli olan makbuzun dsh istemcisine atfedilmesi, alanın adı değil.
        self.assertIn('"dsh"', receipt_path.read_text(encoding="utf-8"), "makbuz dsh istemcisine atfedilmiyor")

        # 4. Gerçek rollback: eski sürüme dönüş, makbuz dosyası korunur.
        rolled = self.cli("rollback", "--json")
        self.assertEqual(rolled.get("status"), "rolled_back", "rollback tamamlanmadı: " + json.dumps(rolled))
        self.assertEqual(rolled.get("version"), old_version)
        self.assertEqual((self.vault / ".beyin-version").read_text(encoding="utf-8").strip(), old_version)
        self.assertNotIn("dsh", self.harnesses(self.vault / ".claude/scripts/beyin_v3.py"),
                         "rollback eski doğrulayıcıya dönmedi")
        self.assertFalse((self.vault / DSH_ADAPTER).exists(), "rollback aday adaptörünü kaldırmadı")
        self.assertTrue(receipt_path.is_file(), "rollback makbuz dosyasını silmemeli")

        # 5. Eski kodla sync: succeeded olmalı, invalid harness olmamalı, makbuz indekslenmeli.
        synced = json.loads(self.run_python(self.vault / "beyin.py", "sync", "--json").stdout)
        invalid = [reason for reason in failures(synced) if INVALID_HARNESS in reason]
        if invalid or synced.get("status") != "succeeded":
            self.fail(
                "RED: eski sürüm rollback sonrası dsh makbuzunu indeksleyemedi. "
                f"status={synced.get('status')!r}, invalid_harness={bool(invalid)}, "
                f"warnings={json.dumps(synced.get('warnings'), ensure_ascii=False)}, "
                f"conflicts={json.dumps(synced.get('conflicts'), ensure_ascii=False)}. "
                "Beklenen: status 'succeeded', 'invalid harness: dsh' yok ve makbuz state indeksinde."
            )

        database = (self.state / "memory.sqlite3").resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(database, uri=True)
        try:
            row = connection.execute("SELECT payload FROM receipts WHERE id=?", (RECEIPT_ID,)).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row, "eski sürüm sync sonrası dsh makbuzu state indeksinde yok")
        self.assertEqual(json.loads(row[0]).get("event_id"), RECEIPT_ID)

        # 6. İkinci makine: boş state dizini aynı vault'u eski kodla tarar. Aynı-state adımı
        #    rollback'in devraldığı indeksi kanıtlar; bu adım eski kodun dosyayı sıfırdan
        #    okuyup `manual` olarak indekslediğini kanıtlar (satır aday derlemeden gelmiyor).
        second_state = self.root / "second-machine-state"
        cold = json.loads(self.run_python(self.vault / ".claude/scripts/beyin_v3_cli.py",
                                          "--vault", self.vault, "--state", second_state, "--json",
                                          "sync").stdout)
        cold_failures = failures(cold)
        self.assertEqual(cold.get("status"), "succeeded",
                         "ikinci makine sync'i temiz değil: " + json.dumps(cold, ensure_ascii=False))
        self.assertFalse([reason for reason in cold_failures if INVALID_HARNESS in reason], cold_failures)
        self.assertTrue(receipt_path.is_file(), "rollback makbuz dosyasını silmemeli")
        cold_database = (second_state / "memory.sqlite3").resolve().as_uri() + "?mode=ro"
        cold_connection = sqlite3.connect(cold_database, uri=True)
        try:
            cold_row = cold_connection.execute("SELECT payload FROM receipts WHERE id=?", (RECEIPT_ID,)).fetchone()
        finally:
            cold_connection.close()
        self.assertIsNotNone(cold_row, "ikinci makinenin eski sync'i dsh makbuzunu indekslemedi")
        self.assertEqual(json.loads(cold_row[0]).get("harness"), "manual",
                         "eski doğrulayıcı makbuzu manual olarak indekslemeli")

        # Mümkünse doctor: eski kod da dsh makbuzuna 'invalid harness' dememeli.
        doctor = self.run_python(self.vault / "beyin.py", "doctor", "--json", expect=None)
        self.assertNotIn(INVALID_HARNESS, doctor.stdout + doctor.stderr,
                         "doctor dsh makbuzunu geçersiz sayıyor")
