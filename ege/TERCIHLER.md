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
  - Kimlik: `origin` remote'undaki **`sahip/repo`** (iki farklı sahipte aynı adlı repo karışmasın);
    görünen proje adı ve `30 Projeler/<ad>` klasörü = repo adı. Remote yoksa klasör adı.
  - **Bundan sonra her proje GitHub (veya şirket projesi ise şirketin git sunucusu) remote'u ile
    çalışılır**; makine değiştirirken commit + push, başlarken pull.
  - Upstream kayıtlarında proje alanı **yok** (oturum→proje bağı yalnız o makinenin yerel
    veritabanında). Bu yüzden proje bağı vault'a yazılır: kayıt özetinin ilk satırı `Proje: <ad>`
    ve kayıt o işin oturum kartına (`30 Projeler/<ad>/Oturumlar/YYYY-AA-GG-SSDD-<makine>.md`)
    bağlanır. Her proje klasöründe `Proje.md` (repo bağı: `proje_id: sahip/repo`), `Oturumlar/`
    (iş başına yeni kart dosyası), `Kararlar.md`, `Kurallar.md`. Tek bir `Durum.md` yerine kart
    kullanılır, çünkü her makine yalnız yeni dosya eklediğinde Obsidian Sync'te çakışma olmaz.
- ✅ (2026-11-16) **Kapanışta otomatik commit+push (wip dalı), açılışta pull — git-devir hook'u.**
  Ayrıntı: [hooks/README.md](hooks/README.md). (Önceki "yalnız uyarı, otomatik push yok" kararının
  yerine geçer.)
  - Commit edilmemiş iş: `wip/<makine>/<dal>` dalına anlık kopya; kullanıcının dalına, index'ine ve
    çalışma ağacına dokunulmaz.
  - Commit edilmiş iş: **kişisel repolarda `main` dahil otomatik push** (asla `--force`).
    Şirket/diğer remote'larda yalnız uyarı.
  - Açılışta temiz ve yalnız gerideyse otomatik ileri sarma; başka makinedeki yarım iş yalnız
    haber verilir, onaysız alınmaz.
  - Makine etiketleri: `is-5090` (şirket PC'si), `ev` (ev PC'si), `macbookneo` (laptop).
- ✅ (2026-11-16) **Yeni repo, vault'ta klasörü yok:** ajan `30 Projeler/<ad>` klasörünü açmadan önce sorar.
- ✅ (2026-11-16) **Proje adı geçmeyen soru:** ajan tahmin etmez, hangi projeyi kastettiğini sorar.
- ✅ (2026-11-16) **Projeye özel `Kararlar.md`:** her proje klasöründe; karar başına tarih · karar ·
  neden · alternatifler. O projede oturum açılınca son birkaç karar otomatik gelir.
  (Upstream'de kararlar yalnız oturum kartındaki `**Karar:**` satırında doğar; kart arşivlenince
  `Kararlar.md`'ye kopyalanır ama açılışta artık gelmez.)
- ✅ (2026-11-16) **Açılış bağlam bütçesi 5.000 karakter** (avenox varsayılanı, değişmiyor).
- ✅ (2026-11-16) **Kayıt şablonu:** sabit 4 satır (Yapılan / Doğrulama / Açık / Öğrenilen), en fazla
  ~600 karakter; başında `Proje: <ad>` satırı.
- ✅ (2026-11-16) **Editör makine = ev PC'si (`ev`):** haftalık gözden geçirme ve yapısal düzenlemeler
  yalnız orada yapılır.
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
  - ✅ (2026-11-16) **Odak alanları — genel anlamda AI Engineering:** LLM'ler · Agentic AI
    geliştirme · sistem mühendisliği ve tasarımı · quantitative engineering ve analiz · digital
    pathology AI engineering · recommendation systems · kurucu bakışı (Türkiye'de ürün satan bir
    şirketin sahibi olarak düşünmek). **Eski tarz MLOps konuları girmez.** Sıklık: haftalık.
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
- Akıllı priz adayı: güç ölçümlü, betik ve webhook destekli bir model (Shelly Plug S MTR Gen3;
  betik için Matter kapalı). Hedef otomasyon: "güç birkaç dakika eşiğin altındaysa kendini kapat",
  zamanlı açılış, ajanın prizi sorgulayabilmesi.
- ✅ (2026-11-16) Mail: ajan **yalnız taslak** oluşturur, asla göndermez. İki hesap: kişisel Gmail
  ve üniversite hesabı (adresler bu depoya yazılmaz; yalnız makinenin yerel ayarında durur).
- CV: vault'ta "Kariyer" ana notu, iş kayıtlarından beslenir; PDF/Word `40 İçerik/CV/`.
  Varsayılan: Türkçe + İngilizce, bir ana CV ve ilana özel sürüm (⏳ açıkça onaylanmadı).
  Upstream'in gelecek "workspace homepages" özelliği (Kariyer örneği) bunun için uygun.

## Açık sorular ❓

1. Repo kökü: her makinede `~/Kod` mı? Şirket PC'sinde repolar Desktop'ta kalsın mı?
2. 2×4090 makinesi: işletim sistemi, Claude hesabı, Codex var mı?
3. Ev PC'si: yalnız şahsi Claude mı, Codex de mi?
4. Ev PC'sinin BIOS'unda "AC gelince aç" ayarı var mı (yoksa Wake-on-LAN)?
5. Yakala: en çok ne yakalanıyor (link, makale, PDF, ekran görüntüsü, ses)?
6. CV varsayılanı (TR + EN, ana CV + ilana özel) uygun mu?
7. `70 Kişisel` aramaya girsin mi (girmesin ise adı `Özel` içeren bir ada çevrilir)?
8. Oturum kartı: Stop'ta zorunlu mu, kayda mı gömülsün?
9. Açılması önerilen `preferences` ayarları onaylı mı?
10. Şirket işlerinin özetlerinin kişisel vault'a gitmesi şirket politikasına uygun mu?
