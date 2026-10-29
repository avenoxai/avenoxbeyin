# Seçilen kanıtın taşınması

`PackedContext.sources` kırpılmamış dar pasajları, `wider` varsa ikinci pasajla
genişletilmiş görünümü veya tercih sırasındaki alternatiflerin listesini taşır.
`evidence` aynı kaynakların kimliğini, SHA-256 ve
revizyonunu, geldiği kanalı ve seçilen kaynak aralıklarını taşır. Aralıklar ham
Markdown baytlarına değil indekslenmiş `record.text` karakterlerine göredir.
Her görünüm, kopyalanan kaynak aralığının çıktıdaki yerini de tutar; böylece
son zarfın ortasında kesilen bir pasajın yalnız gösterilen kısmı hatırlanır.
Bu üçü Python nesnesinin attribute'larıdır; zarf JSON'una eklenmez.

Strict arama seçimini değiştirmez. Jev kartı dar ve varsa geniş seçimin her
pasajına yer ayırır; uzun parçada sorguya uyan pencereyi kullanır. Gevşek adayın
ilgili pasajı karttan önce yerelde seçilir. Kurtarıldığında dar görünüm notun
ilk üç `TOP_CONTEXT_TEXT` penceresidir (en fazla 3.300 karakter). Yerel teslim
önce notun tamamını, sonra açılışla birincil eşleşen pasajı, sonra açılışla
bu pasajın sorguya göre seçilen en fazla 600 karakterlik penceresini taşıyan
`wider` alternatiflerini dener. `pack_widened` yalnız bütün dar kayıtlar teslim
edildikten sonra kalan bütçeye tam sığan ilk alternatifi kullanır. Hiçbiri
sığmazsa açılış kalır; dar görünüm normal zarf kotasıyla kırpılabilir.
Son zarf kendi bütçesiyle bu kararı yeniden verir; ikinci pencere başka kaynağı
veya açılışı kırpamaz. Örtüşen aralıklar yinelenmez. Bütün görünümlerin kaynak
aralıkları saklanır; continuity yalnız gerçekten teslim edilen görünümü
hatırlar. Bu yerel açılış rezervi
Jev kartına eklenmez; kartlar sorguyla ilgili seçimi göstermeye devam eder.
Kartın toplam excerpt sınırı 600 karakterdir; başlık ve aralık işaretleri bu
sınıra dahildir. Tam metne ihtiyaç duyan bir semantik kararı bu görünüm garanti
etmez. Sağlayıcı eşikleri değiştirilmemiştir; yeni görünüm canlı ölçülmelidir.

`render_context` kanıt referanslarını yalnız teslim ettiği kaynaklarla birlikte
döndürür. `remember` bu teslim projeksiyonunu kullanır. Continuity dosyasının
ikinci sürümü yalnız kaynak kimliği/revizyon/hash ve sayısal `[başlangıç, bitiş)`
aralıkları saklar; not metni, bölüm başlığı ve kullanıcı istemi saklamaz.
Kaynak değişince eski aralıklar kullanılmaz. Her devam turunda görünürlük,
güven, proje, supersession, durum, hash ve güncel strict dışlamalar yeniden
denetlenir. Başlık yolu güncel ve hash'i doğrulanmış kaynaktan yeniden üretilir.
Yalnız gösterilen metin korunur; daha büyük devam bütçesi görünmemiş metni açmaz.
Belirsiz devamın konu sözlüğü kaydın başlığından ve doğrulanıp geri kurulan
teslim aralıklarından çıkarılır; notun gösterilmemiş gövdesi bu sözlüğe katılmaz.
`ACTIONS`, `DEICTIC` ve `GENERIC` kümelerindeki genel eylem ve devam sözcükleri
de tanıdık kabul edilir. İstekte bu birleşik sözlük dışında yeni bir konu terimi
varsa o turun yerel arama sonucu korunur. Kontrol, yalnız başlıkta, gösterilmiş
pasajda veya genel komut sözlüğünde bulunan terimlerle ifade edilen farklı bir
konuyu ayrıca ayırt etmez. "Bunu detaylandır" gibi kısa devamlar ve gösterilmiş
pasajın sözcüklerini kullanan devamlar mevcut devam koşullarıyla çalışır.

Kimlikten ibaret eski (sürüm 1) continuity dosyaları aralık bilinmediği için
kullanılmaz. Bir sonraki somut, başarılı teslim bunları sürüm 2 olarak yeniler.
Yirmi dakikalık süre, oturum/istemci/proje sınırı ve belirsiz devamın süreyi
uzatmaması değişmez. Bozuk veya doğrulanamayan referans yerel sorgu sonucuna
düşer. Pasaj motoru olmadan çalışan not düzeyi fallback de tam doğrulanabilen
metin önekini aralık olarak saklayabilir; pasajın yerini tahmin etmez.

Tekrar süzgeci kalan kaynakların `sources`, `wider` ve `evidence` attribute'larını
korur. Hook, o oturumda daha önce gösterildiği için süzülen kayıtları da kendi
kanıt referanslarıyla continuity'ye aktarır.
