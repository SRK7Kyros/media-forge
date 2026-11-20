<#
.SYNOPSIS
    Supervises `mediaforge watch`.

.DESCRIPTION
    Runs the watcher in a loop and restarts it if it exits. The python process
    itself already writes a rotating log (%ProgramData%\media-forge\logs by
    default), so this wrapper only needs to report crashes to the event-free
    stdout that Task Scheduler captures.

    Nothing here parses the source tree; that is the tool's job. Keep this
    script dumb: a supervisor that does work of its own is a second place to
    debug.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $PSCommandPath)),
    [string]$Python = '',
    [int]$RestartDelaySeconds = 30,
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = 'Stop'

# Task Scheduler discards the action's stdout, so a supervisor that only writes
# there is invisible. Mirror every line into <repo>\logs\supervisor.log: the
# restart loop is the only place that can explain why the watcher keeps dying.
$script:SupervisorLog = Join-Path (Split-Path -Parent (Split-Path -Parent $PSCommandPath)) 'logs\supervisor.log'

function Write-Line {
    param([string]$Text)
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Text
    try {
        $dir = Split-Path -Parent $script:SupervisorLog
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -LiteralPath $script:SupervisorLog -Value $line -ErrorAction Stop
    }
    catch {
        # A supervisor that cannot write its own log must still supervise.
    }
    Write-Output $line
}

function Resolve-Python {
    param([string]$Repo)
    if ($Python) {
        if (-not (Test-Path -LiteralPath $Python)) {
            throw "the interpreter given in -Python does not exist: $Python"
        }
        return (Resolve-Path -LiteralPath $Python).Path
    }
    $venv = Join-Path $Repo '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return (Resolve-Path -LiteralPath $venv).Path }
    $onPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    throw "no interpreter found: create $venv or pass -Python <path>"
}

if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'mediaforge\cli.py'))) {
    throw "RepoRoot does not look like a media-forge checkout: $RepoRoot"
}

# Run from the checkout so `-m mediaforge` and a relative MF_PLUGIN_DIR resolve.
Set-Location -LiteralPath $RepoRoot
$pythonPath = Resolve-Python -Repo $RepoRoot
$argumentList = @('-m', 'mediaforge', 'watch') + $ExtraArgs

Write-Line "python: $pythonPath"
Write-Line "repo:   $RepoRoot"

while ($true) {
    Write-Line 'starting mediaforge watch'
    $started = Get-Date
    # Never pipe a native command's stderr into a pipeline while
    # ErrorActionPreference is Stop: PowerShell turns each stderr line into an
    # ErrorRecord, the first one throws, the pipe closes and the child dies.
    # That produced a silent 30-second crash loop (measured 2026-11-12).
    # Redirect to files instead and report them after the child exits.
    $outFile = Join-Path $RepoRoot 'logs\watch.out'
    $errFile = Join-Path $RepoRoot 'logs\watch.err'
    $code = 1
    try {
        $proc = Start-Process -FilePath $pythonPath -ArgumentList $argumentList `
            -WorkingDirectory $RepoRoot -NoNewWindow -PassThru -Wait `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        $code = $proc.ExitCode
    }
    catch {
        Write-Line "watcher failed to start: $($_.Exception.Message)"
    }
    $ran = ((Get-Date) - $started).TotalSeconds
    Write-Line "mediaforge watch exited with $code after $([int]$ran)s"
    foreach ($stream in @($errFile, $outFile)) {
        if (Test-Path -LiteralPath $stream) {
            Get-Content -LiteralPath $stream -Tail 15 | Where-Object { $_ } | ForEach-Object {
                Write-Line ("  " + $_)
            }
        }
    }

    # A clean, long-lived run means somebody asked it to stop (lock taken by a
    # manual command, MF_WATCH_MODE=off, service stop); a crash loop is short.
    if ($code -eq 0 -and $ran -gt 300) {
        Write-Line 'exited cleanly; not restarting'
        break
    }
    # Exit code 4 is "another mediaforge holds the lock": a manual `sync` is
    # running. Wait it out rather than fighting for the lock.
    if ($code -eq 4) {
        Start-Sleep -Seconds 60
        continue
    }
    Start-Sleep -Seconds $RestartDelaySeconds
}
