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

Gercek X11 oturumunda pencere once asagidaki tarayici okuyucusunu dener. Tarayici baglami
alinamazsa pano akisi kullanilir. Wayland'da otomatik sayfa/imlec okuma yoktur; pencere neyi
kaydedecegini panodan okur. Wayland'da `wl-clipboard` paketi (`wl-paste`) gerekir.
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

### Tarayici sayfasi ve imlec baglantisi

19.10.2026 eklemesi; kod tabani `d9aa301`. Bu bolumdeki yeni Windows Gecko/Yandex, macOS AX ve
Linux X11 yollarinin **gercek cihaz dogrulamasi yapilmadi**. Birim testleri saglayici, ABI,
nesne sahipligi, zaman asimi ve belirsizlik kurallarini denetler; kurulu tarayicinin bu
sozlesmeyi sundugunu kanitlamaz. Eski Opera Air olcumu asagida kendi surumuyle belirtilmistir.

Hucreler `sayfa / imlec` yolunu gosterir. `UIA` mevcut Windows adres cubugu/Hyperlink yolu;
`MSAA+UIA` renderer belge degeri ve renderer Hyperlink'i; `Gecko` yeni MSAA Document/Link yolu;
`AX` macOS AXWebArea/AXLink yolu; `AS` yalniz sayfa icin sabit AppleScript yedegi;
`SPI` gercek X11 penceresine baglanan AT-SPI Document/Hyperlink yoludur.
`Bekliyor` okuyucu etkin degil, `Urun yok` ilgili platform icin guncel resmi dagitim yok demektir.
UIA hucrelerinde sema gizliyse ayni child/butcede renderer belge URL'siyle dogrulama gerekir.

| Aile / urun | Windows | macOS | Linux X11 | Linux Wayland |
|---|---|---|---|---|
| Chrome / Chromium | UIA / UIA | AX veya AS / AX | SPI / SPI | Okunmaz |
| Edge | UIA / UIA | AX veya AS / AX | SPI / SPI | Okunmaz |
| Safari / STP | Urun yok | AX veya AS / AX | Urun yok | Urun yok |
| Firefox | Gecko / Gecko | AX / AX | SPI / SPI | Okunmaz |
| Opera | MSAA+UIA | AX veya AS / AX | SPI / SPI | Okunmaz |
| Opera GX | `opera.exe` ise MSAA+UIA | AX veya AS / AX | Urun var; GX kimligi bekliyor | Okunmaz |
| Opera Air | `opera.exe` ise MSAA+UIA | AX veya AS / AX | Urun yok | Urun yok |
| Brave | UIA / UIA | AX veya AS / AX | SPI / SPI | Okunmaz |
| Vivaldi | UIA / UIA | AX veya AS / AX | SPI / SPI | Okunmaz |
| Arc | Bekliyor | AX / AX | Urun yok | Urun yok |
| Zen | Gecko / Gecko | AX / AX | SPI / SPI | Okunmaz |
| Yandex | MSAA+UIA, urun yolu zorunlu | AX / AX | SPI / SPI | Okunmaz |
| Samsung Browser | Urun var; okuyucu bekliyor | Resmi dagitim bulunamadi | Resmi dagitim bulunamadi | Resmi dagitim bulunamadi |

Ana hedef 10 ailedir: Chrome, Edge, Safari, Firefox, Opera, Brave, Vivaldi, Arc, Zen, Yandex.
Opera varyantlari ayni aile icinde ayrica olculur; Samsung ek izleme adayidir. StatCounter'in
Eylul 2026 dunya masaustu tablosunda ilk alti Chrome %65,09, Edge %14,64, Safari %8,06,
Firefox %6,11, Opera %2,38, Brave %1,12'dir. Turkiye tablosunda Yandex %1,21 ile gorunur.
Bu olcum sayfa goruntuleme payidir; kurulum veya tekil kullanici payi degildir. Vivaldi/Arc/Zen
icin kesin ilk 10 sirasi iddia edilmiyor. [Dunya](https://gs.statcounter.com/browser-market-share/desktop/worldwide/),
[Turkiye](https://gs.statcounter.com/browser-market-share/desktop/turkey/),
[yontem](https://gs.statcounter.com/faq); erisim 19.10.2026.

**macOS.** Okuyucu NSWorkspace uygulama PID/bundle kimligini ve pencere sunucusunun gorunur
pencere kimligi/sinirlarini kaydeder. Accessibility izni varsa, sonlandirilabilir yardimci
surecte odakli pencerenin altindaki tek gorunur ust AXWebArea'nin AXURL degeri okunur.
Imlec linki sistemin nokta testiyle bulunur; AXLink siniri imleci icermeli, ust nesne zinciri
ayni belge ve pencereye ulasmalidir. Iki belge/link okumasinda URL ve nesne kimlikleri, sonda
uygulama/pencere/sinirlar ve sheet durumu ayni kalmalidir. Hareket eden imlecin linki atilir.
AXLoadingProgress varsa sonlu ve tam 1 olmali; iki okumada bulunabilirligi de degismemelidir.
Bu ek kontrol, incelenen Mozilla kaynaginda AXLoaded maskesinin yuklenmeyi kacirabilmesi
nedeniyle eklendi; gercek Firefox/macOS surum davranisi ayrica olculmelidir.

Privacy & Security > Accessibility izni link icin gerekir. Automation ayri izindir; AX
erisimi yoksa Safari ve izin listesindeki Chromium turevlerinde yalniz sayfa icin sabit
`/usr/bin/osascript` denenebilir. Script on uygulama, pencere, etkin sekme ve iki tam URL'yi
kontrol eder. AX iki belge/eksik belge URL'si gibi bir belirsizlik gorduyse AppleScript ile
bu karar asilmaz. Izinler otomatik acilmaz. Safari'nin yerel SDEF'i ve her turevin sozlugu
cihazda teyit edilmeli; ozellik/derleme/izin hatasinda adres yoktur. Arc/Firefox/Zen/Yandex
genel Chromium AppleScript sozlugune zorlanmaz. Accessibility kapaliyken ayni pencereye
bagli native sheet ve split view davranisi AppleScript kabul testinde ayrica olculmelidir.

**Linux X11.** `XDG_SESSION_TYPE=x11`, yerel `DISPLAY=:N[.S]` ve bos `WAYLAND_DISPLAY`
gerekir. Yeni Python paketi yoktur; sistemde `libX11.so.6`, `libatspi.so.0`, GLib/GObject ve
erisebilir AT-SPI oturum bus'i bulunmalidir. Eksik kutuphane/izin/agac bos sonuc verir;
kurulum veya ayar degisikligi kendiliginden yapilmaz. EWMH etkin pencere, gercek X input focus,
`/proc` PID/exe/baslangic zamani ve AT-SPI uygulama PID'si eslestirilir. `_NET_WM_PID` istemci
ozelligidir; normal yerel X11 masaustu guveni varsayilir, kotucul ayni kullanici sureclerine
karsi yeni bir guvenlik siniri ilan edilmez.

Tek aktif AT-SPI frame'in sinirlari X11 client sinirlariyla tam eslesmelidir. Chromium
ailesinde Document `URI`, Gecko'da `DocURL` okunur. Iki gorunur dis belge, gecersiz URL'li
ikinci panel, busy/stale agac veya degisen kimlik reddedilir. Iframe sayfa yerine secilmez.
Imlec linkinde nokta testi frame'den baslar; secilen belgeye ulasan gercek LINK, tek valid
Hyperlink anchor'i ve tam URL gerekir. DPI/dekorasyon ofseti tahmin edilmez. Snap, Flatpak,
AppImage, PID namespace, farkli exe adi veya olcek nedeniyle esleme kurulamiyorsa sonuc yoktur.
Opera GX Linux 19.03.2026'da cikti; bu kod yalniz exe gercekte `opera` ise ortak yolu
kullanabilir. GX paketi ve agaci ayri olcum bekler.

**Wayland.** KDE/GNOME/Hyprland/Sway'de AT-SPI bulunmasi tek basina global foreground ve
imlec sahipligi kaniti degildir. XWayland etkin penceresi, native Wayland uygulamasi ondeyken
eski kalabilir. Compositor'a ozel, izin modeli dogrulanmis bir uyarlayici eklenene kadar bu
surum otomatik sayfa ve imlec URL'si vermez; mevcut pano/dosya/metin yakalama devam eder.
`DISPLAY` varligi veya oturum degiskenlerini degistirmek bu denetimi asmak icin kullanilmaz.
Linux'ta ilk pencere/imlec ornegi yardimci surec basladiginda alinir; fiziksel kisayol
aninin kaydedilmis noktasini temsil etmez.

**Secim ve sure.** macOS/Linux tam Tk penceresinde, dogrulanmis sayfa ve ondan farkli imlec
linki varsa `Imlecin altindaki baglanti` varsayilan, `Etkin sayfa` alternatif gelir.
macOS'ta Option+I/Option+S, Linux'ta Alt+I/Alt+S veya satira tiklama kullanilir.
Pano bağlantısı bu durumda kapalı bir seçenektir. macOS/Linux bu sayfa+imleç yolunda
pano metninin tazeliğini doğrulamadığından metin kartı sunulmaz; not kutusuna elle
yazılabilir veya açıkça yapıştırılabilir. Mevcut pano görseli okuyucusu da sayfa varken
görsel teklif etmez. Linux'ta panodaki yerel dosya listesi tarayıcı bağlamına önceliklidir:
dosya önizlemesi gösterilir ve yalnız dosyalar kaydedilir; sayfa ve imleç URL'si karta eklenmez.
Yalnız sayfa akışı mevcut davranışını korur. Tk olmayan `kdialog`/`zenity`/macOS basit soru yedeginde
imlec ve sayfa secicisi yoktur; dogrulanmis sayfa kullanilir.

Yeni Windows belge okuyucusu 1 saniye; macOS tarayici okuma toplami 2,5 saniye (AX payi en
fazla 1 saniye); Linux tarayici cocugu 2 saniye nominal butcelidir. Native tarayici saglayici
cagrilari sonlandirilabilir surectedir. Baslatma/temizleme, macOS pencere sunucusu ve Linux
pano/popup giderleri bu rakamlara eklenebilir. Bunlar olculmus gecikme veya sert gercek zaman
garantisi degildir. Yakalama fiziksel olarak atomik degildir; ornekler arasinda fark edilmeyen
cok hizli degisim olasiligi cihaz testleriyle de tamamen ortadan kaldirilamaz.

Yeni yollar History/session/cookie/profil dosyasi, debug portu, uzanti, tarayici yeniden
baslatma bayragi, sentetik tus veya JS kullanmaz. Izin verilen kaynaktan tam HTTP(S) adresi
gelmezse sema uydurulmaz. Mevcut localhost/ozel IP guvenlik kurallari korunur.

**Cihaz kabul araci.** Repo kokunde, bilinen genel test sayfasina gecmek icin geri sayimi kullan:

```sh
python3 tests/manual_browser_capture.py --delay 5 --repeat 20 --interval 1
python3 tests/manual_browser_capture.py --delay 5 --expected-page https://example.com/
python3 tests/manual_browser_capture.py --delay 5 --expect-empty
```

Windows'ta `python3` yerine `py -3` kullan. Bilinen gercek link icin `--expected-hover TAM_URL`
ekle. Arac kart yazmaz; URL'leri varsayilan gizler, p50/p95 ve tam esitlik sonucunu JSON verir.
`--show-urls` yalniz paylasilabilir test adresleriyle kullanilmali. Beklenen adres verilmediyse
`checked_samples=0`; cikis kodu 0, dogru URL olculdu demek degildir. Beklenti tutmazsa cikis 1'dir.
Her tarayicida normal sayfa/link, taslak adres cubugu, iframe, ayni URL'li iki panel, sekme/
pencere degisimi, ortucu pencere, ikinci monitor/DPI, izin reddi ve soguk saglayiciyi dene.
Yanlis adresi tek ornekte bile kabul etme; bos sonuc oranini basarili yakalamadan ayri kaydet.

Birincil arayuz kaynaklari (erisim 19.10.2026):

- [Mozilla erisilebilirlik mimarisi](https://firefox-source-docs.mozilla.org/accessible/Architecture.html):
  Windows'ta MSAA, IA2 ve UIA provider birlikte vardir; Firefox icin UIA yok denmez.
- [Chromium erisilebilirlik mimarisi](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/accessibility/overview.md)
  ve [Chromium SDEF](https://chromium.googlesource.com/chromium/src.git/+/lkgr/chrome/browser/ui/cocoa/applescript/scripting.sdef).
- [Apple Accessibility izni](https://support.apple.com/guide/mac-help/allow-accessibility-apps-to-access-your-mac-mh43185/mac)
  ve [Automation izni](https://support.apple.com/en-au/guide/mac-help/mchl07817563/mac).
- [AT-SPI Document](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/method.Document.get_document_attribute_value.html),
  [EWMH](https://specifications.freedesktop.org/wm/latest-single/),
  [Wayland](https://wayland.freedesktop.org/docs/book/Protocol.html),
  [XWayland](https://wayland.freedesktop.org/docs/book/Xwayland.html).
- [Chromium URI](https://github.com/chromium/chromium/blob/main/ui/accessibility/platform/ax_platform_node_auralinux.cc)
  ve [Mozilla DocURL](https://github.com/mozilla/gecko-dev/blob/master/accessible/atk/nsMaiInterfaceDocument.cpp):
  motora ozel Document attribute adlari.
- [Opera GX Linux duyurusu](https://press.opera.com/2026/03/19/opera-gx-gaming-browser-lands-on-linux/)
  (19.03.2026), [Opera indirmeleri](https://www.opera.com/download),
  [Arc platform sinirlari](https://resources.arc.net/hc/en-us/articles/19400148671383-Unsupported-Devices),
  [Samsung Windows duyurusu](https://news.samsung.com/global/samsung-takes-its-browser-beyond-mobile-extending-agentic-ai-across-devices)
  (26.03.2026).

### Tarayıcı: Obsidian Web Clipper

1. [Obsidian Web Clipper](https://obsidian.md/clipper) eklentisini kur (Chrome, Firefox, Safari, Edge, Arc, Brave).
2. Eklentinin ayarlarında **Şablonlar > İçe aktar** ile kurulumun kart klasörüne yazdığı
   `beyne-at-web-clipper.json` dosyasını seç (varsayılan `📥 000-Inbox/Yakala/`; tam yolu `kur` söyler).
3. Bir sayfada eklentiye bas, "Beyne at" şablonunu seç, istersen **Neden** bölümüne bir satır yaz, ekle.

Web Clipper sayfayı tarayıcının içinden okur; giriş isteyen sayfalar (Gmail, ücretli makale) da böylece yakalanır.
Bu mevcut istege bagli kurulum, yukaridaki yerel kisayol okuyucularinin gereksinimi degildir.

### Windows: kısayol dinleyicisi

Windows'ta kısayol, tarayıcının (Chrome/Edge/Brave/Vivaldi) adres çubuğunu okur. Adres çubuğu
odaktaysa (yazılıp Enter'a basılmamış taslak) veya değer iki okumada değişiyorsa URL sayılmaz.
Chromium semayi gizlerse ayni sure siniri icinde tek gorunur renderer'in acik MSAA belge
URL'siyle dogrulama yapilir; gorunen metin ve ikinci odaksiz UIA okumasi uyusmalidir.
`https://` tahmini yapilmaz. Yalniz kok adresin sondaki `/` farki kabul edilir; `www`, IDN
ve percent-encoding gosterim farki veya soguk renderer bos sonuca yol acabilir.
Kısayol anında fare bir linkin
(ör. YouTube ana sayfasındaki bir video kartı) üstündeyse o link varsayılan kaynak olur; açık
sayfa **Alt+S**, imlecin altındaki link **Alt+I** ile seçilir. Okuma sırasında pencere veya sekme
değişirse (başlık değişir) iki link de atılır. Link varken de kaynak pencerenin görüntüsü sunulur
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
bağlantısı atılır. Sayfa ve imleç okuması tek bir yardımcı süreçte, 1,8 saniyelik bütçe içinde
yapılır. Chromium web erişilebilirliğini bir süre kullanılmayınca kapatır; o zaman ilk okuma boş
gelir, renderer'a bir UI Automation isteğiyle erişilebilirlik açılır ve belge en fazla 0,8 saniye
beklenir. Erişilebilirlik zaten açıksa okuma eskisi gibi kısa sürer.
Opera Air 136.0.6008.70 üzerinde bayraksız ayrı profilde temel sağlayıcı yolu ölçüldü;
oturum açılmış YouTube/Instagram akışlarında kullanıcı kabul testi ayrıca yapılmalıdır.

**Firefox / Zen / Yandex:** Firefox ve Zen icin `MozillaWindowClass`, foreground/PID ve
gercek exe adi dogrulanir. MSAA agacinda tek gorunur ust Document'in tam degeri okunur;
iframe icine inilmez. Nokta altindaki gercek Link, fiziksel siniri ve ayni Document'e ulasan
IUnknown kimligiyle dogrulanir. Iki link okumasinin URL, ad ve nesne kimligi de ayni olmalidir.
Yandex `browser.exe` adi ancak gercek yol `\\YandexBrowser\\Application\\browser.exe` ile
bitiyorsa kabul edilir; mevcut Opera renderer/MSAA yolu kullanilir. Ozel portable/beta yolu
bu kurala uymuyorsa destek verilmez. Arc/Samsung'in Windows okuyucusu henuz etkin degildir.

Opera Air soguk erisilebilirlik olcumu ve tek UIA uyandirma istegi ayri
[#399](https://github.com/avenoxai/avenoxbeyin/pull/399) calismasidir; bu ekleme o PR'yi
icermiyor. Yandex ortak renderer yolunu kullandigi icin soguk/sicak saglayici sonucu ve
#399 oncesi/sonrasi ayrica olculmelidir. Eski Opera Air sonucu yeni tarayicilar icin kanit degildir.

Tarayıcıdan okunan link (imlecin altındaki ya da açık sayfa) varsa varsayılan odur. Pano metni
yalnız tazeliği doğrulanmışsa görünür; linkin yanında kapalı gelir. Eski pano metni gösterilmez.
Link yoksa dinleyicinin başlangıcından itibaren gözlenen, başarılı kayıtta kullanılmamış pano
revision'ı önceliklidir. İlk kullanım/yeniden başlatmada eldeki panonun yaşı bilinmez: metin
gizlenir; bağlantı ve görüntü kapalı seçenek olarak kalır. Snipping Tool dosya zamanı tek başına
otomatik tazelik sağlamaz.
İptal ve yalnız sayfayı kaydetmek panoyu tüketmez. Pano değişimi yakalama tuşundan sonra
olduysa yeni içerik kendiliğinden kaynak seçilmez. Metin/görüntü aynı pano oturumundan
kopyalanır; gecikmeli veya tutarsız okuma güvenli biçimde atlanır.

Seçim yolları: **Alt+I** imlecin altındaki bağlantı, **Alt+S** etkin sayfa, **Alt+A** alıntılanan
gönderi, **Alt+P** pano bağlantısı, **Alt+M** metin, **Alt+G** pano görüntüsü, **Alt+W** pencere
görüntüsü. Kısayollar not kutusunda da çalışır; büyük harf ve Türkçe I/İ desteklenir.
Birden çok bağlantı birlikte seçilebilir. Ana `url` önceliği: imleç bağlantısı, etkin sayfa,
alıntılanan gönderi, pano bağlantısı. Diğer seçilen adresler kartın `Ek bağlantılar` bölümünde
ve isteğe bağlı `ek_urller` listesinde tutulur; `isle` ana URL'yi işler. Aynı adres ikinci kart
olarak gösterilmez. Yalnız bir bağlantıyı kaydetmek için diğerlerinin işaretini kaldır.
Pano metni, dosya veya mail adresi eklenen mevcut URL kartı da `private` olur.
Mail adresi ana kaynak yerine `ek_urller` içinde seçilmiş olsa da bütün kart özel kalır.
Aynı popup'ın kayıt kimliği tekrar oynatılsa bile ikinci
commit yapılmaz.

Popup sıcak nötr zemin, kiremit vurgu ve küçük kısayol rozetleri kullanır. Seçili kaynaklar
renkli çerçeve ve onay işaretiyle ayrılır. Pencere kaynak monitörün çalışma alanına sığar;
uzun kaynak listesi fare tekerleği veya PgUp/PgDn ile kaydırılır. Not kutusu ve kaydetme
ipuçları yerinde kalır. Pencere görüntüsü kullanıcı seçmeden karta eklenmez; link ve görüntü
birlikte kaydedilebilir.

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

Windows'ta geçici el sıkışma zaman aşımı veya popup'ın beklenmedik kapanması kısayolu bırakmaz.
Önce eski süreç sonlandırılıp beklenir, ardından 0,5 saniye sonra yeni pencere ısıtılır.
Sonucu bilinmeyen yakalama otomatik tekrarlanmaz; sonraki basış yeni yakalamadır.
120 saniye içinde üçüncü geçici hatada dinleyici kapanır ve `yakala kur` ile yenilenmesi istenir.
Protokol uyumsuzluğu veya eski sürecin sonlandırılamaması doğrudan kapanma nedenidir.
Hata aşaması ve toparlanma kararı `yakala/dinleyici.log` içine yazılır.

Popup kaynak monitörde açılır. Windows tarafında Tk yönetilen pencere kullanır; özel
palet/canvas görünümü korunmak üzere yerel başlık/çerçeve stili kaldırılır. Düz **Esc** yerel
odakta hemen iptal eder. Kaynaklar okunurken ve popup açık olduğu sürece, odak nerede olursa
olsun, düşük seviyeli bir klavye kancası (`WH_KEYBOARD_LL`) Esc'e ve değiştirici tuşlara
(Shift, Ctrl, Alt, Win) bakar: popup öndeyse Esc'i Tk işler; kaynak pencere öndeyse (odak
reddedildi ya da sayfaya tıklandı) düz Esc tüketilir ve popup kapanır; başka bir pencere öndeyse
popup kapanır ve Esc o pencereye de iletilir. Değiştiricili Esc'i kanca hiçbir durumda tüketmez
ve popup'ı kapatmaz; Ctrl+Shift+Esc gibi sistem kısayolları çalışır. Popup'ın kendi Tk bağlaması
da yalnız düz Esc'te iptal eder: Caps/Num/Scroll Lock değiştirici sayılmaz, Tk'nin olayda
bildirmediği Win tuşu GUI iş parçacığında eşzamanlı `GetKeyState` ile okunur. Kanca, kurulduğu
anda zaten basılı değiştiriciler için `GetAsyncKeyState` değerini yalnız bir kez ve geri çağrının
dışında okur; sonrasında durumu kendi gördüğü basma/bırakma olaylarıyla (sol ve sağ tuş ayrı)
izler, Alt için olaydaki `LLKHF_ALTDOWN` bayrağına da bakar. Geri çağrının içinde asenkron tuş
durumu sorgulanmaz. Kanca popup kapanınca (kayıt, Esc, Kapat, Alt+F4, hata) kaldırılır;
sonrasında hiçbir uygulamanın Esc'i tutulmaz. Yeniden hotkey kancayı yeni kaynak pencereyle
yeniden kurar, eski yakalama içeriğini değiştirmez. `AttachThreadInput` kullanılmaz.
Görünür **Kapat** ve Alt+F4 da iptaldir. Windows güvenli masaüstü/UIPI/hook reddi için
her durumda global Esc garantisi yoktur. Kartın disk commit'i başladıysa Esc kaydedilmiş
veriyi geriye dönük iptal etmez; yavaş disk I/O'su sırasında arayüz yanıtı gecikebilir.

Pencere görüntüsü için link olsa da **WGC → PrintWindow → görüntü yok** sırası izlenir;
her backend ayrı, sonlandırılabilir süreçtedir. Bu hazırlık popup açılışına süre ekleyebilir.
Yakalama başarısızsa görüntü seçeneği oluşmaz. Önizleme yöntem
adını gösterir. Ekran DC'sinden BitBlt yoktur. Kaynak pencere değişirse sonuç atılır.
WGC, Windows 10 1903+ API yoludur; destek/izin/sürücü başarısızsa PrintWindow denenir.
Korumalı/GPU/exclusive-fullscreen uygulamaların tamamı için başarı veya HDR renk doğruluğu
iddia edilmez. Herhangi bir başarısızlık eski panoyu otomatik kaynak yapmaz.

UIA/pano için dış bekleme 1 saniye (Opera okuyucusu 1,8 saniye), WGC için 2,5 saniye, PrintWindow için 1,2 saniyedir;
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
   (yukarıdaki platform matrisi), Finder'da seçili dosya ya da panodaki metin.
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
- Mail, pano metni ve dosya kartları `visibility: private` ile yazılır. Seçilen ek
  bağlantılardan biri mail adresiyse ana kaynak herkese açık olsa da aynı kural uygulanır.
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

Python HTTP yolu bağlanırken adı yeniden çözer, her cevabı aynı kapıdan geçirir ve soketi denetlenen
IP adresine açar (TLS yine asıl sunucu adını doğrular). Denetimden sonra değişen bir DNS cevabı
(rebinding) bu yolu yerel adrese yönlendiremez. Sistemde proxy ayarlıysa hedefi proxy çözer; bu bağlama
proxy'nin kendisine uygulanmaz. Defuddle ve yt-dlp adı kendileri yeniden çözer ve kendi yönlendirmelerini
izler; bu iki yardımcı için ağ çıkışı izolasyonu yoktur. Ham kaynak metni güvenilmeyen veri olarak işaretlenir;
metnin içindeki rol değişikliği, komut çalıştırma ve dosya silme istekleri talimat sayılmaz.
