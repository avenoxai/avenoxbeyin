#requires -Version 7.0
<#
    Inject relational memory, rules, recent journal context, and the knowledge
    index. Windows port of session-start.sh.

    Every extraction rule, line limit, character cap and trim order below is
    taken from the bash original. Where a bash idiom has no direct PowerShell
    equivalent (awk ranges, sed address ranges) the comment names the line it
    replaces so the two can be diffed by hand.
#>

Set-StrictMode -Version Latest

# session-start.sh:2
if (-not [string]::IsNullOrEmpty($env:BEYIN_INVOKED_BY)) { exit 0 }

# session-start.sh:5-6
. (Join-Path $PSScriptRoot 'lib.ps1')

# session-start.sh:9-10
New-Item -ItemType Directory -Force -Path $script:BeyinStateDir -ErrorAction SilentlyContinue | Out-Null
Clear-BeyinSessionState

# session-start.sh:12-18 -- a fresh session resets its own start time and count,
# unconditionally. Both files are keyed by session, so this cannot clobber a
# concurrent session's state.
$raw = [Console]::In.ReadToEnd()
$key = Get-BeyinSessionKey -RawPayload $raw
if (-not [string]::IsNullOrEmpty($key)) {
    try {
        Set-Content -LiteralPath (Join-Path $script:BeyinStateDir "session_start_time.$key") `
            -Value ([System.DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) -Encoding utf8NoBOM -ErrorAction SilentlyContinue
        Set-Content -LiteralPath (Join-Path $script:BeyinStateDir "prompt_count.$key") `
            -Value 0 -Encoding utf8NoBOM -ErrorAction SilentlyContinue
    }
    catch { }
}


function Get-BeyinFileLines {
    <# Read a file as an array of lines; empty array when missing or unreadable. #>
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @() }
    try { return @(Get-Content -LiteralPath $Path -ErrorAction Stop) }
    catch { return @() }
}


# session-start.sh:20-27 -- awk prints from the first '## Session:' line and
# stops at '## Previous', then sed keeps the first 50 lines. The '## Session:'
# line itself is included; the '## Previous' line is not.
$lastSession = ''
$lastSessionPath = Join-Path $script:BeyinMemoryDir 'Last-Session.md'
$lines = Get-BeyinFileLines -Path $lastSessionPath
if ($lines.Count) {
    $kept = [System.Collections.Generic.List[string]]::new()
    $active = $false
    foreach ($line in $lines) {
        if ($line -match '^## Session:') { $active = $true }
        if ($active -and $line -match '^## Previous') { break }
        if ($active) { $kept.Add($line) }
    }
    $lastSession = ($kept | Select-Object -First 50) -join "`n"
}

# session-start.sh:29-34 -- the '## Active'..'## Closed' range, then only the
# '### ' headings and '**Status:**' lines, then the first 12 of those.
$threads = ''
$threadsPath = Join-Path $script:BeyinMemoryDir 'Threads.md'
$lines = Get-BeyinFileLines -Path $threadsPath
if ($lines.Count) {
    $inRange = $false
    $picked = [System.Collections.Generic.List[string]]::new()
    foreach ($line in $lines) {
        if (-not $inRange -and $line -match '^## Active') { $inRange = $true }
        if ($inRange) {
            if ($line -match '^### ' -or $line -match '^\*\*Status:\*\*') { $picked.Add($line) }
            # sed's range is inclusive of the closing address, but the closing
            # line is '## Closed' and never matches the grep above, so stopping
            # here is equivalent.
            if ($line -match '^## Closed') { break }
        }
    }
    $threads = ($picked | Select-Object -First 12) -join "`n"
}

# session-start.sh:36-39 -- first 60 lines.
$rules = ''
$rulesPath = Join-Path $script:BeyinMemoryDir 'Kurallar.md'
$lines = Get-BeyinFileLines -Path $rulesPath
if ($lines.Count) {
    $rules = ($lines | Select-Object -First 60) -join "`n"
}

# session-start.sh:41-53 -- the LAST '## ' heading and the nine lines after it.
$journal = ''
$journalPath = Join-Path $script:BeyinMemoryDir 'Journal.md'
$lines = Get-BeyinFileLines -Path $journalPath
if ($lines.Count) {
    $lastHeading = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^## ') { $lastHeading = $i }
    }
    if ($lastHeading -ge 0) {
        $end = [Math]::Min($lastHeading + 9, $lines.Count - 1)
        $journal = ($lines[$lastHeading..$end]) -join "`n"
    }
}

# session-start.sh:55-58 -- first 150 lines.
$index = ''
$indexPath = Join-Path $script:BeyinProjectDir 'knowledge\index.md'
$lines = Get-BeyinFileLines -Path $indexPath
if ($lines.Count) {
    $index = ($lines | Select-Object -First 150) -join "`n"
}

# session-start.sh:60-71 -- today's daily log, else YESTERDAY's. The fallback is
# not optional: on a day with no session yet, it is the only recent context.
$daily = ''
$dailyFile = ''
$todayPath = Join-Path $script:BeyinProjectDir ("daily\{0}.md" -f (Get-Date -Format 'yyyy-MM-dd'))
if (Test-Path -LiteralPath $todayPath -PathType Leaf) {
    $dailyFile = $todayPath
}
else {
    $yesterdayPath = Join-Path $script:BeyinProjectDir ("daily\{0}.md" -f (Get-BeyinYesterday))
    if (Test-Path -LiteralPath $yesterdayPath -PathType Leaf) { $dailyFile = $yesterdayPath }
}
if ($dailyFile) {
    try { $daily = (Get-Content -LiteralPath $dailyFile -Tail 25 -ErrorAction Stop) -join "`n" }
    catch { $daily = '' }
}

# session-start.sh:73-87 -- the bare marker plus every per-session one. Each is
# read, turned into a warning, and DELETED: debt is shown once, then cleared.
$reflectionParts = [System.Collections.Generic.List[string]]::new()
$reflectionFiles = [System.Collections.Generic.List[string]]::new()
$reflectionFiles.Add((Join-Path $script:BeyinStateDir 'needs_reflection'))
foreach ($f in @(Get-ChildItem -LiteralPath $script:BeyinStateDir -File -Filter 'needs_reflection.*' -ErrorAction SilentlyContinue)) {
    $reflectionFiles.Add($f.FullName)
}
foreach ($path in $reflectionFiles) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
    $detail = ''
    try { $detail = "$(Get-Content -LiteralPath $path -TotalCount 1 -ErrorAction Stop)".Trim() } catch { }
    if (-not [string]::IsNullOrEmpty($detail)) {
        $reflectionParts.Add("⚠️ Önceki oturum hafıza güncellemeden bitti: $detail. Anlamlı bir şey olduysa 🔮 850-Companion dosyalarını güncelle.")
    }
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
}
$reflection = $reflectionParts -join "`n"


function Limit-BeyinSection {
    <#
    .SYNOPSIS
        session-start.sh:91-103 -- beyin_cap_section.
    .DESCRIPTION
        Hard per-section cap. Over the limit, the text is cut so that the value
        plus a newline plus the note fits, and the note says it was cut.
        Length is counted in UTF-16 units here versus characters in bash; at
        these limits the difference cannot change the outcome.
    #>
    param(
        [string]$Value,
        [Parameter(Mandatory)][int]$Limit,
        [Parameter(Mandatory)][string]$Note
    )
    if ([string]::IsNullOrEmpty($Value)) { return '' }
    if ($Value.Length -le $Limit) { return $Value }
    $keep = $Limit - $Note.Length - 1
    if ($keep -lt 0) { $keep = 0 }
    return ($Value.Substring(0, [Math]::Min($keep, $Value.Length)) + "`n" + $Note)
}

# session-start.sh:105-114
$lastSession = Limit-BeyinSection -Value $lastSession -Limit 4000 -Note '[not: son oturum 4.000 karakterde kırpıldı, beyin-doktor çalıştır]'
$threads     = Limit-BeyinSection -Value $threads     -Limit 2000 -Note '[not: aktif konular 2.000 karakterde kırpıldı, beyin-doktor çalıştır]'
$rules       = Limit-BeyinSection -Value $rules       -Limit 4000 -Note '[not: kurallar 4.000 karakterde kırpıldı, beyin-doktor çalıştır]'
$journal     = Limit-BeyinSection -Value $journal     -Limit 1500 -Note '[not: son Journal 1.500 karakterde kırpıldı, beyin-doktor çalıştır]'
$reflection  = Limit-BeyinSection -Value $reflection  -Limit 1000 -Note '[not: hafıza uyarıları 1.000 karakterde kırpıldı, beyin-doktor çalıştır]'

# session-start.sh:116-120
$truncated = $false
$closing = @'
[Hafıza] Süreklilik senin sorumluluğun. Bu kullanıcı için kim olduğunu anlamak üzere 🔮 850-Companion/Core.md dosyasını oku.
Hafıza protokolü zorunludur.
'@.TrimEnd("`r", "`n")
$truncationNote = '[not: indeks kırpıldı, beyin-doktor çalıştır]'
$capDiagnostic = 'Beyin uyarısı: Oturum başlangıç bağlamı 16.000 karakter sınırına sığmadı. Bölüm limitlerini kontrol etmek için beyin-doktor çalıştır.'

function Build-BeyinContext {
    <# session-start.sh:122-133 -- section order is part of the contract. #>
    $sb = [System.Text.StringBuilder]::new()
    if ($reflection)  { [void]$sb.Append("$reflection`n`n") }
    if ($lastSession) { [void]$sb.Append("[Hafıza: Son Oturum]`n$lastSession`n`n") }
    if ($threads)     { [void]$sb.Append("[Hafıza: Aktif Konular]`n$threads`n`n") }
    if ($rules)       { [void]$sb.Append("[Hafıza: Kurallar]`n$rules`n`n") }
    if ($journal)     { [void]$sb.Append("[Hafıza: Son Journal]`n$journal`n`n") }
    if ($index)       { [void]$sb.Append("[Bilgi Tabanı: İndeks]`n$index`n`n") }
    if ($daily)       { [void]$sb.Append("[Bugünün Logu]`n$daily`n`n") }
    if ($truncated)   { [void]$sb.Append("$truncationNote`n`n") }
    [void]$sb.Append($closing)
    return $sb.ToString()
}

$budget = 16000
$context = Build-BeyinContext

# session-start.sh:136-183 -- over budget, sections are given up in a fixed
# order: index first, then daily, then journal, then reflection. Last-Session,
# Threads, Kurallar and the closing block are protected and never trimmed.
if ($context.Length -gt $budget) {
    $truncated = $true
    $context = Build-BeyinContext

    $over = $context.Length - $budget
    if ($over -gt 0 -and $index) {
        if ($over -ge $index.Length) { $index = '' }
        else { $index = $index.Substring(0, $index.Length - $over) }
        $context = Build-BeyinContext
    }

    $over = $context.Length - $budget
    if ($over -gt 0 -and $daily) {
        # Note the asymmetry, and that it is upstream's: the daily log is cut
        # from the FRONT so the most recent lines survive.
        if ($over -ge $daily.Length) { $daily = '' }
        else { $daily = $daily.Substring($over) }
        $context = Build-BeyinContext
    }

    $over = $context.Length - $budget
    if ($over -gt 0 -and $journal) {
        if ($over -ge $journal.Length) { $journal = '' }
        else { $journal = $journal.Substring(0, $journal.Length - $over) }
        $context = Build-BeyinContext
    }

    $over = $context.Length - $budget
    if ($over -gt 0 -and $reflection) {
        if ($over -ge $reflection.Length) { $reflection = '' }
        else { $reflection = $reflection.Substring(0, $reflection.Length - $over) }
        $context = Build-BeyinContext
    }
}

# session-start.sh:185-187 -- still over after giving up everything optional:
# say so instead of injecting a broken context.
if ($context.Length -gt $budget) { $context = $capDiagnostic }

# session-start.sh:189
if (-not [string]::IsNullOrEmpty($context)) {
    Write-BeyinEmit -Event 'SessionStart' -Text $context
}

# session-start.sh catch-up parity: after context is emitted, ask flush.py to
# compile only completed days. The child is detached so it cannot delay or
# contaminate the hook JSON written above.
try {
    $python = Get-BeyinPython
    $flush = Join-Path $script:BeyinProjectDir '.claude\scripts\flush.py'
    if ($python -and (Test-Path -LiteralPath $flush -PathType Leaf)) {
        Start-BeyinPythonDetached -Python $python `
            -Arguments @($flush, '--maybe-compile') `
            -WorkingDirectory $script:BeyinProjectDir
    }
}
catch { }

exit 0
