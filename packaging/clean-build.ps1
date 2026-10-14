<#
.SYNOPSIS
    Remove build leftovers: everything in build\ and dist\ except the final zips and
    SHA256SUMS.txt.

.DESCRIPTION
    The Windows build (packaging\windows\build-windows.ps1) leaves PyInstaller's work
    folders in dist\.build and the unzipped release folders next to the zips, several
    hundred MB together. The zips are what gets published. Both folders are git-ignored.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\clean-build.ps1 -WhatIf
    Lists what would be removed and how much space it frees, without removing anything.
#>
[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$keep = @("*.zip", "SHA256SUMS.txt")
$freed = 0

foreach ($folder in "build", "dist") {
    $path = Join-Path $Root $folder
    if (-not (Test-Path $path)) { continue }
    foreach ($item in Get-ChildItem $path -Force) {
        $kept = $false
        foreach ($pattern in $keep) {
            if (-not $item.PSIsContainer -and $item.Name -like $pattern) { $kept = $true }
        }
        if ($kept) { continue }
        $bytes = if ($item.PSIsContainer) {
            (Get-ChildItem $item.FullName -Recurse -Force -File | Measure-Object Length -Sum).Sum
        } else { $item.Length }
        if ($PSCmdlet.ShouldProcess($item.FullName, ("Remove ({0:N1} MB)" -f ($bytes / 1MB)))) {
            Remove-Item $item.FullName -Recurse -Force
        }
        $freed += $bytes
    }
}

$verb = if ($WhatIfPreference) { "Would free" } else { "Freed" }
"{0} {1:N1} MB. Kept: the zips and SHA256SUMS.txt." -f $verb, ($freed / 1MB)
