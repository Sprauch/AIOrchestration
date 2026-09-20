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
#   .\AIO.ps1 token        print the dashboard token and the phone URL

param([string]$Command = "start")

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"

# A shell opened before the installs has a stale PATH and will not find claude or gh.
# Rebuilding it from the registry is what makes this work in any window, including one
# that was already open.
$env:PATH = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")

# The dashboard's Bearer token, for approve/deny from a device that is not this one.
#
# WHAT IT DOES AND DOES NOT PROTECT. The token guards gate MUTATIONS - approving or
# denying - and nothing else. The VIEW is never authenticated, so anyone who can reach
# the port can read proposals, diffs and file paths. That is why this is reached over
# Tailscale rather than a firewall hole: the network is the access control, and the token
# is the second lock on the one action that changes something.
#
# Kept OUTSIDE the repository, beside the other credential, for the reason B81 records:
# a secret in a file git can see is a secret waiting to be committed.
$TokenFile = Join-Path $env:USERPROFILE ".api\AIO-dashboard.token"

function Get-DashboardToken {
    if (Test-Path $TokenFile) { return (Get-Content $TokenFile -Raw).Trim() }
    New-Item -ItemType Directory -Force -Path (Split-Path $TokenFile) | Out-Null
    # 32 bytes of CSPRNG, hex. Generated once and reused, so the phone is paired once.
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    Set-Content -Path $TokenFile -Value $token -Encoding ascii -NoNewline
    Write-Host "Created a dashboard token at $TokenFile" -ForegroundColor Yellow
    return $token
}

function Get-TailscaleIp {
    $ts = "C:\Program Files\Tailscale\tailscale.exe"
    if (-not (Test-Path $ts)) { return $null }
    try {
        $out = & $ts ip -4 2>&1 | Select-Object -First 1
        if ($out -match '^\d+\.\d+\.\d+\.\d+$') { return $out }
    } catch { }
    return $null
}

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

        $token = Get-DashboardToken

        Start-Piece "orchestrator" "run"
        Start-Sleep -Seconds 2
        Start-Piece "approvals"    "approve"
        Start-Sleep -Seconds 1
        Start-Piece "dashboard"    "web --token $token"
        Start-Sleep -Seconds 3

        Write-Host ""
        Write-Host "AIO is up in three windows." -ForegroundColor Green
        Write-Host "  dashboard   http://localhost:8081   <- your view of the work"
        Write-Host "  approvals   the window titled 'AIO approvals' prompts you"
        Write-Host "  orchestrator  leave it alone"

        # The phone address, printed rather than looked up each time. Tailscale gives this
        # machine a stable 100.x address reachable from your own devices anywhere, and
        # from nothing else - no port forwarding, no firewall hole, not the public net.
        $ts = "C:\Program Files\Tailscale\tailscale.exe"
        if (Test-Path $ts) {
            $ip = Get-TailscaleIp
            if ($ip) {
                Write-Host ""
                Write-Host "  from your phone   http://${ip}:8081" -ForegroundColor Cyan
                Write-Host "  (Tailscale must be on and signed in on both devices)"
            } else {
                Write-Host ""
                Write-Host "  Tailscale is installed but not connected - run: tailscale up" -ForegroundColor Yellow
            }
        }
        Write-Host ""
        Write-Host "Merging a pull request is the judgement gate. Stop with: .\AIO.ps1 stop"
        Start-Process "http://localhost:8081"
    }
    "token" {
        $token = Get-DashboardToken
        $ts = "C:\Program Files\Tailscale\tailscale.exe"
        # `tailscale ip` writes to stderr and exits non-zero when logged out, which
        # PowerShell renders as a wall of NativeCommandError. Swallow it and report the
        # state in one line instead.
        $ip = Get-TailscaleIp
        Write-Host ""
        Write-Host "dashboard token  $token"
        Write-Host "stored at        $TokenFile"
        if ($ip) {
            Write-Host "phone URL        http://${ip}:8081"
        } else {
            Write-Host "phone URL        (Tailscale not connected - run: tailscale up)"
        }
        Write-Host ""
        Write-Host "The token authorises APPROVE and DENY only. The view is not"
        Write-Host "authenticated at all, which is why this is reached over Tailscale"
        Write-Host "rather than an open port."
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
