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
#   .\AIO.ps1 calib <pct>  record what Claude Code reports as "% used" right now, and
#                          estimate the weekly allowance from how it moves

param([string]$Command = "start", [string]$Arg = "")

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
        Write-Host "Pair a phone with: .\AIO.ps1 pair"
        # Opens with the token so THIS browser is paired without anyone typing it; the
        # page stores it and strips it from the address bar on load.
        Start-Process "http://localhost:8081/?token=$token"
    }
    { $_ -in @("token", "pair") } {
        $token = Get-DashboardToken
        $ip = Get-TailscaleIp
        # The pairing URL carries the token once. Opening it stores the token on that
        # device and strips it from the address bar, so it is never typed and does not
        # linger in history. Pair each device once; after that the plain URL works.
        $phonePair = if ($ip) { "http://${ip}:8081/?token=$token" } else { $null }

        Write-Host ""
        Write-Host "PAIR THIS PHONE - open the link below on it, once:" -ForegroundColor Cyan
        if ($phonePair) {
            Write-Host "  $phonePair"
            try {
                Set-Clipboard -Value $phonePair
                Write-Host "  (copied to clipboard - message it to yourself)" -ForegroundColor DarkGray
            } catch { }
        } else {
            Write-Host "  Tailscale is not connected. Run: tailscale up" -ForegroundColor Yellow
        }
        Write-Host ""
        Write-Host "After pairing, the phone uses the plain address:"
        if ($ip) { Write-Host "  http://${ip}:8081" } else { Write-Host "  (needs Tailscale)" }
        Write-Host ""
        Write-Host "token      $token"
        Write-Host "stored at  $TokenFile"
        Write-Host ""
        Write-Host "The token authorises APPROVE, DENY and STARTING the orchestrator." -ForegroundColor Yellow
        Write-Host "The VIEW is not authenticated at all, which is why this is reached"
        Write-Host "over Tailscale rather than an open port."
    }
    "calib" {
        # CALIBRATING THE WEEKLY ALLOWANCE BY OBSERVATION.
        #
        # The limit is not published and the CLI does not expose it, but Claude Code
        # reports a "% of weekly usage" that moves as tokens are spent. Two readings with
        # a known number of tokens between them give the whole allowance:
        #
        #     allowance = tokens between readings / (percent moved / 100)
        #
        # THE CONFOUND THAT DECIDES WHETHER A READING IS USABLE: that percentage covers
        # the WHOLE PLAN, not just this orchestrator. Any other Claude Code use in the
        # same window - another project, another session, the one you are reading this in
        # - moves the percentage without moving AIO's token count, and the estimate comes
        # out too LOW. Take both readings in a window where AIO is the only thing running.
        #
        # PRECISION: a percentage reported in whole numbers carries +/-0.5pp of rounding,
        # so a 1-point move can be wrong by half. Ten points is worth roughly ten times
        # more than one; the estimate below reports its own error bar so a thin reading
        # is visible as thin rather than quoted as fact.
        $store = Join-Path $env:USERPROFILE ".api\AIO-calibration.json"
        $pct = 0.0
        if (-not [double]::TryParse($Arg, [ref]$pct)) {
            Write-Host "Usage: .\AIO.ps1 calib <percent>" -ForegroundColor Yellow
            Write-Host "  The percentage Claude Code currently reports as used this week."
            Write-Host "  Take one reading, run the orchestrator a while, then take another."
            if (Test-Path $store) {
                Write-Host ""
                Write-Host "Readings so far:"
                (Get-Content $store -Raw | ConvertFrom-Json) | ForEach-Object {
                    "  {0}  {1,7:N2}%  {2,12:N0} tokens" -f $_.at, $_.percent, $_.tokens
                }
            }
            break
        }

        try {
            $snap = (Invoke-WebRequest "http://localhost:8081/api/snapshot" -UseBasicParsing -TimeoutSec 15).Content | ConvertFrom-Json
        } catch { throw "Dashboard is not running - start it first, it holds the token counts." }
        $tokens = [int64]$snap.weekly.used

        $obs = @()
        if (Test-Path $store) { $obs = @((Get-Content $store -Raw | ConvertFrom-Json)) }
        $obs += [pscustomobject]@{ at = (Get-Date).ToString("s"); percent = $pct; tokens = $tokens }
        $obs | ConvertTo-Json -Depth 4 | Set-Content $store -Encoding utf8

        Write-Host ""
        Write-Host ("Recorded: {0:N2}% at {1:N0} tokens this week" -f $pct, $tokens) -ForegroundColor Green

        if ($obs.Count -lt 2) {
            Write-Host ""
            Write-Host "That is the first reading. Run the orchestrator for a while, then take"
            Write-Host "another with a bigger gap - ten percentage points beats one by a lot."
            break
        }

        # Use the two readings furthest apart in percentage: the widest gap has the
        # smallest proportional rounding error, which matters more than recency.
        $sorted = $obs | Sort-Object percent
        $lo = $sorted[0]; $hi = $sorted[-1]
        $dPct = $hi.percent - $lo.percent
        $dTok = $hi.tokens - $lo.tokens

        if ($dPct -le 0 -or $dTok -le 0) {
            Write-Host ""
            Write-Host "Cannot estimate yet: the percentage or the token count has not risen" -ForegroundColor Yellow
            Write-Host "between readings. If the weekly window reset, delete $store and start over."
            break
        }

        $est = [math]::Round($dTok / ($dPct / 100.0))
        # Rounding of +/-0.5pp on each reading bounds the error on the difference.
        $estLo = [math]::Round($dTok / (($dPct + 1.0) / 100.0))
        $estHi = [math]::Round($dTok / ([math]::Max($dPct - 1.0, 0.1) / 100.0))

        Write-Host ""
        Write-Host ("Between readings: {0:N0} tokens moved the meter {1:N2} points" -f $dTok, $dPct)
        Write-Host ""
        Write-Host ("  ESTIMATED WEEKLY ALLOWANCE   {0:N0} tokens" -f $est) -ForegroundColor Cyan
        Write-Host ("  plausible range              {0:N0} - {1:N0}" -f $estLo, $estHi)
        Write-Host ""
        if ($dPct -lt 5) {
            Write-Host "THIN READING. Under five points the range above is wide enough to be" -ForegroundColor Yellow
            Write-Host "misleading. Take another reading after more use before trusting it."
        }
        Write-Host "Set it in agents/config.yaml:"
        Write-Host ("  weekly_token_budget: {0}" -f $est)
        Write-Host ""
        Write-Host "This assumes AIO was the only thing spending your plan between readings."
        Write-Host "If it was not, the real allowance is HIGHER than the estimate."
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
    "propose" {
        # The explicit form:  .\AIO.ps1 propose <file.md | R144 | B67>
        Assert-Ready
        if (-not $Arg) {
            Write-Host "Usage: .\AIO.ps1 propose <file.md | R144 | B67>"
            exit 1
        }
        & $Python -m agents propose $Arg --config agents/config.yaml
    }
    default {
        # A BARE REFERENCE IS A COMMAND IN ITSELF:  .\AIO.ps1 R144
        #
        # The work worth doing is usually already written down in the register or the
        # backlog, so naming it should be the whole instruction. Anything shaped like
        # R<number> or B<number> is a proposal rather than a mistyped subcommand, and
        # reading it that way costs nothing because no subcommand looks like that.
        Assert-Ready
        if ($Command -match '^[RrBb]\d+$') {
            & $Python -m agents propose $Command --config agents/config.yaml
        }
        else {
            & $Python -m agents $Command --config agents/config.yaml
        }
    }
}
