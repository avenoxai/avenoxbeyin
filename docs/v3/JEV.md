# İsteğe bağlı Jev danışmanı

Normal `context`, lifecycle hook'ları ve otomatik işler yerel kalır. Bu eklenti **varsayılan kapalıdır**. Jev bellek kaydetmez, kullanıcı onayı vermez veya kaynak/proje sınırını genişletmez.

## Açık etkinleştirme

Kurucu bu eklentiyi sormaz ve kurmaz. Açmak, kapatmak ve durumu görmek için kurulu kasada:

```sh
python beyin.py jev status
python beyin.py jev shadow
python beyin.py jev on
python beyin.py jev off
```

Kaynak depodan aynı komut: `python scripts/beyin_v3.py --vault /path/to/vault --state /path/to/state jev status`.

`status` yalnız okur; `off|shadow|on` ayarı yazıp sonucu ve `"changed": true` döndürür. Komut kasa indeksine veya kaynak senkronizasyonuna ihtiyaç duymaz. Anahtar kabul etmez ve yazdırmaz; `--key` benzeri bir seçenek bilerek yoktur.

Özellikler ayrı ayrı açılıp kapanır; `--enable` ve `--disable` tekrarlanabilir ve `status` ile birlikte kullanılamaz:

```sh
python beyin.py jev on --enable auto_context
python beyin.py jev shadow --disable answer
```

| Özellik | Kullanan komut | Varsayılan |
| --- | --- | --- |
| `context` | `context --jev` | açık |
| `review` | `jev-review`, `jev-memory` | açık |
| `answer` | `jev-answer` | açık |
| `auto_context` | her turdaki hook yolu; [aşağıda](#her-turda-otomatik-bağlam-auto_context), açıkça etkinleştirilmedikçe kapalı | kapalı |

Kapalı bir özelliğin amacı çağrılırsa istemci anahtar okumadan ve ağa çıkmadan `off` modu ile `feature_disabled` teşhisi döndürür.

`auto_context` açıkken `status` çıktısında `automatic_model_calls: true` olur; komut buna, her turda istemin ve en fazla 8 aday `internal`/`public` notun başlığı ile ilk 600 karakterinin sağlayıcıya gittiğini söyleyen bir `notice` ekler. `private` notlar gönderilmez. Mod `off` değilken anahtar yoksa `warning` eklenir ve çağrılar yerel sonuca düşer.

### Acil kapatma

`BEYIN_JEV_DISABLE=1` ortam değişkeni veya state dizinindeki `jev.disabled` dosyası, kayıtlı modu değiştirmeden çağrıları durdurur. `status` bu durumda `mode: "off"`, eski değeriyle `saved_mode` ve `kill_switch: true` gösterir. Değişken kaldırılınca veya dosya silinince kayıtlı moda dönülür.

### Çağrı kaydı

Her çağrı state dizinindeki `jev-calls.jsonl` dosyasına tek satırlık sayaç yazar: amaç, mod, sağlayıcı (`provider`), önbellek isabeti, `degraded`, hata kodu, gecikme, giriş token sayısı, HTTP istek sayısı (`http_requests`) ve zaman. Sorgu metni, aday metni, yanıt içeriği ve anahtar yazılmaz. Dosya sınırı aşarsa eski yarısı atılır. `jev status` ve `doctor` son 24 saati buradan özetler.

### Elle düzenleme

`jev.json` hala elle düzenlenebilir; `timeout`, `max_candidates`, `base_url`, `env_file` gibi ileri anahtarlar komut yazarken korunur. Komut bu dosyanın tek yazıcısıdır: 0600 izinle atomik yazar. Dosya bozuksa komut hata döndürür ve dosyaya dokunmaz.

```json
{"mode":"shadow","model":"jev-1.13.0","provider":"typesafe","timeout":3,"max_candidates":32,"max_questions":96,"max_input_chars":24000,"cache_ttl":3600}
```

`TYPESAFE_API_KEY` ortam değişkeninde olmalıdır. Anahtarı Markdown'a, sürüm kontrolüne veya komut argümanına yazmayın. `env_file` kullanılırsa mutlak yol gerekir. Sağlayıcı URL'si `base_url` veya `TYPESAFE_BASE_URL` ile seçilir; HTTPS ve yerel HTTP desteklenir. Yerel adreslere (`localhost`, `127.0.0.1`, `::1`) giden çağrılar `HTTP_PROXY`, `ALL_PROXY` veya sistem proxy ayarlarını kullanmaz; uzak HTTPS adresleri tanımlı proxy üzerinden gitmeye devam eder. Vercel uyumlu bir köprü kullanılıyorsa `provider: "vercel"` seçilebilir; bu bir Vercel hesap/anahtar kurucusu değildir. Anahtarsız, yerelde çalışan açık ağırlıklı model için [Laya bölümüne](#yerel-laya-arka-ucu-isteğe-bağlı) bakın.

```sh
python scripts/beyin_v3.py --vault /path/to/vault --state /path/to/state context "kısa notlar" --project demo --jev
```

Kurulu kasada karşılığı: `python beyin.py context "kısa notlar" --project demo --jev --json`. `--json`, danışman teşhislerini de gösterir. İnceleme için `python beyin.py jev-review --project demo --file proposal.json --json` kullanın.

`--jev` ve açık proje kimliği birlikte gerekir. Bu çağrı yerel aramanın en fazla 16 adayından uzaktan değerlendirmeye izin verilenlerin başlıklarını (160 karakter), metin kesitlerini (800 karakter), proje/durum/tarih bilgisini ve sorguyu gönderir. Son çıktıdaki kayıt sayısı ayrı bir sınırdır. `internal` görünürlük de gönderilebilir; yalnız kamu metni için `--audience public` kullanın. `private`, kapsam dışı, güvenilmeyen, değişmiş ve superseded kayıtlar gönderilmez. Yerel sır örüntüsü eşleşirse çağrı yapılmaz; örüntü taraması tüm hassas bilgileri tanıma garantisi değildir.

- `off`: ağ/anahtar/cache erişimi yok.
- `shadow`: puanlar `jev` alanında; yerel sonuç ve sıralama korunur.
- `on`: aday havuzundan seçim yapılır; ilk yerel çıktıya sığmayan ilgili aday alınabilir. Noktalı virgülle ayrılmış en fazla üç alt istek ayrı değerlendirilir. Karakter bütçesi değişmez. Yerel aday aramasının hiç bulmadığı kaydı Jev bulamaz.

`remote_allowed: false` veya `sensitivity: sensitive` kaynak metaverisi, başlık dahil kaydın Jev'e gönderilmesini engeller. `remote_allowed` için metin değil JSON/YAML boolean kullanın. Yerelde uygun bulunan böyle kaynaklar, uzakta seçilenlerle dönüşümlü paketlenir; bütün kotayı baştan kaplayamazlar. `aliases: ["ses ofseti", "audio offset"]` yerel aday aramasını genişletir; izin kapılarını aşmaz.

Çağrıdan sonra kaynak hashleri, indeks revizyonları ve erişim koşulları yeniden kontrol edilir. Değişim varsa puan atılır ve güncel yerel sonuç kullanılır. Timeout, 429, eksik anahtar veya bozuk yanıt yerel sonuçları engellemez. Teşhislerde ham servis hataları bulunmaz.

## Kaydedilmemiş bilgi adayını inceleme

Önce normal `context` ile kaynak kaydının `id` ve `source_sha256` değerlerini alın. Ayrı bir JSON dosyası hazırlayın:

```json
{
  "status":"proposed",
  "project":"demo",
  "claim":"Demo projesinde kısa notlar tercih ediliyor.",
  "evidence":[{
    "record_id":"demo-notlar",
    "source_sha256":"KAYNAK_DOSYANIN_GUNCEL_SHA256_DEGERI",
    "quote":"Demo için kısa notlar kullanalım."
  }]
}
```

```sh
python scripts/beyin_v3.py --vault /path/to/vault --state /path/to/state jev-review --project demo --file proposal.json
```

1–8 kanıt, tam alıntı, aynı proje ve güncel hash zorunludur; alıntı hem indeks metninde hem gerçek kaynakta bulunmalıdır. Kaynak senkronizasyonu eksikse inceleme durur. Yanıt her zaman `approved: false`, `memory_written: false` içerir. Sonuç yalnız iddianın alıntıyla desteklenme puanıdır; başka kartlarla otomatik birleştirme/çelişki çözümü yapmaz. Ayrı inceleyen kişi/ajan kapsam ve zaman sınırını değerlendirir, gerekirse mevcut `note-create` akışını kullanır.

## Kaynaklı hafıza değerlendirmesi (`jev-memory`)

`review` özelliğini kullanır ve aynı `proposal.json` biçimini kabul eder:

```sh
python beyin.py jev-memory --project demo --file proposal.json --json
```

İsteğe bağlı `prior_record_ids: ["eski-karar"]` alanıyla en fazla dört eski kayıt
belirtilebilir. Alan yoksa aynı projedeki uygun yerel adaylardan seçilir; boş liste
eski kayıt karşılaştırmasını kapatır. Bir istekte üç bağımsız Choice sorusu
(destek, kesinlik/niyet, bilgi türü) ve eski kayıt başına bir ilişki sorusu sorulur.
İlişkiler: tekrar, ayrıntılandırma, değişen karar, çelişki veya ilgisiz kayıt.

Herhangi bir cevap düşük güvenliyse, kaynak iddiayı desteklemiyorsa, ifade kesin
değilse veya eski kayıtla çelişiyorsa `route: inspect_sources` döner. Değişen karar
için kaynak tarihleri ayrıca kodda kontrol edilir. Uygun aday bile yalnız
`candidate_for_agent_review` olur; `approved`, `memory_written`, `task_created`
ve `task_completed` her zaman `false` kalır. Aktif ajan kaynağı inceler ve mevcut
yetkisi kapsamında normal kayıt akışını kullanır.

İddia aynı proje ve kitle sınırında reddedilmiş bir `inference` veya `preference`
kaydını yeniden söylüyorsa sonuç `previously_rejected` tanısını ve bu kayıtların
kimliklerini taşır; `candidate_for_agent_review` yerine `inspect_sources` döner.
Bu kontrol her modda, `off` dahil, yerel çalışır: model çağrısı ve yazma yoktur,
reddedilmiş metin ve gerekçe sağlayıcı isteğine girmez. Eşleşme sözcükseldir:
`rejected_matches(store, text, project, audience)` (`beyin_v3.py`) iki taraftaki
metni bağlam aramasıyla aynı biçimde normalleştirir (küçük harf, Unicode biçimi,
Türkçe ekler) ve reddedilen ifadenin, başlığı ya da metniyle, en az %60'ı ve en az
iki terimi yeni iddiada geçiyorsa eşleşir. Başlık ancak en az üç terim taşıyorsa
sayılır; "Kahve tercihi" gibi kısa bir başlık konu etiketidir, iddia değildir.
Anlamca aynı ama başka sözcüklerle yazılmış iddiayı yakalamaz. Olumsuzluğu da
ayırt etmez: reddedilen iddiayı düzelten yeni bir cümle aynı terimleri taşıdığı
için eşleşebilir. Sonuç yalnız yönlendirmeyi `inspect_sources`'a çevirir, hiçbir
şeyi engellemez; son karar kaynağı okuyan ajandadır. Aynı işlev aday üreten başka yollar için de çağrılabilir.

`off` modunda kaynak/alıntı kontrolü yereldir; anahtar, Jev cache veya sağlayıcı
erişimi olmaz. `shadow` değerlendirme yapabilir ama boyutları ve yönlendirmeyi
uygulamaz. İzinli kanıtların ve eski kayıtların tam metni gönderilir; kaynak
başına 4.000 karakter veya toplam istek bütçesi aşılırsa yerel incelemeye dönülür.
Bir kaynak yerel-only ise bütün anlamsal değerlendirme atlanır.

## Jev olmadan devam bağlamı

Hook'un gerçekten teslim ettiği en fazla üç kaynak, aynı harness ve oturumda
"onu da test et" gibi kısa devamlar için hatırlanır. Ham mesaj veya transcript
kaydedilmez; yalnız kaynak kimliği, hash, revizyon, proje ve zaman tutulur.
Somut konudan itibaren 20 dakika sonra süre dolar; belirsiz devamlar süreyi
uzatmaz. Farklı proje, yeni konu, kaynak değişikliği veya izin kaybında kaynak
devralınmaz. Konu ayırımı sınırlı Türkçe/İngilizce kelime kurallarıdır; genel
dil anlama garantisi değildir. Bu yerel özellik Jev modlarından bağımsızdır.

Güncel uygulama, araştırma bağlantıları, ölçüm ve sınırlar:
[PR 69 inceleme kanıtı](decision-quality/REVIEW.md).

## Aktarılan çözümler ve ölçüm

Forn Hafıza OS `524fd07` sürümünden bütçeli istemci, kaynak/scope bağlı puan önbelleği, strict skor doğrulaması ve Vercel'in iki ondalığa yuvarlanmış olasılıkları için dar tolerans aktarıldı. Tolerans yalnız Vercel'de matematiksel olarak mümkün dağılımlara uygulanır; doğrudan TypeSafe doğrulaması gevşetilmez. MIT atfı istemcinin içinde ve [lisans dosyasında](THIRD-PARTY-JEV.txt) bulunur.

Önbellek yalnız doğrulanmış puanları ve sınırlı metaveriyi state dizininde saklar; sorgu, kaynak metni ve anahtar saklamaz. Cache anahtarı kaynak sürümü, proje, amaç, model, endpoint ve rubriğe bağlıdır. Token sayaçları sağlayıcıdan gelirse raporlanır; karakter bütçesi token/fatura değildir. İstemcide özel 400 çağrı sınırı yoktur; önceki deney köprüsünün sayacı sağlayıcı hesap kotası değildi.

Öğrenme değişikliklerini ölçerken kaynakları ve soruları yazımdan önce dondurun. Aynı kod/yönlendirmeyle önce/sonra nihai bağlam teslimini ve proje dışına taşmayı ölçün; model açık/kapalı karşılaştırmasını ayrı yapın. Özel kasadaki kartlar ve deney sonuçları bu depoya taşınmadı. Buradaki sentetik offline testler genel anlamsal başarı veya canlı Jev bağlantısı kanıtı değildir.

## Cevaptaki iddiaları kaynaklarıyla denetleme

`jev-answer`, hazırlanmış bir cevaptaki 1–20 iddiayı ayrı ayrı değerlendirir.
Forn Hafıza OS yerel `jev_answer.py` akışındaki kaynak kapısı ve destek/çelişki
ayrımı V3 kayıt kimliği, proje kapsamı ve revizyon sözleşmesine uyarlandı.
Kişisel kayıtlar ve diğer hafıza sistemi güncellemeleri aktarılmadı.

Önce `context` çıktısından kayıt kimliğini ve güncel hash'i alın. `claims.json`:

```json
[
  {
    "text": "Demo projesinde kısa notlar tercih ediliyor.",
    "citations": [
      {
        "record_id": "demo-notlar",
        "source_sha256": "KAYNAK_DOSYANIN_GUNCEL_SHA256_DEGERI",
        "quote": "Demo için kısa notlar kullanalım."
      }
    ]
  }
]
```

```sh
python beyin.py jev-answer --project demo --file claims.json --json
```

Kaynak depodan kullanım:

```sh
python scripts/beyin_v3.py --vault /path/to/vault --state /path/to/state jev-answer --project demo --file claims.json
```

İddia başına en fazla 8 atıf, toplam 32.000 giriş karakteri kabul edilir.
İddialar sağlayıcıya 8'li paketlerle gider (aynı anda en fazla 4 istek). Paket
içinde her iddia kendi anahtarıyla durur (`items.k3`) ve sorusu o yolu
backtick içinde adresler; liste sırası ve dolaylı atıfla adreslemede canlı
Jev'de puanlar komşu iddialara sızıyordu. Bir paket `max_input_chars` sınırını
aşarsa ağ çağrısı yapılmadan ikiye bölünür. Hata veren bir istek yalnız kendi
paketindeki iddiaları `degraded` yapar.

Alıntıyla birlikte kaynak kayıtlarının tam indeks metni ve sınırlı kapsam
metaverisi gönderilir (iddia başına toplam 4.000 karakter). Tam bağlam sığmazsa
kesilmez: iddia `uncertain` ve `source_context_incomplete` ile yerel incelemeye döner. Alıntı birebir
doğru olup notun devamı tersini söyleyebilir ("eski plan ... bu plan iptal
edildi"); yalnız alıntı gönderildiğinde böyle iddialar `supported` çıkıyordu.
Bu bağlam da aynı sır taramasından geçer; eşleşme varsa o iddia gönderilmez ve
`context_sensitive` ile `degraded` olur.

Boş atıf veya eşleşmeyen hash/alıntı `insufficient` döndürür ve o iddia
sağlayıcıya gönderilmez. Alıntı hem indeks kaydında hem gerçek kaynakta aynen
bulunmalıdır. Diğer Jev komutlarındaki proje, görünürlük, güven ve sır
kontrolleri geçerlidir. Bu açık komut `internal` kayıt alıntılarını ve çevresindeki
metni sağlayıcıya gönderebilir; `private` kayıtlar elenir. Normal bağlam ve hook akışı değişmez.

`mechanical_verified`, kaynak/alıntı eşleşmesini
bildirir; anlamsal doğrulama değildir. `off` ve `shadow` modlarında anlamsal
sonuç `uncertain` kalır. `on` modunda her iddia için tek bir seçim sorusu
sorulur: kanıt iddiayı destekliyor mu, iddiayla çelişiyor mu, yoksa iddia
hakkında bir şey söylemiyor mu. Bu, sağlayıcının kendi atıf denetimi tarifidir.

| Seçim | Güven ≥ 0,8 | Güven < 0,8 |
|---|---|---|
| `supports` | `supported` | `uncertain` |
| `contradicts` | `contradicted` | `uncertain` |
| `says_nothing` | `insufficient` | `uncertain` |

Sonuçta `relation` ve `confidence` alanları da döner; `uncertain` sonuçlarda
teşhis `low_confidence` olur. Güven, sağlayıcının seçenek olasılıklarından
türettiği değerdir; doğruluk olasılığı değildir.

0,8 eşiği sağlayıcı tarifindeki başlangıç değeridir; kalibre edilmiş doğruluk
olasılığı veya genel başarı ölçümü değildir. Çağrıdan sonra
kaynak, indeks revizyonu, erişim koşulları veya yapılandırma değişirse sonuç
`degraded` olur. Servis hataları da doğrulanmış sonuç üretmez. Komut cevabı
yeniden yazmaz, aday onaylamaz veya kanonik hafıza kaydı oluşturmaz;
`approved`, `memory_written` ve `rewrites` daima `false` olur. CLI'nin normal
kaynak senkronizasyonu yerel indeksi yenileyebilir; Jev puan önbelleği de
state dizininde güncellenebilir.

Testler sentetik kaynaklar ve offline transport ile çalışır. Tasarım
2026-09-20'de canlı `jev-1.13.0` üzerinde 26 sentetik Türkçe/İngilizce iddiayla
(olumsuzluk eki, farklı sayı/gün, kapsam genişletme, ilgisiz alıntı, birebir
alıntı ama notun devamı tersini söylüyor) seçildi. Uçtan uca, iki sıralama ve
üçer tekrarla 156 sonuç: 138 beklenen net etiket, 18 `uncertain`, yanlış net
etiket yok; 26 iddia 4 istek ve iddia başına yaklaşık 250 giriş token'ı. İki
Score sorulu ve yalnız alıntı gönderen önceki hal iddia başına yaklaşık 750
token harcıyor ve iptal edilmiş plandan alıntı içeren 4 iddianın 3'ünü
kaçırıyordu. Paketleme bedelsiz değildir: zor iddialar paketin sonlarında
güven kaybedip `uncertain` olabiliyor (iddia başına tek istekte 24/24, pakette
18-21/24). Bu küçük bir yapılandırma denemesidir; gerçek kasalarda genel
doğruluk, maliyet avantajı veya kullanıcı kabulü iddia edilmez.

## Her turda otomatik bağlam (`auto_context`)

Diğer üç özellik elle çağrılır. Bu özellik hook'un içinde, her mesajda çalışır; bu yüzden bir mod seçmek onu **açmaz**, ayrıca istenir:

```sh
python beyin.py jev shadow --enable auto_context   # önce ölç
python beyin.py jev on --enable auto_context       # sonra uygula
python beyin.py jev on --disable auto_context      # yalnız bunu kapat
```

Neden var: yerel "strict" eşleşme bir notu ancak iki ortak kelimeyle getirir. Bu, alakasız notu dışarıda tutar ama başka kelimelerle sorulan soruyu da kaçırır ("deploy sonrası eski sürüme nasıl dönerim" sorusu, içinde "geri alma" yazan notu bulamaz). Açıkken hook iki aşamalı çalışır:

1. Yerel arama, strict eşleşmelere ek olarak en fazla 16 gevşek aday bulur. İki grup dönüşümlü birleştirilir ve uzaktan değerlendirmeye izinli en fazla 8 kayıt seçilir. Yerel-only kayıtlar uzak aday kotasını tüketmez. Havuz aynı görünürlük, güven, tazelik ve supersede kapılarından geçer; `daily/` ve `private` kayıtlar havuza girmez.
2. Tek bir Jev isteği bir konu kapısı ("bu mesaj somut bir konu mu, selam/onay mı") ve aday başına bir evet/hayır sorar.

Sonuç `on` kipinde şöyle uygulanır: konu kapısı 0,25 altındaysa hiçbir not eklenmez. Strict eşleşme 0,4 altına düşerse çıkarılır. Gevşek aday yalnız 0,6 ve üstünde eklenir. En fazla 5 kayıt, aynı karakter bütçesi. Hiçbir şey değişmiyorsa yerel sonuç olduğu gibi kullanılır. `shadow` kipinde çağrı yapılır, sonuç değişmez, yalnız sayaçlar yazılır (`dropped`, `rescued`).

Yerel sonuca geri dönülen durumlar: 12 karakterden kısa mesaj, yerel sır örüntüsü eşleşmesi, eksik anahtar, zaman aşımı, 429, bozuk yanıt, çağrı sırasında değişen kaynak, kill switch. Hook en fazla 2 saniye bekler ve kendi 5 saniyelik sınırına yaklaşmışsa hiç çağırmaz. `jev.json` yoksa Jev modülü içe aktarılmaz.

Bedeli açıkça: her mesajda mesaj metni ve aday notların başlığı ile ilk 600 karakteri sağlayıcıya gider. İstanbul'dan tek atımlık çağrı ortanca 1,3 saniye, yüzde onu 2 saniyenin üstünde sürdü (sunucu ABD batı kıyısında); bu gecikme her eşleşen mesaja eklenir. İstek başına yaklaşık 600-1.500 girdi token'ı harcanır.

Ölçüm (2026-09-20, canlı Jev, 12 sentetik Türkçe/İngilizce not, 26 mesaj, tek koşu): ilgili notu bulma 13/17'den 15/17'ye çıktı, alakasız not enjeksiyonu 2'den 1'e indi, strict'in bulduğu hiçbir ilgili not kaybedilmedi, bir çağrı zaman aşımına uğrayıp yerel sonuca döndü. Bir yanlış ekleme oldu ("bu fonksiyonu test için refactor et" mesajına test stratejisi notu). Bu sentetik bir ölçümdür ve Jev aynı isteğe her seferinde biraz farklı puan verir; gerçek kasada isabet daha düşük çıkabilir. Kendi notlarınızda önce `shadow` ile birkaç gün sayaçlara bakın, sonra `on` yapın.

## Yerel Laya arka ucu (isteğe bağlı)

[Laya](https://github.com/NandhaKishorM/laya), Convai Innovations'ın Apache 2.0 lisanslı, açık ağırlıklı System One karar modelidir. `laya-serve` sunucusu Jev ile aynı `POST /v1/systemone` protokolünü konuşur. Beyin, Laya'yı ayrı kurulan yerel bir sunucu olarak kullanır: Beyin paketine Laya kodu, model ağırlığı veya PyTorch girmez; hook ve CLI `torch` ya da `laya` içe aktarmaz (`tests/v3_stdlib_imports_test.py` bunu denetler). Varsayılan sağlayıcı değişmez; Laya yalnız açıkça seçilince kullanılır ve yalnız gölge modda çalışır: puanları ölçüm için kaydedilir, kullanıcının gördüğü sonucu değiştirmez ([Yalnız gölge mod](#yalnız-gölge-mod)).

### Laya'yı Beyin dışında kurma

Laya Python 3.10+ ve PyTorch ister. Beyin'in ortamına değil, ayrı bir araç ortamına kurun:

    pipx install "laya[serve]"
    LAYA_HOST=127.0.0.1 LAYA_PORT=8765 LAYA_MODELS=multilingual laya-serve

Windows PowerShell:

    $env:LAYA_HOST = "127.0.0.1"; $env:LAYA_PORT = "8765"; $env:LAYA_MODELS = "multilingual"; laya-serve

`LAYA_HOST=127.0.0.1` şarttır: `laya-serve` varsayılan olarak `0.0.0.0` adresini dinler ve aynı ağdaki cihazlara açılır. Beyin'in varsayılan adresi `http://127.0.0.1:8765` olduğu için `LAYA_PORT=8765` verin. `laya-serve`'in kendi varsayılanı olan 8000 sık kullanılan bir geliştirme portudur; Laya kapalıyken orada çalışan başka bir uygulama not metnini almasın diye Beyin 8000'i varsaymaz. İlk çalıştırma modeli Hugging Face'ten indirir (multilingual yaklaşık 650 MB). İndirmeden sonra `HF_HUB_OFFLINE=1` ile başlatmak sunucunun ağa çıkmasını ve istek üzerine yeni model indirmesini önler. Apple Silicon'da `LAYA_DEVICE=mps` kullanılabilir; yalnız CPU varsa `LAYA_THREADS` fiziksel çekirdek sayısını geçmemelidir. Sunucu yüklü modellerle birlikte birkaç GB bellek tutar.

### Açma ve kapatma

    python beyin.py jev shadow --provider laya
    python beyin.py jev status --check
    python beyin.py jev shadow --provider laya --model multilingual
    python beyin.py jev on --provider typesafe

Son komut Jev'e döner; Laya ayarları saklanır. `jev on --provider laya` kabul edilmez (`laya_shadow_only`); aşağıya bakın. `--base-url` ve `--model` yalnız Laya sağlayıcısıyla kabul edilir. `status --check` yalnız yapılandırılmış yerel sunucunun `/health` adresini bir kez okur (1 saniye), not veya anahtar göndermez; TypeSafe'i hiç yoklamaz. `doctor` ağa çıkmaz.

Ayar `jev.json` içinde ayrı bir `laya` bloğunda durur:

    {"mode":"shadow","provider":"laya","laya":{"base_url":"http://127.0.0.1:8765","model":"multilingual"}}

- `base_url` yalnız `127.0.0.1` veya `::1` olabilir; `localhost` bile kabul edilmez, çünkü hosts dosyası onu başka bir adrese yönlendirebilir. `TYPESAFE_BASE_URL` Laya çağrılarını yönlendirmez; ortamdaki HTTP proxy ayarları bu çağrılarda kullanılmaz.
- Anahtar gerekmez. `laya-serve` `LAYA_API_KEY` ile çalışıyorsa aynı değer Beyin'in ortamında (veya `env_file` içinde) `LAYA_API_KEY` olarak bulunmalıdır. `TYPESAFE_API_KEY` Laya'ya hiçbir zaman gönderilmez.
- `model`: `multilingual` (varsayılan; Türkçe ve İngilizce, satır başına 1024 token) veya `english` (yalnız İngilizce, 512 token). Tek model sabitlenir; aynı sorudaki adaylar farklı modellerle puanlanmaz. Sunucu başka bir modelle cevap verirse sonuç atılır (`laya_checkpoint_mismatch`). `typed-decisions` desteklenmez: başka iş akışlarına göre eğitilmiştir ve yüklü olmayan bir sunucuda ilk istekte 843 MB indirme başlatır.
- `timeout` en fazla 10 saniyedir, varsayılan 8. Hook yolu yine en fazla 2 saniye bekler; yetişmezse yerel sonuç kullanılır.
- Laya desteğinden önceki bir sürüme `rollback` yapılırsa eski sürüm `laya` bloğunu tanımaz, ayarı geçersiz sayar ve danışmanı kapatır; TypeSafe anahtarını yerel porta göndermez. Eski sürümde Jev'e dönmek için bloğu `jev.json`'dan silin. Bu davranış `scripts/verify_v3_jev_package.py` içinde yayımlanmış 3.1.0 ile doğrulanır.

### Jev'den farkları

Laya her soruyu ayrı bir satırda değerlendirir ve satırın ilk 512 veya 1024 token'ından fazlasını hata vermeden keser. Jev için tek istekte birden çok notu anahtarla adresleyen biçim Laya'da sonraki notları görünmez yapardı. Bu yüzden Laya arka ucu her isteğe tek aday ve tek soru koyar: `auto_context` için bir konu isteği ve not başına bir istek, `context --jev` için aday ve alt istek başına bir istek, `jev-answer` için iddia başına bir istek, `jev-memory` için üç boyut ve eski kayıt başına birer istek. Sorular kısa, soru önde ve İngilizcedir; seçenek metinleri Jev ile aynıdır. En fazla 48 istek yapılır; istekler tek sırayla gider, çünkü `laya-serve` aynı anda tek çıkarım yapar. `jev-answer` da Laya ile paketleri paralel değil sırayla gönderir.

Metin sığdırılmak için kesilmez. Her istek gönderilmeden önce ölçülmüş bir boyut sınırıyla karşılaştırılır; biri aşarsa hiçbir istek yapılmaz ve sonuç `laya_state_too_large` ile yerel sonuca döner. Tek bilinçli kısaltma `auto_context` isteminin payıdır: konu isteği ve her not isteği, notu kesmeden sınıra sığan kadar istem taşır. Sınırı yine de dolduran bir satır cevaptan sonra yakalanır: satır token sınırına ulaştıysa sonuç atılır (`laya_state_truncated`). Laya'da iddia başına kaynak bağlamı 4.000 yerine 1.200 karakterle (`english` için 400) sınırlıdır; aşan iddia yalnız kendisi için `source_context_incomplete` ile yerel incelemeye döner.

Sınırlar 2026-09-24'te iki modelin kendi tokenizer'ıyla ölçüldü: şablonlar soru ve seçenekler için 39 ile 102 token arası yer tutar, duruma en az 918 (multilingual) veya 403 (english) token kalır. Boyut birimi karakter sayısıdır; rakamlar ve U+024F üstündeki karakterler (CJK, emoji) daha ağır sayılır, Türkçe harfler tek birimdir. Türkçe belgeler, ASCII Türkçe, İngilizce metin, 493 gerçek Markdown not ve Python kodu üzerinde birim başına token en fazla 0,52 (multilingual) ve 0,50 (english) çıktı. Gerçek notlardan kurulan 1.080 `auto_context`, 960 `context --jev` ve bağlam sınırını geçen 909 `jev-answer` isteğinin hiçbiri multilingual sınırını aşmadı; en fazla alanın yüzde 57'sini kullandı. `english` modelinde aynı not isteklerinin hiçbiri sığmadı; bu model yalnız kısa İngilizce içerik içindir ve not boyutlu isteklerde yerel sonuca döner.

`choice` sorularında güven, Laya'nın entropi tabanlı `confidence` alanından değil en olası seçeneğin olasılığından alınır; Laya'nın kalibre ettiği değer budur. Kayıtta `confidence_provenance.origin` değeri `laya_max_probability` olur.

### Yalnız gölge mod

Laya puanları kullanıcının gördüğü hiçbir sonucu değiştirmez. Nedeni aşağıdaki [ölçümdür](#ölçüm): sıfır atışlı (zero-shot) Laya bu karar görevlerinde Jev'den çok daha zayıf çıktı.

- `jev on --provider laya`, Laya ayarlıyken `jev on` ve kayıtlı `on` modundan Laya'ya geçiş `laya_shadow_only` koduyla reddedilir; `jev.json` değişmez. Komut hatası bir sonraki adımı gösteren ASCII Türkçe bir `hint` taşır.
- `jev.json` elle `"mode":"on"` yapılsa da Laya gölge modda çalışır. `jev status` bunu `mode: "shadow"`, `saved_mode: "on"`, `shadow_only: true` ve `mode_refused: "laya_shadow_only"` ile gösterir; `jev status` ve `doctor` insan çıktısında mod `golge (yalniz)` olarak yazılır.
- Laya ile `auto_context` ve `context --jev` bağlamın sırasını ve içeriğini değiştirmez; otomatik bağlam kaydı `scored_unverified` olur. `jev-answer` iddiaları `semantic_shadow` ile `uncertain` kalır, `jev-review` sonucu `mode: "shadow"` taşır ve `jev-memory` karar üretmez (`shadow_not_applied`). Puanlar çıktıda ve sayaçlarda ölçüm için görünür.
- Karar eşikleri `beyin_v3_jev_client.THRESHOLDS` tablosunda sağlayıcı ve model çiftine göre durur. Laya satırları Jev değerlerinin kopyasıdır ve `verified: false` işaretlidir; `status` bunu `calibration: "jev_thresholds_unverified_for_laya"` ve `auto_context_applied: false` ile, çıktılar `calibration: "unverified_for_provider"` ile gösterir. Gölge sınırı bu işaretten bağımsızdır: Laya `client.SHADOW_ONLY` listesinden ayrı bir kararla çıkarılmadıkça bir satırın `verified: true` yapılması hiçbir sonucu değiştirmez.

### Gizlilik

Laya yerelde çalışsa da gönderim kuralları Jev ile aynıdır: `private` notlar, `remote_allowed: false` ve `sensitivity: sensitive` kayıtlar, `daily/` oturum notları ve sır örüntüsüyle eşleşen metinler gönderilmez. Bir loopback portunu yalnız `laya-serve` değil herhangi bir yerel süreç dinleyebilir. Yerel-only notları yalnız yerel Laya'ya açan bir seçenek (`local_only_notes`) ileride ayrıca değerlendirilecek; bugün `laya` bloğunda bu anahtar geçersizdir. Acil kapatma (`BEYIN_JEV_DISABLE=1`, `jev.disabled`), özellik anahtarları, çağrı kaydı ve önbellek aynı şekilde çalışır; kayıtta `provider` alanı `laya`, `http_requests` gerçek istek sayısı olur. Jev ve Laya önbellekleri birbirine karışmaz.

### Ölçüm

Bu depodaki Laya testleri sahte bir yerel sunucuyla çalışır ve model kalitesi hakkında bilgi vermez. Laya'nın kendi README'sindeki doğruluk sayıları bu depoda doğrulanmadı.

<!-- BENCH -->
**Jev ile Laya karşılaştırması (2026-09-24).** Jev: `jev-1.13.0`, resmi TypeSafe API. [Laya](https://github.com/NandhaKishorM/laya): 0.3.20, Hugging Face `convaiinnovations/laya` revizyonu `55cf4c4e`, `multilingual` ve `english` modelleri, loopback üzerinde `laya-serve`, Apple M5 (24 GB, MPS). Laya sıfır atışlı (zero-shot) kullanıldı, ince ayar yapılmadı. Yalnız sentetik, kurgu veya daha önce yayımlanmış etiketli setler kullanıldı; öğeler ve doğru etiketler ilk model çağrısından önce sha256 ile donduruldu. Laya, Jev'e giden baytların aynısının yanında kendisine daha uygun biçimleri de aldı (istek başına tek öğe, İngilizce ifade biçimi, sabit model); tabloda her satırın en iyi Laya sonucu var. Güven en büyük olasılıktır, köşeli parantezler yüzde 95 aralığıdır.

| Görev | Ölçüt | Jev | En iyi Laya |
| --- | --- | --- | --- |
| İddia ve kaynak (190 iddia) | Doğruluk, Beyin `jev-answer` biçimi | 0,930 | 0,579 [0,51; 0,65] |
| İddia ve kaynak (190 iddia) | Beyin biçiminde güven kapısını geçen yanlış `supports` | 6 | 42 |
| Hafıza incelemesi (100 ifade, 64'ü temkinli veya olumsuz) | İlişki doğruluğu | 1,000 | 0,450 [0,36; 0,55] |
| Hafıza incelemesi (100 ifade, 64'ü temkinli veya olumsuz) | Güven kapısını geçen yanlış terfi (64 ifadede) | 0 | 5 (İngilizce ifade biçiminde 11) |
| Eylem risk kapısı (315 sentetik araç çağrısı) | Yakalama / yanlış uyarı | %97,9 / %3,7 | %54,7 / %21,8 |
| Konu kapısı (279 kısa Türkçe ve İngilizce mesaj) | Doğruluk, Beyin ifadesi, eşik 0,25 (Jev: 3 koşu ortalaması) | 0,866 | 0,652 [0,59; 0,71] |
| Küçük değişiklik dayanıklılığı (98 cümle çifti) | İki cümle de doğru tarafta | 0,918 | 0,316 |
| Mevcut sentetik etiketli setler (1.285 ikili karar) | AUC / ECE | 0,994 / 0,073 | 0,609 / 0,227 |

Eylem kapısında gizlilik süzgeci 5 çağrıyı Jev'e göndermedi; Jev oranları kalan 310 çağrı üzerindendir. İki tarafta da HTTP hatası, zaman aşımı veya geçersiz cevap olmadı; Laya her tekrar kontrolünde bit düzeyinde aynı sonucu verdi.

Sonuç açık: sıfır atışlı Laya bu Türkçe ve İngilizce karar görevlerinde Jev'den çok daha zayıf ve denenen hiçbir biçim farkı kapatmadı. Başlıca nedenler: birden çok notu anahtarla adresleyen durumlarda her öğeye neredeyse aynı puan, çoğu olumsuzluğun kaçırılması, kısa veya Türkçe karakter içermeyen Türkçe metnin otomatik yönlendirmede `english` modeline gitmesi ve satır sınırında hatasız kesme. Bu yüzden Beyin'de sonucu değiştirebilen tek sağlayıcı Jev'dir. Laya isteğe bağlı, yalnız gölge modda çalışan yerel bir seçenek olarak kalır: veri makineden çıkmaz, API maliyeti yoktur ve aynı girdiye hep aynı cevabı verir; bu yüzden ölçüm ve gizlilik denemeleri için tutulur. Sunucu 4,5 ile 5,9 GB bellek tutar, soğuk başlangıcı 11 ile 19 saniye sürer. `THRESHOLDS` içindeki Laya satırları `verified: false` kalır. Denenen yüzeyler içinde Laya'nın tek güçlü sinyali, İngilizce ifade biçiminde iki iddianın aynı şeyi söyleyip söylemediği oldu (AUC 0,915); bu kullanım da kendi eşiği ayrı geliştirme ve test setlerinde ölçülmeden açılmamalı.

Sınırlar: öğelerin çoğu sentetik veya kurgu; bazı setlerde öğeleri ve etiketleri aynı yazar hazırladı ve bazı rubrik ile eşikler daha önce Jev'e göre ayarlandı, bu yüzden bazı setler Jev için örneklem içidir. Sonuçlar `55cf4c4e` revizyonundaki sıfır atışlı Laya içindir; ağırlıklar değişebilir, ince ayar denenmedi. Bu karşılaştırmada gecikme ölçülmedi.
<!-- /BENCH -->
