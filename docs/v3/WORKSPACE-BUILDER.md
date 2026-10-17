# Workspace kurulumu ve konuşmalı yönetim

[Workspace belgeleri](workspaces/README.md), sözleşme ve kaynak yetkisi sınırlarını açıklar.

Kullanıcı amacını ve kaynaklarını anlatır; aktif ajan mevcut runtime üzerinden alan tanımını ve somut değişiklik önizlemesini hazırlar. Kullanıcıdan teknik JSON formu doldurması istenmez. Düşük seviyeli plan/preview/apply yolu vardır; kısa kurulum adaptörü geliştirme aşamasındadır.

## Komutlar

| Amaç | Komut |
|---|---|
| Konfigürasyon planı, önizleme, uygulama ve kurtarma | workspace-builder plan / preview / apply / recover / revert |
| Kaynak ve plan onayı, iptal | workspace-authority grant / approve-plan / approve-revert / revoke |
| Günlük kaynaklı bağlam | workspace-context |
| Kanonik not/görev değişikliği | workspace-write |
| Sonlu ortak süreç | workspace-workflow |

Plan ve authority komutları sınırlı JSON'u --file - ile stdin'den alır. workspace-context/write/workflow komutları sürümlü girişlerini --file ile alır. Alanlar [sözleşmede](workspaces/v0.1.0/CONTRACT.md), [yetki adaptöründe](workspaces/AUTHORITY-CLI.md) ve CLI yardımındadır.

## Uygulama sınırı

1. Mevcut kaynak ve kayıtlar bulunur; amaçla ilgili kapsam seçilir.
2. Kaynak yetkisi ve somut konfigürasyon planı ayrı gösterilir.
3. Planın hash'i ve yazılacak dosyalar onaylanır; değişen plan eski onayla uygulanmaz.
4. Konfigürasyon, belge okuyucu hazırlığı ve günlük kullanım sonucu ayrı doğrulanır.
5. Not/görev yazımı mevcut kanonik writer üzerinden yürür.

Kaynaklar varsayılan salt okunur ve yerinde kalır. Kesintide aynı işlemin recover/revert yolu kullanılır; kullanıcı edit'i conflict olarak korunur.

## Belgeler ve görünüm

PDF pypdf ile izole okuyucu sürecinde, DOCX stdlib ZIP/XML ile okunur. TR/EN OCR, Poppler/Tesseract ve tur+eng gerektirir. [Ortak servis](workspaces/SHARED-DOCUMENT-READER.md) ile [OCR önkoşulları](WORKSPACE-BUILDER-OCR.md) bu yolu açıklar.

Markdown görünümünün tasarımı [#397](https://github.com/avenoxai/avenoxbeyin/issues/397) kapsamında izlenir. [Sentetik kullanım senaryoları](WORKSPACE-BUILDER-CONVERSATIONS.md) kabul girdileridir; yazılmış olmaları kabulün tamamlandığını göstermez.
