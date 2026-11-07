## Değişiklik ve gerekçe

<!-- Somut sorun, önceki ve yeni davranış, mevcut çözümün neden yetersiz kaldığı.
Değişikliği tek bir amaçla sınırlayın; ilgisiz düzenlemeleri ayrı tutun. -->

İlgili kayıt: <!-- Tam çözülen hata için Fixes #N; kısmi çalışma için Refs #N. -->
Taban commit: <!-- git merge-base HEAD origin/main -->
<!-- Büyük işlerde bağlı PR'ları, bağımlılık sırasını ve varsa RFC/ADR veya tasarım issue'sunu ilişkilendirin. -->

## Doğrulama

<!-- Çalıştırdığınız komutları, ortamı ve gerçek sonuçları yazın.
Hata düzeltmesinde mümkünse düzeltmeden önce başarısız (RED), sonra başarılı olan regresyonu gösterin.
Davranış koruyan refactor'da önce/sonra API, CLI/JSON, dosya biçimi ve ilgili karakterizasyon testlerini karşılaştırın.
Test yazılamadıysa nedenini ve alternatif doğrulamayı; çalıştırılmadıysa bunun sınırını açıklayın.
Küçük belge değişiklikleri için uygun kontrol yeterlidir; koşulmayan testi geçti saymayın. -->

| Komut / senaryo | Ortam | Sonuç |
| --- | --- | --- |
| | | |

## İlgili sözleşmeler

<!-- Yalnız ilgili satırları doldurun veya uygulanmıyor yazın.
Public API, CLI, hook, state veya veri biçimi değişiyorsa uyumluluk/taşıma ve varsa deprecation yaklaşımını açıklayın.
Riskli mimari/güvenlik/dağıtım değişikliğinde alternatifler, ilgili reviewer, test ve upgrade/rollback planına link verin.
Salt doküman ve küçük düzeltmeler için bu ayrıntılar zorunlu değildir. -->

- Gizlilik, hassas klasörler, güvenilmeyen dış içerik ve kullanıcı dosyalarının korunması:
- Bağlam bütçesi ve salt okunur yolların yazmaması:
- Kurulu paket, kaynak kopyaları ve Python stdlib bağımlılıkları:
- Windows, macOS ve Linux farkları:
- Kurulum, şema veya kalıcı ayar değiştiyse güncelleme/rollback doğrulaması:
- Public arayüz veya dosya biçimi değiştiyse mevcut kullanıcılarla uyumluluk/geçiş durumu:

## Bilinen sınırlar ve katkılar

<!-- Kapsam dışında kalanlar, çalıştırılmayan testler/platformlar ve katkı sağlayanlar.
Kullanıcıya görünen davranış değiştiyse ilgili belge güncellemesini veya eksikse nedenini belirtin.
Token, anahtar veya gerçek özel vault verisi eklemeyin. -->
