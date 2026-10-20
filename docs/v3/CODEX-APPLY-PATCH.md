# Yakala: Codex `apply_patch` okuma sınırı ve Windows uyumluluğu

Araştırma ve Linux ölçümü: **19 Ekim 2026 UTC**. İlgili upstream PR: [#384](https://github.com/avenoxai/avenoxbeyin/pull/384).

## Sonuç ve uygulanan değişiklik

`apply_patch` aracının yazmasını engellemek, mevcut dosya içeriği hakkında tahmin
doğrulamasını tek başına engellemiyor. `-a never -s read-only` altında doğru yama
bağlamı yazma politikasına kadar ilerlerken yanlış bağlam doğrulamada hata veriyor.
Bu ayrım güncel kararlı Codex **0.163.0** ile de tekrarlandı.

Linux'ta ölçülen çözüm, Codex'in belgelenmiş **adlı izin profili** ile yama
doğrulamasının kullandığı dosya sistemi okumasını da kısıtlıyor. Yakala Linux ve
macOS'ta her komutta yeni bir `beyin_yakala_<32 hex karakter>` adı üretip şu iki
`-c` ayarını veriyor; macOS native doğrulaması henüz yapılmadı:

```text
permissions.beyin_yakala_<rastgele>.filesystem={":root"="deny"}
default_permissions="beyin_yakala_<rastgele>"
```

Profilin başka dosya sistemi girdisi veya `extends` atası yok. `:minimal`, Python
dizini, vault ve geçici dizin için ek okuma izni eklenmiyor. İstemciye `-s read-only`
verilmiyor; bu eski seçici adlı profilin yerine geçer. Kullanıcının mevcut
config dosyası değişmiyor. Rastgele ad, aynı adlı kullanıcı profilinden daha dar
bir `read`/`write` girdisinin derin birleştirme yoluyla miras kalmasını önlüyor.
128 bit rastgeleliğin çakışmayı pratikte ihmal edilebilir kılması bir olasılık
değerlendirmesidir; bütün sistem durumlarını tarayarak ispatlanmış benzersizlik değildir.

POSIX ön kontrolü `allowed_permission_profiles`, `allowed_sandbox_modes` veya
`remote_sandbox_config` varsa işi başlatmadan reddeder. Codex 0.163.0, izin
listesinde olmayan rastgele profil yerine yönetilen varsayılana sessizce
dönebildiği için `--strict-config` yeterli değildir [S7]. `CODEX_HOME`
(varsayılan `~/.codex`), `/etc/codex`, proje/üst dizin config'leri ve profil
parçaları taranır; yönetilen dosyalardaki izin seçicileri de reddedilir.
Okunamayan/bozuk config, dolu bulut yönetim önbelleği veya macOS yönetim
tercihi varsa güvenli profil varsayılmaz. İki görünümde de kart ASCII Türkçe
açıklamayla `onay-bekliyor` olur; Windows `-s read-only` yolu değişmez.

**Windows ayrı bir uyumluluk yolu kullanır: `-s read-only`.** Kullanıcının
19.10.2026 tarihli gerçek Windows 11 / Codex 0.155.1 denemesinde önceki düzeltme
(`dc2a1bc2`, fork #9) `AGENTS.md` yüklenirken durdu; model olay dosyası boş kaldı.
Windows `elevated` sandbox kök okuma izni istiyor. Bu nedenle Windows komutuna
root-deny profili verilmez; `gizli` ve `pencere` aynı salt okuma seçicisini
kullanır. Yazma engeli ve diğer araç kapıları korunur, fakat **Windows'ta
`apply_patch` içerik tahmini kanalı kapanmış sayılmaz**. Yeni dalın native Windows
başlangıcı henüz ölçülmedi; aşağıdaki uyumluluk bölümü bu ayrımı açıklar [S12-S14].

`--strict-config` bilinmeyen üst düzey ayarları hata yapar. Desteklenmeyen bayraklar
ve eski/ön sürümler zaten ön kontrolde reddedilir. En eski ikili ölçümü **0.155.1**
olduğu için sürüm alt sınırı değişmedi. Genel kabuk, görüntü okuma, alt ajan, app,
eklenti ve diğer MCP kapıları önceki iş bazındaki kapatma kontrollerini korur.

Bu profil **beta** olarak belgelenmiştir. Bir ayarın kabul edilmesi, gelecekteki
her sürümde aynı güvenlik davranışını garanti etmez. Bu nedenle gerçek ikiliyle
tekrarlanabilen, isteğe bağlı regresyon da eklendi. Native Windows ve macOS için
aşağıdaki ölçümler tamamlanmadan Linux sonucu o platformlara genellenmemelidir.

## Neden bu çözüm?

0.163.0 kaynak kodunda `ApplyPatchHandler::handle_call`, seçili ortamın
`FileSystemSandboxContext` değerini `verify_apply_patch_args` çağrısına geçiriyor.
`Update`/`Move` işlemlerinde bağlam eşleştirme ve `Delete` işleminde eski içerik
okuma bu dosya sistemi üzerinden gerçekleşiyor. `Add` için mevcut içerik
okunmadan yazma kararı veriliyor. Bu yüzden okuma ve yazmayı birlikte reddeden
profil, tahmin sonucuna göre farklılaşan içerik kontrolünden önce uygulanıyor.
Kaynak yolu aşağıdaki [S4] ve [S5] bağlantılarında.

İzin derleyicisi özel profili boş bir kısıtlı dosya sistemi politikasıyla
başlatıyor; kendiliğinden vault veya `:minimal` okuması eklemiyor [S6].
Daha özgül bir izin geniş kök reddini açabileceğinden, salt kök reddini
mevcut sabit adlı profile eklemek yeterli değildir [S2].

Resmî izin sayfası eski sandbox ayarlarıyla yeni profilleri karıştırmama uyarısı
verir [S2]. İncelenen üç sürümde kaynak ve gerçek ikili ölçümü daha özel bir
öncelik kuralını da doğruladı: `SessionFlags` içindeki açık
`default_permissions`, yüklenmiş dosyadaki eski `sandbox_mode` ayarına üstün
geliyor; gerçek `--sandbox`/`-s` komut satırı seçicisi ise profili devre dışı
bırakıyor [S7]. Regresyonun geçici config dosyasında kasıtlı olarak
`sandbox_mode="workspace-write"` bulunuyor. İkinci test ayrıca desteklenen
`--profile fixture_legacy` seçicisiyle `fixture_legacy.config.toml` içindeki
`sandbox_mode="read-only"` değerini yükletiyor. İkisinde de bütün native dosya
okuma/yazma denemeleri reddediliyor. Yakala üretim komutu bu ek `--profile`
bayrağını kullanmaz; ikinci test Codex'in katman önceliğini zorlar. Bu ayrıntı
yalnız incelenen sürümler için kaynak ve çalıştırma kanıtıyla söylenmektedir.

### İncelenen alternatifler

| Yol | Kanıt ve karar |
| --- | --- |
| `--disable apply_patch_freeform` | 0.155.1 ve 0.163.0 özellik listesi/kaynağında `removed`. 0.163.0 ikilisinde bayrağa rağmen aynı içerik tahmini ayrımı oluştu. Kullanılmadı. |
| Salt `read-only` ve `never` | Yazmalar reddedildi; doğru/yanlış bağlam hataları farklı kaldı. Bu bulgunun nedeni. |
| `PreToolUse` içinde `apply_patch` reddi | Desteklenen hook, handler doğrulamasından önce çalışabilir. Ancak sıradan hook hata/timeout/bozuk çıktısında çağrı devam edebilir; tek güvenlik sınırı olarak seçilmedi [S8, S9]. |
| `model_catalog_json` ile `apply_patch_tool_type=null` | Resmî 0.163.0 ikilisinin geçici katalog denemesinde aracı kaldırdı; zorlanmış çağrı `unsupported custom tool call` oldu. Bu, model kataloğunu bütünüyle yönetmeyi gerektiren daha geniş bir müdahale. Model özelliklerini değiştirmeyen dosya sistemi profili seçildi [S3, S10]. |
| Model prompt'unda araç yasağı | Araç seviyesindeki sınırı sağlamaz. Güvenlik çözümü sayılmadı. |

Bağımsız, sadece `apply_patch` aracını kapatan güncel bir CLI anahtarı bulunmadı;
bu sonuç incelenen config şeması, CLI yardım metni, özellik listesi ve kayıt
koduyla sınırlıdır. POSIX profili Linux'ta kanalı kapatır; Windows salt okuma
uyumluluk yolunda bu bulgu açık kalır. Codex deposunda issue açılmadı.

## Gerçek ikiliyle test

`tests/v3_yakala_codex_boundary_test.py` Python stdlib kullanır. Codex projenin
bağımlılığı değildir; `BEYIN_CODEX_BINARY` verilmedikçe normal CI testi atlar.
İkili otomatik indirilmez. Bilinen resmî ikili açıkça seçilir:

```bash
BEYIN_CODEX_BINARY=/absolute/path/to/codex \
BEYIN_CODEX_EVIDENCE=/absolute/path/to/codex-boundary.json \
python3 -m unittest tests.v3_yakala_codex_boundary_test -v
```

Bu iki native regresyon POSIX root-deny sözleşmesini sınar. Windows'ta açık bir
platform gerekçesiyle atlanır; atlama, Windows başlangıcı veya okuma izolasyonu
başarısı olarak sayılamaz. Windows kabul denemeleri aşağıda ayrıca listelenir.

Test yeni geçici `CODEX_HOME`, vault, state ve zararsız notlar oluşturur. Yalnız
`127.0.0.1` üzerinde Responses protokolü taklit edilir; model hesabı, API anahtarı
ve dış model servisi kullanılmaz. Çıkışlar gerçek Codex ikilisinden gelir;
sahte sunucu yalnız modelin araç çağrısını belirler. Girdi olarak seçilen
`gpt-5.5` adı, bu sürümlerin doğrudan custom-tool taşımasını sınamak içindir;
barındırılan bir modelin yetenek, fiyat veya kalitesi ölçülmez.

Her sürümde iki uçtan uca regresyonun her birinde şunlar doğrulandı:

- **28 yama çağrısı:** dört yol yazımı (göreli/mutlak, vault içi/dışı) için
  doğru/yanlış bağlamlı Update ve Move, Delete, mevcut ve yeni dosyaya Add.
  Aynı yol/işlemdeki doğru/yanlış çiftleri birebir aynı çıktı verdi. Update/Move
  ve Delete, dosya okuma hatasıyla; Add, yazma politikasıyla reddedildi.
  İşlem türleri arasında tek bir hata metni kullanıldığı iddia edilmez.
- Dört başlangıç dosyasının SHA-256 değeri korundu. Yeni ya da taşınmış hedef
  oluşmadı; hiçbir yama çıktısı sentetik doğru/yanlış içerik satırını taşımadı.
- **5 gerçek MCP çağrısı:** normal not ve `AGENTS.md` okundu; private not
  okunmadı; yeni sonuç notu yazıldı; `complete` kabul edilip manifest oluştu.
  Yakala'nın işi/PID neslini doğrulayan gerçek stdio MCP süreci çalıştı.
- **Normal hafıza başlangıcı:** testin ürettiği tek sentetik `SessionStart`
  hook'u, `hooks/list` ile görülen kendi hash'i için güven kaydı alır.
  Güven atlama bayrağı verilmez. Hook dosyadan okuduğu bağlamı ilk model
  isteğine ekler; native yama okumaları aynı oturumda yine reddedilir.
- Bilinmeyen üst düzey config anahtarı `--strict-config` altında model
  isteği oluşmadan reddedilir. Çalıştırma sonrasında config hash'i aynıdır.

`--strict-config` için ölçüm kapsamı üst düzey bilinmeyen anahtardır. Araştırmada
`permissions.<başka profil>.unknown_field` gibi iç içe bilinmeyen bir alanın
bu üç sürümde her zaman reddedilmediği de görüldü. Bu bayrak tek başına genel
bir profil doğrulama kanıtı olarak sunulmaz. Kök reddinin etkinliği doğrudan
yama çağrılarıyla sınandı; özel profilde eksik/boş filesystem tablosu da
kısıtlı başlar [S2, S6].

| İkili | Resmî yayın zamanı (UTC) | Linux sonucu |
| --- | --- | --- |
| 0.155.1 | 18.09.2026 20:03:04 | 2/2 gerçek CLI regresyonu geçti; test başına 28 patch + 5 MCP + hook |
| 0.160.0 | 01.10.2026 20:19:13 | 2/2 gerçek CLI regresyonu geçti; test başına 28 patch + 5 MCP + hook |
| 0.163.0 | 15.10.2026 21:30:38 | 2/2 gerçek CLI regresyonu geçti; test başına 28 patch + 5 MCP + hook |
| 0.164.0-alpha.3 | 17.10.2026 04:03:25 | Yalnız kaynak karşılaştırması; gerçek ikili çalıştırılmadı, Yakala ön sürümü reddeder |

Windows uyumluluk değişikliği sonrasında, **19.10.2026 UTC** tarihinde aynı
altı Linux CLI testi `nobody` / UID 65534 ile yeniden geçti: sürüm başına iki
test, sırasıyla **25.313**, **19.736** ve **19.640 saniye**. Toplam 168 patch,
30 gerçek MCP çağrısı ve altı hafıza hook'u denetlendi; config hash'leri korundu.

Güncellenen komut beklentileri ve mevcut Codex ön kontrolleri için **20 hedefli
test, 20 başarı / 0.086 saniye** ölçüldü. Bu koşuya dahil platform/görünüm
matrisi eski kodda iki Windows alt kontrolünde başarısızdı (**1 test / 2 fail,
0.020 saniye**); düzeltmeden sonra **1 test / 1 başarı, 0.025 saniye** oldu.
Matris de Linux'ta UID 65534 ile çalıştı. `subTest` döngülerindeki her kontrol
ayrı test sayılmaz; bu sayılar tam V3 birleşim testinin yerini almaz.

### İkili bütünlüğü

Resmî `codex-x86_64-unknown-linux-musl.tar.gz` varlıkları indirildi; aşağıdaki
SHA-256 değerleri GitHub release API'sindeki `digest` alanıyla eşleşti.

```text
0.155.1 a0ef8b2debc3bf747e07b1a039354de31300ac0dcc2276498ba281470b5d9115
0.160.0 306865417d4ee7a927785852910a527f41e1e159add390ac5ae3accb67d44a13
0.163.0 beb339e8ecd9ba057a6bb2dc81ad8ca474812edfbf0fc5056ca818797648dde4
```

## Windows uyumluluğu ve kalan native doğrulama

19.10.2026 UTC kaynak incelemesinde aynı `elevated` ön koşulu Codex **0.155.1**,
**0.163.0**, **0.164.0-alpha.3** ve `main` **89461ea6** içinde bulundu:
``elevated Windows sandbox requires effective `:root` read access`` [S13, S15].
Kullanıcının bildirdiği başlangıç hatası bu koşulla örtüşür; Linux'ta Windows
sandbox çalıştırılmış değildir. Kullanıcı aynı makinede eski `69ac79c` dalının
`-s read-only` ile iki görünümde de `completed` olduğunu bildirdi. Bu, önceki
dalın kullanıcı ölçümüdür; düzeltilmiş dalın kabul ölçümü değildir.

| Seçenek | Kaynak ve karar |
| --- | --- |
| Windows'ta root-deny | `elevated` kök okuma izni ön koşulunu sağlamaz; bildirilen gerçek başlangıç hatasının nedeni. Windows komutundan kaldırıldı. |
| `:root=read` ve vault/home/state için exact-path deny | `elevated` kaynak testi bu tür alt yol reddini kabul eder. `unelevated` deny-read desteklemez. Kısıtlı profil AGENTS keşfi/okumasını sandbox içinden yapar; izin/metadata hatası başlangıcı durdurabilir. Gerekli başlangıç/runtime yollarını açmadan çalışacağı native ölçülmedi; bu yolları açmak ve listelenmeyen yollar da genel oracle kapanışı sağlamaz. Varsayılan seçilmedi [S12-S14]. |
| MXC | Güncel belge uyumlu Windows 11 cihazı/politikası ister; tek komutlu MXC denemesi 0.162.0+ içindir. `prefer_mxc` destek yoksa legacy seçimine dönebilir. 0.155.1 desteğinin yerine geçirilmedi; MXC altında root-deny başarısı ölçülmedi [S12]. |
| Windows `-s read-only` | Önceki çalışan Windows akışıyla uyum için seçildi. Native yazma izni verilmez; içerik tahmini sınırı açık kalır. |
| Linux/macOS root-deny | Kod yolu korunur. Gerçek ikili kanıtı Linux'a aittir; macOS Seatbelt ölçümü değildir. |

Platform seçimi komut hazırlanırken yapılır; başlangıç hatasından sonra izin
genişletip otomatik yeniden deneme yoktur. Kullanıcının `windows.sandbox`,
`prefer_mxc`, proje/hook güveni ve config dosyaları değiştirilmez. Genel kabuk,
görüntü okuma, alt ajan, app/eklenti ve diğer MCP kapatma kontrolleri, `-a never`,
`--strict-config`, işin ağ seçimi ve güvenilen Yakala MCP köprüsü korunur.

Kalan kabul ölçümleri:

1. **Windows:** güncel dalı kurup gerçek Codex 0.155.1 ile önce `gizli`, sonra
   `pencere`, `ag: kisitli` ve zararsız bir kart kullan. `AGENTS.md` başlangıcının,
   normal güvenilmiş hafıza hook'larının ve
   `read_note → write_note → complete → receipt → Last-Session` zincirinin
   tamamlandığını doğrula. İki görünüm için ayrı job kimliği ve gerçek sonucu
   kaydet; bu belge henüz başarı iddia etmez.
2. **Windows:** CLI sürümü, Windows sürümü, sandbox türü ve dosya sistemi
   bilgisiyle etkin araç/config durumunu kaydet. `apply_patch` yazmasının
   engellendiğini zararsız dosyada ölç; doğru/yanlış bağlam farkının kapandığı
   varsayımını kullanma. UNC, farklı sürücü ve 8.3 alias kapsamı ölçülmedi.
3. **macOS:** yukarıdaki iki gerçek CLI regresyonunu seçilen resmî ikiliyle
   çalıştır; normal hafıza başlangıcını ayrıca doğrula. Linux sonucu bu ölçümün
   yerine geçmez.

Komut matrisi testi Linux'ta üç platform ve iki görünümü taklit eder; gerçek
Windows/macOS sandbox'ını çalıştırmaz. Mevcut POSIX ikili testleri model hesabı
olmadan MCP teslimine kadar gider; receipt/Last-Session yayınını ve masaüstü
bildirimini ölçmez.

Normal hook'lar ve Yakala MCP güvenilen yerel koddur ve kendi dosya erişimini
sürdürür. Profil bu kodları veya CLI'nin başlangıç bağlamı yüklemesini genel
bir süreç sandbox'ına dönüştürmez. Modelin araca sunduğu patch içeriğinin
tahmin doğrulaması burada incelenen somut sınırdır.

## Kaynaklar

Aşağıdaki resmî İngilizce kaynaklar **19.10.2026 UTC** tarihinde incelendi.
Sürüm bilgisi GitHub release listesi, `latest` yönlendirmesi ve release API ile
doğrulandı. Web görünümü bazı GitHub kaynak sayfalarını açamadığında aynı
resmî repo salt okunur `git` checkout ile incelendi.

- **[S1] Sürümler:** [liste](https://github.com/openai/codex/releases),
  [latest](https://github.com/openai/codex/releases/latest),
  [0.155.1](https://github.com/openai/codex/releases/tag/rust-v0.155.1),
  [0.160.0](https://github.com/openai/codex/releases/tag/rust-v0.160.0),
  [0.163.0](https://github.com/openai/codex/releases/tag/rust-v0.163.0),
  [0.164.0-alpha.3](https://github.com/openai/codex/releases/tag/rust-v0.164.0-alpha.3),
  [release API](https://api.github.com/repos/openai/codex/releases?per_page=30).
- **[S2] Adlı profiller, beta, deny, öncelik, MCP kapsamı ve platform sınırları:**
  [Permissions](https://learn.chatgpt.com/docs/permissions).
- **[S3] CLI/config sözleşmesi:**
  [Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference),
  [CLI source](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/cli/src/main.rs).
- **[S4] Yama handler'ı ve sandbox'ın doğrulamaya aktarılması:**
  [0.163.0 apply_patch handler](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/core/src/tools/handlers/apply_patch.rs).
- **[S5] Update/Move/Delete ön okumaları:**
  [apply-patch invocation](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/apply-patch/src/invocation.rs).
- **[S6] Özel profilin derlenmesi:**
  [config/permissions.rs](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/core/src/config/permissions.rs).
- **[S7] `resolve_permission_config_syntax` ve legacy seçici önceliği:**
  [config/mod.rs](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/core/src/config/mod.rs).
- **[S8] Hook güveni, araç sırası ve callback hata sınırları:**
  [Hooks](https://learn.chatgpt.com/docs/hooks).
- **[S9] Hook kararı ve handler çağrısı:**
  [hook_runtime.rs](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/core/src/hook_runtime.rs),
  [tools/registry.rs](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/core/src/tools/registry.rs).
- **[S10] Araç kaydı ve kaldırılmış bayrak:**
  [spec_plan.rs](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/core/src/tools/spec_plan.rs),
  [features/lib.rs](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/features/src/lib.rs).
- **[S11] Strict config yükleme:**
  [config/loader/mod.rs](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/config/src/loader/mod.rs).
- **[S12] Windows sandbox türleri, deny-read sınırları ve MXC gereksinimleri:**
  [Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox).
- **[S13] Elevated kök okuma şartı ve alt yol reddi testi:**
  [0.155.1 resolved_permissions.rs](https://github.com/openai/codex/blob/be2951ea34f0d295ed0becf97079f92fa5f6950e/codex-rs/windows-sandbox-rs/src/resolved_permissions.rs),
  [0.163.0 resolved_permissions.rs](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/windows-sandbox-rs/src/resolved_permissions.rs),
  [unelevated sınırı](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/windows-sandbox-rs/src/lib.rs).
- **[S14] AGENTS keşfi/okuması ve başlangıç hata yolu:**
  [0.155.1 agents_md.rs](https://github.com/openai/codex/blob/be2951ea34f0d295ed0becf97079f92fa5f6950e/codex-rs/core/src/agents_md.rs),
  [0.163.0 agents_md.rs](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/core/src/agents_md.rs).
- **[S15] Sonraki kaynaklarda aynı koşul:**
  [0.164.0-alpha.3](https://github.com/openai/codex/blob/bd43893f3db678fcb7124296905a65b2dfa9dac0/codex-rs/windows-sandbox-rs/src/resolved_permissions.rs),
  [19.10.2026 tarihinde okunan main](https://github.com/openai/codex/blob/89461ea6833832b0551ce24ee32205824d5c108d/codex-rs/windows-sandbox-rs/src/resolved_permissions.rs).

İncelenen tag'lerin çözülmüş commit'leri:

```text
rust-v0.155.1       be2951ea34f0d295ed0becf97079f92fa5f6950e
rust-v0.160.0       a956835d020762cb2b570053af06f643a11c0ecc
rust-v0.163.0       83086188ae0614bd882818ff1aa23f3194962012
rust-v0.164.0-alpha.3 bd43893f3db678fcb7124296905a65b2dfa9dac0
```
