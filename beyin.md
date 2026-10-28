# Avenox Ikinci Beyin V3: ajan kurulum rehberi

Bu dosya, kullanicinin su istegini yerine getirmek icin kanonik kurulum tarifidir:

> Bu klasore ikinci beynimi kur. Mevcut kurulum varsa notlarimi koruyarak guncelle.
> Gerekli indirme ve kurulum adimlarini sen yap. Sonunda ornek bir bilgiyi kaydedip
> yeni oturumda geri okuyarak birlikte dogrulayalim.

Kullanici ZIP indirmek, arsiv acmak veya kurulum komutu aramak zorunda degildir. Bunlari
ajan yapar. Yalnizca istemcinin gosterdigi normal klasor, workspace veya hook guven
incelemesi kullaniciya aittir.

## Degismez kurallar

1. Kurulum hedefi, kullanicinin bu istegi verdigi mevcut vault/workspace klasorudur.
2. Kullanici notlarini silme, tasima, yeniden adlandirma veya `git reset` ile ezme.
3. Var olan V1, V2 veya V3 kurulumunu once tespit et. Resmi installer ve resmi updater
   yedekleme, cakisma ve geri alma kurallarini uygular; bu kontrolleri elle atlama.
4. Yalniz `avenoxai/avenoxbeyin` deposunun en son kararli GitHub release paketini kullan.
   Source code arsivlerini veya ucuncu taraf paketleri kullanma.
5. Ham notlari, kimlik bilgilerini, ozel yollari veya gizli degerleri terminal ciktisina,
   sohbete ya da dis servislere dokme.
6. Kurulum tamamlanmadan basarili deme. Installer ya da updater cikis kodunu, surum
   damgasini ve `beyin.py doctor` sonucunu kontrol et. Installer ciktisindaki `companion`
   ve `skills.conflicts` alanlarini da oku; `needs_attention` gorursen ilgili adimi
   kullaniciya sormadan tamamlama.

## 1. Hedefi ve Python'u bul

- Vault yolu mevcut workspace kokudur. Mutlak ve kanonik yolu kullan.
- Python 3.11 veya daha yenisini ara:
  - macOS/Linux: once `python3`, sonra `python`
  - Windows: once `py -3`, sonra `python`
- Bulunan yorumlayicinin surumunu gercekten calistirip kontrol et.
- Uygun Python yoksa, kullanicinin bu kurulum istegini yetki kabul ederek isletim
  sisteminin resmi paket yoneticisiyle kararli Python 3 surumunu kur. Yonetici izni veya
  isletim sistemi onayi gerekiyorsa yalniz o noktada kisa ve acik bicimde kullanicidan
  onay iste. Rastgele indirme siteleri kullanma.

Obsidian notlari acmak icindir; kurulum sirasinda Obsidian eklentisi gerekmez. Git, pip,
Node, Mem0, API anahtari veya surekli calisan sunucu gerekmez.

## 2. Resmi paketi gecici alana indir ve dogrula

Once son kararli surumun etiketini ogren. Birinci yol GitHub API:

`https://api.github.com/repos/avenoxai/avenoxbeyin/releases/latest`

Cevaptaki `tag_name` alanini kullan; `draft` ve `prerelease` false olmalidir. API
kimliksiz isteklerde IP basina saatlik sinir uygular. HTTP 403 veya 429 donerse API'yi
zorlama ve surumu tahmin etme; ikinci yol olarak su adresin yonlendirmesini oku:

`https://github.com/avenoxai/avenoxbeyin/releases/latest`

Bu adres `https://github.com/avenoxai/avenoxbeyin/releases/tag/vX.Y.Z` adresine
yonlenir; etiket son yol parcasidir.

Release kararli bir `vMAJOR.MINOR.PATCH` etiketi tasimalidir. Etiket `vX.Y.Z` ise gerekli
varliklar sunlardir:

- `beyin-v3-X.Y.Z.zip`
- `beyin-v3-X.Y.Z.zip.sha256`

Surumu sabitleme ve bu satirlardaki `X.Y.Z` yer tutucusunu gercek surum sanma: isimleri
gercek etiketten uret. Indirme adresleri API cevabindaki `browser_download_url`
alanlaridir; yonlendirme yolunu kullandiysan ayni adresler su kaliptadir:

`https://github.com/avenoxai/avenoxbeyin/releases/download/vX.Y.Z/beyin-v3-X.Y.Z.zip`
`https://github.com/avenoxai/avenoxbeyin/releases/download/vX.Y.Z/beyin-v3-X.Y.Z.zip.sha256`

Ikisini HTTPS ile isletim sisteminin gecici klasorune indir. SHA-256 dosyasindaki ilk
alani ZIP'in yerel SHA-256 degeriyle karsilastir. Eslesme yoksa dur, arsivi acma ve
sorunu bildir.

ZIP'i gecici bir klasore acarken her uyenin hedefinin bu gecici klasorun icinde kaldigini
kontrol et. Mutlak yol, `..` ile kacis veya sembolik bag iceren arsivi reddet. Paket
kokunde `manifest.json` ve `scripts/install_v3.py` bulunmalidir.

## 3. Kurulumu veya guncellemeyi uygula

Once vault kokunde `.beyin-version` dosyasina bak ve yola gore ayril.

### 3a. `.beyin-version` yoksa, V1 veya V2 varsa: resmi installer

Buldugun ayni Python yorumlayicisiyla:

```text
<python> <gecici-paket>/scripts/install_v3.py --vault <mutlak-vault-yolu>
```

Eski V1/V2 kurulumunda once ayni komutu sonuna `--plan` ekleyerek calistir; bu hicbir sey
degistirmez, nelerin yazilacagini ve emekli edilecegini gosterir. Kullanici eski bir
runner'i elle degistirmisse installer `Customized legacy runner requires review <yol>`
mesajiyla 1 cikis kodu dondurur. O dosyayi silme veya ezme; icerigini kisaca ozetle,
kullanicinin acik onayini al ve ancak o zaman tek dosya icin
`--accept-customized-legacy <vault-goreli-yol>` kullan. Toplu gecis yoktur. Ayrintilar:
`https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/MIGRATION.md`

### 3b. `.beyin-version` zaten `3.` ile basliyorsa: kurulu updater

Mevcut V3 kurulumunda tercih edilen yol vault icindeki `beyin.py` updater'idir.
Vault kokunde, indirip dogruladigin ZIP ile calistir:

```text
<python> beyin.py update --check --package <indirdigin-zip>
<python> beyin.py update --package <indirdigin-zip>
```

Sonuc `updated` ise surum degismistir, `noop` ise vault zaten gunceldir; ikisi de
basaridir. Bu yol gereksiz yere yeni bir istemci guven incelemesi zorlamaz ve gerekirse
`<python> beyin.py rollback` ile tam geri alinir. Ag erisimi varsa `--package` olmadan da
calisir; ag hatasi (ornegin `http_403`) "guncel" anlamina gelmez. Cok eski bir V3
paketinde `update` komutu taninmiyorsa 3a yoluna dus. `beyin.py --help` ciktisi `update`,
`rollback` ve `recover` komutlarini listelemez; bunlar giris scriptinde ayrica ele alinir
ve calisir. Komutu `--help` listesinde gormedin diye 3a'ya dusme.

### Her iki yol icin gecerli

Vault'ta `.beyin-runtime.json` varsa installer ve updater onun dis yerel state konumunu
korur. Yeni kurulumda state macOS, Linux veya Windows icin uygun yerel uygulama verisi
dizinine, vault disina yazilir. State'i iCloud, OneDrive, Dropbox veya vault icine tasima.

Yonetilen bir dosya elle degistirilmisse her iki yol da
`Reinstall conflict: managed file changed <yol>` hatasi verip 1 cikis kodu dondurur.
Kullanici dosyasini ezme. Cakisan dosyayi ve neden otomatik devam edilemedigini kisa
bicimde bildir. Yarim kalan resmi V3 isleminde once mevcut vault'taki `beyin.py recover`
yolunu kullan; basarisiz olmadan yeniden kopyalama yapma. Kendi curl, kopyalama veya
`git pull` zincirinle kurulu bir vault'u guncelleme.

## 4. Kurulumu dogrula

Vault kokunde su kontrolleri yap:

```text
<python> beyin.py doctor --human
<python> beyin.py preferences --human
```

Ek olarak `.beyin-version`, `.agents/skills/beyin/SKILL.md`,
`.agents/skills/beyin-doktor/SKILL.md` ve `.agents/skills/beyin-guncelle/SKILL.md`
dosyalarinin varligini kontrol et. Kurulumdan sonra gecici ZIP ve acma klasorunu sil;
kullanici notlarina dokunma. Kurulum vault kokundeki `AGENTS.md` dosyasina
`<!-- beyin-v3:start -->` isaretli yonetilen bir bolum ekler; mevcut icerik korunur.
`CLAUDE.md` yoksa yalniz `@AGENTS.md` import satiriyla olusturulur. `AGENTS.md`'yi zaten
import eden `CLAUDE.md`'ye bolum ikinci kez eklenmez; import etmeyen `CLAUDE.md`'ye ayni
bolum eklenir. Kullaniciya verdigin ozette neyin eklendigini yaz.
Kiro destegi iceren bir paket kullaniyorsan `.kiro/hooks/beyin-v3.json` ve
`.kiro/skills/beyin/SKILL.md` dosyalarini da kontrol et; eski paketlerde bu
dosyalar bulunmayabilir. DSH kullaniyorsan `.claude/dsh/beyin-v3-hooks.json`
dosyasini ve profil yamasindaki iki isaretli blogu kontrol et; DSH'yi yeniden
baslatmadan profil degisikligi etkin olmaz.

Kurulum vault kokune yalniz ilgili platformun guncelleme kisayolunu birakir: macOS'ta
`Beyni Güncelle.command`, Windows'ta `Beyni Guncelle.cmd`, Linux'ta `Beyni Güncelle.sh`
ve onu cagiran `Beyni Güncelle.desktop`. Hepsi kurulu `beyin.py update` komutunu
cagirir, ayri bir guncelleme mantigi icermez. Kullanici ileride ajana "beynimi guncelle"
diyerek ya da bu kisayolla guncelleyebilir.

Codex'te `/hooks` incelemesini, Claude Code ve Antigravity'de normal workspace/klasor
guvenini kullaniciya goster. Guven hash'i uydurma veya onayi atlatma. OpenCode icin ek
guven adimi yoktur; vault klasorunde OpenCode acilinca eklenti yuklenir. OMP icin de ek
guven adimi yoktur; vault klasorunde OMP acilinca `.omp/hooks/pre/beyin-v3.ts` hook'u
yuklenir. Hermes Agent kullaniliyorsa installer eklenti dosyalarini vault icine yazar,
ancak baglanti icin `hermes plugins enable beyin-v3` ve yeniden baslatma adimlari
kullaniciya aittir; adimlar
`https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/HERMES.md` icindedir.
Kiro IDE 1.x veya CLI V3 kullaniliyorsa vault'u ac, Agent Hooks panelinde veya
CLI `/hooks` ekraninda Beyin tanimlarini incele ve yeni oturum baslat.
DSH kullaniliyorsa `beyin.py dsh-profile apply --profile "$HOME/.dsh/profiles/desktop"`
ile profil bloklarini ekle (veya kurulumda `--dsh-profile` ver), DSH'yi yeniden
baslat ve yeni oturum ac. Profilde yonetilmeyen bir sandbox-policy ya da
hooks-claude-code girdisi varsa kurucu ona dokunmaz; `configPath` ve
`workspaceRoot` degerlerini kullanici kendisi duzeltir.
Ardindan yeni bir istemci oturumu acilmasini iste. Codex Desktop otomatik hook baglami
gorunmezse bu bir kurulum basarisi iddiasi degildir; kurulu `beyin.py context` ve `sync`
komutlariyla dogrudan tazeleme kullanilabilir.

## 5. Kullanici ile tanis ve gercek geri okuma yap

Kurulumdan sonra `.agents/skills/beyin/SKILL.md` dosyasini oku. Kullaniciya en fazla uc
kisa tanisma sorusu sor. Cevaplarini kaynak Markdown notuna kaydet: kimlik ve hitap
bilgisi companion klasorundeki `Core.md` dosyasina yazilir. Bu klasoru yalniz resmi
installer acar. 3b yolunda (eski bir V3'ten guncelleme) klasor hemen olusmaz; installer
ciktisinda `companion` alani `needs_attention` dondugunde de olusmaz. Iki durumda da
yeni bir kimlik klasoru acma: vault'ta mevcut `Core.md` veya `Soul.md` ara, birden fazla
aday varsa hangisinin kullanilacagini kullaniciya sor. Yeni semantik notlar ise yalniz
`notes/` veya `knowledge/` altina `<python> beyin.py note-create --file NOTE_JSON` ile
olusturulur; klasor yoksa olusur. Sonra `<python> beyin.py sync` ile esitle ve ayni
bilgiyi ayri bir komutta `<python> beyin.py context "<aradigin konu>"` ile geri okuyup
donen `source` yolunu kullaniciya soyle. Dosyayi dogrudan acip okumak geri okuma yerine
gecmez. Uydurma profil bilgisi yazma.

Son olarak kullanicidan yeni bir oturum acip kaydedilen ornek bilgiyi sormasini iste.
Bu yeni oturum geri okumasi yapilmadan "oturumlar arasi hafiza dogrulandi" deme.

Varsayilan tuketim profili Normal'dir. Kullanici isterse beyin skill'i uzerinden
"ekonomik moda gec", "kontrol araligini 30 dakika yap" veya "otomatik kontrolleri
kapat" diyebilir. Yerel kontroller model cagirmaz ve kapali uygulamayi uyandiran bir
zamanlayici kurulmaz.

## Opsiyonel Jev danismani

Jev kurulumun parcasi degildir ve varsayilan kapalidir. Kurulumdan sonra
`<python> beyin.py jev status --human` ciktisi "kapali" ve "Anahtar: yok" gostermelidir;
baska bir sey yapma. Kullanici acikca istemedikce acma, API anahtari isteme veya konuyu
gundeme getirme.

Kullanici isterse kurulu `.agents/skills/beyin/SKILL.md` icindeki Jev bolumunu ve
`https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/JEV.md` adimlarini uygula
(kurulu pakette `docs/` klasoru yoktur). Anahtar `TYPESAFE_API_KEY` ortam degiskeninde
durur; Markdown'a, surum kontrolune veya komut argumanina yazilmaz. `jev` komutu anahtar
kabul etmez ve yazdirmaz. `env_file` kullanilacaksa mutlak yol gerekir. Kural 5 geregi
anahtari sohbetten isteme ve sohbete yazdirma; kullanici onu kendi kabuk ortamina
kendisi koyar. Once `jev shadow` ile olc, sonra `jev on` yap. `jev on` iken ajanin
`context ... --jev`, `jev-answer` ve `jev-review` cagrilari da saglayiciya veri gonderir:
`context --jev` sorgu ile izin verilen her adayin basligini ve metninden bir kesiti
yollar. `auto_context` ayri ve ek
bir opt-in'dir: `jev on --enable auto_context` acildiginda her turda istem metni ve en
fazla 8 aday notun basligi ile ilk 600 karakteri saglayiciya gider. `private` notlar
hicbir yolda gonderilmez; V3.2.0 ve sonrasinda `remote_allowed: false` ve
`sensitivity: sensitive` kayitlar da gonderilmez. Bunlari acmadan once kullaniciya soyle. Mod `off` degilken anahtar yoksa komut
uyarir ve cagrilar yerel sonuca duser. Kapatma: `<python> beyin.py jev off`; acil durumda
`BEYIN_JEV_DISABLE=1` ortam degiskeni kayitli modu degistirmeden cagrilari durdurur.

Anahtar istemeyen yerel secenek: kullanici acikca isterse ayri kurulmus `laya-serve`
(`LAYA_HOST=127.0.0.1 LAYA_PORT=8765`) icin `<python> beyin.py jev shadow --provider laya`,
sonra `jev status --check`. Laya'yi, PyTorch'u veya model agirligini Beyin'in ortamina
kurma. Laya yalniz golge modda calisir: olcumde Jev'den cok daha zayif ciktigi icin
puanlari yalniz olcum icin kaydedilir, hicbir sonucu degistirmez; `jev on --provider laya`
`laya_shadow_only` ile reddedilir ve `jev status` modu `golge (yalniz)` gosterir. Ayni gonderim
kurallari gecerlidir; `private`, `remote_allowed: false` ve `sensitivity: sensitive` kayitlar
Laya'ya da gitmez. Jev'e donus: `jev on --provider typesafe`.

## Manuel kacis yolu

Ajanin ag veya dosya yetkisi gercekten yetersizse, kullaniciyi su sayfaya yonlendir:

`https://avenox.lol/ikincibeyin`

Sayfadaki V3 ZIP dugmesi GitHub'daki en son kararli resmi release paketine ve SHA-256
dosyasina goturur; ayni paketler dogrudan
`https://github.com/avenoxai/avenoxbeyin/releases/latest` sayfasinda da durur. Manuel
kurulumdan sonra vault kokunde `<python> beyin.py update --check` ile surumun guncel
oldugunu dogrula. Bu yol otomatik kurulumun yerine gecen son care yoludur; ilk tercih
degildir.
