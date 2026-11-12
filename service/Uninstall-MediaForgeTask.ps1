<#
.SYNOPSIS
    Stops and unregisters the MediaForge scheduled task. The clone tree and the
    database are left untouched.

.EXAMPLE
    .\Uninstall-MediaForgeTask.ps1
#>
[CmdletBinding()]
param([string]$TaskName = 'MediaForge')

$ErrorActionPreference = 'Stop'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Output "no task named $TaskName"
    return
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Output "removed $TaskName"
