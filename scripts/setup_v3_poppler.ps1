# CI-only PDF dependency. oschwartz10612 packages Poppler for Windows; it is not
# the upstream Poppler publisher. Pin the release archive and its GitHub asset digest.
# Git for Windows' bundled Xpdf 4.06 crashes intermittently on valid PDF fixtures.
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or -not $env:RUNNER_TEMP -or -not $env:GITHUB_PATH) {
    throw 'This setup requires a Windows GitHub Actions runner.'
}
$archiveUrl = 'https://github.com/oschwartz10612/poppler-windows/releases/download/v26.09.0-0/Release-26.09.0-0.zip'
$expectedSha256 = '7a6f256a0ddf7536182246a5733331bf4677cbcc34f4663774947ad34556c8d0'
$installRoot = Join-Path $env:RUNNER_TEMP ('beyin-poppler-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $installRoot | Out-Null
$archive = Join-Path $installRoot 'poppler.zip'
Invoke-WebRequest -Uri $archiveUrl -OutFile $archive -TimeoutSec 120
$actualSha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
if ($actualSha256 -ne $expectedSha256) {
    throw "Poppler archive SHA256 mismatch: $actualSha256"
}
Expand-Archive -LiteralPath $archive -DestinationPath $installRoot
$bin = (Resolve-Path -LiteralPath (Join-Path $installRoot 'poppler-26.09.0/Library/bin')).Path
$executable = Join-Path $bin 'pdftotext.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw 'Pinned Poppler archive does not contain the expected pdftotext.exe.'
}
$executable = (Get-Item -LiteralPath $executable).FullName
$version = (& $executable -v 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0 -or $version -notmatch 'pdftotext version 26\.09\.0') {
    throw "Unexpected pinned Poppler version: $version"
}
# Set this process and subsequent workflow steps, ahead of Git's bundled Xpdf.
$env:PATH = $bin + [IO.Path]::PathSeparator + $env:PATH
if ((Get-Command pdftotext -CommandType Application).Source -ne $executable) {
    throw "Pinned Poppler did not take precedence on PATH: $((Get-Command pdftotext -CommandType Application).Source) != $executable"
}
Add-Content -LiteralPath $env:GITHUB_PATH -Value $bin -Encoding utf8
Write-Host "Verified Poppler 26.09.0 ($actualSha256): $executable"
