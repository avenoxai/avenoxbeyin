# Kurulum, güncelleme ve geri alma

Vault içindeki `beyin.py` tek giriş noktasıdır. Komutları vault klasöründe çalıştır. Aşağıdaki örneklerde macOS/Linux için `python3` kullanılır; Windows'ta bunun yerine `py -3` yazılır. Python 3.11+ gerekir.

## Git kullanmadan ilk kurulum

[Son kararlı release sayfasından](https://github.com/avenoxai/avenoxbeyin/releases/latest) `beyin-v3-X.Y.Z.zip` paketini indirip aç (`X.Y.Z` sayfadaki sürüm numarasıdır). Paket ve minimum gereksinimler sürüm sayfasında belirtilir. GitHub'ın otomatik kaynak arşivi ile ürün paketi farklıdır.

Bir vault klasörü seç veya oluştur. Açılan paketin içinde:

```sh
python3 scripts/install_v3.py --vault "/tam/yol/Beynim"
```

Windows örneği:

```powershell
py -3 scripts/install_v3.py --vault "C:\Notlar\Beynim"
```

Runtime varsayılan olarak işletim sisteminin yerel uygulama verisi alanına, vault dışında kurulur. İstersen kurulumda `--state "/ayri/yerel/dizin"` verebilirsin; yalnız bu uygulama için ayrılmış bir dizin seç. Kurulu giriş ve başlatıcılar bu seçimi hatırlar. Yeni hesap, Git, pip veya servis kurulmaz.

İlk bağlantı için AI istemcisinde vault'u açıp yeni oturum başlat. Hook tanımları değiştiğinde istemcinin gerekli güven incelemesini tamamla; updater güven kaydı üretmez.

## Kontrol ve güncelleme

```sh
python3 beyin.py doctor
python3 beyin.py update --check
python3 beyin.py update
```

`doctor` yerel kayıtları, bekleyen işleri ve sorunları gösterir. Bir metadata raporu gerçek istemci teslimi veya her notun doğru yorumlandığı kanıtı değildir.

`update --check` resmi stable release'i inceler ve paketi geçici alanda doğrular. Vault ve mevcut runtime dosyalarını değiştirmez. Paket verilmezse ağ erişimi gerekir. `update` aynı doğrulamadan sonra yeni sürümü uygular. Aynı sürüme ikinci güncelleme no-op'tur; sayısal sürüm sırası kullanılır. Eski bir paketi yükleyerek downgrade yapılmaz; geri dönüş için `rollback` kullanılır.

Varsayılan indirme kaynağı `avenoxai/avenoxbeyin` deposunun resmi GitHub stable release'idir. Preview, hareket eden geliştirme dalı veya üçüncü taraf paket kaynağı otomatik seçilmez. Stable release ya da beklenen ZIP yoksa bunu hata olarak bildirir; yayın varmış gibi sonuç vermez.

Yerel, önceden indirilmiş ZIP ile ağ gerekmez:

```sh
python3 beyin.py update --check --package "/indirilen/beyin-v3-X.Y.Z.zip"
python3 beyin.py update --package "/indirilen/beyin-v3-X.Y.Z.zip"
```

Yerel paketi yalnız güvendiğin kaynaktan al: checksum bütünlüğü kontrol eder; bağımsız bir imza veya kaynak güveninin yerine geçmez.

## Tıklanabilir başlatıcılar

Kurucu vault'a yalnız ilgili platformun başlatıcısını koyar:

| Platform | Başlatıcı |
| --- | --- |
| macOS | `Beyni Güncelle.command` |
| Windows | `Beyni Guncelle.cmd` |
| Linux | `Beyni Güncelle.desktop` ve `Beyni Güncelle.sh` |

Hepsi kurulu `beyin.py update` komutunu çağırır, ayrı güncelleme mantığı içermez. İşletim sistemi dosyanın açılması/çalıştırılması için izin isteyebilir. Özel Python/runtime yolu kurulumda kaydedilir; Python'u sonradan taşıdıysan kurulumu yeniden değerlendirmek gerekir.

## Kesinti veya sorun

```sh
python3 beyin.py recover
python3 beyin.py rollback
```

`recover`, yarım kalan işlemin journal'ını okuyup planlanan işlemi tamamlar. Bir hata mesajı işlemin iptal edildiği anlamına gelmez; bazı sistem dosyaları yazılmış, başarılı sürüm damgası henüz yazılmamış olabilir.

`rollback`, son sistem işleminin yedeğini geri getirir. [Opsiyonel global köprüyü](GLOBAL-BRIDGE.md) kendin eklediysen, köprüyü içermeyen eski bir sürüme (V3.1.0 ve öncesi) `rollback` veya `uninstall` yapmadan önce global ayardaki köprü handler'larını kaldır; olmayan bir scripti global hook'ta bırakmak istemci hatası üretir. İlk V2 geçişinde önceki sürüm ve tanınan eski runner'lar da geri yüklenir. Temiz V3 kurulumunu geri almak `uninstalled` olarak raporlanır; olmayan bir eski sürüm uydurulmaz. Sonradan eklediğin kullanıcı notları silinmez. Bu komut bütün vault geçmişini geri alan bir işlem değildir.

Yönetilen dosyada araya giren kullanıcı değişikliği varsa işlem bunu ezmek yerine conflict ile durur. Desteklenen JSON ayarlarında ilgisiz değişiklikler korunur; çakışan yönetilen bölüm için inceleme gerekir. Kilitli/aktif bir writer varsa tamamlanmasını bekleyip tekrar dene. Kaybolmuş bir işin kilit/sentinel dosyasını gelişigüzel silme.

Yalnız satır sonu farkı değişiklik sayılmaz: `core.autocrlf` ya da bir editör yönetilen dosyayı CRLF'e çevirmişse güncelleme, kaldırma ve rollback durmaz, dosya yeniden yazılırken stok LF biçimine döner. Conflict mesajı sebebi söyler: `content differs` gerçek bir düzenleme, `deleted` silinmiş dosya demektir.

## `invalid legacy skill hashes` (#73)

17–19 Eylül 2026 arasında `cad7953` (#35) sonrası `main` üzerinden kurulan
bazı vault'lar `3.0.2` damgası taşısa da resmi V3.0.2 etiketinden farklı bir
paket doğrulayıcı kullanır. Bu doğrulayıcı V3.1.0/V3.2.0 manifestindeki yeni
skill yollarını reddeder; `update --check` de aynı hatayı verir. Resmi V3.0.2
paketinden yapılan geçiş bu özel durumu temsil etmez.

V3.2.1 için hazırlanan paket biçimi bu eski doğrulayıcıyla uyumludur; stable
olarak yayımlandıktan sonra normal `update` yeterlidir. Henüz yayımlanmamış
bir düzeltmenin kurulu olduğunu varsayma. Mevcut V3.2.0'a hemen geçmek için:

1. [Resmi V3.2.0 sayfasından](https://github.com/avenoxai/avenoxbeyin/releases/tag/v3.2.0)
   `beyin-v3-3.2.0.zip` ve yanındaki `.sha256` dosyasını indir. ZIP'i açmadan
   ve içindeki kodu çalıştırmadan önce SHA-256 değerini karşılaştır. macOS/Linux
   için `shasum -a 256 beyin-v3-3.2.0.zip`, Windows için
   `Get-FileHash beyin-v3-3.2.0.zip -Algorithm SHA256` kullan; sonuç resmi
   `.sha256` dosyasının ilk alanıyla aynı olmalı.
2. Ajan oturumlarını kapat. ZIP'i vault dışında ayrı bir klasöre aç. Mevcut
   vault'un `.beyin-runtime.json` dosyasındaki `state` yolunu oku; özel state
   kullandıysan varsayılan yeni bir state yaratma. Bu dosyayı değiştirme.
3. Açılan paket klasöründe, gerçek vault ve mevcut state yollarıyla planı incele:

   ```sh
   python3 scripts/install_v3.py --vault "/tam/yol/Beynim" --state "/mevcut/state" --plan
   ```

4. Plan başarılıysa aynı komutu `--plan` olmadan çalıştır. Installer paket
   hashlerini doğrular, mevcut kullanıcı düzenlemelerinde conflict ile durur
   ve güncelleme journal'ı/yedeği oluşturur. Conflict varsa dosyaları ezme,
   önce değişikliği incele. `.beyin-version` veya kurulu Python dosyalarını
   elle değiştirme.
5. Vault içinde `python3 beyin.py doctor` ile sonucu kontrol et. Hook
   tanımları değiştiyse Codex `/hooks` incelemesini tamamla ve yeni oturum aç.
   Sonraki güncellemeler yine normal `update` komutunu kullanır; gerekirse
   `rollback` önceki kurulumu geri getirir.

Windows'ta Python komutları için `py -3` kullan. Bu yol kurulu eski
paket doğrulayıcısını çağırmak yerine doğrulanmış resmi paketin installer'ını
kullanır; kullanıcı notları ve mevcut state dizini korunur.

## Neler değişir?

Paket yalnız yönetilen motor dosyaları, kurulu giriş/başlatıcılar, üç çekirdek skill ve istemci bağlantılarını günceller. Aynı adlı özel skill veya değiştirilmiş yönetilen script sessizce ezilmez. İlgisiz kullanıcı ayarları desteklenen birleştirme kurallarıyla korunur. Markdown notlar, Companion metinleri ve eski günlük/bilgi kaynakları paket içeriğiyle değiştirilmez.

V2 geçişinde hash ile tanınan stok writer'lar geri alınabilir, etkisiz girişlerle değiştirilir. Böylece önceden açılmış istemcide kalan eski komut da model derleyicisini yeniden başlatmaz. Özelleştirilmiş runner'lar ve proje dışındaki zamanlayıcılar ayrıca değerlendirilir. [Geçiş rehberi](MIGRATION.md).

## Uygulanan kontroller

Arşiv izin listesi, dosya SHA256, sürüm ve runtime şeması doğrulanır. Paket Python dosyaları derlenir; yeni kod ayrı sentetik vault'ta init, sync, kaynak arama ve receipt testinden geçer. Gerçek vault notları bu test için kullanılmaz. Uygulamada yerel kilit, önceki/yeni dosya içerikleri ve modlarıyla kalıcı journal, yönetilen dosya hash kontrolleri ve sürümden önce runtime veritabanı kontrolü vardır. Sürüm damgası en son yazılır.

Bu kontroller dağıtık cloud kilidi veya bütün harici uygulamalar için atomik transaction değildir. Başka bir editörün görülen değişiklikleri korunur; gerçek istemci davranışı ayrı doğrulanır. [Platform raporu](PLATFORM-TESTS.md).

## Bileşen hariç tutma ve güncellemeler

Kullanıcı gereksinim duymadığı bileşenleri (`skills/<ad>`, `adapters/<ad>`, `launchers`, `agents_block`, `harnesses/antigravity`) kurulumda `--exclude-component` ile veya çalışma zamanında `beyin.py preferences --exclude-component <ad>` ile hariç tutabilir (`--include-component` ile geri açabilir).

- `update` ve `install` sırasında hariç tutulan bileşenlere ait yönetilen ve değiştirilmemiş dosyalar diskten temizlenir ve manifestten düşürülür.
- Kullanıcı tarafından değiştirilmiş dosyalar asla silinmez, üzerine yazılmaz; korunur ve işlem özetinde bildirilir.
- Kullanıcı ortak dosyalardaki (`AGENTS.md` veya `.agents/hooks.json`) V3 bölgelerini elle kaldırmışsa çakışma üretilmez.
- Rollback yapıldığında önceki durum geri yüklenir; `.beyin-exclusions.json` dosyasındaki hariç tutma tercihleri korunur.

## Geliştiriciler için paket üretimi

Repo kökünde:

```sh
python3 scripts/build_v3_release.py --output "/tmp/beyin-v3-3.4.0.zip" --version 3.4.0
```

Bu komut yalnız yerel ZIP oluşturur, GitHub'a yayınlamaz. Paket `manifest.json`, izin verilen installer/giriş dosyaları, runtime modülleri ve üç skill'i içerir. Manifest sürüm, schema/runtime schema, minimum Python, dosya hashleri ve tanınan legacy hashlerini taşır. Release yayınlama ve final platform CI ayrı işlemlerdir.

## V3.1 sürüm bildirimleri

V3.0.2 kurulumunu bir kere `python3 beyin.py update` ile güncelle. Yeni bildirim kodu bu ilk geçişten sonra çalışır. Windows'ta `py -3` kullan. Oturum açılışı ağ beklemez; kısa ömürlü worker yalnız resmi GitHub metadata'sını günde en fazla bir kez kontrol eder. İlk kontrolün sonucu sonraki oturum veya `doctor` çağrısında görünür. Aynı sürüm her prompt'ta tekrar gösterilmez. Notlar ve prompt'lar bu kontrol için gönderilmez, model çağrısı veya otomatik kurulum yapılmaz.

`update --check --metadata-only` yalnız sürüm bilgisini okur: ZIP indirmez, paket kodu çalıştırmaz ve vault/runtime/cache yazmaz. Tam `update --check` mevcut paket doğrulamasını ve geçici sentetik kurulumu çalıştırmaya devam eder. Metadata sonucu paketin kurulabilirlik kanıtı değildir. Ağ hatası “güncelsin” anlamına gelmez; doctor son başarılı kontrol tarihini ve durumu gösterir.

`preferences --update-notifications off/on` bildirimleri ve otomatik sürüm ağı erişimini yönetir; hafıza senkronizasyon tercihini değiştirmez. `BEYIN_UPDATES_OFF=1` bu tercihten önce gelir. `update --dismiss X.Y.Z` yalnız o sürümün oturum bildirimini susturur; doctor sürümü göstermeye devam eder. Tercih ve cache vault dışında state dizinindedir; eski preference şemasına alan eklenmediğinden eski sürüme rollback güvenlidir. İnternet erişimi kapalıyken `update --package /yol/paket.zip` kullanılabilir.

Online ZIP indirmesinde GitHub asset SHA-256 ve varsa resmi checksum dosyası, herhangi bir paket kodu çalıştırılmadan önce doğrulanır. Cache kurulacak paketin kaynağı değildir; kurulum yeniden resmi metadata okur. Checksum bağımsız yayımlayıcı imzası değildir. `context --no-sync` sürüm kontrolü, worker veya bildirim kaydı oluşturmaz.

## Bakımcı için yayın

`VERSION` ve `docs/v3/releases/X.Y.Z.md` aynı sürümü tanımlar. `Verified V3 release package` workflow'u ZIP'i bir kez build eder; altı OS/Python kombinasyonu bu aynı ZIP'i temiz kurulum, yayınlanmış gerçek V3.0.2 ve V3.1.0 paketlerinden ve `cad7953` ara kurulumundan geçiş, no-op, rollback ve kesinti/recover ile doğrular. PR çalışmaları yayın yapmaz.

Main üzerinde manuel `workflow_dispatch` ve `publish=true`, testler yeşilse aynı bytes'ı önce draft olarak yükler, sonra stable/latest yayınlar ve yayınlanmış asset'i tekrar indirip doğrular. Var olan release'in üzerine yazılmaz; yeni sürüm numarası gerekir. Opsiyonel global köprü (#41) V3.2.0 ile pakete girdi; kurulum ve güncelleme global ayarları değiştirmez ([GLOBAL-BRIDGE.md](GLOBAL-BRIDGE.md)).
