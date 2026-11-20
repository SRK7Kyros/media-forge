<#
.SYNOPSIS
    Registers Jellyfin as a Windows service (nssm) with a server-owned data dir.

.DESCRIPTION
    The Jellyfin installer ships nssm.exe but no longer registers a service (12.x
    deletes an existing one and treats the tray app as the default), so nothing
    is listening and nothing is initialised until this runs. All mutable state
    lives under C:\ProgramData\Jellyfin\Server; the install directory stays
    read-only to the service.

    Keep every argument space-free. nssm hands AppParameters to the Win32 parser
    unquoted and `nssm set ... AppParameters` strips quotes from its own input,
    so a value containing a space arrives truncated (on 12.x that is fatal:
    jellyfin.exe exits 1 and nssm throttles the service to Paused). If a spaced
    path is unavoidable, write Parameters\AppParameters in the registry with real
    quotes and start with `sc.exe start` - a restart does not clear the throttle.

    Elevated only: creating a service and opening the firewall need admin.

.EXAMPLE
    .\Install-JellyfinService.ps1
#>
[CmdletBinding()]
param(
    [string]$InstallDir = 'C:\Program Files\Jellyfin\Server',
    [string]$DataDir = 'C:\ProgramData\Jellyfin\Server',
    [string]$ServiceName = 'JellyfinServer',
    [int]$Port = 8096
)

$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]$identity
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'run this from an elevated PowerShell (creating a service needs admin)'
}

$exe = Join-Path $InstallDir 'jellyfin.exe'
$nssm = Join-Path $InstallDir 'nssm.exe'
$web = Join-Path $InstallDir 'jellyfin-web'
foreach ($path in @($exe, $nssm, $web)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "missing from the Jellyfin install: $path" }
}

# The data dir must exist before the service starts: nssm does not create the
# tree, and Jellyfin fails to initialise if it cannot create it as SYSTEM.
foreach ($sub in @('', 'config', 'cache', 'log', 'metadata', 'plugins', 'root', 'transcodes')) {
    $target = if ($sub) { Join-Path $DataDir $sub } else { $DataDir }
    if (-not (Test-Path -LiteralPath $target)) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    }
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Status -ne 'Stopped') {
        Stop-Service -Name $ServiceName -Force
        $existing.WaitForStatus('Stopped', '00:00:30')
    }
    & $nssm remove $ServiceName confirm | Out-Null
}

# nssm passes AppParameters through the Win32 parser unquoted, so a value with a
# space loses its tail ("--webdir C:\Program" — measured). Keep only paths that
# cannot contain one, and let Jellyfin resolve the web client and ffmpeg from
# its own install directory, which is what AppDirectory is for.
$parameters = @(
    '--service'
    '--datadir', $DataDir
    '--configdir', (Join-Path $DataDir 'config')
    '--cachedir', (Join-Path $DataDir 'cache')
    '--logdir', (Join-Path $DataDir 'log')
) -join ' '

& $nssm install $ServiceName $exe | Out-Null
& $nssm set $ServiceName AppDirectory $InstallDir | Out-Null
& $nssm set $ServiceName AppParameters $parameters | Out-Null
& $nssm set $ServiceName DisplayName 'Jellyfin Server' | Out-Null
& $nssm set $ServiceName Description 'Jellyfin media server' | Out-Null
& $nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
& $nssm set $ServiceName ObjectName LocalSystem | Out-Null
& $nssm set $ServiceName AppStdout (Join-Path $DataDir 'log\service-stdout.log') | Out-Null
& $nssm set $ServiceName AppStderr (Join-Path $DataDir 'log\service-stderr.log') | Out-Null
& $nssm set $ServiceName AppRotateFiles 1 | Out-Null
& $nssm set $ServiceName AppRotateBytes 10485760 | Out-Null
# Jellyfin is a server, not a console app: never let a crash loop spin at 100%.
& $nssm set $ServiceName AppThrottle 5000 | Out-Null

$rule = Get-NetFirewallRule -DisplayName 'Jellyfin Server' -ErrorAction SilentlyContinue
if (-not $rule) {
    New-NetFirewallRule -DisplayName 'Jellyfin Server' -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
}

Start-Service -Name $ServiceName
(Get-Service -Name $ServiceName).WaitForStatus('Running', '00:01:00')

$deadline = (Get-Date).AddMinutes(3)
$up = $false
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/System/Info/Public" `
            -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) { $up = $true; break }
    }
    catch { Start-Sleep -Seconds 3 }
}
if (-not $up) { throw "the service is running but nothing answered on port $Port; see $DataDir\log" }

Write-Output "service $ServiceName running; http://127.0.0.1:$Port answered"
Write-Output "data dir: $DataDir"
