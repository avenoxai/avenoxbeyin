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
| Her uygulamada kısayol | `Control+Option+B` | `Ctrl+Alt+B` (Linux'ta da) |
| Dosya gönderme | Finder'da dosyayı seç, kısayola bas | Sağ tık > Gönder > Beyne At |
| Tarayıcı | Obsidian Web Clipper şablonu | Obsidian Web Clipper şablonu |
| Ajan | `beyin-yakala` skill'i | `beyin-yakala` skill'i |

Kısayol ek paket istemez; Python'un kendisiyle çalışır. Mac'te bir oturum açılış servisi
(LaunchAgent) tuşu dinler, Windows'ta Başlat menüsündeki kısayol tuşu kullanılır.
### Linux

`kur`, `~/.local/share/applications/beyne-at.desktop` dosyasını (`XDG_DATA_HOME` varsa orada) yazar;
komut `python3 <kopya> pencere --vault <vault>` olur ve betiğin bir kopyası state klasöründe durur.
Kısayol `Ctrl+Alt+B` (ya da `--tus`) masaüstüne göre kaydedilir:

- **KDE Plasma:** `kwriteconfig6` (yoksa `kwriteconfig5`) ile `kglobalshortcutsrc` içine yazılır,
  `gdbus` ile oturum kapatmadan etkinleştirilir. Çıktıdaki `kisayol_calisiyor` tuşun gerçekten
  alındığını gösterir. Sınır: kısayol daha önce başka bir komutla kurulduysa (vault taşındı, Python
  değişti) KDE çalışan oturumda eski komutu çalıştırmayı sürdürebilir; oturumu kapatıp açınca yenisi
  geçerli olur. İlk kurulumda bu sorun yok.
- **GNOME:** `gsettings` ile özel bir kısayol (`custom-keybindings`) eklenir; senin mevcut
  kısayolların korunur.
- **Diğerleri:** `.desktop` dosyası yazılır, kısayol kurulmaz; `kur` çıktısındaki komutu masaüstü
  ayarlarında bir kısayola bağla.

Kaldırınca `.desktop` dosyası ve kısayol silinir. Pencere neyi kaydedeceğini panodan okur: bunun için
Wayland'da `wl-clipboard` (`wl-paste`), X11'de `xclip` ya da `xsel` gerekir; yoksa pano okunamaz.
Panodaki bağlantı, kopyalanan dosyalar (`file://`) ya da düz metin yakalanır.

Tam pencere `tkinter` ister. Arch tabanlı dağıtımlarda Python'un `tk` paketi ayrıdır
(`sudo pacman -S tk`); yoksa pencere yerine `kdialog` ya da `zenity` ile yalnız "neden" sorulur
(ikisi de yoksa neden sorulmadan kaydedilir).

Doğrulamak için: `python3 beyin.py yakala durum` (`Kisayol dinleyicisi: calisiyor`), KDE'de
`gdbus call --session -d org.kde.kglobalaccel -o /component/beyne_at_desktop -m org.kde.kglobalaccel.Component.isActive`
(`(true,)` beklenir), sonra kısayola bas.

Yalnız şablon ve skill istiyorsan: `python3 beyin.py yakala kur --kisayol-yok`.

### Kısayolu değiştir

```sh
python3 beyin.py yakala kisayol 'cmd+"'
```

Tuşu yazdığın gibi tarif et: `cmd`, `ctrl`, `alt` (ya da `option`), `shift` ve en sonda tuş.
Örnekler: `ctrl+alt+b` (varsayılan), `cmd+shift+space`, `alt+f5`, `⌘⇧K`. Mac'te karakter etkin klavye
düzeninden bulunur; Türkçe Q'da `"` 1'in solundaki tuştur. Windows'ta Başlat menüsü kısayolu
yalnız Ctrl/Alt/Shift ile harf, rakam ya da F tuşunu kabul eder. Argümansız `kisayol` mevcut tuşu
gösterir. Seçtiğin tuşu başka bir uygulama zaten kullanıyorsa komut uyarır; başka bir tuş dene.

### Tarayıcı: Obsidian Web Clipper

1. [Obsidian Web Clipper](https://obsidian.md/clipper) eklentisini kur (Chrome, Firefox, Safari, Edge, Arc, Brave).
2. Eklentinin ayarlarında **Şablonlar > İçe aktar** ile kurulumun yazdığı
   `📥 000-Inbox/Yakala/beyne-at-web-clipper.json` dosyasını seç.
3. Bir sayfada eklentiye bas, "Beyne at" şablonunu seç, istersen **Neden** bölümüne bir satır yaz, ekle.

Web Clipper sayfayı tarayıcının içinden okur; giriş isteyen sayfalar (Gmail, ücretli makale) da böylece yakalanır.

## Kullanım

1. **Yakala.** Kısayola bas. Pencere neyin kaydedileceğini gösterir: tarayıcıdaki sayfa
   (Mac'te Chrome, Arc, Brave, Edge, Safari), Finder'da seçili dosya ya da panodaki metin.
   İstersen tek satır "neden" yaz, Enter. Esc vazgeçer.
2. **İşlet.** Ajanına "yakalananları işle" de. Oturum başında bekleyen kaynak sayısı da görünür.

Her yakalama `📥 000-Inbox/Yakala/` içinde bir kart olur. Aynı videoyu ya da sayfayı ikinci kez
yakalarsan yeni kart açılmaz; yeni notun aynı karta eklenir.

## İşleme nasıl çalışır

`python3 beyin.py yakala isle` bekleyen kartların metnini çıkarır, sonra ajan dersleri
`knowledge/` notlarına bağlar ve kartı `bitti` ile kapatır.

| Kaynak | Yol |
|---|---|
| Web Clipper ile gelen sayfa | Yakalanan metin kullanılır, ağa çıkılmaz |
| YouTube | [Defuddle](https://github.com/kepano/defuddle) ile bölümlü transkript; olmazsa `yt-dlp` altyazısı; o da yoksa yalnız ses indirilir, yerel Whisper ile yazıya dökülür, ses silinir |
| X, Reddit, GitHub, makale | Defuddle; yoksa düz HTTP ile metin |
| Mail | Seçili metin; tamamı için ajanın Gmail/Outlook bağlantısı |
| PDF, metin dosyası | `pdftotext` ya da doğrudan okuma; görselleri ajan açar |

Ham metin `📥 000-Inbox/Yakala/.ham/` altında durur. Nokta ile başlayan klasör olduğu için
aramaya ve Obsidian'a karışmaz, `.gitignore` ile depoya da girmez.

### İsteğe bağlı araçlar

Hiçbiri zorunlu değil; varsa işleme genişler, yoksa elde olanla devam edilir.
`python3 beyin.py yakala durum` hangilerinin bulunduğunu söyler.

- **Node.js** (`npx`): Defuddle için. Sabit sürüm (`defuddle@0.19.4`) çalıştırılır.
- **yt-dlp**: video altyazısı ve ses.
- **Whisper**: Mac'te `mlx_whisper`, diğerlerinde `whisper` ya da `whisper-ctranslate2`. Ses indirmeyi istemezsen `isle --ses-yok`.
- **pdftotext** (Poppler): PDF metni.

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
beyin.py yakala kur [--kisayol-yok] [--tus 'ctrl+alt+b']
beyin.py yakala kisayol ['cmd+"']
beyin.py yakala kaldir | durum | sablon
```
