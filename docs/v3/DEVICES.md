# Çoklu cihaz düzeni (`devices`)

Beyin yerel önceliklidir ve varsayılan kurulum tek bilgisayar içindir. `beyin.py devices`
komutu, aynı vault'u birden çok cihazda kullanmak isteyenler için **isteğe bağlı ve kademeli**
bir düzenin ilk adımıdır. Komutu hiç çalıştırmazsan hiçbir şey değişmez.

Bu ilk adım git ve arka plan işi gerektirmeyen iki kademeyi kapsar. Git ile birden çok
bilgisayar ve her zaman açık bir merkez makine [#205](https://github.com/avenoxai/avenoxbeyin/issues/205)
altında ayrı değişiklikler olarak ele alınıyor; o zamana kadar git ile eşitleme kuralları
[MULTI-MACHINE.md](MULTI-MACHINE.md) içindedir.

## Kademeler

| Kademe | Kimin için | Gereken | Ek maliyet |
|---|---|---|---|
| `solo` | Tek bilgisayar. Varsayılan. | Yok | Yok |
| `phone` | Bir bilgisayar + telefon/tablet. Telefonda notları Obsidian ile okur ve düzenlersin; Beyin bilgisayarda çalışır. | Bir dosya eşitleme servisi: iCloud, Obsidian Sync ya da başka biri | iCloud: ücretsiz kotaya sığarsa yok. Obsidian Sync: ücretli abonelik |

`--apply --level solo` her zaman geri dönüş yoludur: `devices.json` bozuksa ya da bu sürümün
tanımadığı bir ayar taşıyorsa (ör. daha yeni bir sürümden geri dönüldüyse) onu da varsayılana çevirir.
Diğer seçenekler böyle bir dosyanın üzerine yazmaz, durur.

### Telefon seçenekleri (`--phone-sync`)

| Değer | Nasıl çalışır | Sınırı |
|---|---|---|
| `icloud` | Vault, Mac'te iCloud Drive içindeki Obsidian klasöründe durur; iPhone/iPad Obsidian aynı klasörü açar. | Apple cihazları ister. Beyin Mac'te çalışır; Mac kapalıyken telefondaki değişiklik Beyin'in indeksine Mac açılınca girer. |
| `obsidian-sync` | Obsidian'ın kendi ücretli eşitlemesi. iOS, Android, Windows, macOS ve Linux arasında çalışır. | Ücretli. Gizli klasörleri (`.obsidian` dışında) eşitlemez; bu yüzden her bilgisayar kendi Beyin kurulum dosyalarını tutar. iCloud klasöründeki bir vault ile birlikte kullanma. |
| `other` | Syncthing, Dropbox, OneDrive gibi başka bir servis. | Servise göre değişir; Beyin yalnız çakışma kopyalarını raporlar. |

`phone` kademesinde Beyin'i **tek bilgisayarda** çalıştır. İki bilgisayar aynı dosya eşitleme
klasöründen Beyin çalıştırırsa kurulum dosyaları birbirinin yollarını taşır ve yeniden kurulum
`Reinstall conflict` ile durur.

## Akış: önce bak, sonra planla, sonra uygula

```sh
python3 beyin.py devices --detect          # bu makine neye hazır; hiçbir şey yazmaz
python3 beyin.py devices --plan  --level phone --phone-sync <seçenek>   # ne kalıyor; hiçbir şey yazmaz
python3 beyin.py devices --apply --level phone --phone-sync <seçenek>   # ayarı kaydeder
python3 beyin.py devices                   # şu anki ayar
```

Windows'ta `python3` yerine `py -3` kullan.

- `--detect` işletim sistemini, vault'un bir eşitleme klasöründe olup olmadığını, Obsidian ve
  Obsidian Sync izlerini ve vault bir git deposuysa git dizininin nerede olduğunu bildirir
  (uzak depolar okunmaz). `levels` alanı her kademe için eksikleri,
  `current_fit` ortamın şu an neye benzediğini söyler.
- `--plan` çıktısında `manual_steps` senin elle yapman gerekenleri (vault'u taşımak, telefonda
  Obsidian'ı açmak gibi), `warnings` bilmen gereken riskleri listeler.
- `--apply` yalnız seçilen kademeyi runtime state içindeki `devices.json` dosyasına yazar.
  Vault'a hiçbir şey yazmaz, dosya taşımaz, arka plan işi kurmaz.

Ajanla kuruyorsan ona şunu söylemen yeter: *"devices --detect çalıştır, bana kademeleri anlat,
seçtiğimin planını göster, onaylarsam uygula."*

## Örnekler

### Mac + iPhone, sunucusuz (iCloud)

```sh
python3 beyin.py devices --apply --level phone --phone-sync icloud
```

Komut ayarı kaydeder ve kalan adımları listeler: vault iCloud Drive/Obsidian klasöründe
değilse oraya taşımak ve kurucuyu `--rebind-state` ile yeniden çalıştırmak
([vault taşıma](MULTI-MACHINE.md#vaultu-başka-klasöre-ya-da-hesaba-taşımak)), telefonda
Obsidian ile klasörü açmak. Runtime state iCloud'a konmaz.

### Her platform + telefon, sunucusuz (Obsidian Sync)

```sh
python3 beyin.py devices --apply --level phone --phone-sync obsidian-sync
```

## Git deposu olan vault

Plan iki durumu uyarı olarak yazar; komut git'e dokunmaz:

- `.git` klasörü vault'un içinde: dosya eşitleme servisleri git verisini bozabilir ve onu diğer
  cihazlara taşır.
- `.git` bir dosya (`git init --separate-git-dir` düzeni): içinde bu makinenin git dizininin
  mutlak yolu yazar; eşitleme servisi onu, yolun yanlış olduğu diğer cihazlara taşır.

Uyarı `icloud` ve `other` seçeneklerinde, vault bir eşitleme klasöründeyse ya da Syncthing izi
(`.stfolder`) varsa verilir. Obsidian Sync gizli klasörleri eşitlemediği için tek başına uyarı
gerektirmez. Git dizinini eşitlenen klasörün dışında tut ya da bu vault'ta git kullanma.

## Neye dokunur, neye dokunmaz

- Ayar (`devices.json`) **state klasöründedir**; vault'a ve dolayısıyla diğer cihazlara gitmez.
  Bu komutu içermeyen eski bir motor sürümüne geri dönersen dosya yok sayılır.
- Vault'a hiçbir dosya yazmaz; notları taşımaz, silmez, yeniden adlandırmaz. Vault'u eşitleme
  klasörüne taşımak senin adımındır.
- `--detect`, `--plan` ve durum çıktısı ağa çıkmaz; git için yalnız yerel `rev-parse` çalışır.
- `doctor` kademe açıkken bir satırla gösterir; kapalıyken yalnız `{"enabled": false}` yazar.

## Gerçek cihazda denenenler

| Parça | Durum |
|---|---|
| macOS (Python 3.14), vault iCloud Drive/Obsidian klasöründe: `--detect`, `--plan`, `--apply --level phone --phone-sync icloud`, `doctor`, `--apply --level solo` | Bu sürümle denendi |
| `git init --separate-git-dir` ile kurulmuş kasada `.git` dosyası algılama ve uyarısı | Bu sürümle denendi |
| iPhone'da Obsidian ile aynı iCloud klasörü | Denendi (bu sürüm vault'a yazmadığı için telefon tarafı değişmez) |
| `obsidian-sync` ve `other` telefon seçenekleri | Denenmedi |
| Windows ve Linux'ta `--detect` | Birim testleriyle sınandı, gerçek makinede denenmedi |

## Bilinen sınırlar

- Bu düzen dosya düzeyinde eşitlemedir; cihazlar arası kayıt düzeyinde birleştirme, cihaz
  yetkilendirmesi ve onay protokolü #205 altında açık tasarım işidir.
- iCloud gizli klasörleri de yükler; `.beyin-note-write/` altındaki not yedekleri kendi iCloud
  hesabında durur.
