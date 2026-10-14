# CI-0 spike as LocalSystem: a temporary service OpenChartTest runs run_spike.py once, then
# everything is removed and the cleanup is checked. No port is opened to the network (the
# probe listener binds 127.0.0.1 inside the run), no firewall rule, no broker. Run elevated;
# everything goes to service-spike.log in -Out, with the service's own output
# (run-system.log), NSSM's events and the report copied there even when the run fails.
# The cleanup runs whatever happens, and the window waits for a key at the end.
param(
    [Parameter(Mandatory)] [string]$Python,     # interpreter with numpy (the scratch build env)
    [Parameter(Mandatory)] [string]$Frozen,     # folder of the PyInstaller worker (ocw_spike)
    [Parameter(Mandatory)] [string]$Nssm,
    [Parameter(Mandatory)] [string]$Out         # folder that receives the report and logs
)
$ErrorActionPreference = "Continue"
$Spike = $PSScriptRoot
$Name = "OpenChartTest"
$Root = "C:\ProgramData\OpenChartTest"
$Work = "$Root\work"
$Data = "$Root\data"
$Key = "$Root\secret.key"
$Log = "$Root\run-system.log"     # outside $Work so the cleanup pass cannot remove it early
$Container = "OpenChart.IndicatorWorker.Spike"
New-Item -ItemType Directory $Out -Force | Out-Null
Start-Transcript -Path (Join-Path $Out "service-spike.log") -Force | Out-Null
function Section($t) { Write-Output "`n===== $t" }
function Nssm { & $Nssm @args 2>&1 | ForEach-Object { "$_".Replace("`0", "") } | Where-Object { $_ -ne "" } }
function TestProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -like "$Root*" -or $_.Name -eq "ocw_spike.exe" -or $_.CommandLine -like "*run_spike.py*"
    })
}
$SystemSid = "S-1-5-18"; $AdminsSid = "S-1-5-32-544"
$Me = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value

# Runs the service once with these driver arguments and waits until it has stopped.
function RunOnce([string]$DriverArgs, [int]$Seconds) {
    Nssm set $Name AppParameters $DriverArgs | Out-Null
    Write-Output "parameters as NSSM holds them: $(Nssm get $Name AppParameters)"
    $t0 = Get-Date
    Nssm start $Name   # "unexpected status SERVICE_STOPPED" only means the program exited at once
    do {
        Start-Sleep -Seconds 2
        $s = (Get-Service $Name -ErrorAction SilentlyContinue).Status
    } while ($s -ne "Stopped" -and ((Get-Date) - $t0).TotalSeconds -lt $Seconds)
    Write-Output ("service state {0} after {1:N0} s" -f $s, ((Get-Date) - $t0).TotalSeconds)
    if ($s -ne "Stopped") {
        Write-Output "TIMEOUT: stopping the service"
        Nssm stop $Name
    }
}

$installed = $false
$started = Get-Date
try {
    Section "before"
    if ((Test-Path $Root) -or (Get-Service $Name -ErrorAction SilentlyContinue)) {
        Write-Output "ABORT: a previous test is still installed (the cleanup below still runs)"
        throw "previous test present"
    }

    Section "a service-like layout: release copy, data folder and key file with the installer's ACLs"
    New-Item -ItemType Directory $Work, $Data -Force | Out-Null
    Copy-Item $Frozen "$Work\rt-frozen" -Recurse
    icacls.exe $Data /inheritance:r /grant:r "*${SystemSid}:(OI)(CI)F" "*${AdminsSid}:(OI)(CI)F" "*${Me}:(OI)(CI)M" | Out-Null
    Set-Content $Key "decoy-key" -NoNewline
    icacls.exe $Key /inheritance:r /grant:r "*${SystemSid}:(R)" "*${AdminsSid}:(F)" | Out-Null
    icacls.exe $Data; icacls.exe $Key

    Section "install the temporary service (LocalSystem), manual start, no restart"
    Nssm install $Name $Python
    $installed = $true
    Nssm set $Name AppDirectory $Spike | Out-Null
    Nssm set $Name AppExit Default Exit | Out-Null
    Nssm set $Name AppStdout $Log | Out-Null
    Nssm set $Name AppStderr $Log | Out-Null
    Nssm set $Name AppStdoutCreationDisposition 4 | Out-Null   # append
    Nssm set $Name AppStderrCreationDisposition 4 | Out-Null
    Nssm set $Name AppEnvironmentExtra "PYTHONUNBUFFERED=1" "PYTHONIOENCODING=utf-8" | Out-Null
    Nssm set $Name Start SERVICE_DEMAND_START | Out-Null

    Section "the run: plain, AppContainer and restricted workers, started by SYSTEM, on the service desktop and on a private one"
    $exe = "$Work\rt-frozen\ocw_spike.exe"
    $driver = @(
        "`"$Spike\run_spike.py`"", "--work", "`"$Work`"", "--report", "`"$Work\report-system.json`"",
        "--decoy-data", "`"$Data`"", "--decoy-key", "`"$Key`"", "--grant", "`"$Work\rt-frozen`"",
        "--mode", "`"plain-system=plain:frozen:$exe`"",
        "--mode", "`"appcontainer-system=appcontainer:frozen:$exe`"",
        "--mode", "`"appcontainer-desktop-system=appcontainer+desktop:frozen:$exe`"",
        "--mode", "`"restricted-nolow-desktop-system=restricted-nolow+desktop:frozen:$exe`"",
        "--mode", "`"restricted-desktop-system=restricted+desktop:frozen:$exe`""
    ) -join " "
    RunOnce $driver 600
} catch {
    Write-Output "STOPPED EARLY: $_"
} finally {
    Section "cleanup pass as SYSTEM (decoy credential and registry value, AppContainer profile)"
    if ($installed) {
        $cleanup = "`"$Spike\run_spike.py`" --work `"$Work`" --cleanup-only"
        RunOnce $cleanup 120
    } else {
        Write-Output "the service was never installed: nothing ran as SYSTEM"
    }

    Section "copy out whatever exists"
    foreach ($f in "$Log", "$Work\report-system.json") {
        if (Test-Path $f) { Copy-Item $f $Out -Force; Write-Output "copied $f" } else { Write-Output "missing: $f" }
    }
    Get-WinEvent -FilterHashtable @{ LogName = "Application"; ProviderName = "nssm"; StartTime = $started } -ErrorAction SilentlyContinue |
        Sort-Object TimeCreated | ForEach-Object { "{0:HH:mm:ss} {1} {2}" -f $_.TimeCreated, $_.Id, ($_.Message -replace "\s+", " ") } |
        Set-Content (Join-Path $Out "nssm-events.txt")
    Write-Output "NSSM events: $((Get-Content (Join-Path $Out 'nssm-events.txt') -ErrorAction SilentlyContinue | Measure-Object).Count) lines in nssm-events.txt"

    Section "remove the service, stray processes and the test folder"
    if (Get-Service $Name -ErrorAction SilentlyContinue) {
        Nssm stop $Name
        Nssm remove $Name confirm
    }
    foreach ($p in TestProcesses) {
        Write-Output "stopping leftover process $($p.ProcessId) $($p.Name)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    if (Test-Path $Root) { Remove-Item $Root -Recurse -Force -ErrorAction Continue }

    Section "proof of cleanup"
    sc.exe query $Name | Select-Object -First 2
    Write-Output "sc query exit code: $LASTEXITCODE (1060 = the service does not exist)"
    Write-Output "test folder exists: $(Test-Path $Root); key file exists: $(Test-Path $Key)"
    Write-Output ("firewall rules mentioning OpenChartTest: {0}" -f @(Get-NetFirewallRule -DisplayName "*OpenChartTest*" -ErrorAction SilentlyContinue).Count)
    Write-Output ("processes from the test folder or running the spike: {0}" -f (TestProcesses).Count)
    Write-Output ("nssm.exe processes: {0}" -f @(Get-Process nssm -ErrorAction SilentlyContinue).Count)
    $listen = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).Path -like "*buildenv*" })
    Write-Output "ports listened on by the spike's interpreter: $($listen.Count)"
    $maps = "Registry::HKEY_USERS\S-1-5-18\Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer\Mappings"
    $left = @(Get-ChildItem $maps -ErrorAction SilentlyContinue | Where-Object { (Get-ItemProperty $_.PSPath).Moniker -like "*openchart*" })
    Write-Output "AppContainer profiles left for SYSTEM: $($left.Count)"
    Write-Output "AppContainer folder left: $(Test-Path "$env:SystemRoot\System32\config\systemprofile\AppData\Local\Packages\$($Container.ToLower())")"
    Write-Output "SYSTEM registry decoy left: $(Test-Path 'Registry::HKEY_USERS\S-1-5-18\Software\OpenChartSpike')"
    Write-Output "SYSTEM credential decoy: see the cleanup pass in run-system.log ('credential OpenChartSpike/decoy deleted')"
    Write-Output "files in ${Out}:"; Get-ChildItem $Out | ForEach-Object { "  $($_.Name)  $($_.Length) bytes" }
    Stop-Transcript | Out-Null
    Write-Host "`nDone. The output is in $Out. Press any key to close this window." -ForegroundColor Yellow
    [void]$Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}
