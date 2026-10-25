# Otomatik kontrol ve bağlam tercihleri

Kullanıcı ayarı `.beyin-preferences.json` içinde; yönetilen release dosyası değildir. Installer, updater ve rollback bu dosyayı değiştirmez. Dosya yoksa mevcut normal davranış korunur. Ortak Python uygulaması Windows, macOS ve Linux'ta aynı ayarları okur; yeni servis veya model bağımlılığı yoktur.

`python3 beyin.py preferences --profile economical` ile ekonomik, `--profile normal` ile normal, `--profile manual` ile manuel kullanım. Argüman verilmezse ayarlar okunur. Ajan bunları beyin skill'i üzerinden uygular. `--human` okunabilir çıktı sağlar.

Alanlar: `auto_sync` boolean; `interval_minutes` 0–1440 (0 her olay); `context_mode` turn/session/off; `context_chars` 1000–12000. Karakter sınırı ek hook bağlamının tamamına uygulanır; token kotası değildir. Sayısal ve bilinmeyen alanlar doğrulanır, hatalı dosya sessizce ezilmez.

Ekonomik profil: auto_sync=true, interval_minutes=15, context_mode=session, context_chars=2000. Manuel: auto_sync=false ve context_mode=off. Mevcut ayarların yalnız bir alanını değiştirmek için örneğin `preferences --interval-minutes 30`; profil seçmek otomatik kontrol ve bağlam alanlarını o profile sıfırlar, bağımsız sır süzgeci tercihini korur. Aralık değiştirmek kapalı kontrolü açmaz.

## Günlük receipt düzeni

`python3 beyin.py preferences --daily-layout flat` günlük receipt görünümlerini
`daily/YYYY-MM-DD.md` yoluna yazar. Varsayılan `v3`, mevcut `daily/v3/YYYY-MM-DD.md`
düzenini korur; geri dönmek için `--daily-layout v3` kullan. Tercih sonraki `sync` ile
uygulanır. Ayar vault kökündeki `.beyin-daily-layout.json` dosyasındadır; vault'u Git ile
eşitliyorsan bu dosyayı da eşitle. Makineye özel değildir, profil değişimleri etkilemez.
Ayrı dosya eski sürümlerin sıkı `.beyin-preferences.json` şemasıyla uyumluluğu korur;
eski motorlar bu tercihi uygulamaz, aynı vault'a yazan tüm makineleri güncelle.
Düz görünüm sahipliği ayrı `receipt_flat_views` tablosunda tutulur; 3.11.0'a
`rollback` eski motorun görünüm tablosuna tanımadığı yollar bırakmaz. Eski motor
`daily/v3/` düzenini kullanır ve mevcut düz görünümleri korur. Yeniden güncelleme
sonrası tercih tekrar uygulanır. Obsidian günlük not eklentisi düz görünümü açarsa
oraya yazılan metin de elle düzenlenmiş görünüm sayılır ve kurtarmada arşivlenir.

Flat hedefinde motorun takip etmediği, generated olmayan bir insan günlüğü varsa dosya
korunur, görünüm yazılmaz ve çakışma oluşmaz. Receipt'ler `knowledge/v3/outcomes.md`
içinde kalır; insan günlüğüne referans veren receipt'ler bağlamdan gizlenmez.
Düzen değişince eski takip edilen görünüm yalnız son yazılan baytlarla aynıysa kaldırılır.
Elle düzenlenmiş görünüm korunur ve `sync` ile `doctor` görünür uyarı verir.
`python3 beyin.py sync --recover-views` bu düzenlemeyi yanındaki `.archive/` içine
kayıpsız arşivleyip görünümü yeniden üretir veya eski görünümü kaldırır. Bu komut da
takip edilmeyen insan günlüğüne dokunmaz.

`preferences --human` etkin düzeni ve hedef yol kalıbını gösterir. Flat hedefi sembolik
bağlantıysa hedef dosyaya dokunulmaz; `sync` kaynak uyarısı ile diğer notları işlemeye
devam eder. State yeniden kurulduktan sonra eski düzende kalan `generated: true` ve
`kind: receipt-index` görünümleri de bulunur. Baytları yeni düzende üretilecek içerikle
aynıysa emekliye ayrılır; eşleşmiyorsa elle değiştirilmiş olabilecekleri için korunur
ve yalnız `--recover-views` ile kayıpsız arşivlenir. Flat dosya adı tek başına sahiplik
kanıtı değildir; state ve generated başlığı birlikte kaybolmuşsa insan notu sayılır.

Üretilmiş bir görünüme dayandığı için bağlamdan dışlanan receipt'in kimlik kanıtı
`receipts/<event-id-sha256>.md.view-provenance.json` içinde kalır. Receipt Markdown'ı
değişmez; işaret özet metni veya not içeriği taşımaz. State silinip yeniden kurulduğunda
ya da başka makineye geçildiğinde bu receipt dışarıda kalır; aynı eski günlük yolunu
sonradan kullanan insan notuna dayanan **yeni kimlikli** receipt'ler görünür olabilir.
`receipts/` eşitlenirken bu JSON dosyalarını da eşitle ve geçmiş kanıtı korumak için
silme. İşaret bozuksa sessizce onarılmaz; ilgili receipt dışarıda tutulur, uyarı verilir
ve mevcut görünümler korunur. İşaret yazılamadığında veya POSIX dizin `fsync` başarısız
olduğunda da görünümün son kopyası silinmez; sorun giderilip `sync` yeniden çalıştırılır.
Windows'ta Python'un standart API'siyle dizin `fsync` yoktur; dosya `fsync` ve atomik
yenileme uygulanır. Eski motorlar yeni JSON işaretlerini okuyamaz; dosya biçimi onları
bozmasa da state kaybından sonraki bu dışlama güvencesi için tüm makineleri güncelle.
Önceki sürümden kalan SQLite dışlama satırları ilk `sync` sırasında bu işaretlere
aktarılır. Güncellemeden önce hem state hem eski görünümün kanıtı zaten kaybolmuşsa
geçmiş güvenle çıkarılamaz; eski state veya vault yedeği gerekir. İnsan günlüklerini
yanlışlıkla gizlememek için yalnız dosya adından geçmiş sahiplik uydurulmaz.

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

## Oturum başı hafıza bağlamı

Oturum başındaki hafıza bağlamı (ve "nerede kaldık" gibi süreklilik soruları) `Core`, `Kurallar`,
`Last-Session`, `Threads` ve `Journal` dosyalarını birlikte taşır. Yalnız iki handoff dosyasının
sınırları 3.000 + 8.000 karakter olduğundan 12.000 tavanı dar kalabilir.
`python3 beyin.py preferences --companion-context-chars 24000` bu açılış bağlamına 1.000 ile
24.000 arasında ayrı bir bütçe verir; `0` yeniden `context_chars` değerini kullanır. Diğer
turlardaki bağlam `context_chars` ile sınırlı kalır. Ayar vault tercih dosyasında değil,
runtime klasöründeki `companion-context.json` dosyasında tutulur: eski sürümler tercih
dosyasındaki bilinmeyen alanı ya da aralık dışı değeri reddettiği için rollback güvenli
kalır. Bozuk dosya açılışı `context_chars` sınırına döndürür ve kendiliğinden ezilmez.

İstemcinin kendi sınırı bu bütçeden önce gelir. Claude Code, hook'un `additionalContext`
metni 10.000 karakteri aşınca metni oturum klasöründe bir dosyaya yazar ve modele yalnız dosya
yolu ile ilk ~2.000 karakterlik önizlemeyi verir; bu sınırı yükselten bir ayar yoktur. Dolu
companion'da önizlemeye yalnız `Core` ve `Kurallar` sığar, `Last-Session`, `Threads` ve
`Journal` otomatik bağlamdan düşer (#175). Bu yüzden Claude Code oturumlarında hem açılış hem
tur bağlamı en fazla 9.500 karakterle üretilir (`context_chars` 12.000 olsa da); `preferences`
çıktısı bunu `client_context_notice` ile söyler. Codex de varsayılan olarak 10.000 UTF-8 baytı
(4 baytlık 2.500 yaklaşık token) aşan hook bağlamını dosyaya taşır ve modele yalnız baş ve son
önizlemeyi verir; ortası düşer (`codex-rs/hooks/src/output_spill.rs`). Türkçe harfler 2 bayt
tuttuğu için Codex oturumlarında bağlam aynı 9.500 karakter bütçesiyle üretilir, metin 10.000
baytı geçerse bütçe orantılı küçültülüp yeniden üretilir; bölümler sondan kesilmez. Codex'in
hook başına `additionalContextLimit` ayarı `.codex/hooks.json` içindedir ve bu dosyayı
değiştirmek hook güvenini düşürdüğü için Beyin onu kullanmaz. Antigravity, OpenCode, Hermes ve
OMP için böyle bir sınır ölçülmedi.

Açılışta hafıza dosyaları kırpılıyorsa sorgusuz seçilen ilgisiz bir not eklenmez; kalan
bütçe kırpılan dosyalara döner. Dosyalar tam sığıyorsa en fazla 1.500 karakterlik bir
güncel not eklenebilir.

`Kurallar.md`'nin bu bütçede en fazla kaç karakterinin tam geldiğini `doctor`
`companion_rules_opening` altında söyler; kurallar kırpılıyorsa `--human` çıktısı bunu tek
satırla bildirir (#374). Sayı öbür companion dosyalarının boyutuyla ve receipt'le değişir.
Core 544, Last-Session 1.743, Threads 543 karakter ve bir receipt taşıyan örnek bir kasada
varsayılan 5.000 karakterlik açılış yaklaşık 1.550-1.750, 8.000 yaklaşık 3.050-3.300
karakter gösterir; satır başına ~50 karakterlik kurallarla bu 30 ile 60 kural arasıdır.

## Opt-in sır süzgeci

`python3 beyin.py preferences --secret-filter on` komutu receipt özeti, note-create ve task-create gövdesi, bu komutların ve task-update değişikliklerinin `title`, `next_action`, `completion_criterion` ve `facts` alanlarında yaygın erişim anahtarı biçimlerini yazmadan önce `[REDACTED]` ile değiştirir. Varsayılan kapalıdır; profil değişikliği bu bağımsız tercihi değiştirmez. Kapatmak için `--secret-filter off` kullan.

Ek sabit sır değerleri vault dışındaki runtime klasöründe `secret-patterns.txt` dosyasına, satır başına bir değer olarak yazılabilir. Dosya regex çalıştırmaz; yorum satırları `#` ile başlar. Eşleşen metinler veya değerler sağlık kaydına yazılmaz, yalnız toplam eşleşme sayısı `doctor` sonucunda gösterilir. Bu önlem kazara kalıcı yazımı azaltır; tam bir DLP veya önceden yazılmış notları temizleme aracı değildir.

Süre en son otomatik başlatmaya göre yerel SQLite kaydıyla, istemciler arasında atomik olarak sınırlandırılır. Bir sonraki olay gelmedikçe kontrol çalışmaz. Yeni oturumda daima taze kontrol; kaydedilmiş worker hatasında yeniden deneme. Bu düşük seviyeli kontrol model çağırmaz. Otomatik bağlam kapalı olsa bile açık not/görev/receipt ve context komutları çalışır. Kapatmadan önce başlatılmış bir işlem tamamlanabilir; önceden bekleyen metadata saklanır. Açık `--drain-queue` bakım komutu bu kuyruğu işler.

Aralığa takılmış bir kontrolde turn bağlamı istenirse eski kayıtları güncel diye sunmak yerine kısa tazeleme uyarısı verilir. Ekonomik modda sonraki mesajlarda tekrar bağlam eklenmez; beyin skill'i bilgi gerektiğinde kaynakları doğrudan tazeler.

Özel V2 cron/LaunchAgent/Task Scheduler işleri veya kullanıcının ayrı kurduğu 15 dakikalık ücretli ajan otomasyonları bu tercihlerle kapatılmaz. Önce ilgili işi tespit edip ayrı yönetmek gerekir. V3 kendiliğinden Luna/Sonnet çalıştırmaz.

Bu değişiklik yerel adaydır; yayımlanmış v3.0.0 paketine otomatik olarak eklenmez. Yeni paket yayımlanmadan kullanıcılara mevcut sürüm özelliği diye duyurulmamalıdır.

## Projeyi tanıyan oturum başı

`python3 beyin.py preferences --project-context on` komutu, [global köprünün](GLOBAL-BRIDGE.md) vault dışındaki bir projede açılan oturumun başına o projeye ait kısa bir blok eklemesini sağlar. Varsayılan kapalıdır; kapalıyken köprünün çıktısı değişmez. Vault içindeki oturumlar etkilenmez. Açıldığında:

- Aynı `project_id`'ye (klasör yolunun hash'i) bağlı en yeni receipt özeti tek satır olarak eklenir, en fazla 600 karakter. Receipt'in kaynak dosyası silinmişse ya da receipt veya `refs` içindeki bir kaynak `private` ya da güvenilmez ise o receipt atlanır ve bir öncekine bakılır.
- O projenin tarihi gelmiş görevleri (`due_at` yerel tarihe göre bugün ya da geçmiş, durum `active` veya `waiting`) başlık ve sonraki adımla listelenir. Görev projeye `project` alanı klasör adıyla eşleşerek (büyük/küçük harf duyarsız) bağlanır; aynı adlı iki klasör aynı görevleri görür.
- Görevler normal bağlamla aynı kapılardan geçer: `private` ve güvenilmez kayıt, supersede edilmiş görev ve indekslendikten sonra değişmiş kaynak girmez.
- Başka projelerin ya da projesiz görevlerin başlığı verilmez, yalnız sayı yazılır: `(bu proje disinda N tarihi gelmis gorev)`.
- Blok köprünün `--context-chars` bütçesinden geriye kalanla ve en fazla 1.200 karakterle sınırlıdır. Görev varsa receipt özeti bu alanın yarısını aşmaz. Satırlar bütün olarak eklenir; sığmayan görevler `(+N gorev sigmadi)` ile sayılır.
- Blok salt okunur SQLite okumasıyla kurulur; model çağrısı ve yazma yoktur. Okuma başarısız olursa yalnız bu blok düşer, köprünün başlangıç talimatı aynen verilir.
- Ayar, eski sürümler bilinmeyen tercih alanını reddettiği için `.beyin-preferences.json` içinde değil, runtime klasöründeki `project-context.json` dosyasında tutulur; makineye özeldir ve rollback güvenlidir. Kapatmak için `--project-context off`.

## Hassas klasörlerin korunması

Adında `Kasa`, `Şifreler`, `Müşteriler`, `Özel`, `Private` gibi hassasiyet bildiren bir sözcük bulunan klasörler ana hafıza taramasına alınmaz. Aynı kural alt klasörlerde de geçerlidir; Türkçe harfler, büyük/küçük harfler ve baştaki emoji veya numaralar fark etmez. Bu dosyalar silinmez ya da değiştirilmez.

Eski sürümden kalan bir indeks henüz senkronize edilmemiş olsa bile bu klasörlerdeki kaynaklar otomatik hafıza bağlamında gösterilmez. Bu kaynaklara bağlı receipt özetleri de otomatik açılış bağlamından çıkarılır. Görünür bir hafıza kaydı istiyorsan dosyayı hassas klasör dışında tutmalısın. `visibility: private` kişisel notlar için ayrıca kullanılabilir; yalnızca klasör adı, dosyanın işletim sistemi ve Git izinlerini değiştirmez.

## Opt-in hijyen sinyalleri

Üç sinyal vardır ve üçü de varsayılan olarak kapalıdır; açılmadıkça hook çıktısı ve `doctor` raporu değişmez:

```bash
python3 beyin.py preferences --word-cap-warning on --max-words 800
python3 beyin.py preferences --folder-questions on
python3 beyin.py preferences --promotion on
```

- `--word-cap-warning`: Claude veya Codex bir notu yazdıktan sonra (PostToolUse) not tavanı aşıyorsa tek satırlık bir bölme sinyali verir. Bu bir sinyaldir, dosya bölünmez ya da taşınmaz. Tavan `--max-words` ile 10 ile 100000 arasında ayarlanır (varsayılan 500). Codex'in `apply_patch` düzenlemeleri de ölçülür. Antigravity, OpenCode, OMP ve Hermes'te bu sinyal gösterilmez.
- `--folder-questions`: oturum başında 14 günden uzun süredir sessiz üst klasörler için en fazla üç soru ekler. Yalnız klasörün kendisine ve doğrudan içeriğine bakılır, vault taranmaz. Aynı klasör, yeniden hareketlenene kadar bir kez sorulur.
- `--promotion`: düzenlenen notların vault içi yollarını runtime klasöründeki `touch-log.tsv` dosyasına yazar; `doctor` bunlardan sıcak ve soğuk klasör raporu çıkarır. Taşıma kararı her zaman senindir.

Companion klasörü, kasa sınıfı adlar (`Kasa`, `Şifreler`, `Müşteriler`, `Özel`, `Private` gibi, emoji ya da numara önekli yazımlar dahil), arşiv, şablon ve kod klasörleri bu sinyallerin hepsinden muaftır. Ayarlar, eski sürümler bilinmeyen tercih alanını reddettiği için `.beyin-preferences.json` içinde değil, runtime klasöründeki `hygiene.json` dosyasında tutulur; makineye özeldir, profil değişimi onlara dokunmaz ve rollback güvenlidir. Kapatmak için aynı seçeneği `off` ile ver.


### Gelen kutusu raporu

```bash
python3 beyin.py preferences --inbox-report on --inbox-max-items 10 --inbox-max-days 7
```

Varsayılan kapalıdır. Açıkken `doctor`, adında `inbox` sözcüğü ya da `gelen kutusu` ifadesi geçen üst klasörlerdeki (`📥 000-Inbox`, `00_INBOX`, `Gelen Kutusu`) Markdown notlarını sayar ve en eski notun yaşını gösterir (`inbox` alanı). Yaş notun frontmatter `created` tarihinden alınır; tarih yoksa ya da gerçek bir tarih değilse dosya değişiklik zamanına düşer (klon ve eşitleme istemcileri bu zamanı sıfırlayabilir). Gelen kutun başka bir adla duruyorsa üst klasör adını kendin ver; liste ad tanımanın yerine geçer, büyük/küçük harf ve Unicode yazımı fark etmez:

```bash
python3 beyin.py preferences --inbox-folder "Yakalama" --inbox-folder "📥 Notlar"
python3 beyin.py preferences --inbox-folder ""   # listeyi boşalt, ad tanımaya dön
```

Listede olup vault'ta bulunmayan klasör `error: not_found` ile gösterilir. Not sayısı `--inbox-max-items` değerine ya da en eski not `--inbox-max-days` gününe ulaşan klasör `attention` ile işaretlenir; bu yalnız bir sinyaldir, `doctor` durumunu değiştirmez. Hiçbir not taşınmaz, sınıflandırılmaz ve ajan başlatılmaz; işleme kararı senindir. Okunamayan klasör boş sayılmaz, `error` ile listelenir. Companion ve kasa sınıfı klasörler muaftır, nokta klasörler ve sembolik bağlar sayılmaz. Eski sürümler bilinmeyen `hygiene.json` anahtarını reddettiği için ayar runtime klasöründe ayrı bir `inbox-report.json` dosyasında tutulur; rollback diğer hijyen sinyallerini kapatmaz. Kapatmak için `--inbox-report off`.

## Zamanlanmış sağlık denetimi ve bildirim

Otomatik kontroller bir istemci olayıyla çalışır; hiçbir istemci açılmazsa `doctor`'un gördüğü sorun (sync çakışması, bayat hook yolu, skill/talimat çakışması, görev ve geçerlilik sorunları) bir sonraki oturuma kadar görünmez (#267). `beyin.py scheduled-check` bunu senin kurduğun işletim sistemi zamanlayıcısıyla kapatır: önce `sync`, sonra `doctor` çalıştırır ve sonucu runtime klasöründeki `scheduled-check.json` dosyasına yazar. `doctor` son koşuyu `scheduled_check` alanında yaşıyla gösterir (hiç kurulmadıysa `never_run`); bu alan bilgi amaçlıdır, durumu değiştirmez. Komut model çağırmaz, servis kurmaz, hiçbir şeyi onarmaz; Beyin zamanlayıcı kurmaz.

Çıkış kodu: `0` sağlıklı, `4` dikkat gerekiyor, `1` denetimin kendisi tamamlanamadı, `3` başka bir denetim hâlâ çalışıyor. `2` argparse'ın kullanım hatasıdır (yanlış yazılmış komut, eski sürümde olmayan seçenek); "dikkat" anlamına gelmez. Kilit işletim sisteminin dosya kilididir: çöken bir koşunun kilidi onunla birlikte bırakılır, beklenecek bayat kilit yoktur.

Sonuç dikkat gerektirdiğinde satır runtime klasöründeki `scheduled-notice.json` dosyasına yazılır ve bir sonraki ajan oturumunun açılışında tek satır olarak bir kez söylenir. Bu kanal her zaman çalışır; `--notify` olsun olmasın.

`--notify` eklersen (varsayılan kapalı) aynı anlarda işletim sistemi bildirimi de gösterilir. Ek bağımlılık yoktur: Windows'ta Windows PowerShell 5.1 toast (`pwsh` 7'de bu türler yok), macOS'ta `osascript`, Linux'ta `notify-send` (yoksa bildirim sessizce atlanır, oturum açılışındaki satır yine çıkar). Bildirimin gerçekten görüldüğü bilinemez (`osascript` Odak modunda da başarı döner, Windows toast'ı sessizce düşebilir); bu yüzden bildirim ek kanaldır, oturum satırının yerini tutmaz.

- Yalnız durum değişince: sağlıklıdan dikkate geçişte bir kez, düzelince bir kez (`Beyin: sorunlar giderildi, kasa saglikli.`). Sorun sürdükçe her gece tekrar etmez; yedi günde bir hatırlatır. Denetimin kendisi tamamlanamazsa bildirim değişmez, `doctor` `error` gösterir.
- Metin sabit kalıplardan ve sayılardan oluşur; not adı, yol ya da proje adı yazmaz: `Beyin: sync cakismasi (2), hook uyarisi (1) - ayrinti: beyin.py doctor`. Metin bildirim aracına kabuk üzerinden değil argüman olarak geçer; her alt süreçte zaman aşımı vardır.
- Oturum satırını ilk açılan oturum bir kez söyler; sorun düzelirse söylenmemiş satır geri alınır.
- Bütün durum (son koşu, son söyleme zamanı, bekleyen satır) makineye özel runtime klasöründe ayrı dosyalardadır, vault'a ve `.beyin-preferences.json`'a yazılmaz; rollback bu yüzden bozulmaz ve her makine kendi sağlığını kendisi bildirir. `doctor` bu dosyalardan yalnız bildiği alanları okur, hiçbirine yazmaz; oturum açılışı yalnız küçük satır dosyasını okur, vault taraması yapmaz.

Bildirim yalnız kullanıcı oturumunda çalışan bir zamanlayıcıdan ekrana gelir. Aşağıdaki örnekler bu yüzden kullanıcı oturumunda çalışır; yolları kendi vault'unla değiştir.

**Windows** (Görev Zamanlayıcı, "yalnız kullanıcı oturum açıkken çalıştır": `/RU` verilmeyen görev böyle kurulur; "oturum açık olsun olmasın" seçilirse toast ekrana gelmez). PowerShell'de `--%` satırın geri kalanını `schtasks`'a olduğu gibi geçirir; o olmadan Windows PowerShell 5.1 `/TR` içindeki `\"` kaçışlarını bozar ve vault yolu `/TR` değerinden kopar. `cmd.exe`'de `--%` yazılmaz:

```powershell
schtasks --% /Create /TN "Beyin saglik" /SC DAILY /ST 20:00 /TR "py -3 \"C:\Users\SEN\Beynim\beyin.py\" scheduled-check --notify"
```

**macOS** (LaunchAgent). `/usr/bin/python3` macOS'ta 3.9'dur; Beyin 3.11 ve üstünü ister. `python3 --version` ile 3.11+ olan yolu bul (Homebrew: `/opt/homebrew/bin/python3`, python.org: `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3`). Vault iCloud Drive, Belgeler ya da Masaüstü altındaysa macOS bu klasörleri arka plan işlemlerine kapatır; Sistem Ayarları › Gizlilik ve Güvenlik › Tam Disk Erişimi'ne bu `python3`'ü ekle. Bildirim `osascript` üzerinden geldiği için Bildirim Merkezi'nde "Betik Düzenleyici" (Script Editor) adıyla görünür; ilk bildirimde izin isteyebilir. `~/Library/LaunchAgents/com.avenox.beyin-saglik.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.avenox.beyin-saglik</string>
  <key>ProgramArguments</key><array>
    <string>/opt/homebrew/bin/python3</string>
    <string>/Users/sen/Beynim/beyin.py</string>
    <string>scheduled-check</string><string>--notify</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/Users/sen/Library/Logs/beyin-saglik.log</string>
  <key>StandardErrorPath</key><string>/Users/sen/Library/Logs/beyin-saglik.log</string>
</dict></plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.avenox.beyin-saglik.plist
```

**Linux** (systemd user timer; çıktı `journalctl --user -u beyin-saglik` ile okunur). Beyin Python 3.11 ve üstünü ister; dağıtımın `/usr/bin/python3`'ü daha eski olabilir. `/usr/bin/python3 --version` ile bak, eskiyse `ExecStart`'a 3.11+ olan yolu yaz (ör. `command -v python3.11` çıktısı). `~/.config/systemd/user/beyin-saglik.service`:

```ini
[Unit]
Description=Beyin saglik denetimi

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /home/sen/Beynim/beyin.py scheduled-check --notify
SuccessExitStatus=4
```

`~/.config/systemd/user/beyin-saglik.timer`:

```ini
[Timer]
OnCalendar=*-*-* 20:00
Persistent=true

[Install]
WantedBy=timers.target
```

```sh
systemctl --user enable --now beyin-saglik.timer
```

Düz `cron` kullanıcı oturumu dışında çalışır; bildirim ekrana gelmez ama kayıt ve oturum açılışındaki satır yine çalışır. Çıktıyı `/dev/null`'a atma, bir günlük dosyasına yönlendir (`>> ~/beyin-saglik.log 2>&1`); macOS'ta cron için de Tam Disk Erişimi gerekir.

Kaldırmak için zamanlayıcı görevini sil; Beyin tarafında kapatılacak bir ayar yoktur.

Nerede nasıl doğrulandı:

| Platform | Gerçek masaüstünde | Yalnız testle |
|---|---|---|
| Windows 11 (PowerShell 5.1) | Görev Zamanlayıcı'dan `scheduled-check --notify`: bildirim Bildirim Merkezi'ne düştü, ama tam ekran video açıkken ekrana açılır pencere olarak gelmedi (Windows bu sırada bildirimleri yalnız merkezde tutar; komut yine 0 döndü). Oturum açılışında satır bir kez söylendi | Kilit çakışması, durum geçişleri, argüman literal'i (gerçek `powershell.exe` ile) |
| macOS | Denenmedi | `osascript` argv biçimi, sahte bildirim aracıyla |
| Linux | Denenmedi | `notify-send` argv biçimi ve yokken atlanması |

## Paralel oturum bildirimi

Aynı vault'ta birden çok oturum açıkken ajanlar ortak git index'ine, ortak geçici dosyalara ya da aynı nota dokunabilir. Kart tarafı companion protokolüyle çözülü; bu bildirim kartın dışında kalan ortak şeyler içindir (#170). Varsayılan kapalıdır:

```bash
python3 beyin.py preferences --parallel-sessions on
```

- Açıkken her gerçek kullanıcı isteminde (UserPromptSubmit; ilk istemi SessionStart olarak gönderen Hermes, OpenCode ve Antigravity'de SessionStart) oturum kendi işaretini yazar. Son 45 dakikada etkin olan ve bu oturumda henüz duyurulmamış başka oturum varsa bağlamın başına tek satır eklenir: `[Paralel oturum] Bu vault'ta 2 oturum daha acik: #3f9a1c2b (3 dk once), #e5f6a7b8 (14 dk once). Ayni dosyaya dokunmadan once diskten yeniden oku; commit oncesi git status.`
- Kimlik, o oturumun `Receipt session=` değerinin ilk 8 karakteridir; öbür oturumun Last-Session kartı başlığından bulunur. Satırda en fazla iki oturum adıyla yazılır, fazlası sayılır. Aynı oturum bir oturumda bir kez duyurulur; sonradan açılan bir oturum bir sonraki istemde bir kez söylenir. Alt ajan bildirimleri gibi sentetik turlar ne işaret yazar ne satır alır.
- İşaret runtime klasöründe `session-markers/<sha256(harness + "\0" + session_id)>.json` dosyasıdır ve yalnız `schema, harness, session, first_at, last_at, announced` taşır: istem metni ve çalışma klasörü yazılmaz. Runtime klasörü vault başına ve makineye özel olduğundan başka vault'taki ya da başka makinedeki oturumlar sayılmaz.
- SessionEnd kendi işaretini siler (`/clear` sonrası hayalet oturum kalmaz). Her yazımda 24 saatten eski işaretler silinir ve en fazla 128 işaret tutulur. Okunamayan işaret yok sayılır; işaret hatası hook'u düşürmez, yalnız satır düşer.
- Ayar, eski sürümler bilinmeyen tercih alanını reddettiği için `.beyin-preferences.json` içinde değil, runtime klasöründeki `parallel-sessions.json` dosyasında tutulur; rollback güvenlidir. Kapalıyken hook çıktısı değişmez ve işaret yazılmaz. `doctor` açıkken etkin işaret sayısını salt okunur olarak yazar. Kapatmak için `--parallel-sessions off`.

## Oturum içi bağlam tekrarını önleme (`--context-dedupe`)

`context_mode: turn` açıkken kanca her kullanıcı isteminde leksikal arama yapar. Uzun ve odaklı oturumlarda aynı notların her mesajda tekrar tekrar modele iletilmesi bağlam penceresini gereksiz yere doldurur (#363). Oturum içi tekrar önleme (de-duplication) etkinleştirildiğinde:

```bash
python3 beyin.py preferences --context-dedupe on
```

- Kanca, bu oturumda daha önce teslim ettiği kayıtları (`id`, `source_sha256` ve metin penceresi) runtime klasöründe `session-delivery/<sha256(harness + "\0" + session_id)>.json` dosyasında tutar. Oturum kimliğinin kendisi yazılmaz.
- Mesaj bağlamı pasaj tabanlıdır: uzun bir notun başka bir bölümü (aynı `id`, farklı pencere) yeni sayılır ve teslim edilir; aynı pencere kısaltılmış olsa da tekrar sayılır. Geri tutulan kayıtların bütçesi yeni kayıtlara kalır.
- Arama sonucunda gelen not daha önce iletildiyse ve dosya diskte değişmediyse filtrelenir; mesajda yalnızca yeni ya da güncellenen notlar modele teslim edilir.
- Notların ardından eklenen son receipt bloğu da aynı defterle izlenir; defterde receipt metninin yalnız sha256 değeri tutulur. Aynı receipt oturumda bir kez verilir; yeni bir receipt yazılınca ya da receipt dosyası değişince ve sıkıştırmadan sonra yeniden verilir. Bütçeye sığmadığı için gösterilmeyen receipt teslim edilmiş sayılmaz.
- Bir istemde hiç yeni not kalmazsa kanca boş ek bağlam (`{}`) döner; gereksiz token tüketimi ve dikkat dağınıklığı önlenir. Eşleşen notların hepsi geri tutulduğu halde son receipt bu oturum için yeniyse yalnız receipt verilir. Hiç not eşleşmeyen istemde, tekrar önleme kapalıyken olduğu gibi receipt de eklenmez.
- **Sıkıştırma (Compaction):** Model bağlamı sıkıştırdığında (`PreCompact` veya Claude Code `SessionStart` olayında `source: compact`), eski notlar pencereden düştüğü için teslimat defteri otomatik sıfırlanır.
- `SessionEnd` oturumun teslimat defterini temizler. Dosya diskte düzenlenirse `source_sha256` değiştiği için not yeni sayılır ve tekrar teslim edilir.
- Tümü tekrar sayılan bir istem süreklilik çapasını (`topic-refs`) silmez; sıkıştırmadan sonra gelen kısa bir devam mesajı konuyu kaybetmez.
- Defter oturum başına en çok 512 kayıt ve kayıt başına 8 pencere tutar (en eskisi düşer). Her yazımda 24 saatten eski defterler silinir ve en fazla 128 defter tutulur.
- Ayar, rollback güvenliği için `.beyin-preferences.json` içinde değil, runtime klasöründeki `context-dedupe.json` dosyasında tutulur (varsayılan kapalı). Kapatmak için `--context-dedupe off`.

## Stop'ta receipt hatırlatması

Kurulum, Claude ve Codex için PostToolUse hook'unu yalnız dosya düzenleyen araçlara bağlar (`Edit|Write|apply_patch`). Bu olay geldiğinde hook, vault dışındaki runtime klasörüne oturum kimliğinin hash'iyle adlandırılmış küçük bir düzenleme işareti yazar; transcript okunmaz. Kabuk komutuyla yapılan düzenlemeler bu olayı tetiklemez. Stop'ta runtime kaydında aynı istemci ve aynı `session` değeriyle, düzenlemelerden sonra yazılmış bir receipt yoksa hook oturum başına bir kez Stop'u engeller ve `python3 beyin.py receipt --harness claude --session <değer> --event-id ... --summary ... --ref ...` komutunu (Codex için `--harness codex`, Windows'ta `py -3`; JSON alternatifi `--file RECEIPT_JSON`) `Receipt session=<değer>` bilgisiyle birlikte hatırlatır. Bu değer `--session` ile ya da receipt JSON'undaki `session` alanına yazılmazsa receipt bu checkpoint'i kapatmaz. Receipt'ten sonra yapılan yeni düzenlemeler yeni bir pencere açar.

Stop olayı hatırlatmadan önce kuyruğa alınır. Runtime kaydı okunamazsa akış durdurulmaz. `stop_hook_active` taşıyan ikinci Stop, manuel profil (`auto_sync: false`) ve global köprü hatırlatma yapmaz; tek seferlik hakkı da harcamaz. Kullanıcı mesajında `[kaydetme]` yazarak bu oturumdaki hatırlatmayı kapatabilir; `BEYIN_V3_NO_RECEIPT_REMINDER=1` özelliği tamamen kapatır.

Receipt özetinde kalıcı bir öğrenim ayrı bir satırda, satır başında beyan edilir: `Öğrenilen: <tek cümle>` (ya da `Ders:`, `Learned:`, `Kalıcı öğrenim:`). Öğrenim yoksa gerekçe aynı satırda belirtilir: `Öğrenilen: yok (rutin kontrol)`. `yok`, `hiçbiri`, `bulunmadı`, `none` gibi cevaplar ve boş etiket öğrenim beyanı sayılmaz. Dosya düzenlenen bir oturumda gerekçesiz yalın `Öğrenilen: yok` cevabı bir kez sorgulanır; boş etiket liste başlığı olabileceğinden sorgulanmaz. Cevabın sonundaki parantezli açıklama da (`Öğrenilen: yok (rutin kontrol)`) cevabı değiştirmez; parantezden sonra gelen metin ise beyan sayılır. Etiket yalnız kendi satırında okunur; metin içinde geçen `machine learning:` gibi ifadeler beyan değildir. Receipt öğrenim beyan ettiği halde oturumda `knowledge/`, `🧠 500-Knowledge/` veya `500-Knowledge/` altında bir not yazılmadıysa ya da refs içinde böyle bir not yoksa, Stop kancası oturum başına bir kez ajana öğrenimi `knowledge/concepts/` altına damıtmasını hatırlatır. `knowledge/index.md`, `knowledge/log.md` ve `knowledge/v3/` damıtma sayılmaz; bunlar derleyici, git veya eşitleme tarafından da değişebilir. Receipt hatırlatmasına cevap olarak yazılan receipt, `stop_hook_active` taşıyan Stop'ta değil sonraki Stop'ta kontrol edilir. Kalıcı kavram gerekmiyorsa ajanın bunu tek cümleyle belirtmesi yeterlidir; kanca ikinci kez engellemez. Henüz kalıcı nota dönüşmemiş öğrenimler `python3 beyin.py learn --session SESSION --candidate "Öğrenim" --source notes/kaynak.md --scope PROJE` ile runtime içindeki `learning-state.jsonl` defterine kaydedilebilir; `learn --list` aktif adayları listeler. `--scope` verilmezse kapsam globaldir. Session değeri hook'un verdiği değer olmalıdır. Bu düzenleme penceresinin başlangıcından sonra kaydedilen bir aday, hem bilgi damıtımı hatırlatmasını hem de yalın `yok` sorgusunu karşılar; eski aday sonraki pencerelerde geçerli değildir. Gerekçeli `yok` cevabında, boş etikette ve hatırlatma gerekmeyen diğer receipt'lerde klasör taranmaz. Defter yerel bir ara kayıttır; otomatik damıtma yapmaz.

`beyin.py doctor` komutu sessiz durgunluğu görünür kılmak için son bilgi damıtmasının kaç gün önce yapıldığını ve o tarihten bu yana kaç receipt kaydedildiğini yazar. Gerçek çıktı ASCII'dir, diğer doctor satırları gibi: `Son bilgi damitmasi: 3 gun once (o tarihten beri 12 makbuz)`; hiç kavram notu yoksa `Son bilgi damitmasi: henuz kavram notu damitilmadi (toplam N makbuz)`; ölçüm başarısız olursa `Son bilgi damitmasi: olculemedi (<hata>)`. Stop hatırlatıcısıyla aynı klasörler sayılır: `knowledge/`, `🧠 500-Knowledge/` ve `500-Knowledge/` altındaki Markdown notları. Bu yüzden elle düzenlenen bir `🧠 500-Knowledge/` notu da damıtma sayılır ve sayacı sıfırlar; 3.9.0 ve öncesinde doctor yalnız `knowledge/` klasörüne bakıyordu. Frontmatter'ında `generated: true` olan notu doctor saymaz; Stop hatırlatıcısı bu ayrımı yapmaz. Tarih notun frontmatter'ındaki `updated` ya da `modified` alanından, bu alan yoksa dosya zamanından okunur; çünkü git checkout ve iCloud geri yüklemesi dosya zamanını sıfırlar. `knowledge/index.md`, `knowledge/log.md` ve `knowledge/v3/` sayılmaz, her nottan yalnız ilk 4 KB okunur.

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
- `strict_exclude`: vault köküne göre en fazla 64 yol öneki. `daily/` ve `receipts/` her zaman dışarıdadır, liste bunlara eklenir. Karşılaştırma büyük/küçük harfe ve Unicode biçimine (NFC/NFD) duyarsızdır, `\` ayırıcısı da kabul edilir. Arşiv ya da oturum dökümü klasörleri için uygundur. Bu ayar strict arama yollarında, not düzeyi yedek arama dahil, sonuçları eler. `context_mode: session` açılışındaki companion kaynak seçimine uygulanmaz ve açık non-strict geçmiş aramasını engellemez. Arşiv klasörünü indeksten tamamen uzak tutmak için aşağıda anlatılan `--index-exclude-dir` tercih edilmelidir.
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

## İndeks dışı klasör adları (`index.json`)

Vault'un içine gömülü bir kod deposu varsa, o deponun taşıdığı başka projelerin belgeleri (`3rdparty/SDL2/README.md`, `SECURITY.md` gibi) senin notların gibi indekse girer ve aramada gerçek notların önüne geçebilir. Hangi klasör adlarının indekse hiç girmeyeceğini sen söylersin:

```bash
python3 beyin.py preferences --index-exclude-dir 3rdparty --index-exclude-dir target
python3 beyin.py preferences --index-include-dir target   # listeden çıkar
python3 beyin.py sync
```

- Liste klasör **adı** tutar, yol değil: `3rdparty` yazınca vault'un her derinliğindeki `3rdparty` klasörleri taranmaz. Eşleşme yol parçasının tamamıyla yapılır (`3rdparty-notlari` ya da `my3rdparty` etkilenmez), büyük/küçük harf ve Unicode biçimi (NFC/NFD) fark etmez, Türkçe `İ/i/I/ı` aynı harf sayılır. En fazla 64 ad.
- Beyin kendiliğinden yalnız `node_modules`, `receipts`, `__pycache__`, nokta ile başlayan klasörleri ve sembolik bağları atlar; bu adları listeye yazmak serbesttir ama hiçbir şeyi değiştirmez. `vendor`, `external`, `third_party` gibi adlar **otomatik atlanmaz**: bir vault'ta gömülü kod ağacı olan `Vendor`, başka bir vault'ta tedarikçi notlarının klasörüdür ve bir güncellemeyle notların sessizce aramadan düşmesi istenmedi. Bunun yerine `doctor`, bu adlardan birini taşıyan ve içinde `README.md`, `SECURITY.md`, `CONTRIBUTING.md` gibi yukarı akış belgeleri bulunan indeksli klasörleri sayısıyla birlikte gösterir (`Indeks kapsami (bilgi): ...`) ve çalıştırılacak komutu yazar. Karar senindir.
- `notes`, `knowledge`, `tasks` ve companion klasörü listeye alınamaz: `note-create` ve `task-create` yazdığını indeksten geri okur, oturum başı hafıza bağlamı da companion dosyalarını indeksten alır. Komut bu adları gerekçesiyle reddeder. Aynı nedenle `note-create` ve `task-create`, listedeki bir adın altına düşen yeni bir kaynağı dosyayı yazmadan reddeder.
- Etkin liste her yerde görünür: `sync` çıktısında `index_exclusions` (`excluded_dirs`, bu taramada atlanan klasör sayısı `pruned` ve ilk 20 yol `pruned_dirs`), `doctor` ve `preferences` çıktısında `Indeks disi klasor adlari` satırı. Listen yoksa bu çıktılar değişmez.
- Bir ad eklendiğinde bir sonraki `sync` o klasörlerdeki kayıtları indeksten siler (`deleted` sayısı, `delete` olayları) ve arama onları getirmez; dosyalara dokunulmaz. Ad listeden çıkarıldığında bir sonraki `sync` aynı dosyaları yeniden indeksler. Atlanan klasörler hiç listelenmez ve okunmaz, yani büyük bir ağaç `sync` süresine de yük olmaz.
- Ayar makineye özeldir ve runtime klasöründeki `index.json` dosyasında durur (`{"schema": 1, "excluded_dirs": ["3rdparty"]}`); `.beyin-preferences.json` şemasına girmez. Runtime klasörünün yolu `python3 beyin.py doctor --json` çıktısında `state_location.effective_state` alanındadır (varsayılan konumlar için bkz. QUICKSTART). Dosyayı elle de yazabilirsin; kurallar aynıdır.
- Dosya bozuksa, sembolik bağsa, 64 KB'ı aşıyorsa ya da tek bir geçersiz giriş içeriyorsa (yol ayırıcı, `.`/`..`, boş ad, korunan ad, bilinmeyen anahtar) listenin **hiçbiri** uygulanmaz: `sync` bozulmaz, her şey indekslenir ve neden `sync` çıktısındaki `index_exclusions.error` alanında ve `doctor`da `index.json gecersiz (...)` satırında görünür. Yarım uygulanmış bir liste yerine görünür bir hata tercih edildi.
- Bu bir gizlilik sınırı değildir. Bu ayarı tanımayan bir sürüme (3.9.0 ve öncesi) dönüldüğünde dosya yok sayılır ve o klasörler yeniden indekslenir; yeniden güncellendiğinde liste kaldığı yerden uygulanır. Bir kaynağı bağlamdan kesin olarak uzak tutan şey `visibility: private` alanıdır. İndekste olmayan bir dosyanın frontmatter'ı okunmadığı için görünürlüğü de bilinemez. Bu yüzden listedeki bir klasörün altındaki her dosya, bir receipt'e kaynak olarak verildiğinde özel bir kaynak gibi ele alınır: `recap` yolunu göstermez, yalnız sayar; oturum başı bağlamı ve proje köprüsü de o receipt'i göstermez. Klasör listeden çıkarılıp yeniden indekslendiğinde karar yine notun kendi `visibility` alanına döner.
- Listedeki bir klasörde duran not indekste olmadığı için öbür kurallara da katılmaz: başka bir notu `supersedes` ile emekliye ayırıyorsa o eski not yeniden güncel sayılır, `validity: rejected` taşıyorsa aynı iddianın yeniden önerilmesini artık engellemez. Bir klasörü listeye almadan önce içinde böyle notlar olup olmadığına bak.
- `retrieval.json` içindeki `strict_exclude` ile farkı: o strict aramada sonuçları eler; dosyalar yine taranır, indekslenir ve açık non-strict aramada bulunabilir. `index.json` ise taramanın kendisini keser.
- Opt-in kelime tavanı sinyali de listedeki klasörleri atlar. Gelen kutusu raporu, kapalı görev raporu ve bilgi damıtması tarihi diskteki dosyalara bakar, indekse değil; bu liste onları değiştirmez.

## Bileşen ve skill hariç tutma (susturma)

Kullanıcı gereksinim duymadığı başlangıç skill'lerini, adaptörleri, başlatıcıları veya kancaları susturabilir:

- `python3 beyin.py preferences --exclude-component skills/beyin-doktor`
- `python3 beyin.py preferences --include-component skills/beyin-doktor`

Hariç tutulabilen bileşenler:
`agents_block`, `adapters`, `adapters/hermes`, `adapters/omp`, `adapters/opencode`, `harnesses/antigravity`, `launchers`, `skills`, `skills/beyin`, `skills/beyin-doktor`, `skills/beyin-guncelle`.

Hariç tutma tercihleri vault kökünde bağımsız `.beyin-exclusions.json` dosyasında saklanır (`.beyin-preferences.json` dosyasını değiştirmez; böylece eski sürümlere rollback 3.4.0 uyumlu kalır). Tercih kaydedildiğinde bir sonraki kurulum veya güncellemede uygulanır (`update` veya `install`). Değiştirilmemiş hariç tutulan dosyalar temizlenir, kullanıcının değiştirdiği dosyalar ise çakışma vermeden korunur. `doctor` çıktısı hem uygulanan hariç tutmaları hem de bekleyen değişiklikleri gösterir.

## Hermes platform tercihi (`hermes-platforms.json`)

Hermes Agent entegrasyonunda varsayılan olarak gözetimsiz (unattended / bot) sayılan kanallar (`a2a`, `cron`, `telegram`, `discord`, `slack`, `whatsapp`, `signal`, `matrix`, `email`, `sms`, `webhook`) için makbuz hatırlatması ve oturum sonu kontrol noktası üretilmez.

Hermes'i Slack gibi bir kanal üzerinden kişisel interaktif asistan olarak kullananlar için bu kanal gözetimli olarak işaretlenebilir:

```bash
# Slack'i gözetimli/insanlı say:
python3 beyin.py preferences --hermes-attended-platform slack

# Özel bir bot kanalını gözetimsiz say:
python3 beyin.py preferences --hermes-unattended-platform custom-bot

# Varsayılanlara dön:
python3 beyin.py preferences --hermes-reset-platforms
```

Ayar runtime klasöründeki `hermes-platforms.json` dosyasında tutulur (`{"schema": 1, "attended": ["slack"], "unattended": []}`); `.beyin-preferences.json` şemasına girmez ve rollback güvenlidir.
