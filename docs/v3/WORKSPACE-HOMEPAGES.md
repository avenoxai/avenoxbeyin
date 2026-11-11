# Seçilmiş Workspace bilgilerini ana sayfadan düzenleme

Bu isteğe bağlı ilk aday, mevcut insan notlarındaki seçilmiş `facts` değerlerini
kullanıcının düzenlediği Markdown ana sayfasına bağlar. Kurulum bir alan türü
seçtirmez. Görüşme tarihi metin, tarifteki porsiyon sayısı sayı olabilir.
Yeni servis veya otomatik model çağrısı yoktur.

Kullanıcı mevcut notları ve onlardan göstermek istediği bilgileri seçer. Ajan
seçimi aşağıdaki JSON isteğine yazar ve `python3 beyin.py homepage-create --file
ISTEK.json` çalıştırır. Yeni sayfa için `expected_sha256` `null` olur. Mevcut
insan notuna eklemek için önce `note-read` ile alınan SHA-256 değeri verilir.

```json
{"source":"notes/Başvurularım.md","expected_sha256":null,"title":"Başvurularım","project":"basvurular","selections":[{"source":"notes/Başvuru-Akşam.md","fields":[{"field":"facts.gorusme_tarihi","label":"Görüşme tarihi","editable":true}]}]}
```

Komut her değeri ve kayıt kimliğini güncel esas nottan okur; istek içinde
kimlik, önceki değer veya gizli sayfa işareti verilmez. İşlem `written` veya
değişiklik gerekmiyorsa `unchanged` döner. Aynı isteği yeniden çalıştırmak,
arada kişisel açıklama değişmiş olsa da sayfayı tekrar yazmaz. Başka seçimle
yeniden kurma isteği çakışma verir. Mevcut sayfadaki yazılar korunur; seçilen
bilgiler yeni bir bölümde görünür. Ana sayfanın frontmatter'ında
`workspace_homepage: true` açıkça seçilir.
Proje adı taşımayan mevcut notlar için `project:null` açıkça verilir; farklı
proje veya görünürlükteki notlar aynı sayfaya bağlanmaz. Komut ana sayfayı
oluşturmadan önce kaynakları güncel dizine alır; ayrıca `sync` çalıştırmak gerekmez.
Biçimi geçersiz istek eşitlemeyi başlatmadan reddedilir. Geçerli isteğin
eşitlemesi, önceki işlemlerden kalan toparlamayı da tamamlayabilir.
Sayfadaki kaynak bağlantısı, etiket, satır düzeni ve kişisel not hücrenin dışında
kalır. Kaynak bağlantısı, ana sayfaya göre göreli ve özel karakterleri kodlanmış
Markdown yoludur. Örnek görünen satır:

```markdown
- [Başvuru-Akşam](Ba%C5%9Fvuru-Ak%C5%9Fam.md) · Görüşme (düzenlenebilir): 12 Kasım · Kişisel not: adresi sor
```

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
Sayfa ve kayıt aynı proje ve görünürlükte olmalı; güvenilmeyen veya üretilmiş
kayıt, değişmiş kimlik ve indeks dışı hedef yazılmaz. Salt okunur hücrenin elle
değiştirilmesi kaynak yazımı doğurmaz. Normal not yazımı, esas kayda geri yazarken
frontmatter'ı mevcut JSON biçimine çevirebilir; gövde ve diğer bilgilerin
içeriği korunur. Eşitleme olmayan salt dosya açma olayı tetikleyici değildir.
Obsidian'da değiştirme sonrası olağan eşitleme gerekir. Gerçek editörün eşzamanlı
yazımı mevcut `note-edit` SHA kontrolünün sınırlarına tabidir; çok dosyalı atomik
bir işlem iddiası yoktur.
