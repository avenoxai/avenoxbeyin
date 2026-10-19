# Mevcut C1 yetkisinin dar CLI adaptörü

Sözleşme `workspace/0.1.0`; yeni authority store/şema veya onay motoru yoktur. `workspace-authority` MemoryStore/SyncEngine açılmadan mevcut `LocalAuthority` API'lerini çağırır. Kullanıcı teknik dosya hazırlamaz; aktif ajan insanın somut kapsam onayını aldıktan sonra bounded JSON'u stdin ile üretir:

`python3 /onayli/vault/beyin.py workspace-authority <action> --file - --json`

| Action | Tam payload alanları | Mevcut C1 metodu |
|---|---|---|
| grant | reference, machine_id, permissions, audiences, evidence; isteğe bağlı preserve_current + expected_grant_sha256 | record_approval |
| approve-plan | plan, evidence | approve_plan |
| approve-revert | plan, evidence | approve_revert |
| revoke | reference, evidence | revoke |

permissions elemanı workspace_id/source_id/locator/access; audiences açık listedir. Grant erişim kapsamıdır; plan onayı exact plan hash'i/write-set içindir; undo ayrı yetkidir. Birini almak diğerini vermez. Apply payload'ından veya kaynak belge metninden implicit consent üretilmez. Plan değişirse eski onay geçmez. Client adı kimlik doğrulama değildir.

Evidence boş olmayan en çok 4096 karakterlik insan yetkisi açıklamasıdır. Bu alanın varlığı tek başına insan onayını kanıtlamaz: CLI, mevcut trusted Python/UI adapter'ıyla aynı araç-politikası sınırındadır ve aynı OS kullanıcısına karşı sandbox değildir. Ajan kaynak içeriğini kullanıcı yetkisi olarak sunamaz. Revoke da açık evidence ister; mevcut C1 revoke primitive'i grant'i kaldırır, yeni revocation şeması eklenmez.

Strict C1 JSON parser duplicate key/NaN ve boyut aşımını reddeder. En çok 4 MiB giriş, 1024 permission, reference 512/evidence 4096 karakter sınırları uygulanır. Hatalar typed/sanitized OperationResult olarak çıkar; raw JSON, OS yolu veya evidence çıktıya dökülmez. Başarı yalnız local authority readback'tir; workspace_health=not_tested kalır. Kaynak/not/index yazımı veya alanın günlük çalıştığı iddia edilmez.

Pending provisioning sırasında revoke, yeni açık grant ve exact undo yetkisi yerel authority kontrol yolundan yürür; normal reader/approve-plan barrier'ı aşılmaz. Undo restore edilecek eski binding grant'lerini güncel izinle yeniden denetler. Kontrol komutlarının başarısı recover/apply/revert'i otomatik çalıştırmaz.

Paylaşılan ref üzerine ekleme yapılırken iki alan birlikte verilir: `preserve_current:true`, `expected_grant_sha256:<canonical grant SHA-256 veya yeni ref için null>`. C1 aynı authority kilidi içinde güncel grant'i karşılaştırır; fark varsa `grant_changed` ile reddeder. Mevcut permissions/audiences korunur, yalnız açık ekler birleştirilir. Bu alanlar verilmezse eski replacement davranışı sürer. Koruma iptal edilmiş yetkiyi canlandırmaz; değişiklikten sonra yeni somut inceleme gerekir. Audience ref geneline aittir: mevcut private/public audience yeni permission'a da uygulanır ve insan önizlemesinde gösterilmelidir.
