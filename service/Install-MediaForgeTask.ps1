<#
.SYNOPSIS
    Registers (or replaces) the MediaForge scheduled task that keeps the clone tree current.

.DESCRIPTION
    One task, two triggers, no window:
      * at startup  - the tree is already correct after a reboot;
      * every 5 min - a safety net for a daemon that died in a way the
                      supervisor did not see (it is idempotent and exits at once
                      when the lock is held or nothing changed).

    The default principal is SYSTEM so the task runs without anyone logging in.
    Use -RunAsUser to run as the current user instead (S4U: no password stored,
    but the task then only exists while the profile is loaded).

.EXAMPLE
    .\Install-MediaForgeTask.ps1

.EXAMPLE
    .\Install-MediaForgeTask.ps1 -RunAsUser -RepoRoot D:\Code\media-forge
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $PSCommandPath)),
    [string]$TaskName = 'MediaForge',
    [string]$Python = '',
    [switch]$RunAsUser,
    [switch]$NoSafetyNet
)

$ErrorActionPreference = 'Stop'

$startScript = Join-Path (Split-Path -Parent $PSCommandPath) 'Start-MediaForge.ps1'
if (-not (Test-Path -LiteralPath $startScript)) {
    throw "missing supervisor script: $startScript"
}
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path

$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = @(
    '-NoProfile'
    '-NonInteractive'
    '-ExecutionPolicy', 'Bypass'
    '-File', "`"$startScript`""
    '-RepoRoot', "`"$RepoRoot`""
)
if ($Python) { $arguments += @('-Python', "`"$Python`"") }

$action = New-ScheduledTaskAction -Execute $powershell -Argument ($arguments -join ' ') -WorkingDirectory $RepoRoot

$triggers = @(New-ScheduledTaskTrigger -AtStartup)
if (-not $NoSafetyNet) {
    $triggers += New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew
$settings.DisallowStartIfOnBatteries = $false

if ($RunAsUser) {
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Highest
}
else {
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
}

Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | ForEach-Object {
    Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal `
    -Description 'Maintain the Jellyfin hardlink clone tree beside the media library.' | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
Write-Output "registered $($task.TaskName) as $($task.Principal.UserId) ($($task.State))"
Write-Output "next: Start-ScheduledTask -TaskName $TaskName"
Write-Output "logs: <state dir>\logs\mediaforge.log (default C:\ProgramData\media-forge\logs)"
