# install.ps1
#
# One script for everything that needs registering as a Windows Task
# Scheduler task in this project: the webapp (control panel), the site
# crawler, and the packet reviewer (OCR/applicant-detail pass).
#
# WHY TASK SCHEDULER INSTEAD OF A WINDOWS SERVICE:
#   Modeled on trade-relay/execution-engine/install.ps1's approach for the
#   same reason that applies here too, in a milder form: Task Scheduler
#   running "at logon" in the interactive session is simpler to reason
#   about and debug than a Session-0 service, with no extra service-wrapper
#   code required. Each component actually runs via pythonw.exe (the
#   windowless interpreter) rather than the console-mode muni-*.exe
#   wrappers pip generates, so nothing pops up a terminal window at logon
#   or at the nightly batch times — check data/webapp.log,
#   data/crawl_engine.log, and data/review_engine.log for output instead
#   of watching a console live.
#
# COMPONENTS:
#   webapp - persistent. Logon trigger, restarts itself on crash via the
#            task's own RestartCount (no separate watchdog process - unlike
#            trade-relay's MT5 agent, a Flask dev server has no "hangs
#            without exiting" failure mode tied to an external terminal,
#            so Task Scheduler's built-in restart-on-failure-exit is
#            sufficient here).
#   crawl   - recurring batch job. Runs once daily (or --now for an
#             immediate one-off run), works through whatever municipalities
#             are still 'unchecked' plus flags anything freshly added.
#             Finishes and exits - no persistent process, no watchdog.
#   review  - recurring batch job, same shape as crawl: reviews whatever
#             packets don't have a review_method yet (see review_packets.py).
#
# Usage:
#   Everything (webapp + crawl + review):
#     powershell -ExecutionPolicy Bypass -File install.ps1
#     powershell -ExecutionPolicy Bypass -File install.ps1 uninstall
#     powershell -ExecutionPolicy Bypass -File install.ps1 update
#
#   One component only:
#     powershell -ExecutionPolicy Bypass -File install.ps1 -Component webapp
#     powershell -ExecutionPolicy Bypass -File install.ps1 -Component crawl uninstall
#
#   Run a recurring job's action immediately instead of waiting for its
#   next scheduled time (webapp is always started regardless of this flag):
#     powershell -ExecutionPolicy Bypass -File install.ps1 -Now

param(
    [ValidateSet("install", "uninstall", "update")]
    [string]$Action = "install",

    [ValidateSet("webapp", "crawl", "review", "all")]
    [string]$Component = "all",

    [string]$VenvName = "venv",

    # Also start crawl/review's task immediately after registering it,
    # rather than waiting for the next scheduled trigger time.
    [switch]$Now
)

$ErrorActionPreference = "Stop"
$ProjectDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskFolder  = "\Muni Packet Crawler\"
$VenvScripts = Join-Path $ProjectDir "$VenvName\Scripts"

# ── Runs install/uninstall/update for exactly one component ────────────────
function Invoke-ForComponent([string]$component, [string]$action) {
    # Console-script wrappers (muni-webapp.exe etc.) are always built for the
    # console subsystem by setuptools — running one under Task Scheduler's
    # "AtLogOn, Interactive" trigger leaves a visible terminal window sitting
    # open for as long as the process runs. pythonw.exe is the windowless
    # Python interpreter that ships with every install/venv; running the
    # actual .py entry point through it produces no window at all. The
    # console .exe's existence is still checked below, purely as a signal
    # that `pip install -e .` has actually been run for this venv — it's
    # never executed.
    $pythonw = Join-Path $VenvScripts "pythonw.exe"

    switch ($component) {
        "webapp" {
            $taskName      = "Muni Webapp"
            $description   = "Muni Packet Crawler control panel - dashboard, engine controls, packet review"
            $installMarker = Join-Path $VenvScripts "muni-webapp.exe"
            $exe           = $pythonw
            $scriptArgs    = "`"$(Join-Path $ProjectDir 'src\webapp.py')`""
            $persistent    = $true
        }
        "crawl" {
            $taskName      = "Muni Crawl"
            $description   = "Muni Packet Crawler - crawls municipal sites for meeting packets (recurring daily batch)"
            $installMarker = Join-Path $VenvScripts "muni-crawl.exe"
            $exe           = $pythonw
            $scriptArgs    = "`"$(Join-Path $ProjectDir 'src\crawl_sites.py')`""
            $persistent    = $false
        }
        "review" {
            $taskName      = "Muni Review"
            $description   = "Muni Packet Crawler - OCR/applicant-detail pass over saved packets (recurring daily batch)"
            $installMarker = Join-Path $VenvScripts "muni-review-packets.exe"
            $exe           = $pythonw
            $scriptArgs    = "`"$(Join-Path $ProjectDir 'src\review_packets.py')`""
            $persistent    = $false
        }
    }

    function Stop-TaskNow([string]$name) {
        $t = Get-ScheduledTask -TaskName $name -TaskPath $TaskFolder -ErrorAction SilentlyContinue
        if ($t -and $t.State -eq "Running") {
            Write-Host "  Stopping running task..."
            Stop-ScheduledTask -TaskName $name -TaskPath $TaskFolder -ErrorAction SilentlyContinue
            Start-Sleep 2
        }
    }
    function Remove-TaskNow([string]$name) {
        Stop-TaskNow $name
        if (Get-ScheduledTask -TaskName $name -TaskPath $TaskFolder -ErrorAction SilentlyContinue) {
            Write-Host "  Removing existing task: $name"
            Unregister-ScheduledTask -TaskName $name -TaskPath $TaskFolder -Confirm:$false
        }
    }
    function Cleanup-Orphans {
        # Matching only on the full absolute project path misses anything
        # started with a relative path (e.g. `cd project && python
        # src\webapp.py`, the normal way to run this by hand for dev/
        # testing) — that process's CommandLine never contains $ProjectDir
        # at all, so it silently evades cleanup. Werkzeug's dev server sets
        # SO_REUSEADDR, so a second instance binding the same port doesn't
        # even error — it just silently coexists, and which one actually
        # answers a given request becomes unpredictable. Matching on the
        # known entry-point script names catches both launch styles.
        $escapedDir = [regex]::Escape($ProjectDir)
        $scriptNames = "webapp\.py|crawl_sites\.py|review_packets\.py"
        Get-CimInstance Win32_Process | Where-Object {
            $_.ProcessId -ne $PID -and
            $_.CommandLine -and
            ($_.CommandLine -match $escapedDir -or $_.CommandLine -match $scriptNames) -and
            ($_.Name -like "muni-*" -or $_.Name -like "python*")
        } | ForEach-Object {
            Write-Host "  Stopping orphan PID $($_.ProcessId): $($_.Name)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }

    function Do-Install {
        if (-not (Test-Path -LiteralPath $installMarker)) {
            Write-Host ""
            Write-Host "ERROR: $installMarker not found." -ForegroundColor Red
            Write-Host "  Set up the venv and install this project into it first:" -ForegroundColor Yellow
            Write-Host "    python -m venv $VenvName" -ForegroundColor Yellow
            Write-Host "    $VenvName\Scripts\pip install -e `".[ocr]`"" -ForegroundColor Yellow
            Write-Host "  (the [ocr] extra is optional - only needed for the OCR fallback in review)" -ForegroundColor DarkGray
            exit 1
        }
        if (-not (Test-Path -LiteralPath $exe)) {
            Write-Host ""
            Write-Host "ERROR: $exe not found (expected pythonw.exe alongside the venv's python.exe)." -ForegroundColor Red
            exit 1
        }

        Remove-TaskNow $taskName
        Cleanup-Orphans

        Write-Host ""
        Write-Host "  Registering scheduled task..."
        Write-Host "    Task : $TaskFolder$taskName"
        Write-Host "    Exe  : $exe $scriptArgs"
        Write-Host "    CWD  : $ProjectDir"

        $taskAction = New-ScheduledTaskAction -Execute $exe -Argument $scriptArgs -WorkingDirectory $ProjectDir

        if ($persistent) {
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
            $trigger.Delay = "PT10S"
            $settings = New-ScheduledTaskSettingsSet `
                -MultipleInstances  IgnoreNew `
                -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -RestartCount       10 `
                -RestartInterval    (New-TimeSpan -Minutes 1) `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -StartWhenAvailable
        } else {
            # Daily recurring batch job - runs to completion, no restart
            # policy needed since a mid-run failure just gets picked up
            # again at the next scheduled run (crawl/review both resume
            # from "not yet processed" state, never redo finished work
            # unless --recheck/--rereview is passed manually).
            $trigger = New-ScheduledTaskTrigger -Daily -At 2:00AM
            $settings = New-ScheduledTaskSettingsSet `
                -MultipleInstances  IgnoreNew `
                -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -StartWhenAvailable
        }

        $principal = New-ScheduledTaskPrincipal `
            -UserId    "$env:USERDOMAIN\$env:USERNAME" `
            -LogonType Interactive `
            -RunLevel  Limited

        Register-ScheduledTask `
            -TaskName    $taskName `
            -TaskPath    $TaskFolder `
            -Action      $taskAction `
            -Trigger     $trigger `
            -Settings    $settings `
            -Principal   $principal `
            -Description $description `
            -Force | Out-Null

        if ($persistent -or $Now) {
            Write-Host "  Starting task now..."
            Start-ScheduledTask -TaskName $taskName -TaskPath $TaskFolder
            Start-Sleep 2
            $state = (Get-ScheduledTask -TaskName $taskName -TaskPath $TaskFolder).State
            Write-Host "  Task state: $state" -ForegroundColor $(if ($state -eq "Running") { "Green" } else { "Yellow" })
        } else {
            Write-Host "  Scheduled for 2:00 AM daily (pass -Now to also start it immediately)."
        }

        if ($taskName -eq "Muni Webapp") {
            Write-Host ""
            Write-Host "  Control panel: http://localhost:5000"
            Write-Host "  First visit registers your authenticator app (TOTP) - see auth.py."
        }
    }

    switch ($action) {
        "uninstall" {
            Remove-TaskNow $taskName
            Cleanup-Orphans
            Write-Host "Uninstalled: $taskName"
        }
        "update" { Do-Install }
        default  { Do-Install }
    }
}

# ── Entry point ──────────────────────────────────────────────────────────
$components = if ($Component -eq "all") { @("webapp", "crawl", "review") } else { @($Component) }
foreach ($c in $components) {
    Write-Host ""
    Write-Host "=== $Action $c ===" -ForegroundColor Cyan
    Invoke-ForComponent -component $c -action $Action
}
