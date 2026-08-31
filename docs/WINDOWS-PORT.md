# Yerel Windows portu — ne değişti, neden

Bu belge `SETUP-WINDOWS.md` yolunun neden var olduğunu ve upstream'den nerede
ayrıldığını anlatır. Her madde ölçülmüş bir gözleme dayanır; ölçülmemiş olanlar
en sonda ayrıca listelenir.

## Tek satırlık `beyin.md` girişi artık doğru lane'i seçer

Yerel Windows motorunun var olması tek başına yeterli değildi. Public giriş dosyası
`docs/beyin-v2.md`, platformu ayırmadan `/tmp`, Bash ve `SETUP.md` akışına giriyordu; dolayısıyla
Sıfırdan izleyicisinin kopyaladığı tek satır Windows kurucusuna hiç ulaşmayabiliyordu.

Giriş kapısı artık hedef vault'a yazmadan önce üç lane seçer:

| Ortam | Seçilen yol | Sınır |
| --- | --- | --- |
| macOS / Linux | Mevcut `SETUP.md` | macOS akışı değiştirilmedi |
| Yerel Windows | Fresh temp clone -> `SETUP-WINDOWS.md` -> `install.ps1` | Yalnız temiz kurulum |
| WSL | `SETUP.md`, tamamen aynı WSL dağıtımı içinde | Vault Linux filesystem'de; karma Windows Obsidian runtime'ı doğrulanmış değil |

Bu ayrım `tests/entrypoint_test.py` ile repo seviyesinde kilitlenir. Yerel Windows kurucusunun
çalışan davranışı ayrıca Windows CI'daki preflight, temiz kurulum ve motor testleriyle ölçülür.

## Neden ayrı bir yol var

Upstream'in kendi README'si Windows'u boşluk olarak işaretliyor:

> | Windows | **test edilmedi**, WSL önerilir | WSL içinde Linux yolu geçerli. Yerel Windows için kurulum yolu yok. |

`docs/SPEC-V2.md` ise taşınabilirliği hedef sayıyor: *"Portable. macOS + Linux
for all shell code. Windows: documented as WSL-recommended, not silently
broken."* Bu port o boşluğu doldurur; WSL yolunu değiştirmez.

**macOS ve Linux davranışı değişmedi.** POSIX hook ayarı
`template/.claude/settings.json` içinde kalır; Windows installer yalnız kurduğu
vault'ta bunu `scripts/settings.windows.json` ile değiştirir. `upgrade.sh` ise
yeni ortak kilit modülü `_portalock.py`yi iki motor scriptiyle birlikte kopyalar.

## `scripts/upgrade.sh`'i Windows'ta çalıştırmayın

`upgrade.sh` bir POSIX yükselticisidir ve mevcut `.sh` hook adlarını yönetir.
PowerShell hook'lu bir vault'u güvenle tanıyıp dönüştüren bir yol henüz yoktur;
yanlış yolu otomatik denemek aynı olayı iki kez bağlayabilir. Bu yüzden yerel
Windows desteği şimdilik yalnız temiz kurulum içindir; mevcut vault yükseltmesi
açıkça kapsam dışıdır.

## Kırılan katmanlar

| Katman | Upstream (POSIX) | Windows'ta | Bu repodaki karşılığı |
|---|---|---|---|
| Dosya kilidi | `fcntl.flock` | modül yok, import'ta ölür | `_portalock.py` (`msvcrt.locking`) |
| Ayrık süreç | `start_new_session=True` | yok sayılır | `DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP` |
| Kancalar | `hooks/*.sh` (bash) | bash yok | `hooks/*.ps1` + `pwsh.exe` |
| Kanca kaydı | `"$CLAUDE_PROJECT_DIR/.../x.sh"` | `.ps1` doğrudan çalışmaz | shell'siz `pwsh.exe` + `args` exec-form |
| Çıktı kodlaması | UTF-8 | OEM kod sayfası | `lib.ps1` UTF-8'e sabitler |
| Satır sonu | LF | Git CRLF'e çevirir | `.gitattributes` LF pinler |
| Symlink testi | çalışır | `WinError 1314` | `skipUnless` ile açıkça atlanır |

## Ölçülmüş üç Windows tuzağı

**1. `[Console]::OutputEncoding` OEM'dir.** Türkçe bir makinede cp857. Bu sistem
baştan sona Türkçe bağlam üretir ve hafıza klasörünün adında emoji vardır.
Ölçüm: cp857 altında "Türkçe" geçerli UTF-8 olmayan bayta dönüşüyor ve 🔮
tamamen `?` oluyor — geri dönüşü yok. `lib.ps1` bunu **tek yerde** sabitler;
kanca başına ayarlamak, bir kancanın unutulma biçimidir.

**2. `WindowsApps\python3.exe` Python kurulu olmasa da vardır.** Microsoft
Store'u açan bir kısayoldur. `Get-Command` onu bulur, çalıştırmak hiçbir şey
yapmaz. Varlık kontrolü yapan bir kanca onu Python sanır, motoru "başlattım"
der ve özetleyici hiç çalışmaz. Bu yüzden hem `lib.ps1` hem `install.ps1`
Python'u **çalıştırarak** doğrular, ve `Get-Command -All` kullanır çünkü stub
gerçek kurulumun önünde olabilir.

**3. PowerShell 5.1 gömülü çift tırnakları soyar.** Native komutlara argüman
geçerken `print("X")` Python'a `print(X)` olarak ulaşır ve `NameError` fırlatır.
Ölçüm: tırnak içeren bir probe, Python'u olan bir makinede kapıya "Python yok"
dedirtiyordu. Probe'lar artık hiç tırnak içermez.

## `install.ps1` neden `#requires -Version 7.0` taşımaz

Taşısaydı kapının hedeflediği kullanıcı — pwsh 7'si olmayan — bizim mesajımızı
değil çıplak bir PowerShell sürüm hatası görürdü. Ön kontrol 5.1 uyumlu alt
kümeyle yazılır; kapı geçildikten sonra script kendini `pwsh` altında yeniden
başlatır.

Kapı dört bağımlılığı da (pwsh 7, Python 3, git, claude) **çalıştırarak**
doğrular ve biri bile eksikse **diske hiçbir şey yazmadan** durur. Upstream'in
kendi kuralıyla aynı hizada: *"yarım kurulmuş bir v2'den dürüst bir v1 iyidir."*

## Kancalar kopya değil, çeviridir

PowerShell kancaları bash'ten satır satır çevrildi. Her fonksiyon karşılığı
olduğu `.sh` satırını yorumunda adlandırır. Oturum anahtarı bash'inkiyle
**bayt-aynıdır** (UTF-8 `session_id`'nin SHA-256'sı, küçük harf hex), state
dosya adları ve düz düzen aynıdır, sayaç kilidi yine `mkdir` tabanlıdır,
16.000 karakterlik bağlam bütçesi ve kademeli kırpma sırası aynıdır.

Üç kasıtlı fark: yukarıdaki UTF-8 sabitlemesi, Python'un çalıştırılarak
doğrulanması, ve başarısız flush'ın `flush_last_error`'a kalıcı yazılması
(upstream'in `python3-missing` işareti ve uyarısı ayrıca korunur).

Bir platform farkı: `lib.sh` SHA-256 ve JSON kaçışı için `python3`'e çıkar;
PowerShell'de ikisi de yerlidir, dolayısıyla **kanca katmanı Python'a hiç
ihtiyaç duymaz.** Python yalnız motor için gerekir.

## Doğrulama

```
python tests/scripts_test_windows.py     Ran 25 tests, OK (skipped=1)
pwsh -File tests/Test-Preflight.ps1      TAMAM: 6 test gecti
pwsh -File tests/Test-CleanInstall.ps1   TAMAM: 18 kontrol gecti
```

`Test-CleanInstall.ps1` sıfırdan bir vault kurar, gerçek `SessionEnd` kancasını
gerçek bir payload ile sürer ve ayrık motorun `daily/` altına yazmasını bekler.
Stub'lanan tek şey `claude`'dur; kurulum, kancalar, ayırma ve motor gerçektir.

## Ne iddia etmiyoruz

- **Bu port fiziksel bir izleyici Windows makinesinde test edilmedi.** Windows 11 + PowerShell 7 +
  Python 3 üzerinde CI ve katkıcı doğrulamasıyla ölçüldü, resmî Microsoft/Anthropic desteği değildir.
- **Symlink güvenlik testi Windows'ta kurulamıyor.** `WinError 1314` yüzünden
  saldırı sahnelenemediği için `skipUnless` ile atlanır; o yüzeyde upstream'in
  güvencesine sahip değiliz. Not: junction ve hardlink varyantları ölçüldü ve
  `compile.py` ikisini de reddediyor (`staging-escape`, `staging-special`) —
  savunma tek katmanlı değil.
- **`MAX_PATH` stok bir makinede ölçülmedi.** Uzun yol desteği Windows'ta
  varsayılan olarak kapalıdır. Ölçülen gerçek bir vault'un en uzun yolu 131
  karakterdi, 260 sınırına geniş pay var; `install.ps1` yine de vault yolu
  derinse uyarır.
- **`upgrade.sh` satır numaraları** o günkü upstream sürümüne aittir.
