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
Proje adı taşımayan mevcut notlar için `project:null` açıkça verilir; farklı
`project` metadata'sına veya görünürlüğe sahip notlar aynı sayfaya bağlanmaz.
Klasörlerden gelen etkin proje kapsamına ilişkin açık eksik aşağıda belirtilmiştir.
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
yalnız o bilgiyi, iki değeri ve önceki ortak değeri döndürür; diğer bilgiler
ilerler. Kullanıcı hangi değeri seçtiğinde, yalnız bu hücre veya esas kayıttaki
bilgi seçilen değere getirilir ve normal eşitleme çalıştırılır.

Kesinti kaynak yazımıyla sayfa yazımı arasına gelirse hücredeki önceki ortak
değer korunur. Sonraki eşitleme kaynakla sayfanın aynı yeni değerde olduğunu
görür, kaynağı tekrar yazmadan hücreyi tamamlar. Aradaki son kişisel not korunur.
Tekrar eşitleme değişmemiş Markdown dosyalarını yazmaz.

İlk adayın sınırları: yalnız mevcut tek satırlık skaler `facts` değerleri ve
insan notları; görevler, listeler ve nesneler ayrı yazıcı davranışı gerektirir.
Sayfa ve kayıt aynı `project` metadata'sını ve görünürlüğü taşımalı; üretilmiş
kayıt, değişmiş kimlik ve indeks dışı hedef yazılmaz. Güven denetiminin bazı
işaretleri eksiktir; aşağıdaki açık bulgulara bakın. Salt okunur hücrenin elle
değiştirilmesi kaynak yazımı doğurmaz. Normal not yazımı, esas kayda geri yazarken
frontmatter'ı mevcut JSON biçimine çevirebilir; gövde ve diğer bilgilerin
içeriği korunur. Eşitleme olmayan salt dosya açma olayı tetikleyici değildir.
Obsidian'da değiştirme sonrası olağan eşitleme gerekir. Gerçek editörün eşzamanlı
yazımı mevcut `note-edit` SHA kontrolünün sınırlarına tabidir; çok dosyalı atomik
bir işlem iddiası yoktur.

## Açık inceleme bulguları

Bu sürümde aşağıdaki hatalar henüz düzeltilmedi. Mevcut
başarılı test kapsamı bu açıkları kapatan bir güvenlik kabulü değildir.

- **P1 · Güven işaretleri:** Ana sayfa kapısı yalnız `trust: untrusted`
  denetliyor. Depodaki diğer güvenilmeyen kayıt işaretleri (`trusted: false`,
  `status: untrusted`, `kind: untrusted`) hem kaynakta hem sayfada aynı biçimde
  reddedilmeli. Mevcut denetim bu değerlerle kabul veriyor; güvenilmeyen bilgi
  güvenilir sayfaya taşınabilir.
- **P1 · Etkin proje kapsamı:** Ana sayfa ile kaynağın yalnız `project`
  metadata'sı karşılaştırılıyor. Metadata'sı boş iki not, klasör eşlemelerinde
  farklı projelere ait olabilir. Oluşturma, tekrar ve yenileme mevcut
  `record_scope` kuralını, engellenmiş klasörler dahil, kullanmalı.
- **P2 · Anahtarlı yazma tekrarı:** Düzenlenebilir ana sayfada `idempotency_key`
  içeren bir `note-edit` sonrası aynı istek arada kullanıcı yazımı olmadan
  çakışabiliyor. Sayfanın hücre tabanı yenilenirken son hash sonuçta değişiyor,
  tamamlanma kaydı önceki hash'te kalıyor. Son doğrulanmış hash kaydedilmeli;
  gerçek araya giren kullanıcı düzenlemeleri yine korunmalı.
