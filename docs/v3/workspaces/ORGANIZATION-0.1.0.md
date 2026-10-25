# Ek dosya organizasyon planı 0.1.0

`workspace-organize preview --file REQUEST.json`,
`contract_version: workspace-organization/0.1.0` ve `plan_sha256` taşıyan
sürümlü bir `OrganizationPlan` döndürür. `workspace-organize apply --file PLAN.json`
bu planı aynen uygular. Kaynak `(source_id, relative_path, content_sha256)`,
hedef varsa `(source_id, relative_path)` ile tanımlanır. Bilinmeyen alan/sürüm,
değiştirilmiş plan hash'i, eskimiş kaynak baytları ve değişen P4 kaynak etki
önizlemesi reddedilir.

Varsayılan eylem `link`tir. Sonucu `link_proposed` olur: yalnız yetkili kaynak
kimliği/hash'i döner; fragment/extractor içeren tam SourceRef veya uygulanmış
kanonik bağ değildir. Gerçek kayıt ya da üyelik bağı gerektiğinde mevcut
kanonik writer kullanılır. `unchanged` de dosya yazmayan bir karardır. Bu iki
eylem not yaratmaz ve görünümün güncellendiğini iddia etmez.

`copy` açıkça seçilir. Hedef, vault kökünün altında ayrıca bağlanmış görünür
bir `directory` olmalıdır. Harici kaynak dizinleri mevcut
`external_sources: read_only` politikası gereği salt okunur kalır. Kaynak için
güncel `read` izni; hedef için hem çağıranın hem binding sahibinin mevcut
`LocalAuthority` kayıtlarında ayrı `write_copy` izni ve binding'in workspace
`read` kabulü gerekir. Ayrıca exact `plan_sha256`, insan yetkisi kanıtıyla
`workspace-authority approve-organization` üzerinden onaylanmalıdır;
`revoke-organization` ile kaldırılabilir. Yerel authority kaydı v2'ye çıkar;
eski v1 kayıtları okunur ama kendiliğinden copy yetkisi kazanmaz.
`write_memory` genişletilmez. Hedef ortak Markdown writer'ın dışındadır.

Copy yalnız açık doküman/medya uzantılarını kabul eder. Kanonik `.md` ve kod
dosyası kopyası reddedilir; böylece aynı kayıt ID'siyle ikinci Markdown
üretilmez. Kaynak baytları değişmez. Hedef boş olmalıdır. Aynı baytlı mevcut
dosya bile bu planın kalıcı köken journal'ı yoksa çakışmadır. Case/Unicode
takma yolları, kontrol anındaki symlink/junction, yönetilen/gizli hedef
dizinleri ve kaynağın kendisinin hedef olması reddedilir.

Yerel `workspace-copy/<plan_sha256>.json` journal'ı kaynak/hedef kimliğini ve
plan hash'ini staging öncesi kaydeder. Writer aynı hedef dizininde benzersiz
geçici dosyayı fsync eder ve exclusive hard link ile yayımlar; final ad ya yok
ya tam bayttır. Sonra kaynak/hedef baytlarını, iki grant'i ve exact plan
onayını doğrulayıp journal'ı verified yapar. Verified plan tekrarında hedef
baytları ve güncel yetki yeniden denetlenir; ikinci kopya oluşmaz. Pending
journal ve exact hedef doğrulamayı tamamlayabilir; kısmi veya kullanıcıca
değişmiş hedef çakışma olarak kalır. Eski geçici dosya silinmez. Move, geri
alma ve daha geniş kesinti kurtarması P5b kapsamındadır. Bu journal yerel
kökendir; yeni kanonik kayıt ya da kaynak kopyası değildir. Pending durumda
aynı baytlı hedefin hangi süreç tarafından yayımlandığı kesin olarak
kanıtlanamaz; `reconciled` yalnız planın kaynak/hedef kimliği ile güncel
baytların eşleştiğini bildirir.

`dir_fd` destekleyen sistemlerde hedefin ara dizinleri vault'tan başlayarak
`O_NOFOLLOW` ile açılır; staging, exclusive publish ve geri okuma açık dizin
tanıtıcısına bağlanır. Bu, son yol kontrolünden hemen sonra ara dizinin
symlink'e çevrilerek kopyanın vault dışına yönlendirilmesini önler. Windows
gibi `dir_fd` sunmayan sistemlerde yol yeniden denetlenir ve exclusive publish
uygulanır; aynı hedef ağacını eşzamanlı değiştirebilen başka bir süreç bu
yol tabanlı sınırın dışındadır. Açık dizinin kendisini vault dışına taşıma
yetkisi bulunan eşzamanlı süreç de burada güvenilen yerel dosya sistemi
sınırıdır; ürün bu koşulda tam OS izolasyonu vaat etmez.
`dir_fd` yolunda yayımlamanın bağlı olduğu açık hedef dizini verified
journal'dan önce fsync edilir; bu
başarısızsa journal pending kalır. Windows yolunda dizin fsync garantisi
verilmez; açılışta verified journal hedef baytları yeniden denetler.

Logo çalışma kopyası için örnek önizleme isteği:

```json
{"workspace_id":"client","grant_ref":"approved-client-access","source_id":"source-designs","source":"logo.png","action":"copy","target_source_id":"working-assets","target":"working/logo.png"}
```

Onay isteği `{ "plan": <exact preview>, "evidence": "insanın somut yetkisi" }`
ile `workspace-authority approve-organization --file ...` komutuna verilir.
Organizasyon komutları `--vault` ve vault dışındaki yerel `--state` ister.
Önizleme P4'ün yetkili ek dosya etkisini içerir; bilinmeyen referansları
bilinmiyor olarak tutar. İstek metninden izin çıkarılmaz.
