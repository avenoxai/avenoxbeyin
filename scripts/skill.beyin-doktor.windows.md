---
name: beyin-doktor
description: Beynin sağlık kontrolü. Hook'lar, script'ler, hafıza dosyaları, günlük loglar, derleme durumu ve vault hijyeni tek tabloda raporlanır. "beyin doktor", "doktor", "sağlık kontrolü", "beyin çalışıyor mu", "hafıza bozuk mu" dendiğinde veya bir hafıza mekanizmasının sessizce çalışmadığından şüphelenildiğinde kullan.
---

# Beyin Doktoru (Windows)

Bu skill beynin mekanik katmanını denetler: hook'lar tetikleniyor mu, script'ler çalışmış mı,
loglar tazeliğini koruyor mu, vault kirlenmiş mi. Amaç sessiz arızayı görünür yapmak.

**Bu vault native Windows kurulumudur** (`SETUP-WINDOWS.md` + `scripts/install.ps1`). Motor
`.ps1` kancalarla çalışır, `.sh` kancalarla değil. Depodaki POSIX sürümü macOS ve Linux içindir;
onun komutlarını burada çalıştırma, sahte kırmızı üretir. Farklar:

| POSIX sürümü ne arar | Windows'ta gerçek |
| --- | --- |
| `.claude/hooks/*.sh` | `.claude\hooks\*.ps1` — `settings.json` bunları `pwsh.exe` ile çağırır |
| çalıştırma izni (`chmod +x`) | Windows'ta exec biti yok, kontrol anlamsız |
| `AGENTS.md`, `.agents/skills`, `.codex/hooks` symlink | Windows checkout symlink'i korumaz, `install.ps1` **kopya** bırakır |
| `command -v python3` | `python3.exe` Microsoft Store kısayoludur, Python değildir. Motor `lib.ps1` içindeki `Get-BeyinPython` ile gerçek yorumlayıcıyı bulur |
| iCloud `* 2.md` çakışması | OneDrive `dosya (1).md` veya `dosya-MAKINEADI.md` bırakır |

## Nasıl çalışırsın

1. Vault kökünde (CLAUDE.md/AGENTS.md'nin bulunduğu klasör) çalış. Tüm yollar göreceli, mutlak yol yazma.
2. Aşağıdaki kontrolleri **PowerShell 7 (`pwsh`) ile sırayla çalıştır**. Komutları olduğu gibi
   kullan, tahmin etme. `pwsh` bu kurulumun ön koşuludur, yani kesin vardır; Git Bash yoktur.
3. Çok satırlı blokları `-Command` ile tek satıra sıkıştırmaya çalışma. Bloğu geçici bir `.ps1`
   dosyasına yaz ve `pwsh -NoProfile -ExecutionPolicy Bypass -File <dosya>` ile çalıştır.
   Ters bölü içeren regex'ler satır içi aktarımda bozulur, kontrol de sahte kırmızı verir.
4. Her kontrolün çıktısını 🟢 / 🟡 / 🔴 olarak sınıfla.
5. Sonucu tek bir tabloda ver, her 🔴 için bir düzeltme satırı yaz.
6. En sonda tek cümlelik hüküm ver.

Kontroller salt okunurdur. Hiçbir şeyi kendiliğinden düzeltme, önce raporla, sonra kullanıcı
isterse düzelt.

## Kontroller

### 1. Hook dosyaları yerinde mi

```powershell
foreach ($h in 'session-start','prompt-counter','session-end','pre-compact') {
  $f = ".claude\hooks\$h.ps1"
  if (Test-Path $f) { "$h.ps1: ok" } else { "$h.ps1: DOSYA YOK" }
}
```

🟢 dördü de `ok`. 🔴 eksik.
Düzeltme: eksik dosyayı depodan (`template/.claude/hooks/`) kopyala. Windows'ta çalıştırma izni
diye bir şey yok, `chmod` arama.

### 2. Hook'lar settings.json içinde bağlı mı

```powershell
$s = Get-Content .claude\settings.json -Raw
foreach ($h in 'session-start','prompt-counter','session-end','pre-compact') {
  if ($s -match [regex]::Escape("$h.ps1")) { "${h}: bagli" } else { "${h}: SETTINGS ICINDE YOK" }
}
```

🟢 dördü de bağlı. 🔴 biri bile eksikse o hook hiç çalışmıyor demektir.
Düzeltme: `.claude\settings.json` içindeki `hooks` bloğuna eksik olayı ekle (SessionStart,
UserPromptSubmit, SessionEnd, PreCompact). Doğru şablon `scripts/settings.windows.json`;
`command` alanı `pwsh.exe`, script yolu `args` içinde `-File` ile verilir. Sonra Claude Code'u
yeniden başlat.

### 2b. pwsh.exe PATH'te mi

```powershell
$p = Get-Command pwsh.exe -ErrorAction SilentlyContinue
if ($p) { "pwsh.exe: $($p.Source)" } else { "pwsh.exe: PATH'TE YOK" }
```

Windows'a özgü ve sessiz bir arıza: `settings.json` kancaları **çıplak `pwsh.exe` adıyla**
çağırır. PATH'ten düşerse dört kanca da hiç ses çıkarmadan başarısız olur, hafıza yazılmaz.
🟢 bulundu. 🔴 yok.
Düzeltme: `winget install --id Microsoft.PowerShell --source winget`, ya da mevcut kurulumun
klasörünü PATH'e geri ekle. Sonra Claude Code'u yeniden başlat.

### 3. Özyineleme koruması her hook'ta var mı

```powershell
Get-ChildItem .claude\hooks\*.ps1 | ForEach-Object {
  if (Select-String -LiteralPath $_.FullName -Pattern 'BEYIN_INVOKED_BY' -Quiet) {
    "$($_.Name): guard var"
  } else { "$($_.Name): GUARD YOK" }
}
```

🟢 hepsinde var (`lib.ps1` dahil). 🔴 eksik. Guard'ı olmayan hook, arka plan `claude -p`
çağrısında tekrar tetiklenir ve sonsuz döngü riski doğar.
Düzeltme: dosyanın başına `if ($env:BEYIN_INVOKED_BY) { exit 0 }` ekle.

### 3b. Codex router, skill ve kanca bağlantıları

Windows checkout'u symlink korumadığı için `install.ps1` kopya bırakır. Doğru soru "symlink mi"
değil, "kopya güncel mi ve `hooks.json` mutlak yol mu okuyor".

```powershell
if ((Get-FileHash AGENTS.md).Hash -eq (Get-FileHash CLAUDE.md).Hash) {
  'router: ayni icerik'
} else { 'router: AYRI/ESKI' }
if (Test-Path .agents\skills\beyin-doktor\SKILL.md) { 'skills: kopya yerinde' } else { 'skills: YOK' }
$w = [ordered]@{ SessionStart='session-start.ps1'; UserPromptSubmit='prompt-counter.ps1'; PreCompact='pre-compact.ps1'; SessionEnd='session-end.ps1' }
$t = @{ SessionStart=15; UserPromptSubmit=5; PreCompact=10; SessionEnd=3 }
try {
  $d = Get-Content .codex\hooks.json -Raw | ConvertFrom-Json
  $ok = $true
  foreach ($e in $w.Keys) {
    $n = @(foreach ($m in @($d.hooks.$e)) { foreach ($h in @($m.hooks)) {
      if ($h.command -like "*$($w[$e])*" -and $h.command -match '[A-Za-z]:\\' -and $h.timeout -eq $t[$e]) { 1 }
    } }).Count
    if ($n -ne 1) { $ok = $false; "  $e sayisi: $n (beklenen 1)" }
  }
  if ($ok) { 'codex: ok' } else { 'codex: BOZUK' }
} catch { 'codex: BOZUK/YOK' }
```

🟢 üç satır da ortak/ok. 🟡 `router: AYRI/ESKI`: `CLAUDE.md` kurulumdan sonra elle değişmiş,
kopya geride kalmış. 🔴 `codex: BOZUK`: Codex göreli veya eski bir komut okuyor.
Düzeltme: router için `Copy-Item CLAUDE.md AGENTS.md -Force`. Kancalar için
`python .claude\scripts\render_codex_hooks.py --vault "$PWD" --platform windows`, sonra Codex
içinde `/hooks` ekranından değişen proje kancalarını onayla. Güven hash'ini elle yazma.

### 3c. Antigravity kanca adaptörü

```powershell
try {
  $a = (Get-Content .agents\hooks.json -Raw | ConvertFrom-Json).'avenox-beyin'
  if (@($a.PreInvocation).Count -eq 1 -and @($a.Stop).Count -eq 1) { 'antigravity: ok' }
  else { 'antigravity: BOZUK' }
} catch { 'antigravity: BOZUK/YOK' }
```

🟢 `ok`. 🔴 ise Antigravity hafızayı enjekte etmiyor veya son turu günlüğe taşımıyor.
Düzeltme: `python .claude\scripts\render_antigravity_hooks.py --vault "$PWD" --platform windows`.

### 4. Python ve model CLI

`command -v python3` **kullanma**. Windows'ta `python3.exe` Python kurulu olmasa bile
`WindowsApps` altında bir Microsoft Store kısayolu olarak durur: bulunur ama çalışmaz. Motorun
kendi çözücüsünü sor, o yorumlayıcıyı çalıştırarak doğrular.

```powershell
. .\.claude\hooks\lib.ps1
$py = Get-BeyinPython
if ($py) { "python: $py" } else { 'python: YOK' }
$cli = Get-Command claude -ErrorAction SilentlyContinue
if ($cli) { "claude: $($cli.Source)" }
else {
  $agy = Get-Command agy -ErrorAction SilentlyContinue
  if ($agy) { "agy: $($agy.Source)" } else { 'model CLI: YOK' }
}
```

🟢 Python bulundu ve `claude` veya `agy` var. 🔴 Python yoksa motor hiç çalışmaz; iki model CLI
da yoksa arka plan özetleyici durur.
Düzeltme: `winget install --id Python.Python.3.13 --source winget`; model CLI için
`winget install --id Anthropic.ClaudeCode --source winget`. Kurduktan sonra Claude Code'u
yeniden başlat, PATH yenilensin.

### 4b. Model CLI oturum açmış mı

Varlık yetmez. Arka plan özetleyici ve derleyici `claude -p` ile çalışır; CLI kurulu ama
oturum kapalıysa her flush `Not logged in` alıp ölür ve **hiçbir yerde günlük oluşmaz**.
Masaüstü uygulamasında oturum açık olması CLI'yi kapsamaz, ikisinin kimlik deposu ayrıdır.
`auth status` token harcamaz.

```powershell
$cli = Get-Command claude -ErrorAction SilentlyContinue
if (-not $cli) { 'claude: YOK' }
else {
  $raw = & $cli.Source auth status 2>$null
  try {
    $j = $raw | ConvertFrom-Json
    if ($j.loggedIn) { "oturum: acik (yontem: $($j.authMethod))" } else { 'oturum: KAPALI' }
  } catch { 'oturum: OKUNAMADI' }
}
```

🟢 `oturum: acik`. 🔴 `oturum: KAPALI` veya `OKUNAMADI`: motor çalışır görünür ama hafıza
yazılmaz, en sinsi arıza budur.
Düzeltme: **kullanıcı kendisi** bir terminalde `claude auth login` çalıştırsın (tarayıcıda
OAuth açar). Bu adımı ajan olarak sen yapamazsın ve yapmaya çalışma. Giriş bittikten sonra
`claude auth status` ile doğrula, sonra bir oturum açıp kapat ve 6 numaralı kontrolü tekrarla.

### 5. python3-missing işareti

```powershell
if (Test-Path .claude\scripts\.state\python3-missing) {
  "isaret VAR, tarih: " + (Get-Content .claude\scripts\.state\python3-missing -TotalCount 1)
} else { 'isaret yok' }
```

🟢 işaret yok. 🔴 işaret var: hook'lar çalışan bir Python bulamamış.
Düzeltme: 4 numaralı kontrolü yeşile çevir, sonra dosyayı sil:
`Remove-Item .claude\scripts\.state\python3-missing`.

### 6. Günlük log tazeliği

```powershell
$f = Get-ChildItem daily\*.md -ErrorAction SilentlyContinue |
     Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $f) { 'daily: hic log yok' }
else { "daily: $($f.Name), $([int]((Get-Date) - $f.LastWriteTime).TotalHours) saat once yazildi" }
```

🟢 48 saatten yeni. 🟡 48 ile 96 saat arası, ya da vault bugün kurulmuş ve henüz oturum
bitmemiş. 🔴 96 saatten eski.
Düzeltme: oturum bitir ve yeniden başlat, sonra bu kontrolü tekrarla. Hâlâ boşsa 1, 2, 2b ve 4
numaralı kontrollere dön, arıza flush zincirinde.

### 7. Derleme durumu

```powershell
$f = '.claude\scripts\.state\compile-state.json'
if (Test-Path $f) {
  "state: $([int]((Get-Date) - (Get-Item $f).LastWriteTime).TotalHours) saat once guncellendi"
  $d = Get-Content $f -Raw | ConvertFrom-Json
  "last_run: $($d.last_run)"
  "last_status: $($d.last_status)"
  "ingested: $(@($d.ingested.PSObject.Properties).Count) log"
} else { 'compile: state dosyasi yok, henuz hic derleme calismadi' }
```

🟢 `last_run` 48 saatten yeni ve `last_status` `ok`. 🟡 state yok ama vault yeni kurulmuş veya
henüz akşam 18:00 olmamış. 🔴 `last_status` `fail:` ile başlıyor veya 48 saatten eski.
Düzeltme: elle bir tur çalıştır ve hatayı gör (`$py` için 4 numaralı kontrolü kullan):
`& $py .claude\scripts\compile.py --dry-run`, sonra `& $py .claude\scripts\compile.py`.

### 8. Sağlık kayıtlarındaki son hatalar

`health.json` satır başına bir JSON kaydı tutar (JSONL). Kayıtların hepsi arıza değildir:
`error` alanı `warn:` ile başlıyorsa motor sorunu **kendi çözmüştür** (örneğin
`warn:summary-schema-retried` — özetin ilk çıktısı şemaya uymadı, yeniden denendi, tuttu).
Uyarıyı hata sayan bir kontrol kalıcı kırmızı üretir ve kullanıcıyı doktoru görmezden gelmeye
alıştırır; ikisini ayır.

```powershell
$f = '.claude\scripts\.state\health.json'
if (-not (Test-Path $f)) { 'health: kayit yok' }
else {
  $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  $err = 0; $warn = 0
  foreach ($line in Get-Content $f) {
    if (-not "$line".Trim()) { continue }
    try { $r = "$line" | ConvertFrom-Json } catch { continue }
    $yas = [int]((($now - $r.ts) / 3600))
    if ("$($r.error)" -like 'warn:*') { $tur = 'UYARI'; if ($yas -lt 48) { $warn++ } }
    else { $tur = 'HATA'; if ($yas -lt 48) { $err++ } }
    "$tur | $($r.component) | $($r.error) | $yas saat once"
  }
  "ozet: son 48 saatte $err hata, $warn uyari"
}
```

🟢 kayıt yok, ya da son 48 saatte `0 hata` (uyarı olabilir). 🟡 son 48 saatte uyarı var ama
hata yok: motor kendini toparlamış, tekrarlıyorsa bak. 🔴 son 48 saatte en az bir `HATA`.
Düzeltme: `component` alanına bak. `flush` ise transkript veya model CLI — önce 4b'yi kontrol
et, `claude-exit-1` neredeyse her zaman kapalı oturum demektir. `compile` ise model çağrısı
sorunlu. Kaydı okuduktan ve sebebini giderdikten sonra dosyayı silebilirsin
(`Remove-Item .claude\scripts\.state\health.json`), script yeniden yazar.

### 9. Bilgi indeksi büyüklüğü

```powershell
if (Test-Path knowledge\index.md) { "index: $(@(Get-Content knowledge\index.md).Count) satir" }
else { 'index: DOSYA YOK' }
```

Oturum başında indeksin sadece ilk 150 satırı bağlama giriyor.
🟢 150 satır ve altı. 🟡 151 ile 300 satır arası, alt sıralar artık enjekte edilmiyor.
🔴 300 satırın üstü: özet indeks zamanı.
Düzeltme: indeksi tema başlıklarına göre grupla, eski satırları tek bir özet satırında topla,
detay makalede kalsın. Dosya hiç yoksa depodaki tohum dosyayı geri koy.

### 10. Bulut senkron çakışma dosyaları

iCloud değil OneDrive. OneDrive çakışan kopyayı `dosya-MAKINEADI.md` veya `dosya (1).md` diye
bırakır; `* 2.*` kalıbı da taşınmış bir vault'ta hâlâ çıkabilir.

```powershell
$c = @(Get-ChildItem -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object {
  $_.FullName -notlike '*\.git\*' -and (
    $_.Name -match ' 2\.' -or $_.Name -match ' \(\d+\)\.' -or $_.Name -match "-$env:COMPUTERNAME\."
  )
})
if ($c.Count -eq 0) { 'temiz' } else { $c.FullName }
```

🟢 çıktı `temiz`. 🔴 çıktı var: senkron aynı dosyanın iki kopyasını tutmuş, hafıza dosyalarının
bir kısmı yanlış kopyada olabilir.
Düzeltme: listelenen her dosyayı aslıyla karşılaştır
(`Compare-Object (Get-Content a.md) (Get-Content 'a (1).md')`), gerekli içeriği asıl dosyaya
taşı, sonra çakışma kopyasını sil. Kullanıcıya sormadan silme.

### 11. Git deposu ve kaydedilmemiş değişiklik

```powershell
$null = git rev-parse --is-inside-work-tree 2>$null
if ($LASTEXITCODE -eq 0) { "git: var, kaydedilmemis: $(@(git status --porcelain).Count) dosya" }
else { 'git: REPO YOK' }
```

🟢 repo var ve kaydedilmemiş dosya 50'nin altında. 🟡 50 üstü birikmiş. 🔴 repo yok, yani
hafızanın geri alınabilir bir geçmişi yok.
Düzeltme: repo yoksa `git init -b main`, ardından **`git config core.autocrlf false`** ve ilk
commit. Bu ayar Windows'ta şart: aksi halde sonraki bir checkout `.py` ve `.sh` dosyalarını
CRLF'e çevirir. Birikme varsa commit at.

### 12. Sürüm dosyası

```powershell
if (Test-Path .beyin-version) { "surum: $(Get-Content .beyin-version -TotalCount 1)" }
else { 'surum: DOSYA YOK, v1 vault' }
```

🟢 `2.3.0`. 🟡 `2.2.0`, `2.1.0`, `2.0.0` veya dosya yok.
Düzeltme: **`scripts/upgrade.sh`'i Windows'ta çalıştırma** — her kancayı ikiye katlar ve
yüklenemeyen bir motora sürüm damgalar (bkz. `docs/WINDOWS-PORT.md`). Windows'ta yükseltme
yolu yok: yeni bir vault kurup hafıza dosyalarını elle taşı.

### 13. Kurallar dosyası

```powershell
$k = Get-ChildItem -Directory | Where-Object { $_.Name -like '*850-Companion' } |
     ForEach-Object { Join-Path $_.FullName 'Kurallar.md' }
if ($k -and (Test-Path -LiteralPath $k)) { "kurallar: var, $(@(Get-Content -LiteralPath $k).Count) satir" }
else { 'kurallar: YOK' }
```

Klasör adındaki emoji'yi komuta elle yazma, yukarıdaki gibi joker ile bul: emoji satır içi
aktarımda bozulur ve dosya "yok" görünür.

🟢 var. 🟡 yok: kullanıcının düzeltmeleri kalıcı hale gelmiyor.
Düzeltme: depodaki tohum `Kurallar.md` dosyasını `850-Companion` klasörünün altına kopyala.

### 14. Çift etkin kanca (settings.json + settings.local.json)

Windows'ta komut `pwsh.exe`, script yolu `args` içinde durur. Sadece `command` alanına bakan
POSIX kontrolü burada her olayı `0` sayar ve sahte kırmızı verir; aşağıdaki ikisini birleştirir.

```powershell
$ev = 'SessionStart','UserPromptSubmit','SessionEnd','PreCompact'
$v  = 'session-start.ps1','prompt-counter.ps1','session-end.ps1','pre-compact.ps1'
$n = @{}; foreach ($e in $ev) { $n[$e] = 0 }
foreach ($file in '.claude\settings.json', '.claude\settings.local.json') {
  if (-not (Test-Path $file)) { continue }
  try { $d = Get-Content $file -Raw | ConvertFrom-Json } catch { "${file}: BOZUK JSON"; continue }
  foreach ($e in $ev) { foreach ($m in @($d.hooks.$e)) { foreach ($h in @($m.hooks)) {
    $blob = "$($h.command) $($h.args -join ' ')"
    if (@($v | Where-Object { $blob -like "*$_*" }).Count -gt 0) { $n[$e]++ }
  } } }
}
foreach ($e in $ev) { "${e}: $($n[$e])" }
```

🟢 dört olayın da sayısı tam olarak `1`. 🔴 herhangi biri `2` veya daha fazla: o olayda
kancalar her seferinde iki kez çalışıyor, yani her prompt iki kez sayılıyor ve her oturum
sonunda iki flush tetikleniyor. `0` ise o olay hiç bağlı değil.
Düzeltme: `.claude\settings.local.json` içindeki beyin kanca girdisini sil, ilgisiz
kancalara ve `env`, `permissions` gibi diğer anahtarlara dokunma. Tek bağlantı
`.claude\settings.json` içinde kalmalı.

### 15. Vault içinde sır taşıyabilecek yedek artığı

```powershell
$r = @(Get-ChildItem -Recurse -File -Force -Include '*.yedek','*.yedek-*','settings.local.json.*','*.bak','*.orig' -ErrorAction SilentlyContinue |
      Where-Object { $_.FullName -notlike '*\.git\*' })
if ($r.Count -eq 0) { 'yedek artigi: temiz' } else { $r.FullName }
$tracked = @(git ls-files 2>$null | Select-String 'settings\.local\.json|\.yedek|\.env$')
if ($tracked.Count -eq 0) { 'izlenen sirli dosya yok' } else { $tracked }
```

🟢 iki bölüm de temiz. 🔴 bir yedek dosyası çıkarsa: bu dosyalar `settings.local.json`
kopyası olabilir ve API anahtarı taşır; ikinci bölümde bir şey çıkarsa sır zaten git
tarafından izleniyor demektir.
Düzeltme: yedeği vault dışına taşı; Windows'ta `chmod 600` yoktur, karşılığı
`icacls <dosya> /inheritance:r /grant:r "$env:USERNAME:(F)"`. Git izliyorsa
`git rm --cached <dosya>` ile izlemeden çıkar, `.gitignore` kuralını doğrula, ve
sızmış anahtarı sağlayıcıdan **iptal edip yenile**.

### 16. Grafik bütünlüğü (kırık bağlantı + yetim not)

`PYTHONIOENCODING` şart, süs değil. `graf_kontrol.py` yetim listesinde klasör adlarını basar ve
şablonun içerik klasörlerinin hepsi emoji ile başlar (`📥 000-Inbox`, `🛠️ 600-Arsenal`). Windows'ta
çıktı bir boruya veya dosyaya yönlendirildiğinde Python konsol kod sayfasını kullanır (Türkçe
sistemde `cp1254`, çoğu Batı Avrupa sisteminde `cp1252`) ve emoji `UnicodeEncodeError` ile taramayı
öldürür. Ajan çıktıyı her zaman yakalar, yani yönlendirilmiş hâl normal hâldir. Emoji'li klasörde
ilk yetim not oluştuğu anda kontrol 16 çöker; bu satır onu baştan engeller.

```powershell
. .\.claude\hooks\lib.ps1
$env:PYTHONIOENCODING = 'utf-8'
& (Get-BeyinPython) .claude\scripts\graf_kontrol.py
```

Hafıza düz bir dosya yığını değil **graf**: bir not ancak ona giden bir bağlantı varsa
bulunur. Yetim not diskte durur ama ajan ona ulaşmak için bütün vault'u taramak zorunda
kalır, yani ya token yakar ya bulamayıp uydurur. Kırık bağlantı ise haritada var olmayan
bir adresi gösterir. İkisini de başka hiçbir kontrol yakalamıyor.

🟢 kırık bağlantı 0. 🟡 kırık bağlantı 1–20. 🔴 kırık bağlantı 20 üstü.
Yetim sayısı tek başına 🔴 değildir: taslak, arşivlik ve tek seferlik notlar doğal olarak
yetimdir. Windows kurulumunda `.agents\skills` altındaki **kopyalar** da yetim görünür, bu
normaldir, symlink olmadığı için tarayıcı onları ayrı dosya sayar.
Düzeltme: `--tam` ile listeyi aç. Kırık bağlantı için ya hedef notu oluştur ya bağlantıyı
düzelt; hedef bilerek yoksa (belgede örnek olarak geçen `[[wikilink]]` gibi) dokunma.
Yetim not için ilgili üs nottan veya `Dashboard.md`'den bağlantı ver.

## Rapor formatı

Tüm kontroller bittikten sonra tek tablo bas:

```
| Kontrol | Durum | Bulgu |
| --- | --- | --- |
| Hook dosyaları | 🟢 | dört .ps1 yerinde |
| settings.json bağlantısı | 🟢 | dört olay da bağlı |
| pwsh.exe PATH | 🟢 | C:\Program Files\...\pwsh.exe |
| Özyineleme koruması | 🟢 | hepsinde var |
| Codex bağlantıları | 🟢 | router aynı içerik, hooks.json mutlak |
| Antigravity bağlantıları | 🟢 | PreInvocation ve Stop adaptörü kayıtlı |
| Python ve model CLI | 🟢 | python 3.14.6, claude var |
| Model CLI oturumu | 🟢 | açık (claudeai) |
| python3-missing işareti | 🟢 | işaret yok |
| Günlük log tazeliği | 🟡 | son log 51 saat önce |
| Derleme durumu | 🔴 | last_status fail:timeout |
| Sağlık kayıtları | 🔴 | dün compile hatası |
| Bilgi indeksi | 🟢 | 42 satır |
| Senkron çakışmaları | 🟢 | temiz |
| Git | 🟢 | repo var, 3 dosya kaydedilmemiş |
| Sürüm | 🟢 | 2.3.0 |
| Kurallar | 🟢 | var, 29 satır |
| Çift etkin kanca | 🔴 | SessionEnd 2 kez bağlı |
| Sır yedeği artığı | 🟢 | temiz |
| Grafik bütünlüğü | 🟡 | 4 kırık bağlantı, 12 yetim not |
```

Tablodan sonra sadece 🔴 satırlar için "Düzeltme:" ile başlayan birer satır yaz, komutu da ver.
Sonra tek cümlelik hüküm: örneğin "Beyin ayakta ama derleyici iki gündür takılı, önce onu çöz."
Her şey yeşilse hüküm de kısa olsun: "Beyin sağlıklı, yapılacak bir şey yok."
