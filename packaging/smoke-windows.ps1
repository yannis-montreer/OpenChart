<#
.SYNOPSIS
    Smoke test of the frozen server: a temporary data folder and port, no broker
    credentials. Called by build-windows.ps1, and by CI as a step of its own (so a failure
    here never hides the service test, which needs only the build).

.DESCRIPTION
    Checks the version, self-test, the indicator sandbox (sandbox check), that the server
    answers and serves the web page, the built-in EMA, the encrypted secrets file, and the
    indicator worker. Run by hand the release build is below the minimum isolation and must
    say so. It gets the restricted token; only where that worker was seen to die at start
    with STATUS_DLL_INIT_FAILED (GitHub's runner, CI-1) the job-limited process-only worker
    is accepted, and the server must have logged that very fallback (owner's decision A1).
    Either way the "weaker isolation" notice must show. A service never falls back: that is
    checked by tools\ci\service-reinstall-test.ps1 and the D7 (a) tests.

.EXAMPLE
    .\packaging\smoke-windows.ps1 -ServerDir dist\OpenChart-Server-0.1.0-windows
#>
param(
    [Parameter(Mandatory)] [string]$ServerDir,
    [int]$Port = 8798
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3.0

function Run {
    param([string]$Program, [Parameter(ValueFromRemainingArguments = $true)][string[]]$ProgramArgs)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Program @ProgramArgs 2>&1 | ForEach-Object { "$_" } | Write-Host
    $code = $LASTEXITCODE
    $ErrorActionPreference = $saved
    if ($code -ne 0) { throw "$Program $($ProgramArgs -join ' ') failed (exit $code)" }
}

Write-Host "== Smoke test of the frozen server (temporary data folder, port $Port, no broker)" -ForegroundColor Cyan
# The one fallback A1 allows, exactly as the server logs it.
$fallback = "indicator sandbox fallback: restricted: the worker ended before it was ready: exit code 0xc0000142 STATUS_DLL_INIT_FAILED"
$exe = Join-Path (Resolve-Path $ServerDir) "openchart\openchart.exe"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("openchart-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory $tmp | Out-Null
Run $exe --version
Run $exe self-test
Run $exe sandbox check --data-dir $tmp   # the indicator worker of the frozen build, as this user
$env:OPENCHART_SECRET_KEY = (& $exe secrets new-key).Trim()   # the file store, as in the service
$log = Join-Path $tmp "server.log"
$proc = Start-Process $exe -ArgumentList @("run", "--data-dir", "`"$tmp`"", "--port", "$Port") -PassThru -WindowStyle Hidden -RedirectStandardError $log -RedirectStandardOutput (Join-Path $tmp "out.log")
try {
    $ok = $false
    foreach ($i in 1..60) {
        Start-Sleep -Seconds 1
        try { if ((Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { $ok = $true; break } } catch { }
    }
    if (-not $ok) { Get-Content $log | Write-Host; throw "The frozen server did not start." }
    $page = Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing
    if ($page.Content -notmatch "<div id=`"app`"") { throw "The web page is missing from the build." }
    $setup = Invoke-RestMethod "http://127.0.0.1:$Port/api/setup/status"
    $logText = Get-Content $log -Raw
    foreach ($id in "ema", "sma", "rsi", "macd", "bollinger", "atr", "stochastic", "volume") {
        if ($logText -notmatch "built-in indicators: .*$id") { throw "The built-in indicator $id was not loaded." }
    }
    if ($logText -notmatch "secrets: encrypted file") { throw "The encrypted secrets file was not used." }
    foreach ($i in 1..30) {
        if ($logText -match "indicator sandbox: ") { break }
        Start-Sleep -Seconds 1
        $logText = Get-Content $log -Raw
    }
    $sandbox = [regex]::Match($logText, "indicator sandbox: [^\r\n]*").Value
    Write-Host "   $sandbox"
    $notice = "; weaker isolation: this install cannot isolate custom code"
    if ($sandbox -match "ready \(restricted token\)$([regex]::Escape($notice))") {
        $got = "restricted token"
    } elseif ($sandbox -match "ready \(process only\)$([regex]::Escape($notice))" -and $logText.Contains($fallback)) {
        $got = "process only, after the restricted worker died with STATUS_DLL_INIT_FAILED (A1)"
    } else {
        throw "Expected 'ready (restricted token)', or 'ready (process only)' after exactly the STATUS_DLL_INIT_FAILED fallback, with the weaker-isolation notice; got: $sandbox"
    }
    Write-Host "   health ok, web page served, setup required: $($setup.setup_required), EMA loaded, secrets file used, indicator worker: $got, with its notice" -ForegroundColor Green
} finally {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Remove-Item Env:OPENCHART_SECRET_KEY -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
