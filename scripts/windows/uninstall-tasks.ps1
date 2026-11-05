Stop-ScheduledTask -TaskName "goldtrader-brain" -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName "goldtrader-gateway" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "goldtrader-brain" -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "goldtrader-gateway" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "goldtrader tasks removed"
