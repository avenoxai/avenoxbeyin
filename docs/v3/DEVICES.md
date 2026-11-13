# Çoklu cihaz düzeni (`devices`)

Beyin yerel önceliklidir ve varsayılan kurulum tek bilgisayar içindir. `beyin.py devices`
komutu, aynı vault'u birden çok cihazda kullanmak isteyenler için **isteğe bağlı ve kademeli**
bir düzen kurar. Hiçbir kademe zorunlu değildir; cihazlarına ve bütçene uyan en küçük kademeyi
seçer, ihtiyaç doğunca bir üsttekine geçersin. Komutu hiç çalıştırmazsan hiçbir şey değişmez.

Bu sayfa kurulum akışını anlatır. Hangi dosyaların eşitlendiği, çakışma kuralları ve vault
taşıma ayrıntıları [MULTI-MACHINE.md](MULTI-MACHINE.md) içindedir; telefondan yakalama
sunucusunun API'si [INGEST-SERVER.md](INGEST-SERVER.md) içindedir.

## Kademeler

| Kademe | Kimin için | Gereken | Ek maliyet |
|---|---|---|---|
| `solo` | Tek bilgisayar. Varsayılan. | Yok | Yok |
| `phone` | Bir bilgisayar + telefon/tablet. Telefonda notları Obsidian ile okur ve düzenlersin; Beyin bilgisayarda çalışır. | Bir dosya eşitleme servisi: iCloud, Obsidian Sync ya da başka biri | iCloud: ücretsiz kotaya sığarsa yok. Obsidian Sync: ücretli abonelik |
| `computers` | Her biri Beyin çalıştıran iki ya da daha çok bilgisayar. | git ve sana ait bir uzak depo (özel barındırılan depo, NAS, SSH ile ulaşılan başka bir makine) | Çoğu durumda yok |
| `hub` | Her zaman açık bir merkez makine: depo orada durur, istersen ajan ve yakalama sunucusu 7/24 orada çalışır. | Her zaman açık bir makine (VPS, ev sunucusu, tek kart bilgisayar, eski bir dizüstü), SSH, önerilen: Tailscale | Makinenin maliyeti (VPS ise aylık ücret; evdeki eski bir makineyse elektrik) |

Kademeler birikir: `computers` ve `hub` kademelerinde bilgisayarlardan biri aynı anda telefon
köprüsü olabilir (`--phone-sync`). Bir üst kademeye geçmek alttakini bozmaz; `--level solo`
her zaman geri dönüş yoludur.

### Telefon seçenekleri (`--phone-sync`)

| Değer | Nasıl çalışır | Sınırı |
|---|---|---|
| `icloud` | Vault, Mac'te iCloud Drive içindeki Obsidian klasöründe durur; iPhone/iPad Obsidian aynı klasörü açar. | Apple cihazları ister. Köprüyü Mac kurar: Mac kapalıyken telefondaki değişiklik diğer cihazlara ulaşmaz. |
| `obsidian-sync` | Obsidian'ın kendi ücretli eşitlemesi. iOS, Android, Windows, macOS ve Linux arasında çalışır. | Ücretli. Gizli klasörleri (`.obsidian` dışında) eşitlemez; bu yüzden her bilgisayar kendi Beyin kurulum dosyalarını tutar. iCloud klasöründeki bir vault ile birlikte kullanma. |
| `other` | Syncthing, Dropbox, OneDrive gibi başka bir servis. | Servise göre değişir; Beyin yalnız çakışma kopyalarını raporlar. |

`phone` kademesinde Beyin'i **tek bilgisayarda** çalıştır. İki bilgisayar aynı dosya eşitleme
klasöründen Beyin çalıştırırsa kurulum dosyaları birbirinin yollarını taşır ve yeniden kurulum
`Reinstall conflict` ile durur; iki bilgisayar için `computers` kademesini kullan.

## Akış: önce bak, sonra planla, sonra uygula

```sh
python3 beyin.py devices --detect          # bu makine neye hazır; hiçbir şey yazmaz
python3 beyin.py devices --plan  --level <kademe> [seçenekler]   # ne yapılacak; hiçbir şey yazmaz
python3 beyin.py devices --apply --level <kademe> [seçenekler]   # planı uygular
python3 beyin.py devices                   # şu anki ayar ve son eşitleme turu
```

Windows'ta `python3` yerine `py -3` kullan.

- `--detect` işletim sistemini, vault'un bir eşitleme klasöründe olup olmadığını, Obsidian ve
  Obsidian Sync izlerini, git deposunu ve uzak depoları (parola ve token gösterilmeden), git,
  ssh ve tailscale araçlarının varlığını ve kullanılabilecek zamanlayıcıyı bildirir. `levels`
  alanı her kademe için eksikleri, `current_fit` ortamın şu an neye benzediğini söyler.
- `--plan` çıktısında `actions` komutun kendisinin yapacağı işleri, `manual_steps` senin elle
  yapman gerekenleri (telefonda Obsidian'ı açmak, Tailscale'e giriş yapmak gibi), `warnings`
  bilmen gereken riskleri listeler.
- `--apply` yalnız `actions` altındaki işleri yapar. Bir adım başarısız olursa durur, ayarı
  değiştirmez; nedeni giderip aynı komutu yeniden çalıştırabilirsin.

Ajanla kuruyorsan ona şunu söylemen yeter: *"devices --detect çalıştır, bana kademeleri anlat,
seçtiğimin planını göster, onaylarsam uygula."*

## Örnekler

### 1. Mac + iPhone, sunucusuz (iCloud)

```sh
python3 beyin.py devices --apply --level phone --phone-sync icloud
```

Komut yalnız ayarı kaydeder ve yapılacakları listeler: vault iCloud Drive/Obsidian klasöründe
değilse oraya taşımak ve kurucuyu `--rebind-state` ile yeniden çalıştırmak
([vault taşıma](MULTI-MACHINE.md#vaultu-başka-klasöre-ya-da-hesaba-taşımak)), telefonda
Obsidian ile klasörü açmak. Runtime state iCloud'a konmaz.

### 2. Her platform + telefon, sunucusuz (Obsidian Sync)

```sh
python3 beyin.py devices --apply --level phone --phone-sync obsidian-sync
```

### 3. İki bilgisayar, git ile

İlk bilgisayarda (vault henüz git deposu değilse `--init-git` gerekir; Beyin senden habersiz
depo kurmaz):

```sh
python3 beyin.py devices --apply --level computers --remote <özel deponun adresi> --init-git --interval 15
python3 beyin.py devices --sync-once
```

İkinci bilgisayarda **boş bir klasöre depoyu klonla**, Beyin'i o klasöre kur, sonra:

```sh
python3 beyin.py devices --apply --level computers --interval 15
```

İkinci bilgisayarda ayrı ve dolu bir vault varken onu aynı depoya bağlama: iki kopyadaki aynı
adlı notlar ilk turda çakışır. Önce klonla, eski kopyadaki farklı notları elle taşı.

Uzak depo **özel** olmalı; vault kişisel notlar taşır. HTTPS adresinde zamanlanmış tur parola
yazamaz: git kimlik yardımcısı kur ya da SSH adresi kullan.

### 4. Merkez makine (VPS ya da evde her zaman açık bir makine)

Merkezde:

```sh
python3 beyin.py devices --apply --level hub --role hub --bare ~/git/vault.git --init-git --interval 10 --capture
```

Diğer bilgisayarlarda (önce `git clone ssh://kullanici@merkez/home/kullanici/git/vault.git`):

```sh
python3 beyin.py devices --apply --level hub --role client --hub-host merkez --hub-user kullanici \
  --hub-path /home/kullanici/git/vault.git --interval 15
```

Mac aynı zamanda iPhone köprüsüyse vault iCloud klasöründe durur ve git dizini dışarı alınır:

```sh
python3 beyin.py devices --apply --level hub --role client --phone-sync icloud \
  --hub-host merkez --hub-user kullanici --hub-path /home/kullanici/git/vault.git \
  --git-dir ~/.vault-git/vault.git --interval 15
```

Bu düzende telefonun üç yolu olur: Obsidian ile notlar (iCloud üzerinden, Mac açıkken
merkeze ulaşır), paylaşım menüsünden yakalama (doğrudan merkeze, Mac kapalıyken de) ve
merkezde çalışan ajana uzaktan bağlanma (kullandığın istemcinin uzaktan erişim özelliğiyle;
Beyin bunu kurmaz).

## Özel ağ: Tailscale

`hub` kademesinde cihazların birbirine ulaşması gerekir. SSH ya da yakalama portunu genel
internete açma. Önerilen yol [Tailscale](https://tailscale.com/) gibi bir özel ağdır: her
cihaza kurulur, aynı hesapla giriş yapılır, cihazlar birbirini adıyla görür (`--hub-host merkez`).
Kişisel kullanım için ücretsiz katmanı vardır; koşullar değişebilir, güncel durumu kendi
sitesinden kontrol et.

Beyin Tailscale'i **kurmaz ve hesabına giriş yapmaz**: kurulum yönetici yetkisi ve tarayıcıda
oturum açma ister, bunlar senin kararın. `--detect` yalnız `tailscale` komutunun var olup
olmadığına bakar. `--capture` verildiğinde yakalama sunucusunun dinleyeceği adres için yerel
Tailscale adresi sorulur (`tailscale ip -4`, ağa çıkmaz); adres bulunamazsa sunucu yalnız
`127.0.0.1` üzerinde dinler ve plan bunu uyarı olarak yazar. Başka bir özel ağ kullanıyorsan
adresi `--capture-host` ile ver.

Linux merkezde tipik adımlar (plan bunları `manual_steps` içinde hatırlatır):

1. Tailscale'i kur ve giriş yap (kendi kurulum sayfasındaki komutla), makineye bir ad ver.
2. İstemcilerden SSH anahtarıyla girişi aç (`ssh-copy-id kullanici@merkez`).
3. Bir kez elle bağlan (`ssh kullanici@merkez true`): ilk bağlantıdaki "host anahtarına güven"
   sorusunu zamanlanmış tur cevaplayamaz.
4. Kullanıcı servisleri oturum kapanınca da çalışsın: `sudo loginctl enable-linger "$USER"`.

## Eşitleme turu (`--sync-once`)

`computers` ve `hub` kademelerinde tek tur şunu yapar: yerel değişiklikleri commit eder, uzak
depoyu getirir, yerel commit'leri uzaktakilerin üstüne taşır (rebase), gönderir. Başka cihazdan
not geldiyse aynı turda yerel indeks de güncellenir.

| `status` | Anlamı |
|---|---|
| `ok` | Tur tamam. `committed`, `pulled`, `pushed` ne olduğunu söyler. |
| `offline` | Uzak depoya ulaşılamadı. Yerel commit durur, sonraki tur gönderir. |
| `conflict` | Aynı satırlar iki cihazda değişmiş. Rebase geri alınır: çalışma klasöründe **çakışma işareti kalmaz**, yerel commit'ler durur, uzak değişiklikler uygulanmaz. `conflicts` dosyaları listeler; vault'ta `git pull --rebase` ile elle çöz. |
| `blocked` | Vault'ta yarım kalmış bir rebase/merge var ya da başka bir daldasın. Tur hiçbir şeye dokunmaz. |
| `retry` | Gönderme kabul edilmedi (başka cihaz araya girdi ya da yazma izni yok). Sonraki tur yeniden dener. |
| `already_running` | Başka bir tur sürüyor. |

Çakışmada rebase'in geri alınması bilinçli bir seçimdir: vault bir dosya eşitleme servisiyle
telefona da gidiyorsa `<<<<<<<` işaretli notların telefona yayılması istenmez. Bedeli, çakışma
çözülene kadar o cihazın uzak değişiklikleri almamasıdır; `doctor --human` bunu
"Coklu cihaz esitlemesi durdu" satırıyla gösterir.

`Last-Session.md`, `Journal.md` ve arşiv dosyaları `.gitattributes` içindeki `merge=union`
sayesinde çakışmadan birleşir; `Threads.md`, `Kurallar.md` ve `Core.md` bilerek birleşmez
([neden](MULTI-MACHINE.md#önerilen-gitattributes)).

Commit mesajı `Beyin sync <cihaz> <tarih saat>` biçimindedir. Cihaz etiketi varsayılan olarak
`mac`, `linux` ya da `windows`; `--device-name` ile değiştirebilirsin. Makine adı ve kullanıcı
adı mesaja yazılmaz.

## Zamanlama (`--interval`)

`--interval <dakika>` (5 ile 1440 arası; 0 kapatır) turu düzenli çalıştırır:

| Ortam | Kurulan şey |
|---|---|
| macOS | Kullanıcı LaunchAgent'ı (`~/Library/LaunchAgents/com.avenox.beyin-devices.<kimlik>.plist`) |
| Linux, systemd kullanıcı oturumu varsa | `~/.config/systemd/user/beyin-devices-<kimlik>.service` ve `.timer` |
| Windows | Görev Zamanlayıcı görevi (`BeyinDevices-<kimlik>`) |
| cron olan ama systemd olmayan Linux | Hiçbir şey kurulmaz; plan eklenecek crontab satırını verir |
| Zamanlayıcı yoksa (ör. konteyner) | Hiçbir şey kurulmaz; plan kendi başlangıç betiğine koyacağın döngüyü verir |

`python3 beyin.py devices --remove-schedule` kurulan zamanlamayı kaldırır. Zamanlama yönetici
yetkisi istemez ve sistem geneline bir şey kurmaz.

**macOS + iCloud:** macOS, iCloud Drive, Belgeler ve Masaüstü klasörlerini arka plan işlerine
kapatır. Vault bu klasörlerdeyse zamanlanmış tur `Operation not permitted` ile düşer; plan bunu
uyarır. Çözüm, planda adı geçen Python'a Sistem Ayarları › Gizlilik ve Güvenlik › Tam Disk
Erişimi vermektir.

## Telefondan yakalama (`--capture`)

`--capture`, [yakalama sunucusunu](INGEST-SERVER.md) hazırlar: state klasöründe yalnız senin
okuyabildiğin bir dosyaya rastgele bir token üretir (`ingest-token.env`) ve systemd kullanıcı
oturumu olan Linux'ta sunucuyu servis olarak başlatır. Diğer ortamlarda plan, çalıştırılacak
komutu verir. Token hiçbir çıktıda gösterilmez ve vault'a yazılmaz; telefondaki kestirmeye
dosyadan kendin kopyalarsın. Sunucu yalnız gelen kutusuna ekler, vault'tan hiçbir şey sunmaz.

## Neye dokunur, neye dokunmaz

- Ayar (`devices.json`), son tur kaydı, kilit, günlük ve token **state klasöründedir**; vault'a
  ve dolayısıyla diğer cihazlara gitmez. Eski bir motor sürümüne geri dönersen bu dosyalar
  yok sayılır.
- Vault'ta yalnız `.gitignore` ve `.gitattributes` dosyalarına, işaretli bir blok içinde
  satır ekler. Blok dışındaki satırlarına dokunmaz; zaten yazdığın satırı yinelemez.
  `.claude/scripts` içinde kendi dosyaların varsa klasörün tamamını değil yalnız motor
  dosyalarını dışlar.
- Git'in zaten izlediği kurulum dosyalarını takipten **çıkarmaz**; yalnız uyarır. Bu adım
  diğer cihazlarda dosya silebildiği için [elle ve sırayla](MULTI-MACHINE.md#daha-önce-izlenen-kurulum-dosyalarını-ayırmak) yapılır.
- Notları taşımaz, silmez, yeniden adlandırmaz. Vault'u eşitleme klasörüne taşımak senin adımındır.
- `--git-dir`, git dizinini eşitleme klasörünün dışına alır (`git init --separate-git-dir`).
  Hedef bir eşitleme klasörünün içindeyse reddedilir: eşitleme servisleri git verisini bozar.
- `--detect`, `--plan` ve durum çıktısı ağa çıkmaz. Ağa çıkan tek şey senin yapılandırdığın
  uzak depoya giden `--sync-once` turudur.

## Bilinen sınırlar

- Bu düzen dosya düzeyinde eşitlemedir; cihazlar arası kayıt düzeyinde birleştirme, cihaz
  yetkilendirmesi ve onay protokolü [#205](https://github.com/avenoxai/avenoxbeyin/issues/205)
  altında açık tasarım işidir.
- `icloud` köprüsünde telefon, Mac açık ve çevrimiçi olduğu sürece diğer cihazlarla buluşur.
- iCloud gizli klasörleri de yükler; `.beyin-note-write/` altındaki not yedekleri kendi iCloud
  hesabında durur.
- Zamanlayıcı tanımlarının (LaunchAgent, systemd, Görev Zamanlayıcı) içeriği birim testleriyle
  sınanır. Tanımın işletim sistemine yüklenmesi (`launchctl`, `systemctl`, `schtasks`) bu
  sürümde gerçek makinede denenmedi; yükleme başarısız olursa `--apply` durur, hatayı gösterir
  ve ayarı değiştirmez.
- Özel notları dışarıda bırakan filtreli bir bulut yedeği bu komutun kapsamında değildir.
