# Topluluk araçları

Bu sayfadaki araçlar üçüncü taraf topluluk araçlarıdır; yapımı ve bakımı sahiplerine aittir. avenoxbeyin projesi
bunları desteklemez, güvenlik açısından denetlemez ve davranışlarını doğrulamaz. Listede yer almak bir onay ya da
öneri değildir. Kurmadan önce kendin incele.

Ekleme ölçütü: araç vault'u [Markdown sözleşmesine](v3/MARKDOWN.md) göre okur, belirli klasörlerin var olduğunu
varsaymaz ve iddialarını kendi deposunda nasıl doğrulayacağını gösterir.

## Beyin (macOS)

[yigit2015/beyin-mac](https://github.com/yigit2015/beyin-mac) · MIT · macOS 26+ · üçüncü taraf, denetlenmedi

Vault'u ağ, küre ve genel bakış olarak gösteren salt okunur bir uygulama. avenoxbeyin kodunu içermez ve çalıştırmaz.

- Okuma kuralları için [MARKDOWN.md](v3/MARKDOWN.md)'yi esas alır; neyi okuyup neyi atladığı:
  [docs/UYUMLULUK.md](https://github.com/yigit2015/beyin-mac/blob/main/docs/UYUMLULUK.md).
- Vault'a yazmadığı ve ağa çıkmadığı söylenir. Bu iddialar bağımsız olarak denetlenmedi; kendin
  [`Scripts/verify_offline.sh`](https://github.com/yigit2015/beyin-mac/blob/main/Scripts/verify_offline.sh) ile
  kontrol edebilirsin.
- Aracın README'si uygulamanın App Sandbox kullanmadığını (ağ erişimini macOS sınırlamaz) ve dağıtılan sürümün
  Apple tarafından onaylanmadığını (notarize edilmediğini) belirtir; güncel durumu orada kontrol et.
- Klasör sözleşmesi [#478](https://github.com/avenoxai/avenoxbeyin/issues/478)'de tartışılıyor; sözleşme değişirse
  uyum aracın sahibinin sorumluluğundadır.
- Sorunlar için: [beyin-mac issues](https://github.com/yigit2015/beyin-mac/issues).
