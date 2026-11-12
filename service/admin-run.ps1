<#
.SYNOPSIS
    Executes one queued admin job, then consumes it.

.DESCRIPTION
    This file is the ACTION of the `MediaForgeAdmin` scheduled task, which is
    registered with RunLevel Highest, so it executes with an elevated token and
    no UAC prompt. Nothing here decides what to run: it runs exactly what was
    queued at <ProgramData>\media-forge\admin\job.ps1.

    Deliberate limits:
      * one file, one name, one directory - no arguments, no wildcards;
      * the job is deleted before the exit path, so a stale file cannot re-run;
      * every run is appended to job.log with the job's SHA256, so what ran is
        auditable after the fact.

    Anyone who can write job.ps1 gets admin. That is the point and the price.
#>
[CmdletBinding()]
param(
    [string]$AdminRoot = (Join-Path $env:ProgramData 'media-forge\admin')
)

$ErrorActionPreference = 'Stop'
$job = Join-Path $AdminRoot 'job.ps1'
$log = Join-Path $AdminRoot 'job.log'

if (-not (Test-Path -LiteralPath $AdminRoot)) {
    New-Item -ItemType Directory -Path $AdminRoot -Force | Out-Null
}

if (-not (Test-Path -LiteralPath $job)) {
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format o) no job queued"
    return
}

$hash = (Get-FileHash -LiteralPath $job -Algorithm SHA256).Hash
$isAdmin = [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$header = "$(Get-Date -Format o) job $hash starting (user=$env:USERNAME elevated=$isAdmin)"
Add-Content -LiteralPath $log -Value $header

$failed = $false
try {
    & $job 2>&1 | ForEach-Object { Add-Content -LiteralPath $log -Value $_ }
    if ($LASTEXITCODE) { $failed = $true }
}
catch {
    $failed = $true
    Add-Content -LiteralPath $log -Value ("ERROR: " + $_.Exception.Message)
}
finally {
    Remove-Item -LiteralPath $job -Force -ErrorAction SilentlyContinue
    $verdict = if ($failed) { 'failed' } else { 'ok' }
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format o) job $hash $verdict"
}

if ($failed) { exit 1 }
