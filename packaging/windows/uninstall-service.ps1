<#
.SYNOPSIS
    Remove the OpenChart Windows service and its firewall rule.

.DESCRIPTION
    The data folder (database, ticks, encrypted tokens) and the key file are kept, so
    installing again picks up where it stopped. The script says where they are and how to
    delete them if you want them gone.

    The indicator sandbox goes: its AppContainer profile (removed by running the service
    once as `openchart sandbox remove`, as SYSTEM, whose profile it is) and its read access
    to the release folder. A proof list at the end shows nothing is left.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Name = "OpenChart",
    [string]$Nssm = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3.0
$RuleName = "OpenChart server ($Name, Tailscale only)"

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$admin = (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin -and -not $WhatIfPreference) {
    Write-Host "Administrator rights are needed. Asking Windows for them..."
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", "`"$PSCommandPath`"", "-Name", "`"$Name`"")
    if ($Nssm) { $argList += @("-Nssm", "`"$Nssm`"") }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    exit 0
}

$candidates = @($Nssm, (Join-Path $PSScriptRoot "nssm.exe"), (Join-Path $PSScriptRoot "nssm\win64\nssm.exe")) | Where-Object { $_ }
$nssmExe = $candidates | Where-Object { Test-Path $_ -PathType Leaf } | Select-Object -First 1
if (-not $nssmExe) { $c = Get-Command nssm.exe -ErrorAction SilentlyContinue; if ($c) { $nssmExe = $c.Source } }

$service = Get-Service -Name $Name -ErrorAction SilentlyContinue
$dataDir = $null
$keyFile = $null
$sandboxSid = $null
$exe = $null
if ($service) {
    # Read where its data, key and sandbox are before removing it.
    $params = "HKLM:\SYSTEM\CurrentControlSet\Services\$Name\Parameters"
    if (Test-Path $params) {
        $props = Get-ItemProperty $params -ErrorAction SilentlyContinue
        $exe = $props.Application
        foreach ($line in @($props.AppEnvironmentExtra)) {
            if ($line -like "OPENCHART_DATA_DIR=*") { $dataDir = $line.Substring(19) }
            if ($line -like "OPENCHART_SECRET_KEY_FILE=*") { $keyFile = $line.Substring(26) }
            if ($line -like "OPENCHART_SANDBOX_SID=*") { $sandboxSid = $line.Substring(22) }
        }
    }
    if ($sandboxSid -and $nssmExe -and $exe -and $PSCmdlet.ShouldProcess($Name, "Remove the indicator sandbox's profile (the service runs once as 'openchart sandbox remove')")) {
        # The profile is SYSTEM's: only a process running as SYSTEM can delete it.
        $saved = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $nssmExe stop $Name 2>&1 | Out-Null
        & $nssmExe set $Name AppParameters "sandbox remove" 2>&1 | Out-Null
        & $nssmExe set $Name AppExit Default Exit 2>&1 | Out-Null
        & $nssmExe start $Name 2>&1 | Out-Null
        $t0 = Get-Date
        do {
            Start-Sleep -Seconds 1
            $s = (Get-Service $Name -ErrorAction SilentlyContinue).Status
        } while ($s -ne "Stopped" -and ((Get-Date) - $t0).TotalSeconds -lt 60)
        $ErrorActionPreference = $saved
        Write-Host "Indicator sandbox profile: removal run ($s)."
    }
    if ($sandboxSid -and $exe -and $PSCmdlet.ShouldProcess((Split-Path $exe), "Remove the sandbox's read access")) {
        $saved = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & icacls.exe (Split-Path $exe) /remove "*$sandboxSid" /T /C /Q 2>&1 | Out-Null
        $ErrorActionPreference = $saved
        Write-Host "Indicator sandbox: read access to $(Split-Path $exe) removed."
    }
    if ($PSCmdlet.ShouldProcess($Name, "Stop and remove the service")) {
        $saved = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        if ($nssmExe) {
            & $nssmExe stop $Name 2>&1 | Out-Null
            & $nssmExe remove $Name confirm 2>&1 | Out-Null
        } else {
            Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
            & sc.exe delete $Name 2>&1 | Out-Null
        }
        $ErrorActionPreference = $saved
        Write-Host "Service '$Name' removed." -ForegroundColor Green
    }
} else {
    Write-Host "There is no service named '$Name'."
}

$rule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($rule -and $PSCmdlet.ShouldProcess($RuleName, "Remove the firewall rule")) {
    $rule | Remove-NetFirewallRule
    Write-Host "Firewall rule '$RuleName' removed." -ForegroundColor Green
}

if ($sandboxSid) {
    Write-Host ""
    Write-Host "Indicator sandbox, what is left (all should be 0 / False):"
    $maps = "Registry::HKEY_USERS\S-1-5-18\Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer\Mappings"
    $left = @(Get-ChildItem $maps -ErrorAction SilentlyContinue | Where-Object { (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).Moniker -like "openchart.indicatorworker*" })
    Write-Host "  profile mappings for SYSTEM: $($left.Count)"
    $folder = Join-Path $env:SystemRoot "System32\config\systemprofile\AppData\Local\Packages\openchart.indicatorworker"
    Write-Host "  package folder exists: $(Test-Path $folder)"
    $grants = 0
    if ($exe -and (Test-Path (Split-Path $exe))) {
        $grants = @(& icacls.exe (Split-Path $exe) 2>$null | Select-String -SimpleMatch $sandboxSid).Count
    }
    Write-Host "  grants left on the release folder: $grants"
}

Write-Host ""
if ($dataDir) { Write-Host "Kept: the data folder $dataDir" }
if ($keyFile) { Write-Host "Kept: the key of the encrypted tokens $keyFile" }
Write-Host "Delete them yourself if you no longer need them (back up first: openchart backup)."
