# Workspace ana sayfaları: kurmaca kullanım örnekleri

Bu örneklerdeki Mavi Pusula görüşmeleri, dersler, tarihler ve dosyalar tümüyle
uydurmadır. Betik geçici bir kasaya v3 paketini kurar, herkese açık CLI
komutlarıyla örnekleri oluşturur ve sonuçları denetler. Gerçek bir kullanıcı
kasasına dokunmaz.

Depo kökünden çalıştır:

```sh
python3 scripts/example_workspace_homepages.py --output /tmp/workspace-homepage-examples
```

Windows'ta `--output` için kendi geçici klasöründen bir yol seçebilirsin.
Komut, kontrol sonucunu ekrana ve `result.json` dosyasına yazar. Aynı çıktı
klasöründeki Markdown dosyaları akışın okunabilir anlık görünümleridir;
bağlama ait HTML yorumları bu kopyalardan çıkarılmıştır. `vault/` altındaki
kurmaca not ve kaynak kopyalarıyla birlikte taşınabilirler. Görünümdeki
bağlantılar, akışın **son durumundaki** bu dosyalara gider; geçmiş bir adımın
kaynak içeriğini yeniden kurmaz. Betik özgün bağlantıları geçici kasada,
dışa aktarılan bağlantıları çıktı klasöründe doğrular. Geçici kurulu çalışma
ortamı dışa aktarılmaz.

## Kariyerim: seçilmiş bilgiyi iki yönde düzenleme

Kurmaca Mavi Pusula'nın **Akşam** ve **Gündüz** kayıtları ayrı esas notlardır.
Her ikisi de önce **6 Nisan 2099** görüşme tarihini taşır; davet metni ayrı
bir özgün dosyadır. `homepage-create`, kullanıcının seçtiği `facts.gorusme`
alanlarını **Kariyerim** sayfasına bağlar. Her kaydın bağlantısı ana sayfaya
göre görelidir. Görüşme tarihi düzenlenebilir hücrede, kişisel not hücrenin
dışındadır.

![Kariyerim sayfasının genel Markdown önizlemesi](career.png)

*Kariyerim: üretilen Markdown'ın genel önizlemesi; gerçek Obsidian ekran görüntüsü değildir.*

Örnek akışı şu anlık görünümlerden izle:

| Dosya | Görülen adım |
| --- | --- |
| [career-created.md](career-created.md) | İki kayıtla oluşturulan sayfa ve kişisel not |
| [career-from-source.md](career-from-source.md) | Akşam esas notunda tarih **7 Nisan** olunca ilgili satırın yenilenmesi |
| [career-from-homepage.md](career-from-homepage.md) | Sayfada Akşam tarihi **9 Nisan** olunca esas nota geri yazılması |
| [career-recovered.md](career-recovered.md) | **11 Nisan** kaynak yazımından sonraki kesintinin toparlanması |
| [career-conflict.md](career-conflict.md) | Akşam alanındaki **12/13 Nisan** çakışması; Gündüz satırının ilerlemesi |
| [career-resolved.md](career-resolved.md) | Akşam için **12 Nisan** değeri seçilince yakınsama |

Kaynak nottaki düzenleme sayfaya, sayfadaki düzenlenebilir hücre ise doğru
kaynak nota geçer. Diğer kayıt ve özgün davet dosyası korunur. Betik, kaynak
yazıldıktan sonra eşitlemeyi yalnız örnek amaçlı bir kancayla keser. Sonraki
`sync`, kaynağı yeniden yazmadan sayfayı tamamlar; arada güncellenen en son
kişisel notu korur. Gerçek ürünün bir kesinti kancası yoktur.

Akşam tarihi esas notta **12 Nisan**, sayfada **13 Nisan** yapılınca çakışma
yalnız bu alan için bildirilir. Gündüz tarihi aynı eşitlemede **8 Nisan** olur.
Örnek, Akşam hücresine kaynak değeri olan **12 Nisan** yazıp tekrar eşitler.
Değişmeyen Markdown üzerinde tekrar `sync` çalıştırmak hem dosya baytlarını
hem de değiştirilme zamanını korur.

## Derslerim: genel gezinme sayfası

**Bulut Bahçeleri**, **Ses Haritaları** ve **Işık Atölyesi** için uydurma ders
notları, kaynak metinleri ve çalışma dosyaları oluşturulur. Genel sayfa ders
notlarına, özgün kaynaklara ve ödev dosyalarına göreli bağlantılar verir.
Bir dosyanın varlığı açık görev veya teslim yükümlülüğü anlamına gelmez.

![Derslerim sayfasının genel Markdown önizlemesi](lessons.png)

*Derslerim: üretilen Markdown'ın genel önizlemesi; gerçek Obsidian ekran görüntüsü değildir.*

[lessons-homepage.md](lessons-homepage.md) genel gezinme sayfasını,
[lessons-course.md](lessons-course.md) Bulut Bahçeleri notunu gösterir. Bu
örnek normal `note-create` ve `note-edit` akışını kullanır. Doğrulanmış teslim
tarihi, ilerleme veya seçilmiş çalışma sırası olmadığı için bunları üretmez;
kişisel not yalnız verilen metinle güncellenir. Özgün dosyalar korunur.

## İstek örneği

Aşağıdaki konuşma **yalnızca açıklayıcı bir taslaktır**; betik bir ajanın
kullanıcıyla konuşmayı yorumlayıp işlem yaptığı uçtan uca sınama değildir.

> **Kullanıcı niyeti:** “Akşam ve Gündüz görüşmelerimin tarihlerini Kariyerim
> sayfasında göreyim ve oradan düzeltebileyim. Kendi notumu da ekleyeyim.”
>
> **Önerilen taslak:** “İki mevcut kaydın görüşme tarihi alanını sayfaya
> bağlayayım. Tarihleri esas notlarla eşitleyeyim; kişisel notunu sayfada
> tutayım.”

Kurulumun kapsamı ve `homepage-create` istek biçimi için
[özellik belgesine](../../WORKSPACE-HOMEPAGES.md) bak.
