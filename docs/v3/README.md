# İkinci Beyin V3

Vault dışındaki seçili proje oturumları için: [opsiyonel global köprü](GLOBAL-BRIDGE.md).

V3'te Markdown kaynak, SQLite yerel indeks, Codex/Claude Code/Antigravity, [Hermes Agent](HERMES.md), [OpenCode](OPENCODE.md) ve [OMP](OMP.md) ise aynı motora bağlanan istemcilerdir. Python 3.11+ dışında ek Python paketi gerekmez. Varsayılan akışta Mem0, API anahtarı, daemon veya ayrı sunucu kurulmaz. [Jev danışmanı](JEV.md) isteğe bağlıdır ve varsayılan kapalıdır; `beyin.py jev` ile açılmadıkça normal arama ve hook akışını değiştirmez. Aynı danışman ayrı kurulan yerel Laya sunucusunu da yalnız gölge modda, ölçüm için kullanabilir (`jev shadow --provider laya`).

[Yeni kullanıcı başlangıcı](../../README.md) · [Ajanla kurulum](../../SETUP-V3.md) · [Güncelleme/geri alma](UPDATE.md) · [Birden çok makine](MULTI-MACHINE.md) · [Kaynak şeması](MARKDOWN.md) · [Runtime](RUNTIME.md)

## Kullanıcı paketi

Kurucu ortak motoru, `beyin`, `beyin-doktor`, `beyin-guncelle` skill'lerini, kurulu `beyin.py` girişini ve işletim sistemine uygun güncelleme kısayolunu dağıtır. Paket ZIP olarak açılıp kullanılabilir; Git gerekmez. Kurulu vault kaynak repo klasöründen bağımsız çalışır.

[Son kararlı release](https://github.com/avenoxai/avenoxbeyin/releases/latest) ve içindeki `beyin-v3-X.Y.Z.zip` paketi stable dağıtım noktasıdır. V3.8.1 turunda 1012 test ile 10/10 development ve 6/6 holdout semantik senaryosu geçti; CI aynı paketi Windows, macOS ve Linux'ta doğrular.

## Davranış

- Markdown ekleme, düzenleme, silme ve yeniden adlandırma indeksle birleşir. Task metadata güncellemesi effective revision ve kaynak hash kontrolü kullanır; gövde korunur.
- Kısa lifecycle hook'ları kalıcı metadata kuyruğuna yazar. İş gerektiğinde başlar; istemciler kapalıyken sürekli çalışan servis yoktur.
- Kaynak bağlantılı receipt tekrar denemede aynı sonucu verir. Önceki sonuç sonraki oturumda tarihsel iddia olarak bulunur; ham sohbet doğrulanmış bilgiye çevrilmez.
- Yeni sonuçlar `daily/v3/` ve `knowledge/v3/outcomes.md` altında receipt bağlantılarıyla görünür. Eski günlük ve bilgi metinleri yeniden yazılmaz.
- Ortak skill kaynakları `.agents/skills` içindedir. Claude tarafı platforma göre aynı kaynağa veya kontrol edilen karşılığa erişir; kişisel skill çakışmaları gizlenmez.
- Updater yalnız resmi stable release veya açıkça verilen yerel paketi kullanır. Yönetilen dosya yedekleri, kesinti journal'ı, checksum ve runtime ön kontrolleri uygulanır; başarılı sürüm kaydı en son yazılır.

## V2 geçişi

[V2 migration rehberi](MIGRATION.md) eski kaynakların korunmasını ve writer geçişini anlatır. Tanınan V2 özetleyici/derleyici devreden çıkarılır. Modelle bilgi damıtmayı artık aktif ajan, `beyin` skill'i üzerinden bilinçli olarak yapar; deterministic görünümler bunun yerine bilgi uydurmaz. Özelleştirilmiş veya harici eski işler ayrıca değerlendirilir.

İlk kurulum/geçiş dahil son sistem işlemi `rollback` ile geri alınabilir; yeni kullanıcı notları kalır. Yarım işlemi `recover` tamamlar. [Komutlar ve sınırlar](UPDATE.md).

## Doğrulama sınırı

[Platform testleri](PLATFORM-TESTS.md) ve [gerçek istemci oturumları](LIVE-CLIENTS.md) ayrı kanıtlardır. Runtime'ın daha önceki native CI başarısı yeni updater paketinin final CI veya release kanıtı yerine geçmez. Codex CLI kontrolü de Codex Desktop cold-session kontrolünün yerine geçmez.

Önceden sabitlenen [semantik sözleşme](SEMANTIC-TEST-CONTRACT.md) küçük, sentetik bir kaynak bulma/durum testidir. Motor kelime tabanlı arama kullanır; embedding veya genel doğal dil anlama başarısı iddia edilmez. Fixture'a göre kodlama yapılmaz; development ve holdout sonuçları ayrı kaydedilir. [Temel karşılaştırma](BASELINE.md).

Gerçek kullanımda kancanın enjekte ettiği kaynakların sonradan açılıp açılmadığını Claude Code transkriptlerinden salt okunur sayan geliştirici aracı: [kullanım raporu](USAGE-REPORT.md). Yayın paketine girmez.

## Teknik ve tarihsel kayıt

[Ürünleştirme durumu](PRODUCTIZATION-PLAN.md) · [Komut detayları](QUICKSTART.md) · [Serai desenleri](SERAI-PATTERNS.md).

İlk incelenen public taban `2e074cc` idi. [Public mimari](public-architecture.md), [yerel mimari](local-runtime-architecture.md), [eşitlik/migration değerlendirmesi](parity-and-migration.md) ve [sorun incelemeleri](issue-validation.md) tasarım geçmişidir; mevcut uygulamanın yerine kullanılmaz.
