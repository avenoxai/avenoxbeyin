# Seçilmiş Workspace bilgilerini ana sayfadan düzenleme

Bu isteğe bağlı ilk aday, mevcut insan notlarındaki seçilmiş `facts` değerlerini
kullanıcının düzenlediği Markdown ana sayfasına bağlar. Kurulum bir alan türü
seçtirmez. Görüşme tarihi metin, tarifteki porsiyon sayısı sayı olabilir.
Yeni servis veya otomatik model çağrısı yoktur.

[Sentetik kullanım örnekleri](examples/workspace-homepages/README.md), Kariyer
akışının kurulmasını ve iki yönde düzenlenmesini; genel Dersler sayfasında ise
kaynak, ödev dosyası, doğrulanmış teslim, plan ve kişisel not ayrımını gösterir.
İkincisi normal not yazıcılarını kullanır; olmayan `facts` değerleri üretilmez.
Çalıştırılabilir örnekler CLI kabulüdür. Hazır konuşma taslağı, bağımsız bir
ajanın konuşmayı yorumlayıp kurulum yaptığı uçtan uca kabul değildir.

Kullanıcı mevcut notları ve onlardan göstermek istediği bilgileri seçer. Ajan
seçimi aşağıdaki JSON isteğine yazar ve `python3 beyin.py homepage-create --file
ISTEK.json` çalıştırır. Yeni sayfa için `expected_sha256` `null` olur. Mevcut
insan notuna eklemek için önce `note-read` ile alınan SHA-256 değeri verilir.

```json
{"source":"notes/Kariyer/Ana Sayfa.md","expected_sha256":null,"title":"Kariyerim","project":"kariyer","selections":[{"source":"notes/Kariyer/Aksam.md","fields":[{"field":"facts.gorusme","label":"Görüşme tarihi","editable":true}]},{"source":"notes/Kariyer/Gunduz.md","fields":[{"field":"facts.gorusme","label":"Görüşme tarihi","editable":true}]}]}
```

Komut her değeri ve kayıt kimliğini güncel esas nottan okur; istek içinde
kimlik, önceki değer veya gizli sayfa işareti verilmez. İşlem `written` veya
değişiklik gerekmiyorsa `unchanged` döner. Aynı isteği yeniden çalıştırmak,
arada kişisel açıklama değişmiş olsa da sayfayı tekrar yazmaz. Başka seçimle
yeniden kurma isteği çakışma verir. Mevcut sayfanın başlığı ve yazıları önde
korunur; seçilen bilgiler gövdenin sonuna yeni bir bölüm olarak eklenir. Yeni
sayfa istenen başlıkla açılır. Ana sayfanın frontmatter'ında
`workspace_homepage: true` açıkça seçilir.
Proje alanı taşımayan sayfa için `project:null` açıkça verilir; bu istek değeri
notun frontmatter'ına `null` alanı eklemez. Sayfa ve kaynak aynı etkin proje
kapsamında ve görünürlükte olmalıdır. Etkin kapsam, dolu `project` alanından,
yoksa mevcut klasör eşlemelerinden gelir. Açık alan önceliği, en uzun klasör
eşleşmesi ve engellenmiş klasör kuralları mevcut `record_scope` ile aynıdır.
İki taraf da gerçekten kapsamsızsa bağlanabilir; `shared_unscoped` başka
projedeki bir kaynağı ana sayfaya bağlama izni vermez.
Komut ana sayfayı oluşturmadan önce kaynakları güncel dizine alır; ayrıca
`sync` çalıştırmak gerekmez.
Biçimi geçersiz istek eşitlemeyi başlatmadan reddedilir. Geçerli isteğin
eşitlemesi, önceki işlemlerden kalan toparlamayı da tamamlayabilir.
Sayfadaki kaynak bağlantısı, etiket, satır düzeni ve kişisel not hücrenin dışında
kalır. Kaynak bağlantısı, ana sayfaya göre göreli ve özel karakterleri kodlanmış
Markdown yoludur. Her kaynak, anlaşılır başlığıyla bir kez gösterilir; seçilen
bilgileri ayrı satırlarda, kişisel notu ayrı paragrafta yer alır. Kaynak başlığı
önce notun `title` özelliğinden, yoksa gövdenin ilk satırındaki H1 başlığından,
o da yoksa dosya adından alınır. Örnek okuma görünümü (hücre yorumları gizli):

```markdown
# Kariyerim

## Seçilen bilgiler

Düzenlenebilir değerleri bu notta değiştirebilirsin; değişiklikler olağan eşitlemede esas kayda işlenir. Kişisel notların yalnız bu sayfada kalır.

### [Mavi Pusula — Akşam](Aksam.md)

- **Görüşme tarihi:** 6 Nisan 2099 _(düzenlenebilir)_

**Kişisel notum:** yol süresini ve adresi sor

### [Mavi Pusula — Gündüz](Gunduz.md)

- **Görüşme tarihi:** 6 Nisan 2099 _(düzenlenebilir)_

**Kişisel notum:**
```

Bu görünümde kişisel not normal `note-edit` ile eklenmiştir. Salt okunur bilgi
göstermek için seçimde `editable:false` kullanılır; örnekte iki tarih de
düzenlenebilir seçilmiştir. [Adım adım örnek](examples/workspace-homepages/README.md),
esas notta 6→7 Nisan, sayfada 7→9 Nisan değişikliğini; kesinti, son kişisel not,
dar çakışma ve yazmasız tekrarı ayrı anlık görünümlerle gösterir.

Başlık ve etiketlerdeki Markdown özel işaretleri düz metin olarak gösterilir.
Bu düzen standart Markdown kullanır; ayrı tema, eklenti veya web uygulaması
gerektirmez. Önceden oluşturulmuş sayfalar aynı isteğin tekrarında yeniden
biçimlendirilmez; kullanıcının seçtiği mevcut düzen korunur.

Hücre, değerin hemen çevresindeki HTML yorumlarında kaynak yolu, okunan kayıt
kimliği, bilgi adı, önceki ortak değer ve düzenleme seçimini saklar. Kullanıcı
bunları düzenlemez. Seçim yapılmamış sayfa otomatik bağlanmaz. Yorumlar Markdown
okuma görünümünde gizlidir; kaynak modunda görünür. Henüz bir görsel kurucu yoktur.

`note-edit` ile esas kayıt veya ana sayfa yazıldıktan sonra ve olağan `sync`
sırasında güncel dosyalar yeniden okunur. Kişisel metin güncelleme aralığına
alınmaz. Esas kayıt yazımı mevcut kilit, SHA-256 kontrolü, yedek ve indeks geri
okumasını kullanır. Farklı bilgiler birleşir. Aynı bilgi iki tarafta aynı değere
getirildiyse seçim gerekmez. İki farklı değer varsa `workspace_homepages.conflicts`
`reason:choose_value` ile yalnız o bilgiyi, iki değeri ve önceki ortak değeri
döndürür; diğer bilgiler ilerler. Kullanıcı seçtiği değeri yalnız bu hücreye
veya esas kayıttaki bilgiye yazar ve normal eşitleme çalıştırılır.

Salt okunur hücre elle değiştirildiyse `reason:read_only_field`, kaynak ve
sayfa değeri döner; bu yanıtta `base` bulunmaz. Kaynak yazılmaz; sayfa hücresi
güncel kaynak değerine geri getirilir. Normal `note-edit` sonucunun üst düzey
`written` olması yalnız not yazımını doğrular. İlgili hücrenin eşitlendiğini
bildirmeden önce `workspace_homepages.conflicts` ve `warnings` alanlarını ve
güncel değeri kontrol edin.

Oluşturma sırasında eski SHA için sayfayı yeniden okuyup isteği güncelleyin.
Farklı seçimle zaten bağlı sayfayı yeniden oluşturmak desteklenmez; aynı
isteği tekrar denemek bunu çözmez. Mevcut seçimleri koruyun ve ayrı sayfa gibi
desteklenen bir yol seçin. Aynı seçimle tekrar eski sayfayı yeniden biçimlendirmez.

Kesinti kaynak yazımıyla sayfa yazımı arasına gelirse hücredeki önceki ortak
değer korunur. Sonraki eşitleme kaynakla sayfanın aynı yeni değerde olduğunu
görür, kaynağı tekrar yazmadan hücreyi tamamlar. Aradaki son kişisel not korunur.
Tekrar eşitleme değişmemiş Markdown dosyalarını yazmaz.

İlk adayın sınırları: yalnız mevcut tek satırlık skaler `facts` değerleri ve
insan notları; görevler, listeler ve nesneler ayrı yazıcı davranışı gerektirir.
Bir ana sayfa başka ana sayfanın esas kaynağı seçilemez; doğrudan esas insan
notuna bağlanın. Önceden kalmış veya elle hazırlanmış iç içe bağlar da okuma
sırasında güvenli biçimde dışlanır.
Sayfa ve kayıt aynı etkin proje kapsamını ve görünürlüğü taşımalı; üretilmiş
kayıt, değişmiş kimlik ve indeks dışı hedef yazılmaz. `trust:untrusted`,
`trusted:false`, `status:untrusted` ve `kind:untrusted` hem kaynakta hem sayfada
oluşturma, tekrar ve eşitlemede reddedilir. Salt okunur hücrenin elle
değiştirilmesi kaynak yazımı doğurmaz. Normal not yazımı, esas kayda geri yazarken
frontmatter'ı mevcut JSON biçimine çevirebilir; gövde ve diğer bilgilerin
içeriği korunur. Eşitleme olmayan salt dosya açma olayı tetikleyici değildir.
Obsidian'da değiştirme sonrası olağan eşitleme gerekir. Gerçek editörün eşzamanlı
yazımı mevcut `note-edit` SHA kontrolünün sınırlarına tabidir; çok dosyalı atomik
bir işlem iddiası yoktur.

## Kaynak politikası sonradan daralırsa

Bağlı kaynak izleri indeksleme ve okumada yeniden doğrulanır; ana sayfanın
kendi dosyası değişmemiş olsa da önceki kopya eski izinle kullanılamaz. Kaynak
gizli, güvenilmeyen, başka veya engellenmiş proje kapsamında, eksik ya da
doğrulanamayacak kadar eskiyse bağlı sayfa arama ve bağlamdan çıkarılır. Bu
tüm sayfaya uygulanan daraltmadır; başlık, gövde ve kişisel not dosyada kalır.
Kaynak tekrar uygun hale gelip eşitlenince sayfa yeniden seçilebilir.

Kaynak yalnız `remote_allowed:false` olmuşsa doğrulanan sayfa yerel aramada
kalabilir, fakat ana sayfanın başlığı ve alıntıları da uzak bağlama gönderilmez.
Kaynağın politikası henüz indekslenmemişse sayfa doğrulanana kadar yerel
okumadan da çıkarılır. İki yönlü eşitleme dışarı gönderim politikasını da
karşılaştırır; farklı politikalar arasında yeni değer aktarılmaz. Yerel
eşitlemeyi sürdürmek için kullanıcı ana sayfasını da normal not yazıcısıyla
`remote_allowed:false` yapabilir; komut kaynağın politikasını gevşetmez.

Bu kısıt indeks, arama, kaynak anlık görüntüsü, geçmiş ve Jev'in manuel/otomatik
bağlam seçimlerinde uygulanır. Jev inceleme ve yanıt doğrulama dahil gerçek
gönderimden önce, paketleme ve kuyruk sonrasında bağlı kaynak hash'leri ve
kapsam yeniden kontrol edilir. Bir kontrolün geçmesi sonraki politikayı
kalıcı olarak yetkilendirmez.

## Regresyon doğrulaması ve açık kabul sınırları

`tests/v3_homepage_boundaries_test.py` normal ve temiz kurulu CLI'da dört
güven işaretini, kaynak/sayfa kapılarını, oluşturma/tekrar/iki yönlü eşitlemeyi,
klasör kapsamı değişimini ve engellenmiş klasörleri sınar. Eşit etkin kapsam
ve açık alan önceliği çalışmaya devam eder. Önceki aday bu regresyonlarda
başarısızdı; düzeltme aynı testlerle doğrulanır.

Anahtarlı `note-edit`, hücre tabanı yenilendikten sonraki son hash'i yazıcı
kilidi altında tamamlanma kaydına alır. Aynı istek arada kullanıcı yazımı yoksa
baytları ve mtime'ı koruyarak başarıyla tekrarlanır. Gerçek kullanıcı yazımı
sonrasında aynı anahtar yeniden uygulanmaz, çakışma döner. Kesintiden sonraki
ilk toparlanmada kaynak için de bayt ve mtime korunumu ayrıca ölçülür.

`tests/v3_homepage_policy_test.py`, [kaynak politikası incelemesindeki](https://github.com/avenoxai/avenoxbeyin/pull/647#issuecomment-6826486112)
önceden kopyalanmış değer örneğini eski adayda başarısız olarak üretir. Normal
ve kurulu sürümde politika daralması öncesi/sonrası yerel arama, indeks,
kaynak anlık görüntüsü ve manuel/otomatik uzak kartlar sınanır. Dört Jev
gönderim yolunda gerçek istek oluşturulurken veya gönderim kuyruğunda politika
daraltılır; sentetik sağlayıcı aktarımına sıfır çağrı beklenir. Bunlar gerçek
yerel dosyalar ve ürün koduyla, dış sağlayıcı yerine test aktarımıyla çalışır.

Bu kontroller konuşmalı otomatik kurulumun veya gerçek Obsidian arayüzünün
uçtan uca kabulü değildir. Çok dosyalı atomik işlem garantisi yoktur. Platform
CI sonucu ilgili son commit için ayrıca değerlendirilmelidir.
