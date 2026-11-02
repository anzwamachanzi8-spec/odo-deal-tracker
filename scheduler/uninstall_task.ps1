# Removes the tracker's scheduled task.
#     powershell -ExecutionPolicy Bypass -File scheduler\uninstall_task.ps1
Unregister-ScheduledTask -TaskName "ODO Deal Tracker" -Confirm:$false
Write-Host "Removed 'ODO Deal Tracker'."
