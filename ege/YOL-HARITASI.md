# Yol haritası: avenox'a eklenecekler

Her madde fork'ta `ege/` altında geliştirilir; upstream dosyalarına dokunmadan, ayrı skill/hook/ajan
olarak kurulur. Durum: ⬜ başlanmadı · 🟡 sürüyor · ✅ bitti

## 1. Temel (şirket 5090)

- ⬜ Global köprü (Claude) + `project-context on` + önerilen `preferences` ayarları.
- ⬜ `beyin-yazici` alt ajanı (Sonnet 5.5, medium): receipt, oturum kartı ve not yazımını ana modelden
  devralır. Global `CLAUDE.md` kuralı: "vault yazımını beyin-yazici'ye ver".
- ⬜ `_Sistem/Ajan/` ana kopyalar + makineye kopyalayan tek komut.
- ⬜ `scheduled-check` Görev Zamanlayıcı kaydı.

## 2. Süreklilik

- ⬜ **[İLK İŞ] Proje kimliği = repo adı / git remote**: aynı repo farklı makinelerde tek proje
  sayılsın (upstream `project_id` = klasör yolu hash'i; proje adı ise repo klasör adının vault'taki
  `30 Projeler/<ad>` klasörüyle veya `.beyin-projects.json` ile eşleşmesinden çözülüyor).
- ⬜ **Proje bazında "kaldığım yer"**: oturum başında açılan repoya ait son kart/kayıt.
- ⬜ **Köprüye git uyarıları**: SessionStart'ta `git fetch` + "gerisindesin, pull"; Stop/SessionEnd'de
  "push edilmemiş commit". Ağ yavaşsa hook süresini aşmamak için zaman sınırlı.
- ⬜ **Projeye özel `Kararlar.md`** (tarih · karar · neden · alternatifler) ve açılışta son kararlar.
- ⬜ **Oturum kartı zorunluluğu**: Stop'ta kart kontrolü (ayrı hook) veya kartı receipt'e gömme.
- ⬜ Makine etiketi: her kayıtta `makine: <ad>`.

## 3. Haftalık gözden geçirme skill'i

- ⬜ `triage` + `recap` + Gelen dağıtımı (`00 Gelen` → proje / Öğrendiklerim / Fikirler / görev),
  işlenen günlükleri `60 Günlük/`'e taşıma, gelecek haftanın görevleri.
- ⬜ Ayda bir `audit` + `beyin-kontrol`.
- 🟡 **Skill önerici**: kayıtlarda ve kararlarda tekrar eden iş akışlarını bulur, skill taslağı
  önerir; onayla oluşturulur (upstream'de yok). Skill yazıldı
  ([skills/beyin-skill-onerici](skills/beyin-skill-onerici/SKILL.md)), henüz hiçbir makineye kurulmadı.
- ⬜ Yalnız editör makinede çalışır.

## 4. Öğrenme koçu ("Öğrenmem gerekenler")

- ⬜ Her kayıt/günlükte aday: "Geleceğin AI mühendisi olarak buradan ne öğrenmeli?" (`learn` ile biriktirme).
- ⬜ Haftalık öğretici notlar `50 Öğrendiklerim/`: Ne · Neden önemli · Basit anlatım · Projemden
  örnek · Kendini test et · Kaynak ve sonraki adım.
- ⬜ **AI Mühendisi Yol Haritası** notu: bilinen / eksik konular, haftalık güncelleme.

## 5. Telefon ve uzaktan erişim

- ⬜ Ev PC'si: Remote Control, açılış zinciri, akıllı priz/WOL, güvenli kapatma komutu.
- ⬜ Mail taslağı skill'i (yalnız taslak).
- ⬜ CV skill'i: "Kariyer" ana notundan ilana göre CV (upstream workspace homepages çıkınca onunla).
- ⬜ Yakala kurulumu ve telefon kısayolu (gereksinimler konuşulacak).

## 6. Çoklu makine

- ⬜ Ev PC'si, MacBook Neo (Claude + Codex köprüsü, Codex devri), şirket VM, 4090.
- ⬜ `orkestra` skill'inin Claude-only ve Claude+Codex sürümleri; Codex'e devir notu biçimi.

## Upstream takibi

- Bir sonraki sürümde beklenenler: workspace homepages (Kariyer/proje sayfaları), oturum başında
  gözden geçirme hatırlatması, Windows'ta konsolsuz arka plan işçisi, doctor yedek raporu.
- Release çıkınca: vault'ta `beyin.py update`, fork'ta `git merge upstream/main`.
