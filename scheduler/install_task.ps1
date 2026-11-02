# Registers a Windows Task Scheduler task that runs the tracker every 5 minutes.
#
# Run from the odo-deal-tracker folder in PowerShell (no admin rights needed):
#     powershell -ExecutionPolicy Bypass -File scheduler\install_task.ps1
# Remove it again with:
#     powershell -ExecutionPolicy Bypass -File scheduler\uninstall_task.ps1
#
# Each tick runs `main.py run`, which itself decides between a full run (every 30 min)
# and a fast check, sends alerts, sends the daily digest and writes logs\tracker.log.

$ErrorActionPreference = "Stop"
$TaskName   = "ODO Deal Tracker"
$ProjectDir = Split-Path -Parent $PSScriptRoot
# pythonw.exe = Python without a console window, so nothing pops up every 5 minutes.
$Python     = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $Python)) {
    throw "Virtual environment not found at $Python. Create it first (see README)."
}

$action = New-ScheduledTaskAction -Execute $Python -Argument "main.py run" -WorkingDirectory $ProjectDir

# Start a minute from now, then repeat every 5 minutes indefinitely.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
# StartWhenAvailable: if a run was missed (e.g. laptop asleep), run as soon as possible after waking.
# IgnoreNew: never start a second copy while one is still running.
# ExecutionTimeLimit: kill a run that hangs for 10 minutes.
# The battery options stop Windows from skipping runs when the charger is unplugged.

# Runs as you, only while you're logged in (no password needs to be stored).
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Installed '$TaskName': runs every 5 minutes starting $((Get-Date).AddMinutes(1).ToString('HH:mm'))."
Write-Host "Log file: $ProjectDir\logs\tracker.log"
Write-Host "Check it in Task Scheduler (taskschd.msc) under Task Scheduler Library."
