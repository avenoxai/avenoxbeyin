# SETUP-WINDOWS.md: Activate this second brain on native Windows

> You are Claude Code or Google Antigravity, run from inside a freshly cloned `avenoxbeyin` repo on
> **native Windows** — not WSL. The user wants their own AI second brain.
> The scaffold lives in `./template/`; the mechanical work lives in
> `./scripts/install.ps1`. Speak **Turkish** to the user. This runbook is in
> English only so your instructions stay precise; the system you build talks
> Turkish.
>
> On macOS or Linux, stop and use `SETUP.md` instead.

## Rules (binding)

1. **Preflight first, build second.** Nothing touches the filesystem before the
   dependency gate passes. Running it is your first action, before any question.
2. **Never destroy.** `install.ps1` refuses an existing target and exits 3.
   Installing into an existing vault is out of scope; do not work around it.
3. **Do not install software silently.** When the gate reports something
   missing, show the user the exact `winget` line and ask before running it.
   Installing software is their decision, not yours.
4. **Upgrading an existing v1 vault is out of scope.** Never run
   `scripts/upgrade.sh` here — on Windows it doubles every hook and stamps a
   version onto an engine that cannot load. See `docs/WINDOWS-PORT.md`.
5. **Be the demo.** Narrate in short Turkish lines as you go: "Bağımlılıkları
   kontrol ediyorum...", "Vault iskeletini kuruyorum...", "Kancaları
   bağlıyorum...". Short sentences, no walls of text.
6. **No direct API key.** The summarizer and compiler use the user's existing
   Claude Code or Antigravity login through `claude -p` or `agy -p`.

## PHASE 0: Dependency gate

Run this first. It writes nothing.

```powershell
powershell -NoProfile -Command ". .\scripts\install.ps1 -PreflightOnly; Write-BeyinPreflightReport -Report (Invoke-BeyinPreflight)"
```

Antigravity içinden kuruyorsan aynı kontrolü şu varyantla çalıştır:

```powershell
powershell -NoProfile -Command ". .\scripts\install.ps1 -PreflightOnly; Write-BeyinPreflightReport -Report (Invoke-BeyinPreflight -Harness Antigravity)"
```

It checks four things by **running** each one: PowerShell 7, Python 3, Git,
seçilen model CLI. Presence is not enough — on Windows `python3.exe` exists as a
Microsoft Store shortcut even with Python uninstalled.

Report the result to the user in Turkish.

- **Gate passed** → go to PHASE 1.
- **Something missing** → show the `winget` command the report printed, explain
  in one line what it is for, and **ask** before running it. After they agree
  and it installs, run PHASE 0 again. Do not continue on a failed gate.

## PHASE 1: Interview (Turkish, one question at a time)

| Ask | Fills |
| --- | --- |
| Adın ne? | `{{USER_NAME}}` |
| Ne iş yapıyorsun? (1-2 cümle) | `{{USER_BIO}}` |
| AI ortağına ne ad vermek istersin? | `{{COMPANION}}` |
| Vault nereye kurulsun? | `{{VAULT_PATH}}` |

`{{OS_NAME}}` is derived from the machine name and confirmed by the user
(e.g. `AylinOS`). Default vault path: `~\Documents\<OS_NAME>`.

Prefer a shallow path. Windows has a 260-character limit with long-path support
off by default, and the compiler builds article filenames from titles.
`install.ps1` warns when the path leaves too little room.

## PHASE 2: Install

```powershell
pwsh -NoProfile -File .\scripts\install.ps1 `
  -VaultPath "<VAULT_PATH>" -UserName "<USER_NAME>" -UserBio "<USER_BIO>" `
  -Companion "<COMPANION>" -OsName "<OS_NAME>" -Harness "<Claude|Antigravity>"
```

Exit codes: `0` ok · `1` preflight failed · `2` missing parameter ·
`3` target exists · `4` an unresolved `{{PLACEHOLDER}}` remained.

Anything but 0: stop, tell the user what the code means, do not improvise a fix.

## PHASE 3: Verify

Run all three and report each result:

```powershell
Test-Path "<VAULT_PATH>\.claude\scripts\flush.py"
(Get-Content "<VAULT_PATH>\.claude\settings.json" -Raw | ConvertFrom-Json).hooks.PSObject.Properties.Name.Count
Test-Path "<VAULT_PATH>\.claude\scripts\.state"
Test-Path "<VAULT_PATH>\.agents\hooks.json"
```

Expected: `True`, `4`, `True`, `True`.

## PHASE 4: First-run report (Turkish)

Tell the user, in a few short lines:

- what was installed and where;
- that from now on **the end of every session is captured automatically** — a
  hook writes that conversation into `daily/` without anyone remembering to;
- that once a day the compiler turns those logs into linked articles under
  `knowledge/`, and the next morning that index enters context by itself;
- that `beyin doktor` diagnoses the machine layer if something looks wrong.

Then tell them to open the vault in Obsidian and start a session there.

### Arka plan pencereleri ve yerel workaround (#671)

V3 hook ve sürüm kontrolü işçileri Windows'ta `CREATE_NEW_PROCESS_GROUP |
CREATE_NO_WINDOW` ile başlatılır; `DETACHED_PROCESS` kullanılmaz. Standart
girdi/çıktı kapalı kalır, POSIX yeni oturum davranışı değişmez. Bu, normal
alt süreçlerin gereksiz konsol penceresi açmasını önlemeyi hedefler; açıkça
yeni pencere isteyen harici araçları gizleme garantisi değildir.

Bu iki yönetilen dosyayı eski sürümde elle değiştirdiysen güncelleyici yine
`content differs` ile durabilir. Düzeltme eski manifestteki hash'i değiştirmez
ve yerel düzenlemeni otomatik ezmez. Önce değişikliği yedekleyip diff'i incele;
yalnız bu workaround olduğundan eminsen dosyayı **kurulu sürümün** doğrulanmış
stok kopyasına geri getirip normal güncellemeyi tekrar çalıştır. Başka
değişiklikleri koru; manifest hash'lerini değiştirme veya conflict korumasını
atlama. Ayrıntılar: [güncelleme sözleşmesi](docs/v3/UPDATE.md).
