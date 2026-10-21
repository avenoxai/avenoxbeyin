# Avenoxbeyin'e Katkı Rehberi

Hata bildirimleri, dokümantasyon düzeltmeleri, testler, kod incelemeleri ve yeni fikirler dahil her katkı değerlidir. Bu rehber, insan katkıcılar ve AI ajanlarıyla çalışanlar için ortak bir başlangıç noktasıdır.

**Temel yaklaşım:** Yalnızca çalışan kod değil; gerçekten gerekli, anlaşılır, doğrulanabilir ve uzun vadede bakımı yapılabilir çözümler üretmek.

## 1. Başlamadan önce

- [README](README.md) ve ilgili [V3 belgelerini](docs/v3/README.md) inceleyin; benzer [issue](https://github.com/avenoxai/avenoxbeyin/issues), [PR](https://github.com/avenoxai/avenoxbeyin/pulls) veya mevcut çözüm olup olmadığını kontrol edin.
- Hata düzeltmesinde sorunun nasıl tekrarlandığını, özellik önerisinde hangi ihtiyacın karşılandığını açıklayın.
- Yeni bağımlılık, büyük mimari değişiklik ve kapsamlı özellikler için **önce issue üzerinde görüş alışverişi yapın**. Aynı konuda koordinasyonsuz paralel PR'lardan kaçının.
- **Tek PR, tek konu:** İlgisiz refactor veya biçimlendirme değişikliklerini aynı PR'a karıştırmayın. Küçük belge ve test düzeltmeleri için önceden issue açmak şart değildir. Büyük işi bağımlılıkları belirtilmiş, kendi başına doğrulanabilen küçük PR'lara bölün; birbirini bekleyen PR'ları ve çakışan paralel çalışmaları ilgili issue'da ilişkilendirin.

## 2. Projenin temel ilkeleri

| İlke | Beklenti |
| --- | --- |
| **Local-first** | Varsayılan akış yerel kalmalı; kullanıcı notları ve hassas veriler izinsiz dışarı gönderilmemeli. |
| **Markdown kaynak** | Kullanıcı notları asıl kaynaktır; indeks ve önbellek bunların yerini almamalı. |
| **Az bağımlılık** | Temel V3, **Python 3.11+ standart kütüphanesi** ile çalışmalı; yeni zorunlu paket, hesap, API anahtarı veya arka plan servisi eklenmemeli. |
| **Veri ve uyumluluk** | Kurulum, güncelleme, migration ve rollback kullanıcı verilerini ve mevcut davranışları korumalı. |
| **Gizlilik ve güven** | Özel notlar, güvenilmeyen içerikler, dosya yolları ve bağlam sınırları korunmalı. |
| **İstemci tutarlılığı** | Desteklenen AI istemcilerinin ortak temel davranışları bozulmamalı. |

Bir değişiklik önermeden önce sorun: **Mevcut yapıyla daha basit çözülebilir mi? Çekirdekte mi olmalı, isteğe bağlı mı? Uzun vadeli bakım maliyeti nedir?**

Teknik sözleşmeler: [Runtime](docs/v3/RUNTIME.md) · [Markdown](docs/v3/MARKDOWN.md) · [Güncelleme](docs/v3/UPDATE.md).

## 3. Kod, test ve uyumluluk

- Mevcut kod düzenini izleyin; tekrarlanan modül, gereksiz soyutlama veya bağımlılık eklemeyin.
- Refactor öncesinde korunacak davranışı ve kullanım noktalarını belirleyin. Kullanıcıya görünen davranış değişiyorsa testleri ve belgeleri güncelleyin.
- Hata iddiasını sürüm/commit (`main@<sha>`), yeniden üretim adımları ve **gerçek çıktı** ile destekleyin. Statik analiz veya AI ajanı bulgusu tek başına doğrulama değildir.
- Davranış düzeltmelerinde, düzeltme olmadan başarısız (`RED`), düzeltmeden sonra başarılı olan regresyon testi beklenir. Teknik olarak mümkün değilse nedenini ve alternatif doğrulamayı PR'da açıklayın; istisnanın kabulü maintainer değerlendirmesindedir.
- Semantik testlerde [sabit fixture ve holdout beklentilerini](docs/v3/SEMANTIC-TEST-CONTRACT.md) yalnızca puanı yükseltmek için değiştirmeyin. Gerçek kullanıcı vault'u yerine sentetik veriler ve geçici dizinler kullanın.

### Değişikliğin riskine göre inceleme

Her katkıya aynı ağırlıkta süreç uygulanmaz:

| Değişiklik | Beklenen hazırlık |
| --- | --- |
| **Doküman, test veya küçük, yalıtılmış düzeltme** | Dar kapsam ve ilgili doğrulama yeterlidir; sırf küçük bir düzeltme için RFC ya da kapsamlı mimari rapor gerekmez. |
| **Davranış koruyan refactor** | Önce dış import/CLI/JSON, dosya biçimi ve test patch noktaları gibi korunacak sözleşmeleri belirleyin; aynı girdilerle önce/sonra karakterizasyon ve regresyon testleri çalıştırın. |
| **Mimari, güvenlik, state, installer veya release değişikliği** | Kodlamadan önce issue üzerinde kısa bir RFC/ADR veya tasarım özetiyle seçenekleri, riskleri, kapsam dışını, bağımlılıkları, ilgili uzman/maintainer incelemesini ve uygulanabilen upgrade/rollback test planını tartışın. |

**Geriye dönük uyumluluk:** Mevcut public API/CLI, kullanıcı verisi, yapılandırma, dosya yolu, hook ve paket sözleşmelerini mümkün olduğunca koruyun. Gerçekten uyumsuz bir değişiklik gerekiyorsa etkilenen kullanıcıları, geçiş/migration yolunu, uygun bir deprecation/uyumluluk katmanı ihtiyacını ve geri dönüş sınırlarını önceden açıklayın; iç refactor için gereksiz deprecation başlatmayın. Gerektiğinde önce uyumluluk katmanını, sonra iç taşıma işini ayrı PR'lara bölün.

**Temel V3 kontrolleri** (repo kökünde):

```bash
python -m unittest discover -s tests -p "v3_*test.py"
python scripts/evaluate_v3.py
```

Windows'ta gerekirse `python` yerine `py -3` kullanın. V3 test keşfi `tests/v3_*test.py` desenini kullanır. Subprocess açan testlerde `tests/v3_package_helpers.inherited_env()` kullanın; aksi halde ajan ortamından gelen `BEYIN_V3_SKIP` testleri yanıltıcı biçimde başarılı gösterebilir.

| Değişiklik alanı | Ek kontrol |
| --- | --- |
| Windows motor/adaptör | `python tests/scripts_test_windows.py` ve `python tests/antigravity_test.py` |
| POSIX motor/graf | `python3 tests/scripts_test.py` |
| Windows kurulum | `pwsh -NoProfile -File tests/Test-Preflight.ps1` ve ilgili kurulum testleri |
| Paket/güncelleme | [Güncelleme sözleşmesi](docs/v3/UPDATE.md) ve [release workflow](.github/workflows/release.yml) |

GitHub Actions bazı işleri yalnız ilgili dosyalar değiştiğinde çalıştırır. **Workflow'un tetiklenmemesi testin geçtiği anlamına gelmez.** PR'da yalnız gerçekten çalıştırdığınız komutları, ortamı ve sonuçları belirtin.

**Rollback:** Kalıcı ayar, yeni istemci adı veya kurulum bileşeni ekleyen PR'larda önceki sürüme dönüş mutlaka test edilmelidir. Eski `validate()` sürümleri bilinmeyen anahtarları **ve desteklenen aralığın dışındaki değerleri** reddedebilir. Yeni kalıcı ayarlar, `.beyin-preferences.json` yerine `state/` altında ayrı bir dosyada tutulmalıdır.

## 4. AI ajanlarıyla geliştirme

AI araçları kullanılabilir; ancak **kodun, testlerin ve açıklamaların doğruluğundan katkıcı sorumludur.** Ajanın beyanı test veya review kanıtı değildir.

Önerilen iş akışı:

1. **İncele:** İlgili issue'yu, mevcut kodu, testleri ve bu rehberi ajana incelet.
2. **Sınırla:** Çözülecek problemi ve kapsam dışındaki işleri netleştir.
3. **Denetle:** Üretilen değişiklikleri incele; ilgisiz refactor, gereksiz soyutlama, uydurulmuş API ve zayıflatılmış testleri ayıkla.
4. **Doğrula:** Testleri gerçekten çalıştır; riskleri, eksikleri ve doğrulanmayan iddiaları PR'da açıkla.

Testleri gevşeterek sonuç elde etmeyin, kullanıcı dosyalarını izinsiz değiştirmeyin ve bir issue'yu geniş kapsamlı yeniden yazım izni saymayın. Repository ayarları, yetkilendirme, merge ve release kararları maintainer sorumluluğundadır.

## 5. Issue ve Pull Request

**Hata bildirimi:** Sürüm/commit, işletim sistemi, Python sürümü, istemci, beklenen/gerçek davranış, tekrar adımları ve hassas bilgilerden arındırılmış hata çıktısı paylaşın. [Hata şablonunu](.github/ISSUE_TEMPLATE/bug_report.md) kullanabilirsiniz.

**Özellik önerisi:** Kullanım senaryosunu, mevcut alternatifleri, olası yan etkileri ve bakım maliyetini anlatın. [Özellik şablonu](.github/ISSUE_TEMPLATE/feature_request.md) kullanılabilir.

**PR:** Fork/branch adı için zorunlu bir yöntem yoktur. [PR şablonunda](.github/PULL_REQUEST_TEMPLATE.md) neyin neden değiştiğini, önceki/yeni davranışı, uygulanan testleri ve gerçek sonuçlarını belirtin. Gizlilik, kullanıcı verileri, istemci/platform farklılıkları, denenmeyen platformlar ve gerekiyorsa kurulum/rollback risklerini açıklayın.

- **`Fixes #N`:** Issue tamamen çözülüyorsa; merge edildiğinde issue otomatik kapanır.
- **`Refs #N`:** Kısmi çalışma veya ilgili tartışma için.

Review geri bildirimlerini değerlendirin. **Yeşil CI tek başına merge onayı değildir; `main`'e merge de otomatik release anlamına gelmez.**

## 6. Maintainer inceleme ve bakım süreci

Bu bölüm, katkıcıya ek zorunluluk koymak yerine PR'ların nasıl değerlendirildiğini açıklar:

- Açık PR ve issue'lar bakım turlarında incelenir. Her PR ayrı `git worktree` üzerinde değerlendirilir; iddialar güncel `main` üzerinde gerçek kurulum, dosya veya veritabanı satırıyla doğrulanır. Mock veri tek başına kanıt değildir; regresyonun `RED` aşaması da kontrol edilir.
- Katkıcı commit'leri yeniden yazılmaz (rebase/squash/force-push yapılmaz). Gerekli küçük maintainer düzeltmeleri ayrı commit olarak eklenir ve PR'da açıklanır. PR'lar **merge commit** ile alınır, yazarlık korunur.
- Birden fazla PR aynı turda birleşik ağaçta denenir; çakışmalar çözülür, Python **3.11 ve 3.14** testleri ile `scripts/evaluate_v3.py` çalıştırılır. PR'lar sırayla merge edilir ve son `main` test edilen ağaçla karşılaştırılır.
- CI Linux, macOS ve Windows'ta çalışır; ilk kez katkı verenlerin fork kaynaklı CI koşuları onaydan sonra başlar. PR'sı olmayan yeniden üretilebilir hatalar maintainer tarafından düzeltilebilir ve raporlayan `Reported-by` ile anılabilir. Tasarım ve öneri issue'larına durum notu yazılır. Ürün yönü, merge ve yayın kararları maintainer'a aittir.
- Kritik modüller için alanı bilen reviewer'ların sürece katılması yararlıdır. Katkı hacmi gerektirirse `CODEOWNERS` veya branch rulesets gibi denetimler ayrıca değerlendirilebilir; bunların devreye alınması repo yöneticisinin kararıdır ve bu rehber tek başına yeni bir merge engeli oluşturmaz.

## 7. Gizlilik ve güvenlik

Gerçek notları, sohbet kayıtlarını, kişisel dosya yollarını, özel çalışma alanlarını, token veya API anahtarlarını issue, PR, test fixture'ı ya da loglara eklemeyin. Sentetik veri kullanın ve paylaşılan çıktılardaki hassas bilgileri temizleyin.

Ciddi bir güvenlik açığını herkese açık issue'da ayrıntılandırmak yerine, **kullanılabilir durumdaysa** GitHub'ın özel güvenlik bildirim kanalını veya maintainer ile özel iletişimi tercih edin. Özel kanalın etkinleştirilmesi maintainer kararıdır.

---

Amaç geliştirme hızını gereksiz yere sınırlamak değil, proje büyürken katkı kalitesini ve uzun vadeli bakım kolaylığını korumaktır.

**Küçük ama güvenilir bir katkı, büyük ve bakımı zor bir özellikten daha değerlidir.**
