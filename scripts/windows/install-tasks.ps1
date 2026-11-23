# Registers the Windows Scheduled Tasks that keep the gateway, the brain, the read-only dashboard
# and (once `goldctl signals-login` has been done) the Telegram signal reader running
# (ARCHITECTURE §4, D-056, D-061).
# Run once from an elevated PowerShell in the repository root after `uv pip install -e ".[dev,windows]"`.
# Secrets are read from Windows Credential Manager (goldctl secret-set) or from the task's environment,
# never from files in the repository.
param(
  [string]$Repo = (Get-Location).Path,
  [string]$Config = "config\demo.yaml",
  [string]$Db = "state\demo.sqlite",
  [string]$Provider = "fake",     # "anthropic" once Phase 5 is approved for demo
  [switch]$SignalsIpv6,           # reach Telegram over IPv6 (networks that block it over IPv4)
  # A second instance next to the demo one (D-081), for example the live account:
  #   -Suffix "-live" -Config config\live.yaml -Db state_live\live.sqlite -GatewayPort 8751 -DashboardPort 8761 -Provider anthropic -NoSignals
  [string]$Suffix = "",           # appended to every task name
  [int]$GatewayPort = 8750,
  [int]$DashboardPort = 8760,
  [switch]$NoSignals              # one Telegram login serves one signal reader: only the first instance runs it
)
$ErrorActionPreference = "Stop"   # a failed registration must stop the script, not print "Registered"
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
  Write-Host "This window is not running as administrator, so the tasks cannot be registered." -ForegroundColor Red
  Write-Host "Open one with:  Start-Process powershell -Verb RunAs   (answer Yes), then run this script again there."
  exit 1
}
$py = Join-Path $Repo ".venv\Scripts\goldctl.exe"
if (-not (Test-Path $py)) { throw "goldctl not found at $py; create the venv first" }

$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Days 365) -StartWhenAvailable -MultipleInstances IgnoreNew `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries  # a laptop on battery must still start and keep running
$trigger = New-ScheduledTaskTrigger -AtLogOn
$gwUrl = "http://127.0.0.1:$GatewayPort"

$gw = New-ScheduledTaskAction -Execute $py -Argument "gateway --real --config $Config --db $Db --port $GatewayPort" -WorkingDirectory $Repo
Register-ScheduledTask -TaskName "goldtrader-gateway$Suffix" -Action $gw -Trigger $trigger -Settings $settings -Force | Out-Null

$br = New-ScheduledTaskAction -Execute $py -Argument "brain --provider $Provider --config $Config --db $Db --gateway-url $gwUrl" -WorkingDirectory $Repo
Register-ScheduledTask -TaskName "goldtrader-brain$Suffix" -Action $br -Trigger $trigger -Settings $settings -Force | Out-Null

$dash = New-ScheduledTaskAction -Execute $py -Argument "dashboard --config $Config --db $Db --gateway-url $gwUrl --port $DashboardPort" -WorkingDirectory $Repo
Register-ScheduledTask -TaskName "goldtrader-dashboard$Suffix" -Action $dash -Trigger $trigger -Settings $settings -Force | Out-Null

$names = "goldtrader-gateway$Suffix, goldtrader-brain$Suffix and goldtrader-dashboard$Suffix"
if ($NoSignals) {
  Write-Host "Signal reader not registered for this instance (-NoSignals)."
} elseif (Test-Path (Join-Path $Repo "state\telegram_user.session")) {
  $sigArgs = "signals --config $Config --db $Db" + $(if ($SignalsIpv6) { " --ipv6" } else { "" })
  $sig = New-ScheduledTaskAction -Execute $py -Argument $sigArgs -WorkingDirectory $Repo
  Register-ScheduledTask -TaskName "goldtrader-signals$Suffix" -Action $sig -Trigger $trigger -Settings $settings -Force | Out-Null
  $names += ", plus goldtrader-signals$Suffix (start it the same way)"
} else {
  Write-Host "Signal reader not registered: no Telegram login yet (goldctl signals-login), then run this script again."
}
Write-Host "Registered $names. Start them with:"
Write-Host "  Start-ScheduledTask goldtrader-gateway$Suffix; Start-Sleep 10; Start-ScheduledTask goldtrader-brain$Suffix; Start-ScheduledTask goldtrader-dashboard$Suffix"
Write-Host "Stop:   Stop-ScheduledTask goldtrader-brain$Suffix; Stop-ScheduledTask goldtrader-gateway$Suffix"
Write-Host "Status: .venv\Scripts\goldctl status --db $Db --gateway-url $gwUrl --config $Config    Dashboard (this PC only): http://127.0.0.1:$DashboardPort"
