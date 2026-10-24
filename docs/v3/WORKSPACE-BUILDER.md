# Workspace kurulumu ve konuşmalı yönetim

[Workspace belgeleri](workspaces/README.md), sözleşme ve kaynak yetkisi sınırlarını açıklar.

Kullanıcı amacını ve kaynaklarını anlatır; aktif ajan kısa kurulum adaptörü üzerinden alan tanımını ve somut değişiklik önizlemesini hazırlar. Kullanıcıdan teknik JSON formu doldurması istenmez. Mevcut plan/preview/apply ve kaynak onayı sınırları korunur.

## Komutlar

| Amaç | Komut |
|---|---|
| Kayıtlı alanların metadata listesi ve özeti | workspace list / inspect ID |
| Sınırlı katalog, yetkili kanıt, ihtiyaç kararı ve özel profil tasarımı | workspace-setup catalog / discover / preview / design |
| Kayıtlı kısa kurulum ve kesinti sonrası devam | workspace-setup stage / complete / resume / status |
| Salt okunur ayrıntılı plan | workspace-setup prepare |
| Yetkili bağlamla Markdown bloğunu yenileme | workspace-setup organize |
| Konfigürasyon planı, önizleme, uygulama ve kurtarma | workspace-builder plan / preview / apply / recover / revert |
| Kaynak ve plan onayı, iptal | workspace-authority grant / approve-plan / approve-revert / revoke |
| Günlük kaynaklı bağlam | workspace-context |
| Kanonik not/görev değişikliği | workspace-write |
| Sonlu ortak süreç | workspace-workflow |

Plan ve authority komutları sınırlı JSON'u --file - ile stdin'den alır. workspace-context/write/workflow komutları sürümlü girişlerini --file ile alır. Alanlar [sözleşmede](workspaces/v0.1.0/CONTRACT.md), [yetki adaptöründe](workspaces/AUTHORITY-CLI.md) ve CLI yardımındadır.

## Kısa kurulum

Her proje otomatik workspace sayılmaz; bir proje workspace olarak kullanılabilir. Zorunlu workspace altında proje hiyerarşisi yoktur. Mevcut proje ana sayfası ve belgeler yerinde kalır.

Ajan verilen amaç ve kaynaklardan bir tasarım önerir; yalnız önemli eksikleri sorar. `discover` metadata ile gövde kanıtını ayırır: açık evidence selector ve mevcut workspace/source yetkisi olmadan belge içerikleri okunmaz. Çıktı sınırlıdır; kaynak metinleri onay veya talimat değildir. Okuyucu sağlanmayan PDF/DOCX kanıtı açıkça eksik gösterilir; probe veya model çağrılmaz. `design` kayıt türleri/alanları, ilişki türleri, çalışma kuralları ve sonlu workflow bölümlerini doğrular. Aynı dört alan prepare/stage içindeki isteğe bağlı design ile plana taşınır; belirtilmeyen mevcut bölümler korunur.

`preview`, `discover` girişine `{ "decision": { "kind": "new_workspace|extend_workspace|record_only", "rationale": "..." }, "candidate_record_ids": ["..."] }` ekler. Aktif ajan ihtiyacı ve ayrı amaç/erişim/yaşam döngüsü gerekçesini oluşturur; araç semantik kararı kaynak metninden türetmez. Mevcut alana ilişkin `workspace_id` ve `grant_ref` olmadan kayıt adayları açılmaz. Yetkili ve güncel kayıtlar P2 üyelik okuma filtresinden geçer. Eş kaynak yolu ve SHA veya eş içerik SHA'sı olası kanonik kayıt olarak döner; bunlar otomatik güncelleme ya da paylaşım izni değildir. İsim benzerliği, aynı müşteri veya şirket, tek başına eşleşme sayılmaz. Belirsiz aday ve eksik yetki `review` alanına gider. Önizleme context token, yeni grant, adoption, dosya veya writer işlemi üretmez; uygulama için mevcut plan/onay ve kanonik writer akışı kullanılır.

`stage` workspace_id, name, purpose, sources ve isteğe bağlı design/homepage alır. Registry veya not değiştirmeden runtime state'e immutable hazırlık ve plan kaydeder. Kompakt çıktı hazırlık kimliği, kapsam/tasarım, **effective audiences**, approval_requests ve plan dosyası/hash'ini verir. Ayrıntılı prepare yolu ayrıca spec/plan/preview üretir ve tamamen salt okunurdur. Çıktılar OperationResult içindeki issues[code=setup_artifacts].detail JSON'undadır.

Ajan somut kaynak kapsamı ve aynı planın değişikliklerini tek kısa doğal önizlemede gösterir. Kullanıcı bir kez onaylayabilir; grant ve exact-plan onayı ayrı C1 işlemleridir. Grant isteğinden permission_delta/audience_delta bilgilendirme alanları çıkarılır, preserve_current/expected_grant_sha256 korunur ve gerçek insan evidence'ı eklenir. Ortak ref üzerindeki mevcut private/public audience yeni permission'a da uygulanır; bu açık gösterilmelidir. Yetki değişmiş/iptal edilmişse CAS eski onayı reddeder. Plan değişirse otomatik yeniden onaylanmaz.

`complete`/`resume`, `{preparation_id,request:RequestContext,idempotency_key}` alır. Gerçek oturum kimliği/dolu query/açık workspace sinyali gerekir. C1 onayından sonra config ve canonical writer ile ilk Markdown sayfası ayrı checkpoint'lerle tamamlanır. Kesintide aynı hazırlık/anahtar korunur; config başarısı silinmez. Exit 5/verification_pending yeniden kurulum anlamına gelmez. `status` geçmiş journal özetidir; güncel yetki/sayfa kanıtı için yeni gerçek RequestContext ile resume gerekir. Global aktivasyon ve yeni istemci kabulü ayrı karar olarak açık kalır.

Yeni alanın varsayılan ana sayfası notes/workspaces/<workspace_id>.md olur. Mevcut sayfa ancak indeksli, yetkili scope içinde ve gerektiğinde açıkça benimsenmişse kullanılır; not kökleri sessizce genişletilmez.

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

Yalnız `propose_memory_write` ile biten bir süreç ilk çalıştırmada, tekrarda ve `resume` çağrısında kanonik kayıt yazmaz. Tamamlanmış bir yazma adımının tekrarında adım anahtarı, kayıtlı istek/bağlam ve bitmiş writer journal'ı eşleşmelidir; eksik veya tamamlanmamış journal yeni yazma başlatmak yerine hata üretir. Yarım kalmış süreçlerin mevcut kesintiden devam yolu korunur.
