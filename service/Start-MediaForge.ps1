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

function Write-Line {
    param([string]$Text)
    Write-Output ('{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Text)
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

if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'src\cli.py'))) {
    throw "RepoRoot does not look like a media-forge checkout: $RepoRoot"
}

# Run from the checkout so `-m src` and a relative MF_PLUGIN_DIR resolve.
Set-Location -LiteralPath $RepoRoot
$pythonPath = Resolve-Python -Repo $RepoRoot
$argumentList = @('-m', 'src', 'watch') + $ExtraArgs

Write-Line "python: $pythonPath"
Write-Line "repo:   $RepoRoot"

while ($true) {
    Write-Line 'starting mediaforge watch'
    $started = Get-Date
    try {
        & $pythonPath @argumentList 2>&1 | ForEach-Object { Write-Line $_ }
        $code = $LASTEXITCODE
    }
    catch {
        Write-Line "watcher failed to start: $($_.Exception.Message)"
        $code = 1
    }
    $ran = ((Get-Date) - $started).TotalSeconds
    Write-Line "mediaforge watch exited with $code after $([int]$ran)s"

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
