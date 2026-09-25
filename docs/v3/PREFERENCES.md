# Otomatik kontrol ve bağlam tercihleri

Kullanıcı ayarı `.beyin-preferences.json` içinde; yönetilen release dosyası değildir. Installer, updater ve rollback bu dosyayı değiştirmez. Dosya yoksa mevcut normal davranış korunur. Ortak Python uygulaması Windows, macOS ve Linux'ta aynı ayarları okur; yeni servis veya model bağımlılığı yoktur.

`python3 beyin.py preferences --profile economical` ile ekonomik, `--profile normal` ile normal, `--profile manual` ile manuel kullanım. Argüman verilmezse ayarlar okunur. Ajan bunları beyin skill'i üzerinden uygular. `--human` okunabilir çıktı sağlar.

Alanlar: `auto_sync` boolean; `interval_minutes` 0–1440 (0 her olay); `context_mode` turn/session/off; `context_chars` 1000–12000. Karakter sınırı ek hook bağlamının tamamına uygulanır; token kotası değildir. Sayısal ve bilinmeyen alanlar doğrulanır, hatalı dosya sessizce ezilmez.

Ekonomik profil: auto_sync=true, interval_minutes=15, context_mode=session, context_chars=2000. Manuel: auto_sync=false ve context_mode=off. Mevcut ayarların yalnız bir alanını değiştirmek için örneğin `preferences --interval-minutes 30`; profil seçmek otomatik kontrol ve bağlam alanlarını o profile sıfırlar, bağımsız sır süzgeci tercihini korur. Aralık değiştirmek kapalı kontrolü açmaz.

## Hafıza dosyası sınırları

`Last-Session.md` varsayılan olarak 3.000, `Threads.md` 8.000 karakterle sınırlıdır.
`python3 beyin.py preferences --last-session-chars 4000 --threads-chars 12000` sınırları
1.000 ile 200.000 arasında değiştirir, `0` ilgili sınırı kapatır. Bu iki alan vault
tercih dosyasında değil, runtime klasöründeki `companion-limits.json` dosyasında tutulur:
eski sürümler bilinmeyen tercih alanını reddettiği için rollback güvenli kalır, ayar ise
makineye özeldir. Profil değişikliği sınırları sıfırlamaz. Hatalı değer veya bozuk sınır
dosyası hiçbir ayarı kaydettirmez. Sınır aşılınca oturum başındaki uyarı, `doctor` raporu
ve kayıpsız `companion-compact` komutu [companion incelemesinde](COMPANION-PARITY.md)
anlatılır.

## Opt-in sır süzgeci

`python3 beyin.py preferences --secret-filter on` komutu receipt özeti, note-create ve task-create gövdesi, bu komutların ve task-update değişikliklerinin `title`, `next_action`, `completion_criterion` ve `facts` alanlarında yaygın erişim anahtarı biçimlerini yazmadan önce `[REDACTED]` ile değiştirir. Varsayılan kapalıdır; profil değişikliği bu bağımsız tercihi değiştirmez. Kapatmak için `--secret-filter off` kullan.

Ek sabit sır değerleri vault dışındaki runtime klasöründe `secret-patterns.txt` dosyasına, satır başına bir değer olarak yazılabilir. Dosya regex çalıştırmaz; yorum satırları `#` ile başlar. Eşleşen metinler veya değerler sağlık kaydına yazılmaz, yalnız toplam eşleşme sayısı `doctor` sonucunda gösterilir. Bu önlem kazara kalıcı yazımı azaltır; tam bir DLP veya önceden yazılmış notları temizleme aracı değildir.

Süre en son otomatik başlatmaya göre yerel SQLite kaydıyla, istemciler arasında atomik olarak sınırlandırılır. Bir sonraki olay gelmedikçe kontrol çalışmaz. Yeni oturumda daima taze kontrol; kaydedilmiş worker hatasında yeniden deneme. Bu düşük seviyeli kontrol model çağırmaz. Otomatik bağlam kapalı olsa bile açık not/görev/receipt ve context komutları çalışır. Kapatmadan önce başlatılmış bir işlem tamamlanabilir; önceden bekleyen metadata saklanır. Açık `--drain-queue` bakım komutu bu kuyruğu işler.

Aralığa takılmış bir kontrolde turn bağlamı istenirse eski kayıtları güncel diye sunmak yerine kısa tazeleme uyarısı verilir. Ekonomik modda sonraki mesajlarda tekrar bağlam eklenmez; beyin skill'i bilgi gerektiğinde kaynakları doğrudan tazeler.

Özel V2 cron/LaunchAgent/Task Scheduler işleri veya kullanıcının ayrı kurduğu 15 dakikalık ücretli ajan otomasyonları bu tercihlerle kapatılmaz. Önce ilgili işi tespit edip ayrı yönetmek gerekir. V3 kendiliğinden Luna/Sonnet çalıştırmaz.

Bu değişiklik yerel adaydır; yayımlanmış v3.0.0 paketine otomatik olarak eklenmez. Yeni paket yayımlanmadan kullanıcılara mevcut sürüm özelliği diye duyurulmamalıdır.

## Stop'ta receipt hatırlatması

Kurulum, Claude ve Codex için PostToolUse hook'unu yalnız dosya düzenleyen araçlara bağlar (`Edit|Write|apply_patch`). Bu olay geldiğinde hook, vault dışındaki runtime klasörüne oturum kimliğinin hash'iyle adlandırılmış küçük bir düzenleme işareti yazar; transcript okunmaz. Kabuk komutuyla yapılan düzenlemeler bu olayı tetiklemez. Stop'ta runtime kaydında aynı istemci ve aynı `session` değeriyle, düzenlemelerden sonra yazılmış bir receipt yoksa hook oturum başına bir kez Stop'u engeller ve `python3 beyin.py receipt --file RECEIPT_JSON --harness claude` komutunu (Codex için `--harness codex`, Windows'ta `py -3`) `Receipt session=<değer>` bilgisiyle birlikte hatırlatır. Bu değer receipt JSON'undaki `session` alanına yazılmazsa receipt bu checkpoint'i kapatmaz. Receipt'ten sonra yapılan yeni düzenlemeler yeni bir pencere açar.

Stop olayı hatırlatmadan önce kuyruğa alınır. Runtime kaydı okunamazsa akış durdurulmaz. `stop_hook_active` taşıyan ikinci Stop, manuel profil (`auto_sync: false`) ve global köprü hatırlatma yapmaz; tek seferlik hakkı da harcamaz. Kullanıcı mesajında `[kaydetme]` yazarak bu oturumdaki hatırlatmayı kapatabilir; `BEYIN_V3_NO_RECEIPT_REMINDER=1` özelliği tamamen kapatır.

Receipt özetinde kalıcı bir öğrenim ayrı bir satırda, satır başında beyan edilir: `Öğrenilen: <tek cümle>` (ya da `Ders:`, `Learned:`, `Kalıcı öğrenim:`). Öğrenim yoksa `Öğrenilen: yok` yazılır; `yok`, `hiçbiri`, `bulunmadı`, `none` gibi cevaplar ve boş etiket beyan sayılmaz. Etiket yalnız kendi satırında okunur; metin içinde geçen `machine learning:` gibi ifadeler beyan değildir. Receipt öğrenim beyan ettiği halde oturumda `knowledge/` altında bir not yazılmadıysa ya da refs içinde böyle bir not yoksa, Stop kancası oturum başına bir kez ajana öğrenimi `knowledge/concepts/` altına damıtmasını hatırlatır. `knowledge/index.md`, `knowledge/log.md` ve `knowledge/v3/` damıtma sayılmaz; bunlar derleyici, git veya eşitleme tarafından da değişebilir. Receipt hatırlatmasına cevap olarak yazılan receipt, `stop_hook_active` taşıyan Stop'ta değil sonraki Stop'ta kontrol edilir. Kalıcı kavram gerekmiyorsa ajanın bunu tek cümleyle belirtmesi yeterlidir; kanca ikinci kez engellemez. Öğrenim beyan etmeyen receipt'lerde klasör taranmaz.

`beyin.py doctor` komutu sessiz durgunluğu görünür kılmak için son bilgi damıtmasının kaç gün önce yapıldığını ve o tarihten bu yana kaç receipt kaydedildiğini yazar. Gerçek çıktı ASCII'dir, diğer doctor satırları gibi: `Son bilgi damitmasi: 3 gun once (o tarihten beri 12 makbuz)`; hiç kavram notu yoksa `Son bilgi damitmasi: henuz kavram notu damitilmadi (toplam N makbuz)`; ölçüm başarısız olursa `Son bilgi damitmasi: olculemedi (<hata>)`. Tarih notun frontmatter'ındaki `updated` ya da `modified` alanından, bu alan yoksa dosya zamanından okunur; çünkü git checkout ve iCloud geri yüklemesi dosya zamanını sıfırlar. `knowledge/index.md`, `knowledge/log.md` ve `knowledge/v3/` sayılmaz, her nottan yalnız ilk 4 KB okunur.

Yönetilen blok dışında `AGENTS.md` ya da `CLAUDE.md` içinde `knowledge/` klasörünü derleyiciye bırakan V2 ifadesi (`derleyici yönetir`, `derleyici yazar`, `elle düzenlemeyin`, `compiler-managed` gibi) kalmışsa doctor `Talimat celiskisi: ...` satırını yazar ve durumu `needs_attention` yapar. Bu ifade ajanın knowledge notu yazmaktan kaçınmasına yol açar; satırı kaldırmak uyarıyı kapatır.

## Tur başı bağlam: pasaj düzeyinde strict arama

Her mesajda hook'un eklediği bağlam (`context_mode: turn`) strict aramadan gelir. Bu arama notun tamamını tek bir kelime kümesi olarak puanlamaz; notu Markdown bloklarına böler ve her kaynaktan soruyla en iyi eşleşen bloğu, başlık yoluyla birlikte teslim eder (#83). Cevap uzun bir notun ortasındaysa notun ilk karakterleri değil cevabı taşıyan blok gelir; neredeyse her kelimeyi içeren uzun bir oturum arşivi de her soruda öne çıkamaz. Model çağrısı ve yeni bağımlılık yoktur.

- Bölme: başlıklar (kod blokları içindekiler hariç) bölüm açar, boş satırlar paragrafları ayırır, yaklaşık 900 karakteri aşan bloklar cümle sınırından 200 karakter örtüşen pencerelere bölünür. Frontmatter okunmaz; başlık, takma ad ve `facts` alanları her bloğun arama kelimelerine eklenir.
- Kabul ve sıralama: bir blok en az iki ortak kelime ister. Ağırlık mevcut göreli idf ailesiyle hesaplanır, soru kapsamasıyla (`ortak kelime / soru kelimesi`, payda en fazla 4) çarpılır ve `strict_floor` altında kalırsa blok elenir. Kabul blok frekansıyla, kabul edilen kaynakların sıralaması kaynak frekansıyla yapılır; çok bölüme ayrılmış bir not kendi kelimelerini yaygın göstermez. Uzun, sohbet havasında yazılmış bir mesaj uzunluğu yüzünden elenmez. Proje seçiliyse proje kelimeleri eşleşme sayılmaz.
- Kapılar aynıdır: görünürlük, güven, proje, tazelik ve supersede kuralları not düzeyindeki yolla aynı `_eligible()` fonksiyonundan geçer; bütçe ve citation sözleşmesi aynı `pack_context`'tir.
- Boş sonuç bir cevaptır. Not düzeyindeki eski yola yalnız gerçek bir hata olduğunda ya da indeks ilk kez kurulurken dönülür.

İndeks türetilmiş veridir ve vault dışındaki runtime klasöründe `passages.json` olarak durur (JSON, 0600 izin, atomik yazım, 64 MB sınırı; pickle yoktur). Yalnız değişen kayıtlar yeniden işlenir; kelime ayırıcı değişirse önbellek kendiliğinden yeniden kurulur. İlk kurulum her turda en fazla yaklaşık 1 saniye çalışır ve kaldığı yerden devam eder; tamamlanana kadar o turlar eski yolu kullanır, böylece büyük bir vault hook süresini aşmaz.

### `retrieval.json`

Runtime klasöründeki (konumu için bkz. QUICKSTART) isteğe bağlı `retrieval.json` iki ayar taşır:

```json
{"strict_floor": 0.2, "strict_exclude": ["📦 900-Archive/", "Arsiv/Oturumlar/"]}
```

- `strict_floor`: 0 ile 2 arası sayı, varsayılan 0,20. Yükseltmek yanlış eklemeyi azaltır, cevabı bulma oranını da düşürür.
- `strict_exclude`: vault köküne göre en fazla 64 yol öneki. `daily/` ve `receipts/` her zaman dışarıdadır, liste bunlara eklenir. Karşılaştırma büyük/küçük harfe ve Unicode biçimine (NFC/NFD) duyarsızdır, `\` ayırıcısı da kabul edilir. Arşiv ya da oturum dökümü klasörleri için uygundur.
- Dosya bozuksa ya da bir değer geçersizse varsayılanlar kullanılır; hook bozulmaz.

Bu ayar makineye özeldir ve `.beyin-preferences.json` şemasına girmez: eski bir sürüme dönüldüğünde tanımadığı bir tercih alanı yüzünden hook'un durması istenmedi.

### Ölçüm ve varsayılan `strict_floor`

`python3 scripts/evaluate_v3_passages.py` iki yolu hook'un teslim yolundan (`context_for(strict=True)` ve `render_context`) 2000 ve 5000 bütçede ölçer. Varsayılan korpus sentetik ve deterministiktir; `--vault DIR --questions FILE` aynı ölçümü kendi vault'unuzda salt okunur yapar. `answerable`, etiketli notun teslim edilmesi ve cevap cümlesinin teslim edilen metinde birebir geçmesidir; gürültü, karşılığı olmayan kontrol mesajlarından kaçında kayıt eklendiğidir.

Sentetik korpus (152 not, 42 soru, iki soru biçimi; 15 gündelik, 15 ajan komutu ve 10 genel kelime kontrolü), `strict_floor` 0,20:

| | not düzeyi 2000 | pasaj 2000 | not düzeyi 5000 | pasaj 5000 |
|---|---|---|---|---|
| kısa soru, answerable | 19/42 | 32/42 | 24/42 | 33/42 |
| uzun sohbet mesajı, answerable | 17/42 | 27/42 | 20/42 | 28/42 |
| gürültü: gündelik / ajan komutu | 0/15, 0/15 | 0/15, 1/15 | 0/15, 0/15 | 0/15, 1/15 |

Gerçek bir vault (1.285 not; 88 etiketli soru, her biri kısa, dolgu kelimeli ve uzun sohbet biçiminde, toplam 264 sorgu; 20 gündelik, 40 ajan komutu ve vault'un en sık kelimelerinden kurulmuş 20 kontrol mesajı), hook yolu:

| | not düzeyi | 0,15 | 0,20 | 0,25 | 0,20 ve arşiv dışlaması |
|---|---|---|---|---|---|
| answerable, 2000 | 28/264 | 141/264 | 130/264 | 124/264 | 138/264 |
| answerable, 5000 | 60/264 | 178/264 | 160/264 | 147/264 | 167/264 |
| uzun sohbet mesajı, 5000 | 1/88 | 39/88 | 39/88 | 39/88 | 42/88 |
| gürültü: gündelik | 4/20 | 5/20 | 1/20 | 0/20 | 2/20 |
| gürültü: ajan komutu | 16/40 | 23/40 | 13/40 | 8/40 | 12/40 |
| gürültü: vault'un sık kelimeleri | 0/20 | 19/20 | 16/20 | 5/20 | 16/20 |

Varsayılan 0,20 bu kurala göre seçildi: gündelik ve ajan komutu gürültüsünü bugünkü not düzeyindeki yolun üstüne çıkarmayan en düşük eşik. 0,15 cevabı biraz daha sık getiriyor ama iki gürültü türünde de bugünkü yolu geçiyor; 0,25 daha sessiz, daha az cevap getiriyor. Bedel açık: yalnız vault'un en sık kelimelerinden kurulmuş mesajlarda pasaj yolu çoğu zaman bir blok ekliyor (0,20'de 16/20, not düzeyinde 0/20). Bu size fazla geliyorsa `strict_floor` değerini 0,25 yapın.

Aynı vault'ta ilk indeks kurulumu yaklaşık 3 saniye (birkaç tura bölünür), önbellek 9,1 MB (arşiv dışlanınca 5,5 MB). Ağır yük altında (10 çekirdekte yük ortalaması 40 civarı) sorgu başına ortanca süre not düzeyinde 1,8 saniye, pasaj yolunda 0,5 saniye; ikisinin de yaklaşık 0,3 saniyesi kaynak tazeliği kontrolü. Tek vault ve Türkçe ağırlıklı bir korpus üzerinde ölçüldü; kendi vault'unuzda ölçüp eşiği ona göre ayarlayın.

## Bileşen ve skill hariç tutma (susturma)

Kullanıcı gereksinim duymadığı başlangıç skill'lerini, adaptörleri, başlatıcıları veya kancaları susturabilir:

- `python3 beyin.py preferences --exclude-component skills/beyin-doktor`
- `python3 beyin.py preferences --include-component skills/beyin-doktor`

Hariç tutulabilen bileşenler:
`agents_block`, `adapters`, `adapters/hermes`, `adapters/omp`, `adapters/opencode`, `harnesses/antigravity`, `launchers`, `skills`, `skills/beyin`, `skills/beyin-doktor`, `skills/beyin-guncelle`.

Hariç tutma tercihleri vault kökünde bağımsız `.beyin-exclusions.json` dosyasında saklanır (`.beyin-preferences.json` dosyasını değiştirmez; böylece eski sürümlere rollback 3.4.0 uyumlu kalır). Tercih kaydedildiğinde bir sonraki kurulum veya güncellemede uygulanır (`update` veya `install`). Değiştirilmemiş hariç tutulan dosyalar temizlenir, kullanıcının değiştirdiği dosyalar ise çakışma vermeden korunur. `doctor` çıktısı hem uygulanan hariç tutmaları hem de bekleyen değişiklikleri gösterir.
