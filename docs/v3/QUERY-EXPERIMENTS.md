# Sorgu değişikliğini ölçmek

Bir sorgudaki dolgu sözcüklerini çıkarmak ile arama motorunun stopword listesini
değiştirmek aynı deney değildir. #679'daki 48/54 → 52/54 saha sonucu bir **sorgu
yeniden yazımıdır**; burada tekrarlandığı veya tüm kasalara genellendiği iddia edilmez.

## Aynı korpus ve etiketlerle iki sorguyu karşılaştırma

Repo kökünde, yalnız Python 3.11+ ile:

```sh
python scripts/compare_v3_queries.py --fixture tests/fixtures/v3/turkish_query_pairs.json --variants tests/fixtures/v3/turkish_query_variants.json
```

- `fixture`, `evaluate_v3_search.py` biçimindeki sentetik veya hassas bilgilerden
  arındırılmış `records` ve `cases` listelerini içerir. Her sorunun tek bir
  `target_id` etiketi vardır.
- `variants`, `{"variants": [{"id": "soru-kimliği", "query": "yeni sorgu"}]}`
  biçimindedir. **Bütün sorular** tam bir kez bulunmalıdır; değiştirmediğiniz
  kontrol sorularını da aynı metinle yazın. Eksik/ek/yinelenen kimlik, boş sorgu
  veya etiketi değiştirme girişimi reddedilir. Böylece yalnız iyileşen soruları
  seçerek bir karşılaştırma üretilemez.
- İki kol aynı geçici korpus ve hedef etiketleriyle çalışır. Runtime, kayıt
  tokenizasyonu, kalıcı ayarlar veya canlı kasa değiştirilmez. Model çağrısı yoktur.
- `non_strict`, normal not düzeyi aramayı; `context`, ortak strict passage
  bağlam yolunu ölçer. Bunlar ayrı raporlanır. Bu, gerçek hook teslimi veya Jev
  kararlarının ölçümü değildir; kurulu kasanın ayarlarını kopyalamaz.
- JSON her soru için önce/sonra hedef sırasını, iyileşen/gerileyen/değişmeyen
  durumunu; toplamda recall@1/3/5, MRR ve farklarını içerir. Tek hedef etiketi
  **precision** ölçmez: diğer dönen notların alakasız olduğu varsayılmaz.
- Zamanlar tanısaldır. İlk kol önbelleği ısıtır; bu deneyden hız üstünlüğü çıkarılmaz.

Çıktı sorguları ve kaynak adlarını içerir. Özel kasadan türetilen fixture,
varyant ve sonuçları herkese açık issue/PR'a koymayın. Komut sonuçları yalnız
stdout'a yazar; dosyaya kaydetmek isterseniz özel bir konuma yönlendirin.

### Eklenen küçük örneğin sınırları

`turkish_query_pairs.json` v1: 10 sentetik not, 6 İngilizce konuya yöneltilmiş
Türkçe dolgu sorusu ve 4 değiştirilmemiş kontrol. `var`, “Hakkında sayfası”,
“ilgi/konu” ve yalnız dolgu içeren sorgu bilinçli olarak korunur. Bunlar gerçek
konu veya başlık olabilir; kör bir sözcük silme kuralı değildir. Fixture ve
varyantlar sonuçlara bakılmadan sabitlenmiştir; ayrı bir holdout seti değildir.

`main@3841217` üzerinde bu örnekte normal aramanın recall@1 değeri **0.9 → 0.9**,
strict bağlamınki **0.4 → 1.0** çıktı. Normal aramada raporlanan saha bozulmasını
bu küçük korpus yeniden üretmedi. Strict sonuçtaki artış da üretim davranışını
değiştirmek için tek başına yeterli değildir. Testler belirli bir kazancı
zorunlu kılmaz; hesaplama ve eşleştirme sözleşmesini denetler. Mevcut dondurulmuş
semantik/search fixture'ları değişmez.

## Kendi kasanızda mevcut salt-okunur ölçüm

#675'in istediği kurulu `selftest` komutu, otomatik sonuç geçmişi veya güncelleme
kapısı henüz yok. Kaynak repo üzerinden mevcut passage değerlendirmesi kullanılabilir:

```sh
python scripts/evaluate_v3_passages.py --vault /kasanizin/yolu --questions /ozel/konum/sorular.json
```

Windows'ta da tam yolları tırnak içine alabilirsiniz. Soru dosyasının biçimi:

```json
{
  "questions": [
    {"query": "örnek proje yedek sırası", "source": "knowledge/ornek.md", "sentence": "Yedek önce doğrulanır."}
  ],
  "controls": {"unrelated": ["Bugün kahve içelim mi?"]}
}
```

`source` kasa içindeki gerçek göreli yol, `sentence` nottaki gerçek cevap
cümlesidir. Önce 10–20 temsilî soruyu ve ilgili notlarını etiketleyin; sonuçlara
bakarak yalnız başarılı soruları seçmeyin. Bu betik not ve passage yollarını
ayrı karşılaştırır: `recall` kaynak teslimini, `answerable` ayrıca cevap
cümlesinin teslim edilen metinde yer almasını, `noise` kontrol sorgularında
herhangi bir kayıt teslimini ölçer. Kullanıcının mevcut indeksini/ayarlarını
ölçmek yerine geçici runtime oluşturur; gerçek kasa veya kalıcı state yazılmaz.
Önceki raporu kendiniz saklayın; aynı sorular/korpus/ayarlar olmadan iki sayıyı
“sürüm iyileşmesi” olarak yorumlamayın.
