# Tercihler ve kararlar

Durum işaretleri: ✅ karar verildi · ⏳ önerildi, onay bekliyor · ❓ açık soru

## Genel

- ✅ Eski kişisel vault sistemi kaldırıldı (yedekli); kurallar **sıfırdan**, ayrıntılı sorularla kurulur.
- ✅ Avenox reposu vault'un dışında durur.
- ✅ Ana ajan: Claude Code (Max). Codex (Plus) yalnız Mac ve uzak makinede ikincil.
- ✅ Şirket bilgisayarında **yalnız Claude** kullanılır.
- ✅ Şirket (Enterprise) oturumlarının kayıtları normal görünürlükte (`internal`) kalır.

## Notlar, günlük, görevler

- ✅ Telefondan yakalama: Obsidian'a yaz, Sync taşısın.
- ✅ Günlük not = günlük + ham yakalama: `00 Gelen/YYYY-MM-DD.md`.
  Şablon `_Sistem/Şablonlar/Günlük`: **Öğrendiklerim / Fikirler / Yapılacaklar**.
- ✅ Gelen yalnız **haftalık gözden geçirmede** dağıtılır; işlenen günlükler `60 Günlük/`'e gider.
- ✅ Görevler avenox `tasks/` klasöründe, her görev bir dosya.
- ✅ Avenox'un varsayılan `📋 Templates` klasörü korunur.

## Kurallar ve kayıt

- ✅ Kural düzeltmesi: **hemen yaz, haber ver**.
- ✅ (2026-11-16) **İş kaydı özetini Sonnet 5.5, medium effort yazar.** Kayıt/kart/not yazımı
  `beyin-yazici` alt ajanına verilir; ana model (Opus) yalnız ham bilgiyi (ne yapıldı, değişen
  dosyalar, test, açık adım) iletir. Önceki "Haiku" kararının yerine geçer.
  Avenox'ta özeti varsayılan olarak oturumdaki ana model yazar; Stop hook'u yalnız yazdırır.
- ⏳ Oturum kartının da zorunlu tutulması (Stop'ta kart kontrolü) veya kartın kayda gömülmesi.
- ✅ (2026-11-16) **Proje kimliği = repo adı / git remote** (fork'un ilk düzeltmesi). "Kaldığım yer"
  proje bazında ve makineler arasında ortak olur. Upstream'de kayıtların `project_id`'si klasör
  yolunun hash'i; Windows ve Mac'te aynı repo iki ayrı proje sayılıyor.
- ✅ (2026-11-16) **Köprüye git uyarıları:** oturum açılınca "repo uzak dalın gerisinde, önce pull";
  oturum kapanırken "push edilmemiş commit var". Otomatik commit/push yok.
- ✅ (2026-11-16) **Projeye özel `Kararlar.md`:** her proje klasöründe; karar başına tarih · karar ·
  neden · alternatifler. O projede oturum açılınca son birkaç karar otomatik gelir.
  (Upstream'de kararlar yalnız oturum kartındaki `**Karar:**` satırında doğar; kart arşivlenince
  `Kararlar.md`'ye kopyalanır ama açılışta artık gelmez.)
- ✅ (2026-11-16) **Açılış bağlam bütçesi 5.000 karakter** (avenox varsayılanı, değişmiyor).
- ⏳ Kayıt şablonu: sabit 4 satır (Yapılan / Doğrulama / Açık / Öğrenilen), en fazla ~600 karakter.
- ✅ (2026-11-16) **Skill önerici:** haftalık gözden geçirmede tekrar eden iş akışlarını ve kararları tespit edip
  skill taslağı önermek; onaylanırsa oluşturmak. Upstream'de böyle bir özellik yok.
  Skill: [skills/beyin-skill-onerici/SKILL.md](skills/beyin-skill-onerici/SKILL.md).

## Avenox'tan seçilen ek özellikler

- ✅ **triage**: haftalık gözden geçirmenin motoru.
- ✅ **recap**: "ne yaptım" soruları.
- ✅ **audit**: ayda bir; `beyin-kontrol` ile birlikte.
- ✅ **learn → "Öğrenmem gerekenler"** (genişletilecek):
  - Her iş kaydı ve günlük notta ajan düşünür: *"Geleceğin AI mühendisi olarak buradan ne öğrenmeli?"*
  - Adaylar birikir; haftada bir `50 Öğrendiklerim/` altına **öğretici not**:
    Ne · Neden önemli · Basit anlatım · Kendi projemden örnek · Kendini test et (2–3 soru) · Kaynak ve sonraki adım.
  - **AI Mühendisi Yol Haritası** notu: bildiğim / eksik konular, haftalık güncellenir.
- ✅ **Yakala**: alınacak; nasıl çalışacağı konuşulacak.
- ✅ **scheduled-check**: alınacak; önerilen: ev PC'si açılışında + şirket PC'sinde günde bir, `--notify`.

## Açılması önerilen ayarlar (⏳ onay bekliyor)

| Ayar | Neden |
|---|---|
| `preferences --project-context on` | Repoda o projenin son kaydı ve görevleri |
| `preferences --secret-filter on` | Kayıtlara anahtar/şifre sızmasın |
| `preferences --inbox-report on --inbox-folder "00 Gelen"` | Gelen birikimini haftalık gözden geçirmeye taşır |
| `preferences --context-dedupe on` | Aynı oturumda tekrar bağlam vermez (token) |
| `scheduled-check --notify` (Görev Zamanlayıcı) | Günlük indeks + sağlık |
| `--parallel-sessions on` | Ev PC'si ve telefon kurulunca |
| `mutation-queue` | Bağlantısı kopan 4090 için ileride |

Alınmayacaklar: `gecmis-import` (V2'ye ait, V3'te çalışmaz), eski `template/.claude/hooks`
(V2), `word-cap-warning`, `promotion`, `web`, diğer istemci adaptörleri.

## Kurulum tercihleri

- ✅ Kurulum her makinede resmi release ZIP'inden, checksum doğrulamalı, **aynı sürüm**.
- ✅ Hariç tutulan bileşenler: `adapters`, `harnesses/antigravity`.
- ✅ Sync'te hariç: `daily/v3`, `knowledge/v3` (her cihazda; headless'ta `ob sync-config --excluded-folders`).
- ✅ Önce ana makine kurulur (companion dosyaları oluşsun), diğerleri Sync bitince kurulur.
- ✅ Vault'ta uçtan uca şifreleme var; headless kurulumda şifre **elle** girilir, komuta yazılmaz.

## Telefon ve uzaktan erişim (⏳ önerilen kurgu)

- Ev PC'si (5060 Ti) **beyin sunucusu**: vault klasöründe `claude remote-control` çalışır,
  telefondaki Claude uygulamasından bağlanılır. VPS gerekmez; port açılmaz.
- Açma: **akıllı priz + BIOS "AC gelince aç"** (öneri) veya Wake-on-LAN; kapatma ajana
  "kapat" diyerek, prizden değil.
- Mail: ajan **yalnız taslak** oluşturur, asla göndermez.
- CV: vault'ta "Kariyer" ana notu, iş kayıtlarından beslenir; PDF/Word `40 İçerik/CV/`.
  Upstream'in gelecek "workspace homepages" özelliği (Kariyer örneği) bunun için uygun.

## Açık sorular ❓

1. Repo kökü: her makinede `~/Kod` mı? Şirket PC'sinde repolar Desktop'ta kalsın mı?
2. Haftalık gözden geçirmeyi yapan **editör makine**: ev PC'si mi, şirket 5090 mı?
3. 2×4090 makinesi: işletim sistemi, Claude hesabı, Codex var mı?
4. Ev PC'si: yalnız şahsi Claude mı, Codex de mi?
5. Açma yöntemi: akıllı priz var mı, modem WOL destekliyor mu, sürekli açık mı?
6. Mail hesabı: kişisel mi şirket mi? "Yalnız taslak" kuralı onaylı mı?
7. Öğrenme notları: odak alanları (LLM ajanları, MLOps, model eğitimi, sistem tasarımı…) ve sıklık.
8. Yakala: en çok ne yakalanıyor (link, makale, PDF, ekran görüntüsü, ses)?
9. CV: tek ana CV mi, ilana özel mi; Türkçe / İngilizce?
10. `70 Kişisel` aramaya girsin mi (girmesin ise adı `Özel` içeren bir ada çevrilir)?
11. Oturum kartı: Stop'ta zorunlu mu, kayda mı gömülsün?
