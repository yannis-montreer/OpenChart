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
    smoke-tested on a temporary data folder and port (no broker credentials;
    packaging\smoke-windows.ps1).

.EXAMPLE
    .\packaging\build-windows.ps1 -Python .\buildenv\Scripts\python.exe -Nssm C:\tools\nssm-2.24\win64\nssm.exe
#>
param(
    [string]$Python = "python",
    [string]$Nssm = "",
    [string]$Version = "",
    [string]$WebView2Licence = "",   # LICENSE.txt of the Microsoft.Web.WebView2 NuGet package
    [int]$SmokePort = 8798,
    [switch]$SkipSmoke   # CI runs packaging\smoke-windows.ps1 as a step of its own
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

if ($SkipSmoke) {
    Write-Host "   -SkipSmoke: run packaging\smoke-windows.ps1 -ServerDir $serverDir yourself." -ForegroundColor Yellow
} else {
    & (Join-Path $PSScriptRoot "smoke-windows.ps1") -ServerDir $serverDir -Port $SmokePort
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
