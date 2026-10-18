<#
.SYNOPSIS
    Install (or update) the OpenChart server as a Windows service, with NSSM.

.DESCRIPTION
    - Registers the service (default name "OpenChart"): starts on boot, restarts when the
      server stops or crashes, logs to <data folder>\logs\service.log (rotated at 10 MB;
      the server deletes rotated logs beyond the newest 5, once a day).
    - Runs it as LocalSystem. LocalSystem cannot read your Windows Credential Manager, so
      the OANDA and Telegram tokens are copied from it into the encrypted file
      <data folder>\secrets.enc, whose key goes to a file only SYSTEM and Administrators
      can read (docs/decisions.md, M8).
    - Adds a Windows Firewall rule: the app port, inbound, only from Tailscale addresses
      (100.64.0.0/10).
    - Warns if the PC goes to sleep, and prints the powercfg commands to stop that (it does
      not run them).
    - Backs up an existing data folder first (openchart backup), and stops if that fails.
    - Sets up the indicator sandbox (docs/custom-indicators.md): custom indicators run in a
      worker inside an AppContainer that may read the release folder and nothing else. Only
      with the release build; -NoSandbox removes it (custom indicators then do not run).
    Run it again to change the port or the data folder: it updates the service in place.

    In the release zip, run install-service.cmd (it asks for administrator rights).

.EXAMPLE
    .\install-service.ps1
    Defaults: openchart\openchart.exe and nssm.exe next to this script, data in
    C:\ProgramData\OpenChart\data, port 8000.

.EXAMPLE
    .\install-service.ps1 -Exe C:\Users\me\OpenChart\.venv\Scripts\openchart.exe -DataDir C:\Users\me\OpenChart\data -Nssm C:\tools\nssm.exe
    A source checkout instead of the release build, keeping its data folder.

.EXAMPLE
    .\install-service.ps1 -WhatIf
    Shows what would be done, changes nothing.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Name = "OpenChart",
    [string]$Exe = (Join-Path $PSScriptRoot "openchart\openchart.exe"),
    [string]$DataDir = (Join-Path $env:ProgramData "OpenChart\data"),
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [string]$BindAddress = "0.0.0.0",
    [string]$KeyFile = (Join-Path $env:ProgramData "OpenChart\secret.key"),
    [string]$Nssm = "",
    [string]$FirewallRemote = "100.64.0.0/10",
    [switch]$NoFirewall,
    [switch]$NoStart,
    [switch]$NoSandbox,
    [switch]$NoBackup
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3.0

$RuleName = "OpenChart server ($Name, Tailscale only)"
$SystemSid = "S-1-5-18"
$AdminsSid = "S-1-5-32-544"

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "   $text" -ForegroundColor Green }
function Note($text) { Write-Host "   $text" }
function Warn($text) { Write-Host "   WARNING: $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "`nERROR: $text" -ForegroundColor Red; exit 1 }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Run a program and collect what it prints (stdout and stderr) as text. In Windows
# PowerShell 5.1 a native program's stderr would otherwise become terminating errors.
function Exec {
    param([string]$Program, [Parameter(ValueFromRemainingArguments = $true)][string[]]$ProgramArgs)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $lines = & $Program @ProgramArgs 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{ Code = $LASTEXITCODE; Lines = @($lines); Text = (@($lines) -join "`n") }
    } finally {
        $ErrorActionPreference = $saved
    }
}

function Invoke-Nssm {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$NssmArgs)
    $r = Exec $script:NssmExe @NssmArgs
    if ($r.Code -ne 0) { Fail "nssm $($NssmArgs -join ' ') failed: $($r.Text)" }
}

# --- Administrator rights ---------------------------------------------------------------
if (-not (Test-Admin) -and -not $WhatIfPreference) {
    Write-Host "Administrator rights are needed (service, firewall). Asking Windows for them..."
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", "`"$PSCommandPath`"")
    foreach ($p in $PSBoundParameters.GetEnumerator()) {
        if ($p.Value -is [switch]) { if ($p.Value) { $argList += "-$($p.Key)" } }
        else { $argList += "-$($p.Key)"; $argList += "`"$($p.Value)`"" }
    }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    exit 0
}

Write-Host "OpenChart service setup" -ForegroundColor White

# --- What to run ------------------------------------------------------------------------
Step "Checking the files"
$Exe = [IO.Path]::GetFullPath($Exe)
if (-not (Test-Path $Exe -PathType Leaf)) {
    Fail "The server program was not found: $Exe`n       Pass its path with -Exe (the release zip has it in openchart\openchart.exe)."
}
$r = Exec $Exe --version
if ($r.Code -ne 0) { Fail "$Exe does not run: $($r.Text)" }
Ok "Server: $Exe ($($r.Text.Trim()))"

$candidates = @($Nssm, (Join-Path $PSScriptRoot "nssm.exe"), (Join-Path $PSScriptRoot "nssm\win64\nssm.exe")) | Where-Object { $_ }
$script:NssmExe = $candidates | Where-Object { Test-Path $_ -PathType Leaf } | Select-Object -First 1
if (-not $script:NssmExe) {
    $onPath = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($onPath) { $script:NssmExe = $onPath.Source }
}
if (-not $script:NssmExe) {
    Fail "nssm.exe was not found next to this script or on PATH.`n       It is in the release zip; or install it with: winget install NSSM.NSSM"
}
Ok "NSSM: $script:NssmExe"

$DataDir = [IO.Path]::GetFullPath($DataDir)
$KeyFile = [IO.Path]::GetFullPath($KeyFile)
Ok "Data folder: $DataDir"
Ok "Port: $Port (listening on $BindAddress)"

# --- An existing service ---------------------------------------------------------------
# Stopped before anything touches the data folder: a running server holds the folder's
# lock, and `openchart migrate` and `secrets to-file` refuse while it does.
$existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
if ($existing -and $existing.Status -ne "Stopped") {
    Step "Stopping the running service '$Name'"
    if ($PSCmdlet.ShouldProcess($Name, "Stop the running service before updating")) {
        $r = Exec $script:NssmExe stop $Name
        if ($r.Code -ne 0) { Fail "Could not stop the service: $($r.Text)" }
        Ok "Stopped."
    }
}

# --- Data folder ------------------------------------------------------------------------
Step "Data folder"
$env:OPENCHART_DATA_DIR = $DataDir   # for the openchart commands this script runs
Remove-Item Env:OPENCHART_SECRET_KEY -ErrorAction SilentlyContinue
Remove-Item Env:OPENCHART_SECRET_KEY_FILE -ErrorAction SilentlyContinue
if ((Test-Path (Join-Path $DataDir "openchart.db")) -and -not $NoBackup) {
    # Before anything changes it: an upgrade may migrate the database.
    if ($PSCmdlet.ShouldProcess($DataDir, "Back up the data folder (openchart backup)")) {
        $r = Exec $Exe backup --data-dir $DataDir
        $r.Lines | Where-Object { $_ -notmatch "^\d{4}-\d\d-\d\d " } | ForEach-Object { Note $_ }
        if ($r.Code -ne 0) { Fail "The backup failed (see above); nothing was changed. -NoBackup skips it." }
        Ok "Backed up before the update."
    }
}
if (-not (Test-Path $DataDir)) {
    if ($PSCmdlet.ShouldProcess($DataDir, "Create the data folder, readable by SYSTEM, Administrators and you only")) {
        New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $r = Exec icacls.exe $DataDir /inheritance:r /grant:r "*${SystemSid}:(OI)(CI)F" "*${AdminsSid}:(OI)(CI)F" "*${me}:(OI)(CI)M"
        if ($r.Code -ne 0) { Fail "Could not set the data folder's permissions: $($r.Text)" }
        Ok "Created, with access for SYSTEM, Administrators and you only."
    }
} else {
    Ok "Exists; its permissions are left as they are."
}
if ($PSCmdlet.ShouldProcess($DataDir, "Create or upgrade the database")) {
    $r = Exec $Exe migrate
    if ($r.Code -ne 0) { Fail "openchart migrate failed: $($r.Text)" }
    Ok "Database ready."
}

# --- Secrets ----------------------------------------------------------------------------
Step "Tokens (OANDA, Telegram)"
Note "The service runs as LocalSystem, which cannot read your Credential Manager."
Note "Your saved tokens are copied into an encrypted file; its key file is readable by SYSTEM and Administrators only."
if ($PSCmdlet.ShouldProcess($KeyFile, "Copy the tokens into $DataDir\secrets.enc, keyed by this file")) {
    $r = Exec $Exe secrets to-file --data-dir $DataDir --key-file $KeyFile
    $r.Lines | Where-Object { $_ -notmatch "^\d{4}-\d\d-\d\d " } | ForEach-Object { Note $_ }
    if ($r.Code -ne 0) { Fail "Moving the tokens failed (see above)." }
    $r = Exec icacls.exe $KeyFile /inheritance:r /grant:r "*${SystemSid}:(R)" "*${AdminsSid}:(F)"
    if ($r.Code -ne 0) { Fail "Could not restrict access to ${KeyFile}: $($r.Text)" }
    Ok "Key file locked: SYSTEM (read), Administrators (full)."
}

# --- Indicator sandbox ------------------------------------------------------------------
# Custom indicators run in a worker inside an AppContainer ("OpenChart.IndicatorWorker",
# no capabilities: no network, no files but what its SID is granted). It gets read and
# execute on the release folder only; the data folder and the key file keep their ACLs.
# The server creates the container's profile itself, as SYSTEM, when it starts.
Step "Indicator sandbox"
$ReleaseDir = Split-Path $Exe
$isRelease = (Test-Path (Join-Path $ReleaseDir "_internal")) -and ($Exe -notlike "*\.venv\*")
$SandboxSid = $null
$r = Exec $Exe sandbox sid
$knownSid = if ($r.Code -eq 0) { $r.Lines | Where-Object { $_ -match '^S-1-15-2-' } | Select-Object -First 1 } else { $null }
if ($NoSandbox) {
    if ($knownSid -and $PSCmdlet.ShouldProcess($ReleaseDir, "Remove the sandbox's read access")) {
        Exec icacls.exe $ReleaseDir /remove "*$knownSid" /T /C /Q | Out-Null
    }
    Ok "No sandbox (-NoSandbox): custom indicators do not run on this install; built-in ones do."
    Note "Its profile is removed by uninstall-service.ps1, or the next time it is set up."
} elseif (-not $isRelease) {
    Note "Custom indicators need the release build; with a source checkout they do not run under the service."
    Note "Built-in indicators, charts and alerts are not affected."
} elseif (-not $knownSid) {
    Warn "Could not get the sandbox's identity: $($r.Text)"
    Note "Custom indicators will not run; everything else works. Rerun this installer after fixing it."
} elseif ($PSCmdlet.ShouldProcess($ReleaseDir, "Grant the indicator sandbox read and execute on the release folder")) {
    $g = Exec icacls.exe $ReleaseDir /grant "*${knownSid}:(OI)(CI)RX" /T /C /Q
    if ($g.Code -ne 0) {
        Warn "Could not grant the sandbox read access to ${ReleaseDir}: $($g.Text)"
        Note "Custom indicators will not run; everything else works. Rerun this installer after fixing it."
    } else {
        $SandboxSid = $knownSid
        Ok "The sandbox ($SandboxSid) may read and run $ReleaseDir, nothing else."
    }
}

# --- Service ----------------------------------------------------------------------------
Step "Windows service '$Name'"
$existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
if ($existing) {
    if ($PSCmdlet.ShouldProcess($Name, "Update the existing service")) {
        Invoke-Nssm set $Name Application $Exe
        Ok "Existing service updated (it was stopped above)."
    }
} elseif ($PSCmdlet.ShouldProcess($Name, "Register the service")) {
    Invoke-Nssm install $Name $Exe
    Ok "Registered."
}
if ($PSCmdlet.ShouldProcess($Name, "Configure: start on boot, restart on exit, LocalSystem, logs")) {
    $logDir = Join-Path $DataDir "logs"
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $log = Join-Path $logDir "service.log"
    Invoke-Nssm set $Name AppParameters "run --data-dir `"$DataDir`" --host $BindAddress --port $Port"
    Invoke-Nssm set $Name AppDirectory $DataDir
    $envExtra = @("OPENCHART_SECRET_KEY_FILE=$KeyFile", "OPENCHART_DATA_DIR=$DataDir", "PYTHONUNBUFFERED=1", "PYTHONIOENCODING=utf-8")
    # Recorded so the server knows the sandbox was set up and the uninstaller removes
    # exactly this grant.
    if ($SandboxSid) { $envExtra += "OPENCHART_SANDBOX_SID=$SandboxSid" }
    Invoke-Nssm set $Name AppEnvironmentExtra @envExtra
    Invoke-Nssm set $Name DisplayName "OpenChart server"
    Invoke-Nssm set $Name Description "OpenChart: charts, price feed and alerts. http://localhost:$Port"
    Invoke-Nssm set $Name ObjectName LocalSystem
    Invoke-Nssm set $Name Start SERVICE_AUTO_START
    # Restart whenever the server exits; NSSM waits longer if it keeps failing within 30 s.
    Invoke-Nssm set $Name AppExit Default Restart
    Invoke-Nssm set $Name AppRestartDelay 5000
    Invoke-Nssm set $Name AppThrottle 30000
    # On stop: Ctrl+C first, so the server saves its buffered ticks; then end it (NSSM
    # 2.24 ends the whole process tree).
    Invoke-Nssm set $Name AppStopMethodConsole 20000
    Invoke-Nssm set $Name AppStdout $log
    Invoke-Nssm set $Name AppStderr $log
    Invoke-Nssm set $Name AppStdoutCreationDisposition 4
    Invoke-Nssm set $Name AppStderrCreationDisposition 4
    Invoke-Nssm set $Name AppRotateFiles 1
    Invoke-Nssm set $Name AppRotateOnline 1
    Invoke-Nssm set $Name AppRotateBytes 10485760
    # If NSSM itself ever dies, Windows restarts the service.
    Exec sc.exe failure $Name "reset=" 86400 "actions=" restart/10000/restart/30000/restart/60000 | Out-Null
    Exec sc.exe failureflag $Name 1 | Out-Null
    Ok "Starts on boot, restarts after any exit, runs as LocalSystem."
    Ok "Log: $log"
}

# --- Firewall ---------------------------------------------------------------------------
if (-not $NoFirewall) {
    Step "Windows Firewall"
    if ($PSCmdlet.ShouldProcess($RuleName, "Allow TCP $Port inbound from $FirewallRemote only")) {
        Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        New-NetFirewallRule -DisplayName $RuleName -Group "OpenChart" -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $Port -RemoteAddress $FirewallRemote -Profile Any `
            -Description "OpenChart web page and API, reachable over Tailscale only." | Out-Null
        Ok "Rule '$RuleName': TCP $Port from $FirewallRemote (Tailscale) only."
    }
    # A broader rule for the same program (for example one Windows created when python.exe
    # first asked to listen) would let other networks in too.
    $broad = @()
    $programs = @($Exe, (Join-Path (Split-Path $Exe) "python.exe"))
    foreach ($filter in Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue) {
        $prog = $filter.Program
        if (-not $prog -or $prog -eq "Any") { continue }
        if ($programs -notcontains $prog -and $prog -notlike "*\openchart.exe" -and $prog -notlike "*\python*.exe") { continue }
        $rule = $filter | Get-NetFirewallRule -ErrorAction SilentlyContinue
        if (-not $rule -or $rule.Enabled -ne "True" -or $rule.Direction -ne "Inbound" -or $rule.Action -ne "Allow") { continue }
        $addr = ($rule | Get-NetFirewallAddressFilter).RemoteAddress
        if ($addr -contains "Any") { $broad += $rule }
    }
    foreach ($rule in $broad) {
        Warn "The rule '$($rule.DisplayName)' lets $((($rule | Get-NetFirewallApplicationFilter).Program)) accept connections from any address."
        Note "   To keep OpenChart on Tailscale only, disable it:  Disable-NetFirewallRule -Name '$($rule.Name)'"
    }
}

# --- Sleep ------------------------------------------------------------------------------
Step "Sleep"
function Get-PowerIndex($sub, $setting) {
    # The last two hex values printed are the AC (plugged in) and DC (battery) settings, in
    # seconds; the labels are in the Windows display language, the order is not.
    $hex = @(& powercfg.exe /query SCHEME_CURRENT $sub $setting 2>$null | Select-String -Pattern ':\s*0x([0-9a-fA-F]+)\s*$' | ForEach-Object { [Convert]::ToInt64($_.Matches[0].Groups[1].Value, 16) })
    if ($hex.Count -ge 2) { return $hex[$hex.Count - 2] }
    return $null
}
$standby = Get-PowerIndex SUB_SLEEP STANDBYIDLE
$hibernate = Get-PowerIndex SUB_SLEEP HIBERNATEIDLE
if ($standby -gt 0 -or $hibernate -gt 0) {
    $after = @()
    if ($standby -gt 0) { $after += "sleeps after $([math]::Round($standby / 60)) min" }
    if ($hibernate -gt 0) { $after += "hibernates after $([math]::Round($hibernate / 60)) min" }
    Warn "When plugged in, this PC $($after -join ' and ') without use."
    Note "A sleeping PC sends no alerts (they are caught up, late, when it wakes)."
    Note "To keep it awake when plugged in, run in this window:"
    Note "    powercfg /change standby-timeout-ac 0"
    Note "    powercfg /change hibernate-timeout-ac 0"
} elseif ($null -eq $standby) {
    Note "Could not read the sleep settings; check Settings > System > Power & sleep."
} else {
    Ok "This PC does not go to sleep when plugged in."
}

# --- Start ------------------------------------------------------------------------------
if (-not $NoStart -and $PSCmdlet.ShouldProcess($Name, "Start the service")) {
    Step "Starting"
    # NSSM 2.24 may say "unexpected status SERVICE_START_PENDING": the server is still
    # starting. What counts is whether it answers.
    $serviceLog = Join-Path $DataDir "logs\service.log"
    $logStart = if (Test-Path $serviceLog) { (Get-Item $serviceLog).Length } else { 0 }
    $started = Exec $script:NssmExe start $Name
    $url = "http://127.0.0.1:$Port/api/health"
    $up = $false
    foreach ($i in 1..60) {
        Start-Sleep -Seconds 1
        try {
            $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { $up = $true; break }
        } catch { }
    }
    if ($up) {
        Ok "Running."
    } else {
        Warn "The server did not answer within a minute. See the log: $(Join-Path $DataDir 'logs\service.log')"
        if ($started.Text) { Note "nssm start said: $($started.Text)" }
    }
    if ($up -and $SandboxSid) {
        # The server starts the first worker in the background and says how it went.
        $verdict = $null
        foreach ($i in 1..60) {
            if (Test-Path $serviceLog) {
                $fs = [IO.File]::Open($serviceLog, "Open", "Read", "ReadWrite")
                try {
                    $fs.Seek([math]::Min($logStart, $fs.Length), "Begin") | Out-Null
                    $text = (New-Object IO.StreamReader($fs)).ReadToEnd()
                } finally { $fs.Close() }
                $m = [regex]::Match($text, "indicator sandbox: (ready \(AppContainer\)|unavailable: [^\r\n]*)")
                if ($m.Success) { $verdict = $m.Groups[1].Value; break }
            }
            Start-Sleep -Seconds 1
        }
        if ($verdict -eq "ready (AppContainer)") {
            Ok "Indicator sandbox: ready (AppContainer)."
        } elseif ($verdict) {
            Warn "Indicator sandbox: $verdict"
            Note "Custom indicators will not run; everything else works. Rerun this installer after fixing it."
        } else {
            Warn "The indicator sandbox did not report within a minute. See the log: $serviceLog"
        }
    }
}

Step "Done"
$hostName = $env:COMPUTERNAME.ToLower()
Note "On this PC:            http://localhost:$Port"
Note "From your devices:     http://${hostName}:$Port   (over Tailscale, with MagicDNS on)"
Note "Stop / start:          nssm stop $Name   /   nssm start $Name   (or the Services app)"
Note "Remove:                uninstall-service.ps1 (data and key are kept)"
Note "Back up the data:      openchart backup --data-dir `"$DataDir`""
