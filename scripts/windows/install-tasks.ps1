# Registers two Windows Scheduled Tasks that keep the gateway and the brain running (ARCHITECTURE §4).
# Run once from an elevated PowerShell in the repository root after `uv pip install -e ".[dev,windows]"`.
# Secrets are read from Windows Credential Manager (goldctl secret-set) or from the task's environment,
# never from files in the repository.
param(
  [string]$Repo = (Get-Location).Path,
  [string]$Config = "config\demo.yaml",
  [string]$Db = "state\demo.sqlite",
  [string]$Provider = "fake"      # "anthropic" once Phase 5 is approved for demo
)
$py = Join-Path $Repo ".venv\Scripts\goldctl.exe"
if (-not (Test-Path $py)) { throw "goldctl not found at $py; create the venv first" }

$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Days 365) -StartWhenAvailable -MultipleInstances IgnoreNew
$trigger = New-ScheduledTaskTrigger -AtLogOn

$gw = New-ScheduledTaskAction -Execute $py -Argument "gateway --real --config $Config --db $Db" -WorkingDirectory $Repo
Register-ScheduledTask -TaskName "goldtrader-gateway" -Action $gw -Trigger $trigger -Settings $settings -Force | Out-Null

$br = New-ScheduledTaskAction -Execute $py -Argument "brain --provider $Provider --config $Config --db $Db" -WorkingDirectory $Repo
Register-ScheduledTask -TaskName "goldtrader-brain" -Action $br -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host "Registered goldtrader-gateway and goldtrader-brain. Start them with:"
Write-Host "  Start-ScheduledTask goldtrader-gateway; Start-Sleep 10; Start-ScheduledTask goldtrader-brain"
Write-Host "Stop:   Stop-ScheduledTask goldtrader-brain; Stop-ScheduledTask goldtrader-gateway"
Write-Host "Status: .venv\Scripts\goldctl status"
