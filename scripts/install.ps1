<#
    beyin v2 -- native Windows installer.

    DELIBERATELY NO `#requires -Version 7.0`.

    The whole point of the preflight below is to tell a user without PowerShell 7
    what to install. With a #requires line, that exact user -- the one the gate
    exists for -- would see a bare PowerShell version error instead of our
    message. So the preflight is written in the PowerShell 5.1-compatible subset:
    no ternary, no ??, no -Encoding utf8NoBOM, no ForEach-Object -Parallel.
    Once the gate passes, the script re-launches itself under pwsh for the real
    work and everything from that point may use 7-only syntax.

    Usage:
      pwsh -NoProfile -File .\scripts\install.ps1 -VaultPath "C:\Users\me\Documents\AylinOS" `
           -UserName "Aylin" -UserBio "Ürün tasarımcısı" -Companion "Echo" -OsName "AylinOS"

      powershell -NoProfile -File .\scripts\install.ps1 -PreflightOnly   # just check

    Exit codes:
      0 ok | 1 preflight failed | 2 missing parameter | 3 target exists
      4 unresolved {{PLACEHOLDER}} remained
#>
[CmdletBinding()]
param(
    [string]$VaultPath,
    [string]$UserName,
    [string]$UserBio,
    [string]$Companion,
    [string]$OsName,
    [ValidateSet('Claude', 'Antigravity')][string]$Harness = 'Claude',
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'


function Test-BeyinDependency {
    <#
    .SYNOPSIS
        Proves a dependency works by RUNNING it.
    .DESCRIPTION
        The probe carries NO quotes on purpose: Windows PowerShell 5.1 strips
        embedded double quotes when passing arguments to native commands, so
        print("X") reaches Python as print(X) and raises NameError. Measured.

        Presence is not proof on Windows. C:\...\WindowsApps\python3.exe exists
        with Python uninstalled -- it is a Microsoft Store shortcut. Get-Command
        finds it, running it does nothing. Same discipline applied to all four
        dependencies so none of them can pass on a stub.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$ExpectPattern
    )
    try {
        $out = & $Path @Arguments 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $text = ($out | Out-String).Trim()
        return ($text -match $ExpectPattern)
    }
    catch { return $false }
}


function Find-BeyinPython {
    # -All matters: the Store stub can sit ahead of a real install on PATH.
    foreach ($name in @('python3', 'python')) {
        foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if (-not $cmd.Source) { continue }
            $ok = Test-BeyinDependency -Path $cmd.Source `
                -Arguments @('-c', 'import sys; print(sys.version_info.major)') `
                -ExpectPattern '^[3-9]$'
            if ($ok) { return $cmd.Source }
        }
    }
    return $null
}


function Find-BeyinPwsh {
    foreach ($cmd in @(Get-Command pwsh -All -ErrorAction SilentlyContinue)) {
        if (-not $cmd.Source) { continue }
        $ok = Test-BeyinDependency -Path $cmd.Source `
            -Arguments @('-NoProfile', '-Command', '$PSVersionTable.PSVersion.Major') `
            -ExpectPattern '^([7-9]|\d{2,})$'
        if ($ok) { return $cmd.Source }
    }
    return $null
}


function Find-BeyinAgy {
    foreach ($cmd in @(Get-Command agy -All -ErrorAction SilentlyContinue)) {
        if (-not $cmd.Source) { continue }
        $ok = Test-BeyinDependency -Path $cmd.Source `
            -Arguments @('--version') -ExpectPattern '^\d+\.\d+'
        if ($ok) { return $cmd.Source }
    }
    return $null
}


function Invoke-BeyinPreflight {
    <# Returns @{ Ok; Missing; Python; Pwsh }. Touches nothing on disk. #>
    param([ValidateSet('Claude', 'Antigravity')][string]$Harness = 'Claude')
    $missing = New-Object System.Collections.ArrayList
    $result = @{ Ok = $true; Missing = @(); Python = $null; Pwsh = $null; Runner = $null }

    $pwshPath = Find-BeyinPwsh
    if ($pwshPath) {
        $result.Pwsh = $pwshPath
    }
    else {
        [void]$missing.Add("PowerShell 7 bulunamadi (sistemde $($PSVersionTable.PSVersion) var)`n  winget install --id Microsoft.PowerShell --source winget")
    }

    $pythonPath = Find-BeyinPython
    if ($pythonPath) {
        $result.Python = $pythonPath
    }
    else {
        [void]$missing.Add("Python 3 bulunamadi (WindowsApps stub'i Python degildir)`n  winget install --id Python.Python.3.13 --source winget")
    }

    $git = Get-Command git -ErrorAction SilentlyContinue
    $gitOk = $false
    if ($git -and $git.Source) {
        $gitOk = Test-BeyinDependency -Path $git.Source -Arguments @('--version') -ExpectPattern '^git version \d'
    }
    if (-not $gitOk) {
        [void]$missing.Add("Git bulunamadi`n  winget install --id Git.Git --source winget")
    }

    if ($Harness -eq 'Antigravity') {
        $agyPath = Find-BeyinAgy
        if ($agyPath) { $result.Runner = $agyPath }
        else {
            [void]$missing.Add("Antigravity CLI bulunamadi (agy)`n  https://antigravity.google/docs/cli/")
        }
    }
    else {
        $claude = Get-Command claude -ErrorAction SilentlyContinue
        $claudeOk = $false
        if ($claude -and $claude.Source) {
            $claudeOk = Test-BeyinDependency -Path $claude.Source -Arguments @('--version') -ExpectPattern '\(Claude Code\)'
        }
        if ($claudeOk) { $result.Runner = $claude.Source }
        else {
            [void]$missing.Add("Claude Code bulunamadi`n  https://claude.com/claude-code")
        }
    }

    $result.Missing = $missing.ToArray()
    $result.Ok = ($missing.Count -eq 0)
    return $result
}


function Write-BeyinPreflightReport {
    param([Parameter(Mandatory = $true)][hashtable]$Report)

    if ($Report.Ok) {
        Write-Host "On kontrol gecti."
        Write-Host "  pwsh   : $($Report.Pwsh)"
        Write-Host "  python : $($Report.Python)"
        Write-Host "  runner : $($Report.Runner)"
        return
    }

    Write-Host ""
    foreach ($m in $Report.Missing) {
        Write-Host ("X " + $m)
        Write-Host ""
    }
    Write-Host "Kurulum baslatilmadi. Diske hicbir sey yazilmadi."
    Write-Host "Yukaridakileri kurup bu script'i tekrar calistirin."
    Write-Host "winget yoksa: https://aka.ms/powershell  ve  https://www.python.org/downloads/"
}


function Merge-BeyinHooks {
    <#
    .SYNOPSIS
        Merges hook registrations into settings.json. Idempotent.
    .DESCRIPTION
        The user's existing settings survive. A command already registered is not
        added twice -- upstream's upgrade.sh doubling every event is exactly the
        failure this prevents.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$SettingsPath,
        [Parameter(Mandatory = $true)][string]$TemplatePath
    )

    $template = Get-Content -LiteralPath $TemplatePath -Raw | ConvertFrom-Json
    if (Test-Path -LiteralPath $SettingsPath) {
        $current = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
    }
    else {
        $current = [pscustomobject]@{}
    }

    if (-not $current.PSObject.Properties['hooks']) {
        $current | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]@{})
    }

    foreach ($eventName in $template.hooks.PSObject.Properties.Name) {
        $wanted = @($template.hooks.$eventName)
        $wantedHook = $wanted[0].hooks[0]
        $wantedSignature = $wantedHook | ConvertTo-Json -Compress -Depth 10

        if (-not $current.hooks.PSObject.Properties[$eventName]) {
            $current.hooks | Add-Member -NotePropertyName $eventName -NotePropertyValue @()
        }

        $existing = @($current.hooks.$eventName)
        $already = $false
        foreach ($entry in $existing) {
            foreach ($h in @($entry.hooks)) {
                $signature = $h | ConvertTo-Json -Compress -Depth 10
                if ($signature -eq $wantedSignature) { $already = $true }
            }
        }
        if (-not $already) {
            $current.hooks.$eventName = @($existing + $wanted)
        }
    }

    $json = $current | ConvertTo-Json -Depth 20
    Set-Content -LiteralPath $SettingsPath -Value $json -Encoding utf8NoBOM
}


function Resolve-BeyinPlaceholders {
    <# Replaces every {{KEY}} in the installed vault's .md and .json files. #>
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )
    $files = Get-ChildItem -LiteralPath $Root -Recurse -File -Include *.md, *.json -ErrorAction SilentlyContinue
    foreach ($f in $files) {
        $text = Get-Content -LiteralPath $f.FullName -Raw -ErrorAction SilentlyContinue
        if ([string]::IsNullOrEmpty($text)) { continue }
        if ($text -notmatch '\{\{') { continue }
        foreach ($k in $Values.Keys) {
            $text = $text.Replace('{{' + $k + '}}', [string]$Values[$k])
        }
        Set-Content -LiteralPath $f.FullName -Value $text -Encoding utf8NoBOM
    }
}


# Dot-source with -PreflightOnly to load the functions without running anything.
# The tests rely on this and so does the SETUP-WINDOWS.md runbook.
if ($PreflightOnly) { return }


# ---------------------------------------------------------------------------
# Everything below runs only after the gate passes.
# ---------------------------------------------------------------------------

$report = Invoke-BeyinPreflight -Harness $Harness
Write-BeyinPreflightReport -Report $report
if (-not $report.Ok) { exit 1 }

if ($PSVersionTable.PSVersion.Major -lt 7) {
    # Reachable only when the gate PASSED, i.e. pwsh exists but this script was
    # started with Windows PowerShell. Hand the real work to pwsh.
    $forward = @()
    foreach ($k in $PSBoundParameters.Keys) {
        $v = $PSBoundParameters[$k]
        if ($v -is [switch]) { if ($v.IsPresent) { $forward += "-$k" } }
        else { $forward += @("-$k", [string]$v) }
    }
    & $report.Pwsh -NoProfile -File $PSCommandPath @forward
    exit $LASTEXITCODE
}

foreach ($p in 'VaultPath', 'UserName', 'Companion', 'OsName') {
    if ([string]::IsNullOrWhiteSpace((Get-Variable -Name $p -ValueOnly))) {
        Write-Host "Eksik parametre: -$p"
        exit 2
    }
}

# Never destroy. Installing into an existing vault is out of scope by design.
if (Test-Path -LiteralPath $VaultPath) {
    Write-Host "Hedef zaten var: $VaultPath"
    Write-Host "Mevcut bir vault'a kurulum bu script'in kapsaminda degil. Bkz. README."
    exit 3
}

# MAX_PATH warning. Long-path support is OFF by default on Windows and the limit
# is 260. The compiler builds article filenames from titles, so a deep vault path
# can push them over. Measured: a real vault's longest path was 131 characters,
# so this warns rather than blocks.
$longPaths = 0
try {
    $lp = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction SilentlyContinue
    if ($lp) { $longPaths = $lp.LongPathsEnabled }
}
catch { $longPaths = 0 }
$budget = 260 - $VaultPath.Length - 'knowledge\concepts\'.Length
if ($longPaths -ne 1 -and $budget -lt 80) {
    Write-Host ""
    Write-Host "UYARI: vault yolu derin ($($VaultPath.Length) karakter)."
    Write-Host "  Uzun yol destegi kapali; makale adlarina $budget karakter kaliyor."
    Write-Host "  Daha sig bir yol onerilir, ornegin C:\Users\<ad>\Documents\<OsName>."
    Write-Host ""
}


$repoRoot = Split-Path -Parent $PSScriptRoot
Copy-Item -LiteralPath (Join-Path $repoRoot 'template') -Destination $VaultPath -Recurse

# The shared template keeps the POSIX hook wiring used by SETUP.md and
# upgrade.sh. A native Windows install replaces only that wiring with the
# PowerShell equivalent; the two platforms must never share one settings file.
$windowsSettings = Join-Path $repoRoot 'scripts\settings.windows.json'
Copy-Item -LiteralPath $windowsSettings `
          -Destination (Join-Path $VaultPath '.claude\settings.json') -Force

# Same reasoning for the diagnostic skill. The POSIX beyin-doktor looks for
# .sh hooks, symlinked Codex stores and `command -v python3`; on Windows the
# engine has none of those, so every one of those checks reports red on a
# healthy vault. A doctor that is permanently red teaches the user to ignore
# it, which is worse than having no doctor. Swap in the PowerShell edition
# here, BEFORE the .agents\skills copy below picks .claude\skills up.
Copy-Item -LiteralPath (Join-Path $repoRoot 'scripts\skill.beyin-doktor.windows.md') `
          -Destination (Join-Path $VaultPath '.claude\skills\beyin-doktor\SKILL.md') -Force

Resolve-BeyinPlaceholders -Root $VaultPath -Values @{
    OS_NAME    = $OsName
    USER_NAME  = $UserName
    USER_BIO   = $UserBio
    COMPANION  = $Companion
    VAULT_PATH = $VaultPath
    TODAY      = (Get-Date -Format 'yyyy-MM-dd')
}

# Windows checkouts do not reliably preserve repository symlinks. Materialize
# the same router and skill content for Codex, while .claude remains canonical
# for the executable engine on this platform.
Copy-Item -LiteralPath (Join-Path $VaultPath 'CLAUDE.md') `
          -Destination (Join-Path $VaultPath 'AGENTS.md') -Force
$agentSkills = Join-Path $VaultPath '.agents\skills'
New-Item -ItemType Directory -Force -Path $agentSkills | Out-Null
Copy-Item -Path (Join-Path $VaultPath '.claude\skills\*') `
          -Destination $agentSkills -Recurse -Force

& $report.Python (Join-Path $VaultPath '.claude\scripts\render_codex_hooks.py') `
    --vault $VaultPath --platform windows | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Codex hooks.json üretilemedi.'
}

& $report.Python (Join-Path $VaultPath '.claude\scripts\render_antigravity_hooks.py') `
    --vault $VaultPath --platform windows | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Antigravity hooks.json üretilemedi.'
}

New-Item -ItemType Directory -Force -Path (Join-Path $VaultPath '.claude\scripts\.state') | Out-Null

Merge-BeyinHooks -SettingsPath (Join-Path $VaultPath '.claude\settings.json') `
                 -TemplatePath $windowsSettings

# The runbook is told to resolve every placeholder; this is what proves it did.
$left = @(Get-ChildItem -LiteralPath $VaultPath -Recurse -File -Include *.md, *.json -ErrorAction SilentlyContinue |
          Select-String -Pattern '\{\{[A-Z_]+\}\}' -ErrorAction SilentlyContinue)
if ($left.Count -gt 0) {
    Write-Host ""
    Write-Host "HATA: cozulmemis placeholder kaldi:"
    foreach ($m in $left) { Write-Host "  $($m.Path):$($m.LineNumber)" }
    exit 4
}

Write-Host ""
Write-Host "Kurulum tamam: $VaultPath"
Write-Host "  motor : .claude\scripts  (python: $($report.Python))"
Write-Host "  kanca : 4 adet, .claude\settings.json icinde kayitli"
Write-Host "  codex : AGENTS.md + .agents\skills + .codex\hooks.json"
Write-Host "  antigravity : .agents\hooks.json (ortak motor adaptoru)"
exit 0
