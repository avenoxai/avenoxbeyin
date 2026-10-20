# Avenoxbeyin'e Katkı Rehberi

Hata bildirimleri, dokümantasyon düzeltmeleri, testler, kod incelemeleri ve yeni fikirler dahil her katkı değerlidir. Bu rehber, **insan katkıcılar ve AI ajanlarıyla çalışanlar** için ortak bir başlangıç noktasıdır.

**Temel yaklaşım:** Yalnızca çalışan kod değil; gerçekten gerekli, anlaşılır, test edilebilir ve uzun vadede bakımı yapılabilir çözümler üretmek.

---

## 1. Başlamadan önce

- [README](README.md) ve değişiklikle ilgili [V3 belgelerine](docs/v3/README.md) göz atın.
- Benzer bir [issue](https://github.com/avenoxai/avenoxbeyin/issues), [PR](https://github.com/avenoxai/avenoxbeyin/pulls) veya mevcut bir çözüm olup olmadığını kontrol edin.
- Yeni bir bağımlılık, büyük mimari değişiklik veya kapsamlı özellikler için kodlamaya başlamadan önce issue üzerinden görüş alışverişi yapın. Paralel ve koordinasyonsuz PR'lar inceleme yükünü katlar.
- Mümkünse **tek PR'da tek konuya** odaklanın. İlgisiz refactor, biçimlendirme ve temizlik değişikliklerini hata düzeltmeleriyle karıştırmayın.
- Küçük belge ve test düzeltmeleri için önceden issue açmak şart değildir.
- **İlişkilendirme sözleşmesi:**
  - Tam çözülen hata ve işler için: `Fixes #N` (`Fixes` satırı merge anında ilgili issue'yu otomatik kapatır).
  - Kısmi çalışma, devam eden araştırma veya ilgili tartışmalar için: `Refs #N`.

---

## 2. Projenin temel ilkeleri

| İlke | Katkıdan beklenti |
| --- | --- |
| **Local-first** | Varsayılan akış tamamen yerel kalmalıdır. Kullanıcı notları ve hassas veriler izinsiz dışarı gönderilmez; telemetri veya zorunlu bulut bağımlılığı eklenemez. |
| **Markdown kaynak** | Kullanıcı notları ve Markdown dosyaları asıl gerçektir. İndeks, önbellek ve türetilmiş görünümler bunların yerini alamaz. |
| **Az bağımlılık** | Temel V3, **Python 3.11+ standart kütüphanesi** (`stdlib`) ile çalışmalıdır. Yeni zorunlu harici paket, hesap, API anahtarı veya arka plan daemon servisi getirilemez. |
| **Veri ve uyumluluk** | Güncelleme, kurulum, geçiş (migration) ve geri alma (rollback) kullanıcı notlarını ve mevcut davranışları korumalıdır. |
| **Gizlilik ve güven** | Özel notlar, gizli klasörler, güvenilmeyen dış içerikler, sistem yolları ve bağlam sınırları sıkı bir şekilde izole edilmelidir. |
| **İstemci tutarlılığı** | Desteklenen AI istemcilerinin (Claude Code, Antigravity, Codex, OpenCode, Kiro vb.) ortak temel davranışları ve kontratları bozulmamalıdır. |

Bir özellik veya değişiklik önerirken şu soruları sorun:
*Mevcut yapıyla daha basit çözülebilir mi? Çekirdekte (core) olması gerekiyor mu, yoksa isteğe bağlı/harici mi olmalı? Uzun vadeli bakım maliyeti nedir?*

Teknik ayrıntılar için: [Runtime Sözleşmesi](docs/v3/RUNTIME.md) · [Markdown Sözleşmesi](docs/v3/MARKDOWN.md) · [Güncelleme ve Rollback](docs/v3/UPDATE.md).

---

## 3. Bakım turları nasıl işliyor?

Avenoxbeyin'de PR'ların nasıl incelendiğini bilmek, katkınızı incelemeye hazırlamanıza yardımcı olur:

- **Gerçek ortamda yeniden üretim:** Her turda açık PR ve issue'lar incelenir; her PR ayrı bir çalışma ağacında (`git worktree`) test edilir. PR'daki iddia güncel `main` üzerinde gerçek kurulum, gerçek veritabanı satırı veya gerçek dosya ile yeniden üretilir; testteki mock/sahte satır tek başına kanıt sayılmaz. Regresyon testinin düzeltme olmadan düştüğü (`RED`) bizzat doğrulanır.
- **Yazarlık ve commit bütünlüğü:** Katkıcı commit'leri yeniden yazılmaz (rebase, squash veya force-push yapılmaz). Gereken küçük düzeltmeler varsa maintainer tarafından dalınıza küçük ve ayrı bir commit olarak eklenir; PR `merge commit` ile ana dala alınır ve yazarlığınız tam korunur. Maintainer commit'i varsa PR yorumunda neyin neden değiştirildiği açıklanır.
- **Birleşik ağaç testi:** Aynı turda birden çok PR varsa hepsi yerelde birleşik bir ağaçta toplanır, çakışmalar çözülür. Tam test paketi Python 3.11 ve Python 3.14 üzerinde yerel olarak koşulur ve `scripts/evaluate_v3.py` çalıştırılır.
- **CI ve yayın ayrımı:** CI; Linux, macOS ve Windows üzerinde koşar. Yeşil CI tek başına merge onayı değildir; `main` dalına merge edilmek de otomatik olarak kararlı sürüm yayınlandığı anlamına gelmez. Sürüm yayını ayrı bir karardır.

---

## 4. Kod ve testler

- Mevcut kod düzenini ve konvansiyonları takip edin; aynı işi yapan ikinci bir modül veya gereksiz soyutlama eklemeyin.
- Hata düzeltirken hatayı kanıtlayan ve düzeltme olmadan düşen bir **regresyon testi** ekleyin.
- V3 test keşfi: Testler repo kökünde `tests/v3_*test.py` deseniyle keşfedilir. Alt süreç (subprocess) açan testlerde ortamı `tests/v3_package_helpers.inherited_env()` ile kurun (bu kural, aktif ajan oturumundaki `BEYIN_V3_SKIP` değişkeninin testi sahte biçimde yeşile boyamasını önler).
- **Geriye dönük uyumluluk ve Rollback kuralı:** Kalıcı ayar, yeni istemci adı ya da kurulum bileşeni ekleyen bir PR'da önceki sürüme rollback senaryosu mutlaka denenmelidir. Eski sürümlerin `validate()` fonksiyonu bilinmeyen anahtarları ve desteklenen aralığın dışındaki değerleri reddettiği için, yeni kalıcı ayarlar `.beyin-preferences.json` yerine `state/` altında ayrı bir dosyada tutulmalıdır.
- Semantik testlerde [sabit sentetik fixture ve holdout beklentilerini](docs/v3/SEMANTIC-TEST-CONTRACT.md) yalnızca puan yükseltmek amacıyla değiştirmeyin. Testlerde gerçek kullanıcı vault'u yerine sentetik veriler ve geçici dizinler kullanın.

### Temel test komutları (repo kökünde):

```bash
# V3 birim ve entegrasyon testleri
python -m unittest discover -s tests -p "v3_*test.py"

# Çevrimdışı geri getirme ve doğruluk değerlendirmesi
python scripts/evaluate_v3.py
```

*Windows ortamında gerekirse `python` yerine `py -3` kullanabilirsiniz.*

Değişiklik alanına göre çalıştırılması gereken ek kontroller:

| Değişiklik alanı | İlgili kontrol |
| --- | --- |
| Windows motor / adaptör | `python tests/scripts_test_windows.py` ve `python tests/antigravity_test.py` |
| POSIX motor / graf | `python3 tests/scripts_test.py` |
| Windows kurulum | `pwsh -NoProfile -File tests/Test-Preflight.ps1` ve ilgili kurulum testleri |
| Paketleme ve güncelleme | [Güncelleme sözleşmesi](docs/v3/UPDATE.md) ve `.github/workflows/release.yml` |

> [!NOTE]
> GitHub Actions bazı testleri yalnızca ilgili dosya yolları değiştiğinde çalıştırır. **Workflow'un tetiklenmemiş olması testin geçtiği anlamına gelmez.** PR açıklamasında gerçekten çalıştırdığınız komutları, ortamı ve sonuçları dürüstçe belirtin; çalıştırmadığınız platformlar için başarı iddiasında bulunmayın.

---

## 5. AI ajanlarıyla geliştirme

AI araçlarının ve kodlama ajanlarının kullanımı serbesttir ve teşvik edilir. Ancak:
- **Sorumluluk katkıcıya aittir:** Gönderilen kodun, testlerin ve dokümantasyonun doğruluğundan, güvenliğinden ve kalitesinden katkıcı sorumludur.
- **Halüsinasyonları ve aşırı mühendisliği temizleyin:** Ajanların ürettiği sahte mock'ları, gereksiz soyutlama katmanlarını, var olmayan sistem çağrılarını ve aşırı geniş `*args, **kwargs` maskelemelerini PR'a taşımayın.
- **Kapsamı dar tutun:** Ajanların dosyanın tamamını gereksizce yeniden biçimlendirmesine veya ilgisiz yerleri refactor etmesine izin vermeyin.

---

## 6. PR gönderme kontrol listesi

PR açmadan önce:
1. Değişiklik dar kapsamlı ve tek bir amaca mı odaklanıyor?
2. Projenin yerel-first ve saf Python stdlib prensiplerine uyuyor mu?
3. Düzeltme için düşen bir regresyon testi eklendi mi (kod değişikliklerinde)?
4. Yerel testler (`v3_*test.py` ve `evaluate_v3.py`) çalıştırıldı mı?
5. PR açıklaması resmi şablonu (`.github/PULL_REQUEST_TEMPLATE.md`) eksiksiz dolduruyor mu?
6. Hassas veri, kişisel dosya yolu, token veya gerçek kullanıcı notu sızıntısı yok mu?

---

## 7. Gizlilik ve güvenlik

Gerçek kişisel notları, özel çalışma alanlarını, sohbet kayıtlarını, dosya yollarını, token veya API anahtarlarını asla issue, PR, test fixture'ı veya loglara eklemeyin. Test ve örneklerde her zaman sentetik veriler kullanın.

Ciddi bir güvenlik açığı bulursanız, ayrıntıları herkese açık bir issue'da paylaşmak yerine, kullanılabilir durumdaysa GitHub'ın özel güvenlik bildirim kanalını veya maintainer ile özel iletişimi tercih edin. Özel bildirim kanalının etkinleştirilmesi maintainer kararıdır.

Bu rehber, [#427](https://github.com/avenoxai/avenoxbeyin/issues/427) içindeki @ersn-dev taslağı ve maintainer açıklamalarından yararlanır. Depo ayarları ve bakım rollerinin paylaşılması gibi diğer öneriler ayrı değerlendirilir; rehberin eklenmesi issue'nun tamamını çözmez.
