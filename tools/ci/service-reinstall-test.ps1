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

    The indicator sandbox (CI-1) is checked as LocalSystem along the way: the installer
    reports "Indicator sandbox: ready (AppContainer)", the service log shows every probe
    the minimum needs denied and a private desktop, one worker process runs, and after the
    uninstall the proof list is all zeros.

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
# NSSM reports START_PENDING until the program has run for its throttle period (up to about
# 30 s), even when the server already answers; Windows shows "Running" only after that.
function WaitRunning([int]$Seconds = 60) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $status = [string](Get-Service $Name -ErrorAction SilentlyContinue).Status
        if ($status -eq "Running") { return $status }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    return $status
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
    $st = WaitRunning
    Check ($st -eq "Running") "Windows reports the service Running ($st)"
    Check (Healthy) "it answers on 127.0.0.1:$Port"
    $pidBefore = ServicePid

    Section "1b. the indicator sandbox, as LocalSystem"
    Check ((Index $first.Lines "Indicator sandbox: ready \(AppContainer\)") -ge 0) "the installer reports the sandbox ready (AppContainer)"
    $serviceLog = Get-Content (Join-Path $DataDir "logs\service.log") -Raw
    $probes = [regex]::Match($serviceLog, "indicator sandbox probes: ([^\r\n]*)").Groups[1].Value
    Write-Host "    probes: $probes"
    foreach ($p in "read the database", "list the data folder", "read the key file", "open the server process", "connect to 127.0.0.1", "connect out \(192.0.2.1:9\)", "start a program") {
        Check ($probes -match "$p (denied|absent|error|timed out)") "probe '$p' is refused"
    }
    Check ($probes -match "environment clean") "the worker's environment holds no secrets"
    Write-Host "    notes: $([regex]::Match($serviceLog, 'indicator sandbox notes: ([^\r\n]*)').Groups[1].Value)"
    # Finding 2: the one write an AppContainer has is its own folder; it stays writable
    # (CI-1 investigation). The probe still runs; the note must say so truthfully and the
    # cap must be on (owner's decision B1).
    $notes = [regex]::Match($serviceLog, 'indicator sandbox notes: ([^\r\n]*)').Groups[1].Value
    Check ($probes -match "write into its own folder (allowed|denied|error)") "the write probe on its own folder ran"
    Check ($notes -match "its own folder is writable: all scripts of one worker share it, and what they write stays there until the folder is emptied at the next worker start; capped at \d+ MB") "the note says its own folder is writable, shared, emptied at each start and capped"
    Check ($serviceLog -match "indicator sandbox notes: [^\r\n]*a private desktop of its own") "the worker has a private desktop"
    Check ($serviceLog -match "indicator worker ready \(appcontainer") "the worker runs in the AppContainer"
    # The worker is created at normal priority and lowered by the service after its hello
    # (docs/investigations/worker-start-under-load.md): the log must say both happened, for
    # the same process, and the process itself must be below normal (base priority 6).
    $ready = [regex]::Match($serviceLog, "indicator worker ready \(appcontainer, pid (\d+)\)")
    $lowered = [regex]::Match($serviceLog, "indicator worker \(pid (\d+)\): priority lowered to below normal")
    Check ($ready.Success) "the service log says the worker reached ready"
    Check ($lowered.Success) "the service log says the worker's priority was lowered to below normal"
    Check ($ready.Success -and $lowered.Success -and $ready.Groups[1].Value -eq $lowered.Groups[1].Value) "ready and lowered name the same worker process"
    Check (-not ($serviceLog -match "could not lower the worker's priority")) "no priority failure in the service log"
    $workers = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "$Release*" -and $_.CommandLine -like "*indicator-worker*" })
    Check ($workers.Count -eq 1) "one worker process runs ($($workers.Count))"
    Check ($workers.Count -eq 1 -and $workers[0].Priority -eq 6) "the running worker is at below-normal priority (base priority $($workers[0].Priority))"

    Section "2. negative control: commands beside the running service"
    $m = Run "openchart migrate" @("migrate")
    Check ($m.Code -eq 4) "migrate is refused while the service holds the folder (exit 4)"
    Check ($m.Text -match "stop it first") "with the explanation"
    $s = Run "openchart stats" @("stats", "--data-dir", $DataDir)
    Check ($s.Code -eq 0) "stats runs beside the service"

    Section "3. reinstall over the running service"
    $st = WaitRunning
    Check ($st -eq "Running") "the service runs before the reinstall ($st)"
    $again = Install "reinstall"
    $lines = $again.Lines
    $stop = Index $lines "Stopping the running service"
    $migrated = Index $lines "Database ready"
    $tokens = Index $lines "Tokens \(OANDA"
    Check ($again.Code -eq 0) "the reinstall finished (exit $($again.Code)); its migrate would have been refused had the service still run"
    Check ($stop -ge 0 -and $stop -lt $migrated) "the service was stopped before the database was touched (step order)"
    Check ($migrated -ge 0 -and $migrated -lt $tokens) "the migration ran, before the tokens were moved"
    $st = WaitRunning
    Check ($st -eq "Running") "Windows reports the service Running again ($st)"
    Check (Healthy) "and answers"
    $pidAfter = ServicePid
    Check ($pidAfter -ne 0 -and $pidAfter -ne $pidBefore) "with a new process ($pidBefore -> $pidAfter)"
} finally {
    Section "4. uninstall and cleanup"
    $removal = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Release "uninstall-service.ps1") -Name $Name -Nssm $Nssm 2>&1 |
        ForEach-Object { "$_" })
    $removal | ForEach-Object { Write-Host "    | $_" }
    $proof = $removal -join "`n"
    Check ($proof -match "profile mappings for SYSTEM: 0") "no AppContainer profile left for SYSTEM"
    Check ($proof -match "package folder exists: False") "no AppContainer folder left"
    Check ($proof -match "grants left on the release folder: 0") "no sandbox grant left on the release folder"
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
# Explicitly: the last program run was `sc.exe query` failing with 1060 (the service is
# gone, as it should be), and a CI step ends with that leftover exit code otherwise.
exit 0
