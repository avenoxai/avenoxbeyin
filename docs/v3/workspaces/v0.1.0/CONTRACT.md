# workspace/0.1.0 — G0 veri ve işlem sınırı

Sürüm: workspace/0.1.0. Normatif wire şekli [schema.json](schema.json), normatif işlem/erişim anlamı bu belgedir. Şekil doğrulaması yetki doğrulaması yerine geçmez.

## Günlük davranış

Kullanıcı “Bu dersin ödevine geri bildirimle devam edelim” der. Aktif ajan konuşma konusunu mevcut task kimliğiyle ilişkilendirir. C'nin WorkContext'i ders materyalini, ders notunu, açık ödevi/teslimi, önceki öğrenimi ve başarı notu/geri bildirimi birlikte getirir. Ağırlığı bilinmeyen 82 puanından dönem notu hesaplanmaz. Sonuç mevcut öğrenim notunu geliştirir; task'ın sonraki adımı mevcut task servisiyle değişir. Sonraki oturum doğru kaynağı yeniden bulur. Kullanıcı workspace/klasör formu doldurmaz.

Custom fixture'da aynı işlem fotoğraf çekim konusu, çekim işi, sonuç ve teknik öğrenimiyle yapılır. Profildeki tür adları değişir; Python'a alan enum'u eklenmez. Seramik kabul örneği development fixture'ından ayrıdır. Proje ve kariyer kapsamı da aynı kayıt/ilişki modeline bağlanır; G0 bunların ürün kabulünü tamamlamaz.

## Taşıma, sürüm ve kimlik

Her belge `{ "contract_version": "workspace/0.1.0", "type": "WorkspaceProfile", "data": {...} }` zarfını kullanır. Zarf tipi schema içindeki arayüz adıdır. Eksik zorunlu alan, bilinmeyen sürüm/alan/eylem veya geçersiz güvenlik değeri reddedilir. Yalnız `extensions` serbest scalar gösterim verisi alır; erişim, routing, workflow veya yürütme yetkisi vermez. `facts` bir düz scalar veya scalar-list metadata haritasıdır; kod/ifade değildir.

Workspace/source/workflow kimlikleri ASCII stable ID'dir; kullanıcı adı ve dosya adı kimlik değildir. Mevcut kayıt/task `id` değerleri korunur. Explicit ID'li not rename'de aynı kalır. Bugünkü path-derived `md-*` kimliği rename'de değişir: G0 bunun tersini vaat etmez. C5 adoption gerektiğinde kullanıcı edit'ini koruyan planla explicit kimlik ekler; geçmişi topluca yeniden yazmaz.

### Metin kodlama ve terminal sınırları

- Workspace sözleşme JSON'u, registry/journal/cache dosyaları ve uygulamanın ürettiği Markdown UTF-8'dir. Kanonik hash'ler **baytlar** üzerinden hesaplanır; platforma özgü satır sonu veya konsol kodlaması dönüşümü bu baytları değiştiremez. Geçersiz baytlar sessizce düzeltilmez.
- İzole PDF/DOCX okuyucusunun stdin/stdout JSON protokolü UTF-8 bayttır. Üst süreç stdout'u katı biçimde doğrular; yanlış kodlanmış yanıt başarısız olur. OCR/yerel araç stderr'i aynı protokolün parçası değildir.
- Makineye yönelik CLI JSON çıktısı kodlamadan bağımsız ASCII-escaped JSON'dur; girdi JSON dosyaları ve stdin UTF-8 kabul edilir. Kurulu `beyin.py --human` çıktısı gerçek terminalde terminalin kodlamasını kullanır; başka programa yönlendirildiğinde UTF-8 üretir. Terminalde temsil edilemeyen karakterler yalnızca görünür escape biçimine düşer, sessizce `?` ile kaybedilmez.
- Kullanıcının dış kaynak dosyalarına bu kural zorla uygulanmaz: özgün baytlar değiştirilmez. Mevcut düz-metin çıkarıcısı UTF-8 (isteğe bağlı BOM) bekler; geçersiz metin `partial / invalid_text_encoding` olarak raporlanır. Kodlama tahmini veya otomatik başka kodlamaya dönüştürme yapılmaz. Alternatif kodlama desteği ayrı ve açık bir kullanıcı seçimi/policy gerektirir.

## M01: workspace başına JSON ve yerel sahiplik

Önerilmiş tek registry JSON yerine kullanıcı **workspace başına JSON** seçti. Bu sürümün dosya düzeni:

| Konum | Sahiplik / anlam |
|---|---|
| vault `.beyin-workspaces/format.json` | RegistryFormat: sabit registry kimliği ve layout; merkezi değişen revision sayacı değildir. |
| vault `.beyin-workspaces/workspaces/<workspace_id>.json` | WorkspaceProfile; kendi pozitif revision'ı ve exact dosya hash'i. |
| vault `.beyin-workspaces/sources/<source_id>.json` | Ortak taşınabilir SourceDescriptor. Aynı özgün kaynak iki workspace'te aynı ID ile kullanılır. |
| state `workspace-bindings/<source_id>.json` | SourceBinding: bu makine locator'ı, yerel grant'ler, revision ve authority_ref. Taşınabilir registry'ye/senkron vault'a yazılmaz. |
| mevcut Markdown / mevcut SQLite | İnsan bilgisi / türetilmiş indeks. Kayıt başına dosya zorunlu değildir. |
| state `workspace-provision/` | D2'nin dayanıklı operasyon journal/yedekleri; updater journal'ından ayrıdır. |
| state `workspace-workflow/` | Süreç run/event/step durumları; operasyonel state, disposable indeks değildir. |

Bu yollar G0'ın yeni dosya düzenidir; mevcut kurulumda var oldukları söylenmez. `.beyin-workspaces` gizli dizini sıradan Markdown taramasına eklenmez. Kaynak descriptor'ı içerik kopyası değildir. Tanımsal ortak kaynak değişikliği etkilediği workspace'lerle birlikte planlanır; aynı kaynak için çelişen descriptor kabul edilmez. Yeni makinede descriptor bulunması grant değildir: binding yoksa `unbound`.

RegistrySnapshot, gözlenen dosyaların sıralı `(relative_path, content_sha256)` listesini ve profil/kaynak projeksiyonunu taşır. `snapshot_sha256` bu listenin canonical hash'idir. Her değişen dosyanın kendi before hash'i/revision'ı vardır. Bütün registry'ye artan tek revision eklenmez. D1 bütün snapshot'ı plan önkoşulu olarak tutar; ilgisiz bir workspace değişirse güvenli yeniden plan ister. Daha dar dependency snapshot optimizasyonu ayrı sözleşme değişikliğidir. Binding snapshot hash'i, source_id'ye göre sıralı `{source_id, revision, content_sha256}` listesinden gelir; content hash, SourceBinding zarfının canonical JSON'undan hesaplanır. Boş binding listesi de hash'lenir.

Birden çok dosyaya atomik replace, toplu transaction değildir. Katılan okuyucu/yazıcılar updater ile aynı yerel koordinasyon sınırını kullanır: `update-lock.sqlite3` öncesinde başka workspace/memory kilidi alınmaz; memory writer gerekirse bundan sonra alınır. D2 bu kilidi tutarken SyncEngine'i aynı memory kilidinin içine yeniden çağırmaz. Dosyaların yayını tamamlanmadan operasyon pending kalır. Reader pending çok dosyalı yayını karışık snapshot olarak sunmaz; `operation_pending` ile durur. Kesinti journal'ı ve kilit sırası C1/D2/D4 entegrasyon/fault testinde zorunludur. Bu cihazlar arası dağıtık kilit değildir.

## Minimum arayüzler

| Arayüz | Zorunlu bilgi ve günlük kullanım |
|---|---|
| WorkspaceProfile | id/revision/ad/amaç/lifecycle; source_ids, note_roots, scoped working_rules, memory/access policy, kullanıcı record_types/relation_types ve workflows. Boş tanım listeleri basit alana izin verir. |
| SourceDescriptor / SourceBinding | Taşınabilir source ID/tür/etiket; ayrı yerel locator/machine/revision/grant/authority. D'nin binding_request'i yetki değil taleptir. `write_memory` yalnız vault'un onaylı note/task alanlarına uygulanabilir; dış directory/git'e grant edilmez. |
| SourceRef | source_id + göreli dosya + özgün byte SHA256 + fragment + extractor id/version/settings hash. Locator taşımaz. Aynı adlı iki kaynak source_id ile ayrılır. |
| RequestContext | request/session/client/query, doğrulanacak grant_ref, audience/no_memory, kullanıcının açık seçimi veya konuşma konusu/onaylı kaynak eşleşmesi sinyalleri ve bütçe. Ajan önerir; servis hedefi/yetkiyi doğrular. |
| WorkContext | hazır/partial/ambiguous/unbound/denied; oturuma bağlı context_token, workspace/profile sürümü, registry/binding snapshot, konu/rule/records/relations/source refs, eksikler ve sınırlı bulgular. |
| Record / Relation | Mevcut id/revision/source_ref/text/facts/kind; workspace/entity_type/visibility/trust; task için mevcut status/due_at/next_action. İlişkinin iki ucu stable RecordRef ve kanıtları vardır. Başarı notu ve ders notu ayrı türler olabilir. |
| MemoryWriteIntent | workspace/context/policy-profile revision/binding snapshot/idempotency/gerekçe; hedef kaynak/id/revision/hash, işlem/body/metadata/evidence içeren değişiklikler. Bir niyet task ilerlemesini ve mevcut notu ayrı adımlarda kapsayabilir. |
| WorkspaceSpec | Kullanıcı amacı, **aynı WorkspaceProfile**, source descriptor ve binding talepleri, benimsenen mevcut kayıt referansları, gereken capability, açık sorular ve authority_ref. |
| ProvisionPlan | spec/snapshot/binding hash'leri; gerçek root+relative_path write-set, exact before/after hash ve UTF-8 yeni içerik, sahiplik, bağımlılıklar, gerekli yetki, verify ve revert sınırı. Serbest shell yoktur. |
| OperationResult | operation_status, workspace_health, parça durumları, capability, verification, issues ve next_action. İşlemin uygulanması, workspace sağlığı ve canlı teslim birbirinden ayrıdır. |
| WorkflowDefinition / Event / Run | Kullanıcıca tasarlanan süreç, kaynaklı tetikleyici kayıt ve dayanıklı adım durumu. Aşağıdaki bölümle birlikte yorumlanır. |

`record_types.fields` string/number/boolean/date/record_ref gibi basit alan doğrulamasını tarif eder; metadata'daki referans metni RecordRef'e çözülmeden erişim üretmez. Workflow, bu ortak kayıtlar üzerinde çalışır. Büyük ontology, graph DB, hesaplama dili veya alan başına modül bu sözleşmenin gereği değildir.

İlişkilerin kanonik kaynağı, sahip kaydın Markdown JSON frontmatter'ındaki `workspace_relations` listesidir; Record projeksiyonunda `relations` olarak görünür. WorkContext bunun yetkili birleşimini sunar; ayrı registry dosyasında ikinci kayıt/görev gerçeği tutulmaz. Her ilişkide bir sahip kayıt vardır; kapsayıcı kaydın SourceRef'i ilişkinin kökenini kanıtlar. Relation evidence_refs boş olabilir; aynı dosyanın kendi hash'ini frontmatter'ına koyan döngü kurulmaz. MemoryWriteIntent'in `relation_changes` listesi ID'ye göre ekle/güncelle işlemidir; ilgisiz ilişkileri silmez. Kaynak/task güncellemesiyle aynı CAS altında mevcut writer'a eklenir. C4, task metadata allowlist'ini yalnız bu doğrulanmış alan için dar genişletir. İlişkinin her ucu ayrı policy denetiminden geçer.

## Kaynak, bağlam ve policy sınırı

Etkili erişim = geçerli profil üyeliği/policy ∩ bu makinenin doğrulanmış binding grant'i ∩ çağrı yetkisi/işlem ∩ kayıt scope/visibility/trust/freshness. Grant_ref/authority_ref/context_token kullanıcı veya model tarafından yazılmış oldukları için geçerli sayılmaz; yerel yetki kaydı ve güncel kapsamla doğrulanır. Client adı kimlik doğrulama değildir. Request'in `private` istemesi tek başına private erişim vermez.

Profil working_rule metni yalnız onaylı kullanıcı yapılandırması olarak alınır. Kaynak içeriğinden profil/izin/çalıştırılabilir step otomatik türetilmez. Kaynak veri olarak kullanılır. İlişki, parent veya ad benzerliği izin üretmez. RecordRef çözümü ve ilişki genişletmesi her uçta aynı policy'yi uygular; yasak hedefin adı/id'si/snippet'i/edge'i veya hata ayrıntısı yayımlanmaz. Kamuya çıkan hata yetkisiz hedefin varlığını ifşa etmez; yetkili yerel operasyon log'u ayrıca erişim denetimindedir.

Oturum/istek bağlamı kullanılır; tek global active-workspace yoktur. Birbirine eşit olası hedefte `ambiguous` döner, kalıcı yazma durur. Kısa açıklama sorusundan sonra doğru hedefle yeni bağlam alınır. Konu değişimi başka oturumu değiştirmez veya yeni grant oluşturmaz. `no_memory=true` bütün otomatik note/task/öğrenim yazmalarını ve workflow yan etkilerini durdurur. Var olan rutin kayıt yetkisi tekrar onay istemek için sıfırlanmaz.

SourceRef'te `relative_path` hiçbir zaman eski vault-relative `source` alanına `../` eklenerek açılmaz. Gerçek yol, symlink/junction, kardeş prefix, null byte, Unicode/case çakışmaları işlem anında doğrulanır. ID'den dosya adı üretmek için doğrulanmış ASCII ID kullanılır. SourceRef hash'i özgün dosyanın tamamıdır; extraction cache anahtarı source hash + extractor version + settings hash'tir. Özgün dosya/izin değişirse eski içerik güncel diye yayımlanmaz. Okuma/extraction sonunda ve write/return öncesinde tekrar kontrol edilir.

PDF fragment'i 1 tabanlı fiziksel sayfa; DOCX block/table/row/cell konumları 1 tabanlı extractor konumlarıdır, baskı sayfası değildir. Lines aralığı 1 tabanlı ve iki uç dahildir. Son < başlangıç geçersizdir. PDF/DOCX, ilk teslimin zorunlu capability'sidir. G0 fixture'ı metin/Markdown kullanır; OCR/parser/paket kanıtı değildir.

## Kullanıcı tanımlı süreç motoru — M02

İlk teslimin motoru kullanıcı amacından doğal dille tasarlanmış, sınırlı eylemlerle çalışan süreçleri destekler. Akademik fixture süreci: `record_changed(grade_feedback)` → aktif ajandan kaynaklı öğrenim + task ilerlemesi + sonuç ilişkisi içeren tek niyet iste → ortak writer ile uygula → readback doğrula. Custom fixture'da yalnız entity_type/kimlikler değişir; aynı DAG ve eylemler çalışır. Her geri bildirimde yeni workspace/not açılmaz.

WorkflowDefinition id/revision/name, trigger, AND koşulları ve sonlu bağımlılık grafiği taşır. Koşullar yalnız `event.record.status/entity_type/facts.<alan>` üzerinde exists/equals kullanır. Exists'te value alanı null olur. WorkflowInput ya literal scalar'dır ya da event/context/önceki step sonucuna doğrulanmış referanstır; eval, template kodu veya serbest disk yolu değildir. Referanslar tür ve erişim denetimli olmalı; gelecekteki/bağımsız step çıktısı okunamaz.

İlk eylem kataloğu: link_records, update_task, propose_memory_write, apply_memory_write, propose_provision, apply_provision, verify_readback, request_user_input. Eylem adları var olan ürün API'si değildir. Apply adımları ayrı writer/provisioner geliştirmez; MemoryWriteIntent/ProvisionPlan sınırlarını çağırır. Propose_memory_write aktif ajana döner (`awaiting_agent`); ayrı varsayılan model servisi başlatmaz. Belirsiz içerik veya eksik kullanıcı yetkisi `awaiting_user` üretir. Tanımın onaylanması ilerideki her dosya/erişim değişikliğine peşin onay değildir. Yeni erişim ve dış eylem mevcut kapsamlı plan yetkisine tabidir.

| Eylem | Typed giriş → çıkış / etki |
|---|---|
| propose_memory_write | result RecordRef + isteğe bağlı target/task_id → aktif ajanın kaynaklı MemoryWriteIntent referansı; kendi başına yazmaz. |
| apply_memory_write | intent_ref → ortak writer'ın OperationResult referansı; her parça/readback doğrulanır. |
| update_task | doğrulanmış task hedefi + metadata/relation değişikliği → update_task intent'i aynı writer'a; task gövdesi korunur. |
| link_records | yetkili iki RecordRef + ilişki türü + sahip kayıt → relation_changes taşıyan intent aynı writer'a; ikinci ilişki deposu yok. |
| propose_provision / apply_provision | WorkspaceSpec referansı → saf ProvisionPlan; plan_ref + geçerli authority → D2 OperationResult. |
| verify_readback | intent_ref veya operation_ref → kaynak/indeks/taze sorgu kontrolleri; başarısızsa completed yok. |
| request_user_input | somut eksik alan/soru → awaiting_user; cevap yeni context/intent/plan doğrulamasından geçer. |

WorkflowInput literal/ref, bu türlere ait scalar ID/alanları bağlar; karmaşık niyet/spec/plan nesneleri yetkili yerel referansla çözülür. Her action kendi zorunlu giriş adlarını/türlerini doğrular. Eksik/fazla veya başka yetki nesnesine işaret eden giriş yürütülmez. Fixture'da `result`, `target`, `task_id` ve `intent` bu bağlamanın somut örneğidir.

Event yalnız mevcut kaynak/kayıt değişiminin veya açık manual çağrının doğrulanmış kimliğini taşır. Kaynak dosyasındaki “şimdi bunu çalıştır” metni event değildir. İzinsiz kayıt tetikleyici/evidence olarak da kullanılamaz. Task işleminden veya sync sonrası doğrulanmış değişimden event üretimi C3'ün işi; watcher/daemon/scheduler zorunlu değildir. Kullanıcı zamanlayıcı veya dış mesaj gönderimi istemiş sayılmaz.

Run kimliği canonical `[workspace_id, workflow_id, workflow_revision, event_id]` SHA256'ıdır. Her step anahtarı `run_id/step_id`; event/definition payload'ı aynı anahtarla değişirse collision conflict olur. Tanım hash'i run'a sabitlenir. DAG döngüsü ve aynı causal run'ın kendini yeniden tetiklemesi reddedilir. Tek event içinde tamamlanmamış adımın bağımlısı çalışmaz. Definition değişince eski run sessizce yeni adımlara geçirilmez; kontrollü yeni plan gerekir.

Step niyeti yan etkiden önce dayanıklı yazılır; writer/provisioner idempotency anahtarı aynı step'e bağlıdır. Crash'ten sonra completed receipt'e bakıp kör başarı verilmez: sahip olduğu yan etki ve güncel izin/readback yeniden incelenir. Bekleyen run indeks rebuild/cleanup ile silinmez. İzin iptali veya manuel edit run'ı durdurur; workflow, onay veya CAS kontrolünü geçmez. Partial step sonucu run'ı completed yapmaz. Birden fazla not/task değişikliği tek ACID değildir; C4/D2 alt journal'ları ve run checkpoint'i birlikte uzlaştırılır.

## Yazma, plan hash'i ve sonuçlar

Task değişimi `SyncEngine.update_task` yoluyla yapılır; indexed-only `MemoryStore.update_task` kanonik Markdown yazımı için alternatif değildir. `update_note` C4'te aynı SyncEngine'e eklenecek yeni dar yetenektir; bugün mevcut metodmuş gibi çağrılmaz. Task için body bu sözleşmede boş kalır, mevcut gövde korunur. Create hedefinde expected_revision/hash null; update/link hedefinde gerçek revision/hash zorunludur. Note/task kökleri profil ∩ yerel yetki ∩ mevcut writer sınırıyla belirlenir. Mevcut farklı konumlu task'lar taşınmaz. Create öncesi mevcut not araması ve gerekçe gerekir.

MemoryWriteIntent bağlam/snapshot yetkisini yeniden doğrular. Her değişikliğin evidence source hash'i ve hedef exact hash/revision'ı doğrulanır. Replay aynı niyetle aynı etkidir; aynı key farklı payload conflict'tir. Readback kaynak → indeks → taze sorgu olarak ayrı kaydedilir. `skip` ve `review` yazmayan geçerli kararlardır. Büyük mevcut notun tamamını keyfi yeniden yazmak yerine yalnız gerekçeli değişiklik uygulanır; JSON/YAML/frontmatter, insan MOC bölümü, wikilink/backlink/attachment ve manuel edit korunur.

Canonical JSON: UTF-8, BOM/yeni satır yok; `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)` byte çıktısı. Duplicate JSON key ve NaN/Infinity reddedilir. Dizi sırası anlamlıdır; Unicode içerik normalize edilmez. Bu G0/Python wire kuralıdır; RFC 8785 uygulaması iddiası yoktur. `spec_sha256`, WorkspaceSpec zarfının canonical hash'idir. `plan_sha256`, plan data içinden yalnız plan_id ve plan_sha256 çıkarıldıktan sonraki canonical hash'tir. Görüntüleme zamanı, rastgele ID veya dosya yazım zamanı hash girdisine eklenmez. Gösterilen diff/izin özeti bu hash'e ve exact write-set'e bağlıdır.

Planlama saf okumadır. Fixture planı mevcut kayıtları benimser, descriptor/binding/profile dosyalarının exact UTF-8 bytes'ını sunar; kaynak/Markdown kopyalamaz. Ops create/update ile sınırlıdır; sonraki installer etkileri için typed plan genişlemesi gerekir. Rastgele shell/komut/URL indirme bu sürümün plan op'u değildir. Apply öncesi stale snapshot, before hash, yetki, sahiplik veya plan hash'i değişmişse eski yetki taşınmaz. Recover/revert yalnız sahip olduğu ve beklenen old/new hash'te kalan dosyayı değiştirir. Kullanıcı edit'i korunur.

| Durum / kod | Etki |
|---|---|
| invalid_schema / unsupported_contract | Yan etki yok; bu çağrıyı reddet. Bozuk aktif registry geniş fallback vermez. |
| scope_denied / source_unbound / authority_required | Kaynak veya kalıcı yazma yok; yetkili kapsamda somut next_action. |
| ambiguous_context | Bir odak sorusu; hedef kesinleşene kadar kalıcı yazma yok. |
| stale_source / revision_conflict / plan_changed / idempotency_collision | Conflict; güncel kaydı yeniden oku/değerlendir, force overwrite yok. |
| operation_pending / busy | Aynı hedefte karışık snapshot veya ikinci uygulama yok; recover/retry. |
| requires_ocr / requires_password / missing_dependency / unsupported | Capability açık kalır; içerik bulunmuş gibi davranılmaz. |
| partial / failed / awaiting_agent / awaiting_user | Gerçek parçaları ve sonraki adımı bildir; bütün işi ready/completed yapma. |

`operation_status=applied` + `workspace_health=verification_pending` geçerlidir. Aynı şekilde bir kaynağın ready olması canlı istemci teslimi değildir. CLI bağlandığında normal çıkış kodları: 0 = istenen işlem/plan tamamlandı; 2 = schema/girdi; 3 = conflict/busy/pending; 4 = denied/unbound/eksik yetki; 5 = failed/partial veya uygulanmış işlemin zorunlu doğrulaması tamamlanmadı. JSON içindeki ayrıntı korunur; 0 tek başına ürün kabulü sayılmaz. Bu kodlar yeni CLI'lar için sözleşmedir, mevcut komutların exit kodlarını G0 değiştirmez.

## Migration ve kabul sınırı

Registry yokken V3 korunur. C1 dormant/opt-in okuyucu/validator ile başlar; bütün retrieval/snapshot/passage/companion/bridge/advisor/yazma yollarında ortak policy tamamlanmadan yeni mod etkinleştirilmez. Legacy `project` ve folder mutasyonları C5'te tek truth adapter'ına bağlanır. G0 source/task/frontmatter alanlarını topluca yeniden yazmaz; indeks migration'ı yapmaz. Bilinmeyen workspace/global durum sessizce paylaşılmaz. Şemayı anlamayan eski sürüme downgrade açık restore gerektirir; güvenlik garantisi varsayılmaz.

I1: D gerçek serviste alanı kurar; C kaynak+iş+geçmişle çalışır, mevcut not/task'ı günceller ve yeni sorguda bulur; süreç aynı event'i tekrarsız yürütür. Q1: PDF/DOCX, seçilmiş OCR ve canlı/platform matrisi, holdout custom, scope/freshness/manuel edit/kesinti testleri tamamlanır. G0, bu kabulleri fixture okuyarak geçmiş saymaz. Tolerans: yetkisiz içerik/metadata sızıntısı, özgün kaynak silme ve kullanıcı edit'ini ezme sıfırdır.

Local source roots use the same validation during approval, context/adoption, reading, cataloging and canonical writing. Only validated OS path aliases (macOS /var and Windows spelling aliases) compare as the same root; exact grant locator strings remain unchanged. Parent traversal and additional symlink/junction redirects are rejected.

## v0.1.0 adayındaki çıktı, replay ve görünüm uyumu

Makine JSON çıktısı CLI/okuyucu sınırında UTF-8 bayt olarak üretilir; süreç ortamının veya terminalin kodlaması bu protokolü değiştirmez. Yapılandırılmış sonuç döndüren CLI çağrısı stdout/stderr kodlamasını değiştirmez. İnsan terminal çıktısı kendi terminal politikasını, yönlendirilmiş insan çıktısı belgelenmiş UTF-8 biçimini kullanır. Bu politika kullanıcı kaynaklarını dönüştürme izni değildir; özgün kaynak baytları korunur. Metadata-only bağlantı yazımı CRLF ve eksik son satır sonunu da korur.

Başarılı kanonik işlemden türetilen ortak ana sayfa internal kapsamındadır; private istekten üretilse de private kayıt başlığı, kimliği, yolu veya durumu bu sayfaya aktarılmaz. Kaynak etiketi/locator'ı için de çağrı grant'inin internal yetkisi gerekir. Görünüm yenilemesinin başarısızlığı esas kanonik işlemin tamamlandığı gerçeğini silmez. Bu adayın WorkflowRun şeması completed durumunda next_action için null veya yalnız managed_view_stale: önekli açıklamayı kabul eder; completed/skipped adım zorunluluğu korunur. Bu, önceki adayın completed için yalnız null kabul eden dar koşulundan açık bir uyum farkıdır; frozen fixture baytları değiştirilmemiştir.

Kendi tetikleyici kaydını değiştiren workflow'un tekrarında, eski event/evidence ancak aynı kaydedilmiş request/event/definition/run/step kimliği ve tam MemoryWriteIntent payload'ı ile eşleşen writer journal kanıtıyla kabul edilir. Journal'daki tam before SourceRef, hedef/action kimliği ve revision; güncel yetki/trust denetimi; kaynak baytları ile indeksin exact after hash/revision eşleşmesi birlikte gerekir. done işareti tek başına kanıt değildir. İlişkisiz stale evidence, grant iptali veya dış kullanıcı değişikliği bu yolla aşılmaz.

Tam before-ref/hedef bilgisi olmayan eski günlükler, niyetteki özgün kanıtla eşleşmeyen self-event veya aynı hedefte ardışık birden fazla geçiş otomatik olarak birleştirilmez; mevcut doğrulama hatası korunur ve yeni yetkili inceleme gerekir. Yeni proposal context denetimleri değişmez; recovery için request veya intent payload'ı yeniden yazılmaz.
