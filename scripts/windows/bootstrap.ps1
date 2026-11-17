# One-shot installer for the owner's Windows machine (docs/WINDOWS_SETUP.md Parts A–B).
# Run in PowerShell (normal user is fine):
#   Set-ExecutionPolicy -Scope Process Bypass; .\scripts\windows\bootstrap.ps1
# Or, before cloning:
#   irm https://raw.githubusercontent.com/Zaigham110/trader/claude/ecstatic-clarke-8nleqk/scripts/windows/bootstrap.ps1 | iex
# It installs Git, Python 3.12 and uv with winget if missing, clones the repository to $HOME\trader
# on the development branch, creates the virtual environment, installs the package with the
# Windows extras (MetaTrader5, keyring) and runs the test suite. It never asks for or stores secrets.
$env:PYTHONUTF8 = "1"
$ErrorActionPreference = "Stop"
$Branch = "claude/ecstatic-clarke-8nleqk"
$Repo = "https://github.com/Zaigham110/trader.git"
$Dest = Join-Path $HOME "trader"

function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }

Step "Checking winget"
if (-not (Have winget)) {
  throw "winget is not available. Install 'App Installer' from the Microsoft Store, then rerun."
}

Step "Git"
if (-not (Have git)) { winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements | Out-Null }

Step "Python 3.12 (64-bit)"
$pyOk = $false
if (Have python) {
  $v = (& python --version 2>&1)
  if ($v -match "Python 3\.(1[2-9])") { $pyOk = $true }
}
if (-not $pyOk) { winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements | Out-Null }

Step "uv"
if (-not (Have uv)) { powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" }

# refresh PATH for this session so freshly installed tools resolve
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
foreach ($c in @("git", "uv")) { if (-not (Have $c)) { throw "$c still not on PATH; close and reopen PowerShell, then rerun this script." } }

Step "Repository -> $Dest ($Branch)"
if (-not (Test-Path $Dest)) { git clone $Repo $Dest }
Set-Location $Dest
git fetch origin $Branch
git checkout $Branch
git pull --ff-only origin $Branch

Step "Virtual environment and package"
if (-not (Test-Path ".venv")) { uv venv .venv }
uv pip install -e ".[dev,windows]"

Step "MetaTrader5 package import"
& .venv\Scripts\python -c "import MetaTrader5 as m; print('MetaTrader5', getattr(m, '__version__', 'ok'))"

New-Item -ItemType Directory -Force -Path state, reports\mt5, reports\phase7, data\bars | Out-Null

Step "Tests (about 2 minutes; output saved to reports\pytest-windows.txt)"
& .venv\Scripts\python -m pytest -q -rf --tb=short 2>&1 | Tee-Object -FilePath reports\pytest-windows.txt | Select-Object -Last 25
if ($LASTEXITCODE -ne 0) {
  Write-Host "Some tests failed on this machine. The install is complete; send reports\pytest-windows.txt to the developer before starting the system." -ForegroundColor Yellow
}

Write-Host "`nInstall complete. Next (docs/WINDOWS_SETUP.md Part C):" -ForegroundColor Green
Write-Host "  1. Install MetaTrader 5 from Exness, log into the DEMO account, enable 'Allow algorithmic trading'."
Write-Host "  2. Store secrets:  .venv\Scripts\goldctl secret-set MT5_LOGIN   (then MT5_PASSWORD, MT5_SERVER,"
Write-Host "     ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_CHAT_ID, GATEWAY_TOKEN)"
Write-Host "  3. Preflight:      .\scripts\windows\preflight.ps1"
