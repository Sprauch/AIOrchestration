# AIO - one entry point for the orchestration workgroup.
#
# WHY THIS EXISTS: every piece below needs the same three things - a refreshed PATH, the
# right Python, and the repository root as the working directory. Remembering that each
# time is friction, and friction is what this whole project is trying to remove.
#
#   .\AIO.ps1              start everything: orchestrator, approval console, dashboard
#   .\AIO.ps1 run          the orchestrator only
#   .\AIO.ps1 approve      the approval console only
#   .\AIO.ps1 web          the browser dashboard only
#   .\AIO.ps1 monitor      the terminal dashboard instead of the browser one
#   .\AIO.ps1 once         a single cycle, then exit - use this for a smoke test
#   .\AIO.ps1 check        preflight only; changes nothing
#   .\AIO.ps1 stop         stop everything AIO started

param([string]$Command = "start")

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"

# A shell opened before the installs has a stale PATH and will not find claude or gh.
# Rebuilding it from the registry is what makes this work in any window, including one
# that was already open.
$env:PATH = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")

function Assert-Ready {
    if (-not (Test-Path $Python)) { throw "Python 3.12 not found at $Python" }
    $svc = Get-Service Memurai -ErrorAction SilentlyContinue
    if (-not $svc) { throw "Memurai (Redis) is not installed." }
    if ($svc.Status -ne "Running") {
        Write-Host "Memurai is not running; starting it..." -ForegroundColor Yellow
        Start-Service Memurai
    }
}

function Start-Piece($title, $argline) {
    Start-Process powershell -ArgumentList @(
        "-NoExit", "-Command",
        "`$host.UI.RawUI.WindowTitle='AIO $title'; Set-Location '$Root'; " +
        "`$env:PATH=[Environment]::GetEnvironmentVariable('Path','Machine')+';'+[Environment]::GetEnvironmentVariable('Path','User'); " +
        "& '$Python' -m agents $argline"
    )
}

Set-Location $Root

switch ($Command.ToLower()) {
    "check" {
        Assert-Ready
        & $Python -m agents preflight --config agents/config.yaml
    }
    "start" {
        Assert-Ready
        Write-Host "Preflight..." -ForegroundColor Cyan
        & $Python -m agents preflight --config agents/config.yaml
        if ($LASTEXITCODE -ne 0) { throw "Preflight failed - not starting." }

        Start-Piece "orchestrator" "run"
        Start-Sleep -Seconds 2
        Start-Piece "approvals"    "approve"
        Start-Sleep -Seconds 1
        Start-Piece "dashboard"    "web"
        Start-Sleep -Seconds 3

        Write-Host ""
        Write-Host "AIO is up in three windows." -ForegroundColor Green
        Write-Host "  dashboard   http://localhost:8081   <- your view of the work"
        Write-Host "  approvals   the window titled 'AIO approvals' prompts you"
        Write-Host "  orchestrator  leave it alone"
        Write-Host ""
        Write-Host "Merging a pull request is the judgement gate. Stop with: .\AIO.ps1 stop"
        Start-Process "http://localhost:8081"
    }
    "stop" {
        # Only the windows AIO opened: matched on the title set in Start-Piece, so an
        # unrelated python or powershell session is not caught in the sweep.
        $killed = 0
        Get-Process powershell -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowTitle -like "AIO *" } |
            ForEach-Object { Stop-Process -Id $_.Id -Force; $killed++ }
        Get-Process python -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -eq $Python } |
            ForEach-Object { Stop-Process -Id $_.Id -Force; $killed++ }
        Write-Host "Stopped $killed AIO process(es). Memurai keeps running - it is a service."
    }
    default {
        Assert-Ready
        & $Python -m agents $Command --config agents/config.yaml
    }
}
