<#
.SYNOPSIS
    Registers the prompt-free elevated runner as a scheduled task.

.DESCRIPTION
    Task `MediaForgeAdmin` runs service\admin-run.ps1 with RunLevel Highest as
    S4U (no stored password, no interactive logon), so a non-elevated session can
    trigger it with `Start-ScheduledTask` and get an elevated process without a
    UAC prompt.

    SECURITY: this is a standing privilege boundary. Any process running as this
    user can write <ProgramData>\media-forge\admin\job.ps1 and therefore obtain
    admin. Remove it with:
        Unregister-ScheduledTask -TaskName MediaForgeAdmin -Confirm:$false

    Elevated only, once.

.EXAMPLE
    .\Install-AdminTask.ps1
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'MediaForgeAdmin',
    [string]$Runner = (Join-Path (Split-Path -Parent $PSCommandPath) 'admin-run.ps1')
)

$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = [Security.Principal.WindowsPrincipal]$identity
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'run this from an elevated PowerShell (registering a highest-privilege task needs admin)'
}
if (-not (Test-Path -LiteralPath $Runner)) { throw "missing runner: $Runner" }
$Runner = (Resolve-Path -LiteralPath $Runner).Path

$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $Runner

$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType S4U -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew
# No trigger: this task exists only to be started on demand.
$settings.DisallowStartIfOnBatteries = $false

Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | ForEach-Object {
    Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Settings $settings `
    -Principal $principal `
    -Description 'Runs the queued admin job as this user with highest privileges (no UAC).' | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
Write-Output "registered $($task.TaskName) as $($task.Principal.UserId) / $($task.Principal.RunLevel) / $($task.Principal.LogonType)"
Write-Output "queue a job:  <ProgramData>\media-forge\admin\job.ps1"
Write-Output "run it:       Start-ScheduledTask -TaskName $TaskName"
Write-Output "read output:  <ProgramData>\media-forge\admin\job.log"
