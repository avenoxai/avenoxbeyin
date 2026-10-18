# Workspace kurulumu ve konuşmalı yönetim

[Workspace belgeleri](workspaces/README.md), sözleşme ve kaynak yetkisi sınırlarını açıklar.

Kullanıcı amacını ve kaynaklarını anlatır; aktif ajan kısa kurulum adaptörü üzerinden alan tanımını ve somut değişiklik önizlemesini hazırlar. Kullanıcıdan teknik JSON formu doldurması istenmez. Mevcut plan/preview/apply ve kaynak onayı sınırları korunur.

## Komutlar

| Amaç | Komut |
|---|---|
| Kayıtlı alanların metadata listesi ve özeti | workspace list / inspect ID |
| Sınırlı kaynak kataloğu ve kısa girdiden plan | workspace-setup catalog / prepare |
| Yetkili bağlamla Markdown bloğunu yenileme | workspace-setup organize |
| Konfigürasyon planı, önizleme, uygulama ve kurtarma | workspace-builder plan / preview / apply / recover / revert |
| Kaynak ve plan onayı, iptal | workspace-authority grant / approve-plan / approve-revert / revoke |
| Günlük kaynaklı bağlam | workspace-context |
| Kanonik not/görev değişikliği | workspace-write |
| Sonlu ortak süreç | workspace-workflow |

Plan ve authority komutları sınırlı JSON'u --file - ile stdin'den alır. workspace-context/write/workflow komutları sürümlü girişlerini --file ile alır. Alanlar [sözleşmede](workspaces/v0.1.0/CONTRACT.md), [yetki adaptöründe](workspaces/AUTHORITY-CLI.md) ve CLI yardımındadır.

## Kısa kurulum

Ajan, prepare için workspace_id, name, purpose ve kaynakların path/name alanlarını hazırlar. İsteğe bağlı homepage mevcut proje ana sayfasını gösterebilir. Çıktı spec, plan ve somut preview içerir; bu adım dosya yazmaz veya erişim izni oluşturmaz. catalog yalnız dosya metadata'sını sınırlı derinlikte listeler; belge gövdesi okumaz.

Plan ve kaynak izinleri onaylandıktan sonra mevcut apply yolu kullanılır. Yeni alanın varsayılan ana sayfası notes/workspaces/<workspace_id>.md dosyasıdır. Ana sayfa teknik profilde extensions.setup_homepage alanındadır. Mevcut sayfa kullanılacaksa kanonik not olarak indeksli, kapsama alınmış ve gerektiğinde açıkça benimsenmiş olmalıdır; mevcut profilin not kökleri sessizce genişletilmez.

## Uygulama sınırı

1. Mevcut kaynak ve kayıtlar bulunur; amaçla ilgili kapsam seçilir.
2. Kaynak yetkisi ve somut konfigürasyon planı ayrı gösterilir.
3. Planın hash'i ve yazılacak dosyalar onaylanır; değişen plan eski onayla uygulanmaz.
4. Konfigürasyon, belge okuyucu hazırlığı ve günlük kullanım sonucu ayrı doğrulanır.
5. Not/görev yazımı mevcut kanonik writer üzerinden yürür.

Kaynaklar varsayılan salt okunur ve yerinde kalır. Kesintide aynı işlemin recover/revert yolu kullanılır; kullanıcı edit'i conflict olarak korunur.

## Belgeler ve görünüm

PDF pypdf ile izole okuyucu sürecinde, DOCX stdlib ZIP/XML ile okunur. TR/EN OCR, Poppler/Tesseract ve tur+eng gerektirir. [Ortak servis](workspaces/SHARED-DOCUMENT-READER.md) ile [OCR önkoşulları](WORKSPACE-BUILDER-OCR.md) bu yolu açıklar.

Markdown görünümü kaynak bağlantıları, işler ve notları içerir. Başarılı kanonik değişiklik yapılandırılmış ana sayfadaki beyin-workspace marker çiftinin arasını yeniler; dışındaki kullanıcı gövdesi korunur. Bozuk/yinelenen marker, iptal edilmiş yetki veya eşzamanlı kullanıcı edit'i açık hata üretir. Görünüm yenilenemediğinde esas kayıt başarı sonucu korunur ve managed_view_stale bildirilir; journal ve yeni yetkili bağlamla yeniden yenileme gerekir.

[#397](https://github.com/avenoxai/avenoxbeyin/issues/397) kapsamındaki alana özel panolar ve ortak proje/kural yükleme entegrasyonu açık kalır. [Sentetik kullanım senaryoları](WORKSPACE-BUILDER-CONVERSATIONS.md) kabul girdileridir; yazılmış olmaları kabulün tamamlandığını göstermez.

## Tekrar ve recovery sınırı

Aynı request ve workflow kimliğiyle tekrar, yalnız o run'ın writer journal'ında birebir doğrulanmış before→after geçişi için devam edebilir. Grant iptali ve sonradan yapılan kullanıcı edit'i reddedilir. Eski eksik journal'lar ve aynı hedefte ardışık birden fazla geçiş otomatik onarılmaz; hata sonrası mevcut kayıtları koruyarak yetkili inceleme gerekir. Ortak internal ana sayfa private kayıt metadata'sını içermez. Ayrıntılar [sözleşmede](workspaces/v0.1.0/CONTRACT.md).
