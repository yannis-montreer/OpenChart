<#
.SYNOPSIS
    Build the Windows release: the server folder (for the service) and the viewer .exe.

.DESCRIPTION
    Needs: the web UI built (frontend: npm ci; npm run build), and a Python with the
    project, PyInstaller and pywebview installed:
        python -m pip install . pyinstaller pywebview packaging
    Output (all under dist\, nothing installed or started outside it):
        dist\OpenChart-Server-<version>-windows.zip   server, service scripts, nssm.exe
        dist\OpenChart-Viewer-<version>-windows.zip   OpenChart-Viewer.exe
        dist\SHA256SUMS.txt
    Both zips carry LICENSE, NOTICE and THIRD-PARTY-LICENSES.txt. The frozen server is
    smoke-tested on a temporary data folder and port (no broker credentials).

.EXAMPLE
    .\packaging\build-windows.ps1 -Python .\buildenv\Scripts\python.exe -Nssm C:\tools\nssm-2.24\win64\nssm.exe
#>
param(
    [string]$Python = "python",
    [string]$Nssm = "",
    [string]$Version = "",
    [string]$WebView2Licence = "",   # LICENSE.txt of the Microsoft.Web.WebView2 NuGet package
    [int]$SmokePort = 8798
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3.0
$Root = Split-Path $PSScriptRoot -Parent
$Dist = Join-Path $Root "dist"
$Work = Join-Path $Dist ".build"

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Run {
    param([string]$Program, [Parameter(ValueFromRemainingArguments = $true)][string[]]$ProgramArgs)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Program @ProgramArgs 2>&1 | ForEach-Object { "$_" } | Write-Host
    $code = $LASTEXITCODE
    $ErrorActionPreference = $saved
    if ($code -ne 0) { throw "$Program $($ProgramArgs -join ' ') failed (exit $code)" }
}

if (-not (Test-Path (Join-Path $Root "src\openchart\web\index.html"))) {
    throw "Build the web UI first: cd frontend; npm ci; npm run build"
}
if (-not $Version) {
    $Version = (& $Python -c "import openchart; print(openchart.__version__)").Trim()
}
$ServerName = "OpenChart-Server-$Version-windows"
$ViewerName = "OpenChart-Viewer-$Version-windows"
New-Item -ItemType Directory -Force -Path $Work | Out-Null

Step "Licences of the bundled software"
$serverLic = Join-Path $Work "licenses\server"
$viewerLic = Join-Path $Work "licenses\viewer"
$serverArgs = @((Join-Path $PSScriptRoot "third_party_licenses.py"), "--root", "openchart", "--out", (Join-Path $serverLic "THIRD-PARTY-LICENSES.txt"))
if ($Nssm) {
    # NSSM states its licence in its README: keep that section.
    $readme = Join-Path (Split-Path (Split-Path $Nssm)) "README.txt"
    if (-not (Test-Path $readme)) { throw "NSSM's README.txt (its licence) was not found next to $Nssm" }
    $lines = Get-Content $readme
    $at = [array]::IndexOf($lines, ($lines | Where-Object { $_ -match "^Licen[cs]e\s*$" } | Select-Object -First 1))
    $nssmLicence = Join-Path $Work "nssm-licence.txt"
    $lines[$at..([math]::Min($at + 8, $lines.Count - 1))] | Set-Content -Encoding ascii $nssmLicence
    $serverArgs += @("--add", "NSSM (nssm.exe, the service manager)|2.24|Public domain|$nssmLicence")
}
Run $Python @serverArgs
$viewerArgs = @((Join-Path $PSScriptRoot "third_party_licenses.py"), "--root", "pywebview", "--no-frontend", "--out", (Join-Path $viewerLic "THIRD-PARTY-LICENSES.txt"))
if ($WebView2Licence) {
    $viewerArgs += @("--add", "Microsoft Edge WebView2 SDK (WebView2Loader.dll, Microsoft.Web.WebView2.*.dll, bundled by pywebview)|see licence|BSD-3-Clause (Microsoft)|$WebView2Licence")
} else {
    Write-Host "   No -WebView2Licence given: the WebView2 SDK licence text is not added (the release workflow adds it)." -ForegroundColor Yellow
}
Run $Python @viewerArgs

Step "PyInstaller: server"
$env:OPENCHART_LICENSES_DIR = $serverLic
Run $Python -m PyInstaller --noconfirm --clean --log-level WARN --distpath (Join-Path $Work "pyinstaller") --workpath (Join-Path $Work "pyinstaller-work") (Join-Path $PSScriptRoot "pyinstaller\openchart-server.spec")
Step "PyInstaller: viewer"
$env:OPENCHART_LICENSES_DIR = $viewerLic
Run $Python -m PyInstaller --noconfirm --clean --log-level WARN --distpath (Join-Path $Work "pyinstaller") --workpath (Join-Path $Work "pyinstaller-work") (Join-Path $PSScriptRoot "pyinstaller\openchart-viewer.spec")
Remove-Item Env:OPENCHART_LICENSES_DIR

Step "Release folders"
$serverDir = Join-Path $Dist $ServerName
$viewerDir = Join-Path $Dist $ViewerName
foreach ($d in $serverDir, $viewerDir) { if (Test-Path $d) { Remove-Item $d -Recurse -Force }; New-Item -ItemType Directory $d | Out-Null }
Copy-Item (Join-Path $Work "pyinstaller\openchart") (Join-Path $serverDir "openchart") -Recurse
foreach ($f in "install-service.ps1", "install-service.cmd", "uninstall-service.ps1", "uninstall-service.cmd") {
    Copy-Item (Join-Path $PSScriptRoot "windows\$f") $serverDir
}
if ($Nssm) { Copy-Item $Nssm (Join-Path $serverDir "nssm.exe") } else { Write-Host "   No -Nssm given: nssm.exe is not in the server zip." -ForegroundColor Yellow }
Copy-Item (Join-Path $serverLic "THIRD-PARTY-LICENSES.txt") $serverDir
Copy-Item (Join-Path $PSScriptRoot "windows\README-server.txt") (Join-Path $serverDir "README.txt")
Copy-Item (Join-Path $Work "pyinstaller\OpenChart-Viewer.exe") $viewerDir
Copy-Item (Join-Path $viewerLic "THIRD-PARTY-LICENSES.txt") $viewerDir
foreach ($d in $serverDir, $viewerDir) {
    foreach ($f in "LICENSE", "NOTICE") { Copy-Item (Join-Path $Root $f) (Join-Path $d "$f.txt") }
}

Step "Smoke test of the frozen server (temporary data folder, port $SmokePort, no broker)"
$exe = Join-Path $serverDir "openchart\openchart.exe"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("openchart-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory $tmp | Out-Null
Run $exe --version
Run $exe self-test
Run $exe sandbox check --data-dir $tmp   # the indicator worker of the frozen build, as this user
$env:OPENCHART_SECRET_KEY = (& $exe secrets new-key).Trim()   # the file store, as in the service
$log = Join-Path $tmp "server.log"
$proc = Start-Process $exe -ArgumentList @("run", "--data-dir", "`"$tmp`"", "--port", "$SmokePort") -PassThru -WindowStyle Hidden -RedirectStandardError $log -RedirectStandardOutput (Join-Path $tmp "out.log")
try {
    $ok = $false
    foreach ($i in 1..60) {
        Start-Sleep -Seconds 1
        try { if ((Invoke-WebRequest "http://127.0.0.1:$SmokePort/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { $ok = $true; break } } catch { }
    }
    if (-not $ok) { Get-Content $log | Write-Host; throw "The frozen server did not start." }
    $page = Invoke-WebRequest "http://127.0.0.1:$SmokePort/" -UseBasicParsing
    if ($page.Content -notmatch "<div id=`"app`"") { throw "The web page is missing from the build." }
    $setup = Invoke-RestMethod "http://127.0.0.1:$SmokePort/api/setup/status"
    $logText = Get-Content $log -Raw
    if ($logText -notmatch "indicators: ema") { throw "The built-in EMA indicator was not loaded." }
    if ($logText -notmatch "secrets: encrypted file") { throw "The encrypted secrets file was not used." }
    foreach ($i in 1..30) {
        if ($logText -match "indicator sandbox: ") { break }
        Start-Sleep -Seconds 1
        $logText = Get-Content $log -Raw
    }
    $sandbox = [regex]::Match($logText, "indicator sandbox: [^\r\n]*").Value
    if ($sandbox -notmatch "ready \(restricted token\)") { throw "The indicator worker did not start: $sandbox" }
    Write-Host "   health ok, web page served, setup required: $($setup.setup_required), EMA loaded, secrets file used, indicator worker ready" -ForegroundColor Green
} finally {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Remove-Item Env:OPENCHART_SECRET_KEY -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
foreach ($f in "LICENSE.txt", "NOTICE.txt", "THIRD-PARTY-LICENSES.txt") {
    foreach ($d in $serverDir, $viewerDir) { if (-not (Test-Path (Join-Path $d $f))) { throw "$f missing in $d" } }
}

Step "Zips"
$sums = @()
foreach ($name in $ServerName, $ViewerName) {
    $zip = Join-Path $Dist "$name.zip"
    if (Test-Path $zip) { Remove-Item $zip }
    Compress-Archive -Path (Join-Path $Dist $name) -DestinationPath $zip -CompressionLevel Optimal
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    $sums += "$hash  $name.zip"
    Write-Host ("   {0} ({1:N1} MB)" -f $zip, ((Get-Item $zip).Length / 1MB))
}
$sums | Set-Content -Encoding ascii (Join-Path $Dist "SHA256SUMS.txt")
Write-Host "`nDone." -ForegroundColor Green
