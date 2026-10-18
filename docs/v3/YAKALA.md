# Yakala: tek tuşla beyne at

Video, tweet, makale, mail, PDF ya da aklına gelen bir cümle: gördüğün yerde tek tuşla yakala,
ajanın sonra okuyup dersleri ikinci beynine bağlasın. İsteğe bağlıdır; kurmazsan hiçbir şey değişmez.

## Kurulum (bir kez)

Vault klasöründe:

```sh
python3 beyin.py yakala kur
```

Windows'ta `py -3 beyin.py yakala kur`. Ya da ajanına "yakala aracını kur" de.

Kurulum şunları yapar:

| | Mac | Windows |
|---|---|---|
| Her uygulamada kısayol | `Control+Option+B` | `Ctrl+Alt+B` |
| Dosya gönderme | Finder'da dosyayı seç, kısayola bas | Sağ tık > Gönder > Beyne At |
| Tarayıcı | Obsidian Web Clipper şablonu | Obsidian Web Clipper şablonu |
| Ajan | `beyin-yakala` skill'i | `beyin-yakala` skill'i |

Kısayol ek paket istemez; Python'un kendisiyle çalışır. Mac'te bir oturum açılış servisi
(LaunchAgent), Windows'ta oturum açılışında başlayan küçük bir dinleyici tuşu dinler.
Yalnız şablon ve skill istiyorsan: `python3 beyin.py yakala kur --kisayol-yok`.

**Mevcut kurulum:** [#299 güvenlik güncellemesini](YAKALA-GUVENLIK-299.md) aldıktan sonra
vault klasöründe `python3 beyin.py yakala kur` komutunu bir kez yeniden çalıştır
(Windows'ta `py -3 beyin.py yakala kur`). Böylece daha önce yazılmış `beyin-yakala` skill'i
yeni güvenilmeyen içerik ve rastgele sınır kurallarını alır. Yalnız şablon/skill kullanıyorsan
`--kisayol-yok` ekle; kullanıcının kendi işaretsiz skill'i korunur.

### Linux

Linux'ta arka planda dinleyen bir servis yok; kısayol masaüstünün kendi kısayol ayarına yazılır.
`kur`, `~/.local/share/applications/beyne-at.desktop` dosyasını yazar (`XDG_DATA_HOME` mutlak bir
yolsa orada). Komut `python3 <state>/yakala/beyin_v3_yakala.py pencere --vault <vault>` olur.
Betiğin bu kopyası state klasöründe durur ve pencereyi açarken vault'taki güncel betiğe geçer;
güncellemeden sonra `kur`'u yeniden çalıştırman gerekmez.

`Ctrl+Alt+B` (ya da `--tus`) masaüstüne göre kaydedilir:

| Masaüstü | Kısayol | Nerede denendi |
|---|---|---|
| KDE Plasma 6 (Wayland) | `kur` kurar | Gerçek masaüstünde: kısayol, bağlantı ve düz metin |
| GNOME | `kur` kurar | Gerçek masaüstünde denenmedi; yalnız birim testleri var |
| KDE Plasma 5 | `kur` dener (`kwriteconfig5`) | Denenmedi |
| Diğerleri (Hyprland, Sway, XFCE...) | Kurulmaz; `kur` bağlanacak komutu verir | Hyprland'de `kur` çıktısı ve `zenity` penceresi denendi |

- **KDE Plasma:** `kwriteconfig6` (yoksa `kwriteconfig5`) ile `kglobalshortcutsrc` içine yazılır,
  `gdbus` ile oturum kapatmadan etkinleştirilir. `kur` sonra tuşun kimde olduğunu kglobalaccel'e
  sorar: çıktıdaki `kisayol_calisiyor` ancak tuş bu kısayola bağlıysa `true` olur; tuş başka bir
  uygulamadaysa ya da soru yanıtsız kalırsa `false`. Sınır: kısayol daha önce başka bir komutla
  kurulduysa (vault taşındı, Python değişti) KDE çalışan oturumda eski komutu çalıştırmayı
  sürdürebilir; oturumu kapatıp açınca yenisi geçerli olur. İlk kurulumda bu sorun yok.
- **GNOME:** `gsettings` ile özel bir kısayol (`custom-keybindings`) eklenir; mevcut kısayolların
  korunur. Burada `kisayol_calisiyor` yalnız ayarın yazıldığını söyler, tuşun başka bir kısayolla
  çakışmadığını söylemez. Tuş çalışmazsa Ayarlar > Klavye altındaki özel kısayollarda `Beyne at`
  girdisine bak ve bir issue aç.
- **Diğer masaüstleri:** `.desktop` dosyası yazılır, kısayol kurulmaz. `kur` çıktısındaki `ipucu`
  bağlanacak komutun tamamını verir; onu masaüstünün kısayol ayarına ekle. KDE ya da GNOME'da kayıt
  doğrulanamazsa da aynı komut `ipucu` olarak verilir.

Linux'ta tuş Ctrl, Alt ya da Super ile birlikte bir harf, rakam, boşluk ya da F1-F12 olabilir.
Kısayol tek bir vault'a hizmet eder: başka bir vault'ta `kur` çalıştırırsan kısayol ona geçer ve
çıktıdaki `onceki_vault` eskisini söyler. `kaldir` yalnız bu vault'un kurduğu `.desktop` dosyasını
ve kısayolu siler. Masaüstü `XDG_CURRENT_DESKTOP` değişkeninden tanınır; `kur` ve `kaldir`'ı
masaüstü oturumundaki bir terminalden çalıştır. SSH gibi bu değişkenin olmadığı bir oturumda
kısayol kurulmaz, kaldırırken de masaüstündeki kısayol ayarı yerinde kalır.

Pencere neyi kaydedeceğini panodan okur: Wayland'da `wl-clipboard` paketi (`wl-paste`) gerekir.
Kurulu değilse kısayol çalışsa bile pano bağlantısı/metni pencereye gelmeyebilir; `yakala kur`,
`yakala durum` ve boş yakalama penceresi bunu belirtir. Paketi dağıtımının paket yöneticisiyle
kurduktan sonra `yakala durum` ile uyarının kaybolduğunu kontrol et; Yakala'yı yeniden kurman gerekmez.
X11'de tam pencere metni kendisi okur; kopyalanan dosyalar ve `kdialog`/`zenity` yolu için `xclip`
ya da `xsel` gerekir. Panoda tek bir bağlantı varsa bağlantı, dosya
yöneticisinden kopyalanan dosyalar (`file://` listesi) varsa dosyalar, başka metin varsa metin
yakalanır. Wayland'da yalnız bir PNG görsel kopyalandıysa tam pencerede küçük önizlemesi görünür;
başlangıçta eklenmez, `Tab` veya karta tıklama ile seçilip PNG dosya kartı olarak kaydedilebilir.
Metin ya da bağlantı varsa öncelik onlardadır. Görsel okunamıyorsa veya `wl-paste` yoksa mevcut
metin akışı değişmez. X11 pano görseli bu sürümde desteklenmez. Pano metninin en fazla 1 MB'ı,
görselin en fazla 64 MB'ı okunur; metin kesilirse kartta belirtilir.
Kopyalanan dosyaların yakalanması gerçek masaüstünde denenmedi, yalnız birim testleri var.

Tam pencere `tkinter` ister. Arch tabanlı dağıtımlarda Python'un `tk` paketi ayrıdır
(`sudo pacman -S tk`), Debian ve Ubuntu'da `sudo apt install python3-tk`. `tkinter` yoksa pencere
yerine `kdialog`, o da yoksa `zenity` ile yalnız "neden" sorulur; iptal edersen kart yazılmaz.
Üçü de yoksa hiçbir şey kaydedilmez ve `kur` bunu uyarı olarak söyler.
Bazı paketli Python/Tk sürümleri yalnız `fixed` yazı tipini görür; Türkçe karakterler ve oklar
pencerede Unicode kaçışları gibi görünebilir. Linux kısayolunun durum-kopyası, kendi Python'unun
Tk fontlarını kontrol eder; uygunsuzsa ve `/usr/bin/python3` kullanılabilir Tk fontlarına sahipse
pencereyi onunla açar. Sistem Tk yoksa dağıtımının paket yöneticisiyle kur; uygun başka Python da
yoksa mevcut yorumlayıcı ve `kdialog`/`zenity` geri dönüşleri korunur. Bu denetim kart içeriğini
başka bir yere göndermez. **Mevcut Yakala kurulumunda motor güncellemesinden sonra** kısayolun
önceden kopyalanmış kodunu yenilemek için vault kökünde `python3 beyin.py yakala kur` çalıştır.
Yalnız `yakala durum` veya `.desktop` dosyasını elle değiştirmek kopyayı yenilemez; KDE etkin
kısayolun eski `Exec` satırını ayrıca bellekte tutabilir. Bu yeniden kurulum eski kartları taşımaz
veya silmez; kayıtlı kısayol ve seçilmiş klasör kullanılır.

Doğrulamak için: `python3 beyin.py yakala durum` (`Kisayol dinleyicisi: calisiyor`), KDE'de
`gdbus call --session -d org.kde.kglobalaccel -o /component/beyne_at_desktop -m org.kde.kglobalaccel.Component.isActive`
(`(true,)` beklenir), sonra kısayola bas.

### Kısayolu değiştir

```sh
python3 beyin.py yakala kisayol 'cmd+"'
```

Tuşu yazdığın gibi tarif et: `cmd`, `ctrl`, `alt` (ya da `option`), `shift` ve en sonda tuş.
Örnekler: `ctrl+alt+b` (varsayılan), `cmd+shift+space`, `alt+f5`, `⌘⇧K`. Mac'te karakter etkin klavye
düzeninden bulunur; Türkçe Q'da `"` 1'in solundaki tuştur. Windows'ta kısayol
yalnız Ctrl/Alt/Shift ile harf, rakam ya da F tuşunu kabul eder. Argümansız `kisayol` mevcut tuşu
gösterir. Seçtiğin tuşu başka bir uygulama zaten kullanıyorsa komut uyarır; başka bir tuş dene.

Mac'te dinleyici arka planda çalışır, Dock'ta görünmez; kapanırsa sistem onu yeniden başlatır.
Dinleyici `kur`'un yazdığı bir kopyadan çalışır: bir güncellemeden sonra onu da yenilemek için
`python3 beyin.py yakala kur` komutunu yeniden çalıştır (tuşun korunur). Çalışıp çalışmadığını
`python3 beyin.py yakala durum` söyler.

### Tarayıcı: Obsidian Web Clipper

1. [Obsidian Web Clipper](https://obsidian.md/clipper) eklentisini kur (Chrome, Firefox, Safari, Edge, Arc, Brave).
2. Eklentinin ayarlarında **Şablonlar > İçe aktar** ile kurulumun kart klasörüne yazdığı
   `beyne-at-web-clipper.json` dosyasını seç (varsayılan `📥 000-Inbox/Yakala/`; tam yolu `kur` söyler).
3. Bir sayfada eklentiye bas, "Beyne at" şablonunu seç, istersen **Neden** bölümüne bir satır yaz, ekle.

Web Clipper sayfayı tarayıcının içinden okur; giriş isteyen sayfalar (Gmail, ücretli makale) da böylece yakalanır.

### Windows: kısayol dinleyicisi

Windows'ta kısayol, tarayıcının (Chrome/Edge/Brave/Vivaldi) adres çubuğunu okur. Adres çubuğu
odaktaysa (yazılıp Enter'a basılmamış taslak) veya değer iki okumada değişiyorsa URL sayılmaz.
Chromium şemayı gizlediği için şemasız adrese `https://` eklenir. Kısayol anında fare bir linkin
(ör. YouTube ana sayfasındaki bir video kartı) üstündeyse o link varsayılan kaynak olur; açık
sayfa **Alt+S**, imlecin altındaki link **Alt+I** ile seçilir. Okuma sırasında pencere veya sekme
değişirse (başlık değişir) iki link de atılır. Link yoksa kaynak pencerenin görüntüsü sunulur
(önce Windows.Graphics.Capture, olmazsa PrintWindow). Görüntü kapalı gelir; **Alt+W** veya satıra
tıklama ile eklenir, çünkü `isle` görseli ajana olduğu gibi okutur. History/oturum veritabanı okunmaz;
localhost/özel IP için mevcut ağ güvenliği sınırı korunur.

**Gönderiler (X, Instagram):** Tweet metni veya akıştaki video bir link değildir. Açık sayfa
X (`x.com`, `twitter.com`) ya da Instagram ise ve imleç bir linkin üstünde değilse, altındaki en
içteki ARIA `article` öğesi (tek gönderi) alınır ve içindeki ilk gönderi bağlantısı
(`x.com/<kullanıcı>/status/<id>`, `instagram.com/p|reel/<kod>/`) imleç linki olur. Başka sitelerde
bu yol çalışmaz: bir haber sayfasının gövdesi de tek bir `article` olabilir ve metnin andığı tweet,
imlecin üstünde durduğu gönderi değildir; orada kaynak açık sayfa kalır. Sayfa adresi
okunamazsa da bu yol kapalıdır. Aynı gönderideki ikinci, farklı gönderi bağlantısı alıntılanan
gönderidir; **Alt+A** ile seçilebilir, varsayılan seçili değildir. X'in fotoğraf/video/analytics
alt sayfaları gönderi adresine indirgenir; Instagram ses sayfası (`/reels/audio/<id>/`) gönderi
sayılmaz. Bağlantı sayfanın kendisinden okunur; gönderi adresi görselden veya kullanıcı adından
üretilmez.

**Opera (Opera Air dahil):** Süreç adı `opera` olan pencerede adres çubuğu okunmaz; sayfanın ayrı
Chromium alt penceresi okunur. Sayfa URL'si MSAA Document nesnesinin gerçek değerinden, imleç
bağlantısı aynı alt pencerenin UI Automation bağlantılarından alınır. Adres çubuğundaki taslak
okunmaz; açık sayfanın yüklenmiş adresi kullanılır. Eklenti, yeniden başlatma veya
erişilebilirlik başlatma bayrağı gerekmez. Tek görünür renderer, pencere/süreç sahipliği ve
okuma öncesi/sonrası sayfa URL'si doğrulanır. Birden fazla görünür renderer, görünmeyen belge,
değişen sayfa veya zaman aşımı varsa URL verilmez. Bağlantı iki okumada değişirse yalnız imleç
bağlantısı atılır. Sayfa ve imleç okuması tek bir yardımcı süreçte, 1 saniyelik bütçe içinde
yapılır.
Opera Air 136.0.6008.70 üzerinde bayraksız ayrı profilde temel sağlayıcı yolu ölçüldü;
oturum açılmış YouTube/Instagram akışlarında kullanıcı kabul testi ayrıca yapılmalıdır.

Tarayıcıdan okunan link (imlecin altındaki ya da açık sayfa) varsa varsayılan odur; pano içeriği
yalnız seçenek olarak, işaretsiz sunulur. Link yoksa dinleyicinin başlangıcından itibaren gözlenen,
başarılı kayıtta kullanılmamış pano revision'ı önceliklidir. İlk kullanım/yeniden başlatmada eldeki panonun yaşı bilinmez: içerik kapalı
seçenek olarak kalır. Snipping Tool dosya zamanı tek başına otomatik tazelik sağlamaz.
İptal ve yalnız sayfayı kaydetmek panoyu tüketmez. Pano değişimi yakalama tuşundan sonra
olduysa yeni içerik kendiliğinden kaynak seçilmez. Metin/görüntü aynı pano oturumundan
kopyalanır; gecikmeli veya tutarsız okuma güvenli biçimde atlanır.

Seçim yolları: **Alt+I** imlecin altındaki bağlantı, **Alt+S** etkin sayfa, **Alt+A** alıntılanan
gönderi, **Alt+P** pano bağlantısı, **Alt+M** metin, **Alt+G** pano görüntüsü, **Alt+W** pencere
görüntüsü.
Alternatifler birbirini silmez. Bir URL seçimi diğer URL'yi kapatır. Pano metni/dosya eklenen
mevcut URL kartı da `private` olur. Aynı popup'ın kayıt kimliği tekrar oynatılsa bile ikinci
commit yapılmaz.

Aynı sayfanın ikinci yakalaması yeni kart açmaz, mevcut karta eklenir. Sayfa kimliği her
sistemde aynı kuralla hesaplanır (senkronlanan bir vault'u Mac ve Windows birlikte okuyabilir):
aynı YouTube videosu (`youtu.be`, `watch?v=`, `shorts/`, `live/`, `embed/`, `music.youtube.com`)
ve aynı X/Twitter gönderisi tek kartta toplanır; alan adı tam eşleşir (`notyoutube.com` YouTube
sayılmaz). Diğer sayfalarda şema, `www.`/`m.` öneki, sondaki `/` ve bölüm işareti (`#baslik`)
fark yaratmaz; varsayılan dışı port (`:8443`) ve yönlendirme işaretleri (`#/sayfa`, `#!sayfa`)
ayrı sayfa sayılır.

**Tek aktif popup:** tekrar kısayol yeni pencere açmaz, mevcut metni ve kaynak seçimini
koruyarak açık popup'a odak isteği gönderir. Dinleyici ile warm pencere, instance/schema/
sürüm/HWND doğrulamalı READY → foreground izni → ACTIVATE konuşur. İkinci çağrı FOCUS'tur.
Eski protokol sessizce kabul edilmez; bu güncellemeden sonra `py -3 beyin.py yakala kur`
ile dinleyici kopyasını yenilemek gerekir. IPC pipe'ları hotkey thread'ini bloke etmez.
Dinleyici kapanışı önce STOP gönderir; yanıt vermeyen pencere ve okuyucuları sahip olunan
Job Object üzerinden sonlandırılır. Başka bir vault'a ait state silinmez.

Popup kaynak monitörde açılır. Windows tarafında Tk yönetilen pencere kullanır; özel
palet/canvas görünümü korunmak üzere yerel başlık/çerçeve stili kaldırılır. **Esc** yerel
odakta hemen iptal eder. Kaynaklar okunurken ve popup açık olduğu sürece, odak nerede olursa
olsun, düşük seviyeli bir klavye kancası (`WH_KEYBOARD_LL`) yalnız Esc'e bakar: popup öndeyse
Esc'i Tk işler; kaynak pencere öndeyse (odak reddedildi ya da sayfaya tıklandı) Esc tüketilir ve
popup kapanır; başka bir pencere öndeyse popup kapanır ve Esc o pencereye de iletilir. Kanca
popup kapanınca (kayıt, Esc, Kapat, Alt+F4, hata) kaldırılır; sonrasında hiçbir uygulamanın
Esc'i tutulmaz. Yeniden hotkey kancayı yeni kaynak pencereyle yeniden kurar, eski yakalama
içeriğini değiştirmez. `AttachThreadInput` veya `GetAsyncKeyState` kullanılmaz.
Görünür **Kapat** ve Alt+F4 da iptaldir. Windows güvenli masaüstü/UIPI/hook reddi için
her durumda global Esc garantisi yoktur. Kartın disk commit'i başladıysa Esc kaydedilmiş
veriyi geriye dönük iptal etmez; yavaş disk I/O'su sırasında arayüz yanıtı gecikebilir.

URL doğrulanmışsa ek ekran görüntüsü işi yapılmaz. Diğer durumda **WGC → PrintWindow →
görüntü yok** sırası izlenir; her backend ayrı, sonlandırılabilir süreçtedir. Önizleme yöntem
adını gösterir. Ekran DC'sinden BitBlt yoktur. Kaynak pencere değişirse sonuç atılır.
WGC, Windows 10 1903+ API yoludur; destek/izin/sürücü başarısızsa PrintWindow denenir.
Korumalı/GPU/exclusive-fullscreen uygulamaların tamamı için başarı veya HDR renk doğruluğu
iddia edilmez. Herhangi bir başarısızlık eski panoyu otomatik kaynak yapmaz.

UIA/pano için dış bekleme 1 saniye, WGC için 2,5 saniye, PrintWindow için 1,2 saniyedir;
süreç başlatma/sonlandırma ek giderleri olabilir. PNG/DIB en fazla **16.777.216 piksel**,
görüntü payload'ı 64 MiB, metin UTF-8 payload'ı 4 MiB, IPC metadata'sı 64 KiB kabul eder.
Büyük ilan edilmiş PNG boyutu Tk'ye verilmez. Kart/ham geçici yazım hataları temizlenir;
hard kill/güç kesintisi veya dosya sisteminin silmeyi reddetmesi için mutlak silinme
sözü verilmez. Dinleyici normal/zorunlu popup kapanışında kendi TEMP ağacını da temizler.

`kur`, Başlangıç klasörüne (`shell:startup`) `Beyne At Dinleyici` kısayolu yazar ve dinleyiciyi
başlatır. Bir oturumda tek dinleyici vardır; başka vault'ta `kur` tuşu o vault'a devreder.
`py -3 beyin.py yakala durum` durumunu, state içindeki `yakala/dinleyici.log` hatayı gösterir.
Vault taşınmışsa eski yola yeniden yazılmaz; yeni konumda tekrar kurulum yapılır. Kısayol
geçici dosyada oluşturulup doğrulandıktan sonra değiştirilir; stop zaman aşımı başarı sayılmaz.

ABI kaynakları, sınırlamalar ve gerçek masaüstü kabul komutları:
[Windows yakalama ABI ve test rehberi](WINDOWS-CAPTURE-ABI.md).

## Kullanım

1. **Yakala.** Kısayola bas. Pencere neyin kaydedileceğini gösterir: tarayıcıdaki sayfa
   (Mac'te Chrome, Arc, Brave, Edge, Safari), Finder'da seçili dosya ya da panodaki metin.
   İstersen tek satır "neden" yaz, Enter. Esc vazgeçer.

   Windows'ta panoda görüntü varsa pencere onu küçük önizlemesiyle gösterir. Son başarılı kayıttan
   sonra panoya kopyalanan görüntü (ör. `Win+Shift+S`) eklenmeye hazır gelir: Enter onu kartların
   yanındaki `dosyalar/` klasörüne PNG olarak kaydeder ve Gönder > Beyne At ile aynı dosya kartını
   açar. Tazelik saatle değil pano sıra numarasıyla ölçülür; iptal (Esc) panoyu tüketmez.
   Tazeliği doğrulanamayan görüntü kapalıdır; **Alt+G** veya satıra tıklama ile değiştirilebilir.
   Pano metni/bağlantısı ve pencere görüntüsü yukarıdaki Windows seçim kurallarını izler.
2. **İşlet.** Ajanına "yakalananları işle" de. Oturum başında bekleyen kaynak sayısı da görünür.

Her yakalama kart klasöründe (varsayılan `📥 000-Inbox/Yakala/`) bir kart olur. Aynı videoyu ya da
sayfayı ikinci kez yakalarsan yeni kart açılmaz; yeni notun aynı karta eklenir.

### Kartların klasörü

Gelen kutusu klasörlerinden yalnız birinde `Yakala/` zaten varsa kartlar oraya yazılır; böyle tek
bir klasör yoksa başlangıç klasörü `📥 000-Inbox` kullanılır, o da yoksa vault'un en üstünde adı gelen
kutusu olan tek klasör (`000-Inbox`, `00_INBOX`, `Gelen Kutusu`). Karar verilemiyorsa tahmin edilmez
ve başlangıç yolu açılır. Nokta ile başlayan klasörler, sembolik bağlar, arşiv ve kasa türü adlar
gelen kutusu sayılmaz.

Başka bir yer istiyorsan kurarken söyle:

```sh
python3 beyin.py yakala kur --klasor "Notlar/Yakala"
```

- Klasör vault'un içinde olmalı. Dışarı çıkan bir yol reddedilir ve hiçbir şey yazılmaz.
- Web Clipper şablonu, skill, oturum bildirimi ve `durum` aynı klasörü gösterir. Klasörü
  değiştirdiysen şablonu Web Clipper'a yeniden aktar.
- Eski klasördeki kartlar taşınmaz. `kur` kaç kartın geride kaldığını söyler; işlenmelerini
  istiyorsan onları yeni klasöre sen taşı.
- Seçim bu makinenin durum klasöründe saklanır. Aynı vault'u başka bir makinede de kullanıyorsan
  orada da aynı komutu çalıştır.
- Kayıtlı klasör silinir ya da adı değişirse yukarıdaki kurala dönülür; `durum` kullanılan klasörü söyler.

## İşleme nasıl çalışır

`python3 beyin.py yakala isle` bekleyen kartların metnini çıkarır, sonra ajan dersleri
`knowledge/` notlarına bağlar ve kartı `bitti` ile kapatır.

| Kaynak | Yol |
|---|---|
| Web Clipper ile gelen sayfa | Yakalanan metin kullanılır, ağa çıkılmaz |
| YouTube | [Defuddle](https://github.com/kepano/defuddle) ile bölümlü transkript; olmazsa ya da dili konuşma diliyle uyuşmazsa `yt-dlp` altyazısı; o da yoksa yalnız ses indirilir, yerel Whisper ile yazıya dökülür, ses silinir |
| X, Reddit, GitHub, makale | Defuddle; yoksa düz HTTP ile metin |
| Mail | Seçili metin; tamamı için ajanın Gmail/Outlook bağlantısı |
| PDF, metin dosyası | `pdftotext` ya da doğrudan okuma; görselleri ajan açar |

Ham metin kart klasörünün içinde `.ham/` altında durur. Nokta ile başlayan klasör olduğu için
aramaya ve Obsidian'a karışmaz. `.ham/` ve yakalanan dosyaların durduğu `dosyalar/`, içlerine yazılan
`.gitignore` ile kart klasörü hangisi olursa olsun depoya da girmez.

### Videoda transkriptin dili

Otomatik dublajlı bir videoda YouTube her dublaj için ayrı bir altyazı izi sunar. `isle`,
`yt-dlp` kuruluysa videoda konuşulan dili videonun bilgisinden öğrenir, Defuddle'dan o dildeki
transkripti ister ve gelen dili kontrol eder. Uyuşmazsa `yt-dlp` ile yalnız konuşma dilindeki
izi indirir; elle yazılmış altyazı otomatik olandan önce gelir. Sonuç kartın frontmatter'ına yazılır:

- `dil`: kaydedilen transkriptin dili.
- `altyazi`: `yt-dlp` ile alınan iz, örneğin `en-orig (otomatik)` ya da `tr (elle)`.
- `uyari`: yalnız bir sorun belirtisi varsa yazılır: transkript konuşma dilinde alınamadıysa ya da
  video dublajlıyken (birden çok dilde ses ya da `-orig` izi) konuşulan dil belirlenemediyse.

`yt-dlp` kurulu değilse dil doğrulanamaz: Defuddle'ın seçtiği transkript eskisi gibi kaydedilir ve
dili `dil` alanına yazılır. `dil` videoda konuşulan dil değilse `yt-dlp` kurup
`python3 beyin.py yakala isle <id>` ile yeniden dene; `uyari` olan kart da aynı komutla yeniden
denenir. Daha önce işlenmiş kartlar değişmez.

### İsteğe bağlı araçlar

Hiçbiri zorunlu değil; varsa işleme genişler, yoksa elde olanla devam edilir.
`python3 beyin.py yakala durum` hangilerinin bulunduğunu söyler.

- **Node.js** (`npx`): Defuddle için. Sabit sürüm (`defuddle@0.19.4`) çalıştırılır.
- **yt-dlp**: videoda konuşulan dil, altyazı ve ses.
- **Whisper**: Mac'te `mlx_whisper`, diğerlerinde `whisper` ya da `whisper-ctranslate2`. Ses indirmeyi istemezsen `isle --ses-yok`.
  Videoda konuşulan dili `yt-dlp` söyleyebildiyse Whisper'a `--language` olarak verilir; dil bilinmiyor ya da video
  dublajlıysa Whisper dili kendisi tanır. Model seçmek için `BEYIN_WHISPER_MODEL` ortam değişkeni
  (ör. `large-v3-turbo`) `--model` olarak geçer. `whisper-ctranslate2`'nin varsayılanı `small`; Türkçe gibi
  dillerde terimleri bozabilir (likidite → "liquidite"). GPU varsa daha büyük bir model seç.
  NVIDIA kartlı makinede `whisper-ctranslate2` CUDA'yı kendisi seçer; CUDA kütüphanesi yüklenemezse (ör.
  `cublas64_12.dll` yok) ses bir kez CPU ile yeniden yazıya dökülür ve kartın uyarısı hatayı adıyla söyler.
  Hızlandırmak için eksik kütüphaneyi kur (ör. `pip install nvidia-cublas-cu12` ve klasörünü PATH'e ekle).
  Whisper hiç metin üretmezse kartta Whisper'ın kendi son hata satırı yazar.
- **pdftotext** (Poppler): PDF metni.

Windows'ta bu araçlar bir `.cmd`/`.bat` sarmalayici olarak kuruluysa, argumanlar komut
yorumlayicidan (cmd.exe) gecmeden dogrudan calistirilir; bu mumkun degilse o arac atlanir ve
elde olanla devam edilir.

## Gizlilik

- Yakalama ağa çıkmaz; yalnız vault'a bir dosya yazar.
- Ağ yalnız `isle` adımında ve yalnız yakaladığın kaynaklar için kullanılır. Defuddle,
  X gönderisi sayfada yoksa gönderi adresini FxTwitter API'sine sorar.
- Mail, pano metni ve dosya kartları `visibility: private` ile yazılır.
- Kaldırmak için `python3 beyin.py yakala kaldir`: kısayol, skill ve bildirim gider, kartların kalır.

## Komutlar

```text
beyin.py yakala              yakalama penceresi
beyin.py yakala ekle URL|DOSYA|METIN [--neden "..."]
beyin.py yakala liste [--durum bekliyor|cikarildi|islendi|hata]
beyin.py yakala isle [ID ...] [--ses-yok] [--tekrar]
beyin.py yakala bitti ID --bilgi knowledge/concepts/x.md [--ozet "..."]
beyin.py yakala kur [--kisayol-yok] [--tus 'ctrl+alt+b'] [--klasor "Notlar/Yakala"]
beyin.py yakala kisayol ['cmd+"']
beyin.py yakala kaldir | durum | sablon
```

### Kaynak güvenliği

Yakala kartı oluşturmak ağ kullanmaz. HTTP(S) kaynağını işlemeden önce yerel/özel IP adresleri,
URL kullanıcı bilgileri ve bozuk adresler reddedilir. Defuddle ve yt-dlp'ye URL gönderilmeden önce
DNS yanıtları da denetlenir; Python HTTP yolunun yönlendirmeleri aynı kapıdan geçer. DNS geçici
olarak çözümlenemezse kart hata notuyla kalır, sıradaki kartlar işlenmeye devam eder.

Bu denetimler ilk hedefteki özel adresleri engeller. Yardımcı araçların kendi yönlendirmeleri ve
DNS rebinding için tam ağ izolasyonu sağlamaz. Ham kaynak metni güvenilmeyen veri olarak işaretlenir;
metnin içindeki rol değişikliği, komut çalıştırma ve dosya silme istekleri talimat sayılmaz.
