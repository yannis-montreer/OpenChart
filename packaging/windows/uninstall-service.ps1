<#
.SYNOPSIS
    Remove the OpenChart Windows service and its firewall rule.

.DESCRIPTION
    The data folder (database, ticks, encrypted tokens) and the key file are kept, so
    installing again picks up where it stopped. The script says where they are and how to
    delete them if you want them gone.
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
if ($service) {
    # Read where its data and key are before removing it.
    $params = "HKLM:\SYSTEM\CurrentControlSet\Services\$Name\Parameters"
    if (Test-Path $params) {
        $envExtra = (Get-ItemProperty $params -ErrorAction SilentlyContinue).AppEnvironmentExtra
        foreach ($line in @($envExtra)) {
            if ($line -like "OPENCHART_DATA_DIR=*") { $dataDir = $line.Substring(19) }
            if ($line -like "OPENCHART_SECRET_KEY_FILE=*") { $keyFile = $line.Substring(26) }
        }
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

Write-Host ""
if ($dataDir) { Write-Host "Kept: the data folder $dataDir" }
if ($keyFile) { Write-Host "Kept: the key of the encrypted tokens $keyFile" }
Write-Host "Delete them yourself if you no longer need them (back up first: openchart backup)."
