#requires -Version 7.0
<#
    End-to-end proof that a clean install works on native Windows.

    Without this test the repo can only claim the pieces compile. It installs a
    vault from scratch into a temporary directory, then drives the real
    SessionEnd hook with a real payload and waits for the engine to write a
    daily log.

    `claude` is stubbed on PATH so the run costs no quota and no network. That
    is the only stubbed piece: the installer, the hooks, the detach and the
    engine are all the real ones.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$installer = Join-Path $repo 'scripts\install.ps1'

$failures = [System.Collections.Generic.List[string]]::new()
$total = 0
function Assert($name, $condition, $detail) {
    $script:total++
    if ($condition) { Write-Host "  ok   $name" }
    else { Write-Host "  FAIL $name -- $detail"; $script:failures.Add($name) }
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("beyin e2e-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$vault = Join-Path $tmp 'AylinOS'
$binDir = Join-Path $tmp 'bin'
New-Item -ItemType Directory -Force -Path $binDir | Out-Null

Write-Host 'Temiz kurulum uctan uca testi'

try {
    # Stub kurulumdan ONCE hazirlanir: install.ps1'in on kontrolu claude arar,
    # ve CI runner'inda gercek claude yoktur. Boylece uc paket de CI'da kosar.
    $savedPath = $env:PATH
    $env:PATH = "$binDir;$savedPath"
    # ---------------------------------------------------------- claude stub
    # ---------------------------------------------------------- claude stub
    # Ozet metni AYRI bir dosyaya yazilir ve stub yalnizca onu basar. Boylece
    # Turkce karakterler hicbir kacis katmanindan gecmez.
    #
    # Basliklar flush.py'in EXPECTED_SECTIONS'ina (flush.py:34-39) BIREBIR
    # uymak zorunda: ASCII'ye sadelestirilmis bir baslik reddedilir ve daily/
    # altina hicbir sey yazilmaz.
    $summary = @(
        '## Bağlam'
        'Test bağlamı.'
        '## Önemli Konuşmalar'
        '- Test konuşması.'
        '## Alınan Kararlar'
        '- Test kararı.'
        '## Öğrenilenler'
        '- Test öğrenimi.'
        '## Yapılacaklar'
        '- Test işi.'
    )
    Set-Content -LiteralPath (Join-Path $binDir 'summary.txt') -Value $summary -Encoding utf8NoBOM

    $stubPy = Join-Path $binDir 'claude_stub.py'
    Set-Content -LiteralPath $stubPy -Encoding utf8NoBOM -Value @(
        'import os, sys'
        'args = sys.argv[1:]'
        '# Windows varsayilani OEM kod sayfasidir; Turkce basliklar orada bozulur.'
        'sys.stdout.reconfigure(encoding="utf-8")'
        '# --version on kontrolden gelir ve stdin GONDERMEZ; stdin okumak'
        '# burada suresiz bloklar. Olculdu: test 5 dakika asildi.'
        'if "--version" in args:'
        '    print("0.0.0-stub (Claude Code)")'
        '    raise SystemExit(0)'
        'sys.stdin.read()'
        'here = os.path.dirname(os.path.abspath(__file__))'
        'with open(os.path.join(here, "summary.txt"), encoding="utf-8") as fh:'
        '    sys.stdout.write(fh.read())'
    )
    Set-Content -LiteralPath (Join-Path $binDir 'claude.cmd') -Encoding ascii -Value @"
@echo off
python "%~dp0claude_stub.py" %*
"@

    # ---------------------------------------------------------------- install
    & pwsh -NoProfile -File $installer `
        -VaultPath $vault -UserName 'Aylin' -UserBio 'Urun tasarimcisi' `
        -Companion 'Echo' -OsName 'AylinOS' | Out-Null
    $installExit = $LASTEXITCODE
    Assert 'kurulum_sifir_ile_biter' ($installExit -eq 0) "cikis kodu: $installExit"

    foreach ($p in '.claude\scripts\flush.py', '.claude\scripts\compile.py',
        '.claude\scripts\antigravity_hooks.py', '.claude\scripts\render_antigravity_hooks.py',
        '.claude\scripts\graf_kontrol.py',
        '.claude\scripts\_portalock.py', '.claude\hooks\lib.ps1',
        '.claude\hooks\session-end.ps1', '.claude\scripts\.state',
        '.claude\settings.json', '.agents\hooks.json', 'daily', 'knowledge\index.md') {
        Assert "kuruldu_$($p -replace '[\\.]', '_')" (Test-Path -LiteralPath (Join-Path $vault $p)) "eksik: $p"
    }

    $left = @(Get-ChildItem -LiteralPath $vault -Recurse -File -Include *.md, *.json -ErrorAction SilentlyContinue |
        Select-String -Pattern '\{\{[A-Z_]+\}\}' -ErrorAction SilentlyContinue)
    Assert 'placeholder_kalmadi' ($left.Count -eq 0) "kalan: $($left.Count)"

    $settings = Get-Content -LiteralPath (Join-Path $vault '.claude\settings.json') -Raw | ConvertFrom-Json
    $events = @($settings.hooks.PSObject.Properties.Name)
    Assert 'dort_kanca_kayitli' ($events.Count -eq 4) "kanca sayisi: $($events.Count)"
    $handlers = @($settings.hooks.PSObject.Properties | ForEach-Object { $_.Value[0].hooks[0] })
    $execFormOk = @($handlers | Where-Object {
        $_.command -eq 'pwsh.exe' -and @($_.args).Count -eq 5 -and $_.args[4] -match '^\$\{CLAUDE_PROJECT_DIR\}/.+\.ps1$'
    }).Count -eq 4
    Assert 'kancalar_exec_form_kullaniyor' $execFormOk 'pwsh.exe ve args dizisi bekleniyordu'

    # --------------------------------------------------- windows doktor skill
    # The POSIX skill greps for .sh hooks and `command -v python3`; shipped
    # unchanged it reports red on a healthy Windows vault. Both stores must
    # carry the PowerShell edition, and .agents is a copy rather than a
    # symlink here, so it is checked separately instead of assumed.
    foreach ($store in '.claude\skills', '.agents\skills') {
        $skill = Join-Path $vault "$store\beyin-doktor\SKILL.md"
        if (-not (Test-Path -LiteralPath $skill)) {
            Assert "doktor_skill_var_$($store -replace '[\\.]', '_')" $false "eksik: $skill"
            continue
        }
        # Positive marker only. The Windows edition documents what it changed,
        # so it quotes `chmod +x` and `hooks/*.sh` too -- a negative match on
        # those passes for the wrong reason. `Get-BeyinPython` is the engine
        # function the POSIX edition has no reason to name.
        $body = Get-Content -LiteralPath $skill -Raw
        Assert "doktor_skill_windows_$($store -replace '[\\.]', '_')" `
            ($body -match 'Get-BeyinPython') "POSIX surumu kurulmus: $store"
    }

    # ------------------------------------------------------- idempotent merge
    . $installer -PreflightOnly
    Merge-BeyinHooks -SettingsPath (Join-Path $vault '.claude\settings.json') `
        -TemplatePath (Join-Path $repo 'scripts\settings.windows.json')
    $again = Get-Content -LiteralPath (Join-Path $vault '.claude\settings.json') -Raw | ConvertFrom-Json
    Assert 'birlestirme_idempotent' (@($again.hooks.SessionStart).Count -eq 1) `
        "SessionStart tekrarlandi: $(@($again.hooks.SessionStart).Count)"


    # ------------------------------------------------------------ transcript
    $transcript = Join-Path $tmp 'transcript.jsonl'
    $lines = foreach ($i in 1..6) {
        (@{ type = 'user'; message = @{ role = 'user'; content = "kullanici mesaji $i" } } | ConvertTo-Json -Compress)
        (@{ type = 'assistant'; message = @{ role = 'assistant'; content = "asistan cevabi $i" } } | ConvertTo-Json -Compress)
    }
    Set-Content -LiteralPath $transcript -Value $lines -Encoding utf8NoBOM

    $payload = @{
        session_id      = 'e2e-clean-install'
        transcript_path = $transcript
        hook_event_name = 'SessionEnd'
    } | ConvertTo-Json -Compress

    # ---------------------------------------------------- drive the real hook
    # PATH yukarida kuruldu
    $savedProject = $env:CLAUDE_PROJECT_DIR
    try {

        $env:CLAUDE_PROJECT_DIR = $vault

        $payload | & pwsh -NoProfile -File (Join-Path $vault '.claude\hooks\session-end.ps1') | Out-Null
        $hookExit = $LASTEXITCODE
        Assert 'sessionend_kancasi_sifir_ile_biter' ($hookExit -eq 0) "cikis kodu: $hookExit"

        # The summarizer is detached on purpose, so poll rather than assume.
        # How long it needs is a property of the MACHINE, not of the code: on a
        # developer box it lands in a second or two, on a GitHub windows runner
        # Defender scans every process launch and 60 seconds was not enough
        # (measured -- the job's orphan-process cleanup was still terminating
        # live python processes afterwards). Same scale knob as the engine suite.
        $scale = 1.0
        if ($env:BEYIN_TEST_TIMEOUT_SCALE) {
            [double]::TryParse($env:BEYIN_TEST_TIMEOUT_SCALE,
                [System.Globalization.NumberStyles]::Float,
                [System.Globalization.CultureInfo]::InvariantCulture, [ref]$scale) | Out-Null
        }
        $waitSeconds = [int](60 * $scale)
        $deadline = (Get-Date).AddSeconds($waitSeconds)
        $wrote = $false
        while ((Get-Date) -lt $deadline) {
            if (@(Get-ChildItem -LiteralPath (Join-Path $vault 'daily') -Filter '*.md' -ErrorAction SilentlyContinue).Count -gt 0) {
                $wrote = $true; break
            }
            Start-Sleep -Milliseconds 500
        }
        Assert 'motor_daily_altina_yazdi' $wrote "daily/ $waitSeconds saniyede bos kaldi"

        if ($wrote) {
            $log = @(Get-ChildItem -LiteralPath (Join-Path $vault 'daily') -Filter '*.md')[0]
            $body = Get-Content -LiteralPath $log.FullName -Raw
            Assert 'gunluk_log_bes_baslik_iceriyor' `
                ($body -match 'Bağlam' -and $body -match 'Alınan Kararlar' -and $body -match 'Yapılacaklar') `
                'log govdesi beklenen Turkce basliklari tasimiyor'
        }

        $errFile = Join-Path $vault '.claude\scripts\.state\flush_last_error'
        Assert 'flush_hatasi_yok' (-not (Test-Path -LiteralPath $errFile)) `
            "flush_last_error yazilmis: $(if (Test-Path -LiteralPath $errFile) { Get-Content -LiteralPath $errFile -Raw })"

        $pyMissing = Join-Path $vault '.claude\scripts\.state\python3-missing'
        Assert 'python_eksik_isareti_yok' (-not (Test-Path -LiteralPath $pyMissing)) 'python3-missing yazilmis'
    }
    finally {
        $env:PATH = $savedPath
        if ($null -eq $savedProject) { Remove-Item Env:\CLAUDE_PROJECT_DIR -ErrorAction SilentlyContinue }
        else { $env:CLAUDE_PROJECT_DIR = $savedProject }
    }
}
finally {
    Start-Sleep -Milliseconds 500
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
if ($failures.Count -gt 0) {
    Write-Host "BASARISIZ: $($failures.Count)/$total -- $($failures -join ', ')"
    exit 1
}
Write-Host "TAMAM: $total kontrol gecti -- temiz kurulum uctan uca calisiyor"
exit 0
