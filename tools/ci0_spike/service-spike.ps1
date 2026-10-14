# CI-0 spike as LocalSystem: a temporary service OpenChartTest runs run_spike.py once, then
# everything is removed and the cleanup is checked. No port is opened to the network (the
# probe listener binds 127.0.0.1 inside the run), no firewall rule, no broker. Run elevated;
# everything goes to service-spike.log next to -Out.
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
$Container = "OpenChart.IndicatorWorker.Spike"
Start-Transcript -Path (Join-Path $Out "service-spike.log") -Force | Out-Null
function Section($t) { Write-Output "`n===== $t" }
$SystemSid = "S-1-5-18"; $AdminsSid = "S-1-5-32-544"
$Me = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value

Section "before"
if ((Test-Path $Root) -or (Get-Service $Name -ErrorAction SilentlyContinue)) {
    Write-Output "ABORT: a previous test is still installed"; Stop-Transcript; exit 1
}

Section "a service-like layout: release copy, data folder and key file with the installer's ACLs"
New-Item -ItemType Directory $Work, $Data -Force | Out-Null
Copy-Item $Frozen "$Work\rt-frozen" -Recurse
icacls.exe $Data /inheritance:r /grant:r "*${SystemSid}:(OI)(CI)F" "*${AdminsSid}:(OI)(CI)F" "*${Me}:(OI)(CI)M" | Out-Null
Set-Content $Key "decoy-key" -NoNewline
icacls.exe $Key /inheritance:r /grant:r "*${SystemSid}:(R)" "*${AdminsSid}:(F)" | Out-Null
icacls.exe $Data; icacls.exe $Key

Section "install the temporary service (LocalSystem), run once, no restart"
$exe = "$Work\rt-frozen\ocw_spike.exe"
$params = @(
    "`"$Spike\run_spike.py`"", "--work", "`"$Work`"", "--report", "`"$Work\report-system.json`"",
    "--decoy-data", "`"$Data`"", "--decoy-key", "`"$Key`"", "--grant", "`"$Work\rt-frozen`"",
    "--mode", "`"plain-system=plain:frozen:$exe`"",
    "--mode", "`"restricted-system=restricted:frozen:$exe`"",
    "--mode", "`"appcontainer-system=appcontainer:frozen:$exe`""
) -join " "
& $Nssm install $Name $Python | Out-Null
& $Nssm set $Name AppParameters $params | Out-Null
& $Nssm set $Name AppDirectory $Spike | Out-Null
& $Nssm set $Name AppExit Default Exit | Out-Null
& $Nssm set $Name AppStdout "$Work\run-system.log" | Out-Null
& $Nssm set $Name AppStderr "$Work\run-system.log" | Out-Null
& $Nssm set $Name Start SERVICE_DEMAND_START | Out-Null
$t0 = Get-Date
& $Nssm start $Name | Out-Null
do { Start-Sleep -Seconds 2; $s = (Get-Service $Name).Status } while ($s -ne "Stopped" -and ((Get-Date) - $t0).TotalSeconds -lt 600)
Write-Output ("service state {0} after {1:N0} s" -f $s, ((Get-Date) - $t0).TotalSeconds)
foreach ($x in Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "$Root*" -or $_.CommandLine -like "*run_spike.py*" }) {
    Write-Output "still running: $($x.ProcessId) $($x.Name)"
}
Copy-Item "$Work\report-system.json", "$Work\run-system.log" $Out -ErrorAction Continue

Section "remove the service and the test folder"
& $Nssm stop $Name 2>$null | Out-Null
& $Nssm remove $Name confirm | Out-Null
Start-Sleep -Seconds 2
Remove-Item $Root -Recurse -Force

Section "proof of cleanup"
sc.exe query $Name | Select-Object -First 2
Write-Output "sc query exit code: $LASTEXITCODE (1060 = the service does not exist)"
Write-Output "test folder exists: $(Test-Path $Root); key file exists: $(Test-Path $Key)"
Write-Output ("firewall rules mentioning OpenChartTest: {0}" -f @(Get-NetFirewallRule -DisplayName "*OpenChartTest*" -ErrorAction SilentlyContinue).Count)
Write-Output ("processes from the test folder or running the spike: {0}" -f @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "$Root*" -or $_.Name -eq "ocw_spike.exe" -or $_.CommandLine -like "*run_spike.py*" }).Count)
Write-Output ("nssm.exe processes: {0}" -f @(Get-Process nssm -ErrorAction SilentlyContinue).Count)
$listen = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).Path -like "*buildenv*" })
Write-Output "ports listened on by the spike's interpreter: $($listen.Count)"
$maps = "Registry::HKEY_USERS\S-1-5-18\Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer\Mappings"
$left = @(Get-ChildItem $maps -ErrorAction SilentlyContinue | Where-Object { (Get-ItemProperty $_.PSPath).Moniker -like "*openchart*" })
Write-Output "AppContainer profiles left for SYSTEM: $($left.Count)"
Write-Output "AppContainer folder left: $(Test-Path "$env:SystemRoot\System32\config\systemprofile\AppData\Local\Packages\$($Container.ToLower())")"
Write-Output "SYSTEM registry decoy left: $(Test-Path 'Registry::HKEY_USERS\S-1-5-18\Software\OpenChartSpike')"
Stop-Transcript | Out-Null
