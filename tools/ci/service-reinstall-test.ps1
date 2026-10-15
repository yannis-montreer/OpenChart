<#
.SYNOPSIS
    Install the service, then reinstall over it while it runs: the installer must stop it
    before it touches the database. Run elevated. Cleans up after itself.

.DESCRIPTION
    Used by CI (windows-package job) on the release build, and usable by hand on a PC
    with a temporary service name, a test port and a scratch data folder (a copy of real
    data if wanted, never the live folder).

    1. Installs the service (-NoFirewall, 127.0.0.1, the test port) and waits for it.
    2. Negative control: `openchart migrate` on the folder while the service runs must be
       refused (exit 4: the service holds the data-folder lock); `stats` still runs.
    3. Reinstalls over the running service. Because migrate is refused while the service
       holds the lock, the reinstall only succeeds if the installer stopped the service
       before touching the database. Also checked: the order of its steps, that the
       service runs again (a new process) and answers.
    4. Uninstalls and checks the cleanup (service gone, no process, port free).

.EXAMPLE
    .\tools\ci\service-reinstall-test.ps1 -Release dist\OpenChart-Server-0.1.0-windows -Nssm nssm\nssm-2.24\win64\nssm.exe -Work $env:RUNNER_TEMP
#>
param(
    [Parameter(Mandatory)] [string]$Release,   # the unzipped server folder (install-service.ps1, openchart\)
    [Parameter(Mandatory)] [string]$Nssm,
    [Parameter(Mandatory)] [string]$Work,      # scratch folder for the data folder and key file
    [string]$Name = "OpenChartCI",
    [int]$Port = 18765,
    [string]$DataDir = ""                      # default: a new folder under -Work (may hold a copy of data)
)
$ErrorActionPreference = "Stop"
$Release = (Resolve-Path $Release).Path
$Nssm = (Resolve-Path $Nssm).Path
$Exe = Join-Path $Release "openchart\openchart.exe"
$Installer = Join-Path $Release "install-service.ps1"
$Work = (Resolve-Path $Work).Path
if (-not $DataDir) { $DataDir = Join-Path $Work "oc-service-test-data" }
$DataDir = [IO.Path]::GetFullPath($DataDir)
# The cleanup deletes the data folder: it must be a scratch folder under -Work, never a
# live one.
if (-not $DataDir.StartsWith($Work.TrimEnd("\") + "\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "-DataDir must be inside -Work ($Work); this test deletes it afterwards."
}
$Key = Join-Path $Work "oc-service-test.key"
$failures = @()

function Section($t) { Write-Host "`n===== $t" }
function Check($ok, $what) {
    if ($ok) { Write-Host "  ok    $what" } else { Write-Host "  FAIL  $what"; $script:failures += $what }
}
function Healthy {
    try { return (Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 }
    catch { return $false }
}
function ServicePid {
    $s = Get-CimInstance Win32_Service -Filter "Name='$Name'" -ErrorAction SilentlyContinue
    if ($s) { return [int]$s.ProcessId } else { return 0 }
}
function Install($label) {
    $log = Join-Path $Work "install-$label.log"
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Installer -Name $Name -Exe $Exe `
        -DataDir $DataDir -Port $Port -BindAddress 127.0.0.1 -KeyFile $Key -Nssm $Nssm -NoFirewall 2>&1 |
        ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $out | Set-Content $log
    $out | ForEach-Object { Write-Host "    | $_" }
    return [pscustomobject]@{ Code = $code; Lines = @($out) }
}
function Run($label, [string[]]$ArgList) {
    $saved = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $env:OPENCHART_DATA_DIR = $DataDir
    try { $out = & $Exe @ArgList 2>&1 | ForEach-Object { "$_" }; $code = $LASTEXITCODE }
    finally { Remove-Item Env:OPENCHART_DATA_DIR; $ErrorActionPreference = $saved }
    Write-Host "    $label -> exit $code"
    $out | Select-Object -Last 3 | ForEach-Object { Write-Host "    | $_" }
    return [pscustomobject]@{ Code = $code; Text = (@($out) -join "`n") }
}
function Index($lines, $pattern) {
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match $pattern) { return $i } }
    return -1
}

if (Get-Service $Name -ErrorAction SilentlyContinue) { throw "A service named $Name exists already: remove it first." }
try {
    Section "1. first install"
    $first = Install "first"
    Check ($first.Code -eq 0) "the installer finished (exit $($first.Code))"
    Check ((Get-Service $Name).Status -eq "Running") "the service runs"
    Check (Healthy) "it answers on 127.0.0.1:$Port"
    $pidBefore = ServicePid

    Section "2. negative control: commands beside the running service"
    $m = Run "openchart migrate" @("migrate")
    Check ($m.Code -eq 4) "migrate is refused while the service holds the folder (exit 4)"
    Check ($m.Text -match "stop it first") "with the explanation"
    $s = Run "openchart stats" @("stats", "--data-dir", $DataDir)
    Check ($s.Code -eq 0) "stats runs beside the service"

    Section "3. reinstall over the running service"
    Check ((Get-Service $Name).Status -eq "Running") "the service runs before the reinstall"
    $again = Install "reinstall"
    $lines = $again.Lines
    $stop = Index $lines "Stopping the running service"
    $migrated = Index $lines "Database ready"
    $tokens = Index $lines "Tokens \(OANDA"
    Check ($again.Code -eq 0) "the reinstall finished (exit $($again.Code)); its migrate would have been refused had the service still run"
    Check ($stop -ge 0 -and $stop -lt $migrated) "the service was stopped before the database was touched (step order)"
    Check ($migrated -ge 0 -and $migrated -lt $tokens) "the migration ran, before the tokens were moved"
    Check ((Get-Service $Name).Status -eq "Running") "the service runs again"
    Check (Healthy) "and answers"
    $pidAfter = ServicePid
    Check ($pidAfter -ne 0 -and $pidAfter -ne $pidBefore) "with a new process ($pidBefore -> $pidAfter)"
} finally {
    Section "4. uninstall and cleanup"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Release "uninstall-service.ps1") -Name $Name -Nssm $Nssm 2>&1 |
        ForEach-Object { Write-Host "    | $_" }
    Start-Sleep -Seconds 2
    $saved = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    sc.exe query $Name | Out-Null; $sc = $LASTEXITCODE
    $ErrorActionPreference = $saved
    Check ($sc -eq 1060) "the service is gone (sc query exit $sc)"
    $procs = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "$Release*" })
    Check ($procs.Count -eq 0) "no process runs from the release folder"
    Check (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) "port $Port is free"
    Check (@(Get-NetFirewallRule -DisplayName "*$Name*" -ErrorAction SilentlyContinue).Count -eq 0) "no firewall rule"
    Remove-Item $DataDir, $Key -Recurse -Force -ErrorAction SilentlyContinue
}

if ($failures.Count) {
    Write-Host "`n$($failures.Count) check(s) failed:"; $failures | ForEach-Object { Write-Host "  - $_" }
    exit 1
}
Write-Host "`nAll checks passed."
