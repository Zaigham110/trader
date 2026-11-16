# Preflight after secrets are stored (docs/WINDOWS_SETUP.md Part D). MT5 must be running and logged in.
# Runs doctor, llm-check, the read-only and trading on-account suites (demo only) and the spec dump,
# and writes everything the developer asked for into reports\mt5\.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$g = ".venv\Scripts\goldctl"
New-Item -ItemType Directory -Force -Path reports\mt5 | Out-Null
Write-Host "== doctor" -ForegroundColor Cyan
& $g doctor --config config\demo.yaml --db state\demo.sqlite | Tee-Object reports\mt5\doctor.txt
Write-Host "== llm-check" -ForegroundColor Cyan
& $g llm-check | Tee-Object reports\mt5\llm-check.txt
Write-Host "== mt5-tests read-only" -ForegroundColor Cyan
& $g mt5-tests --real --no-trade --out reports\mt5\readonly.md
Write-Host "== mt5-tests with 0.01-lot demo trades" -ForegroundColor Cyan
& $g mt5-tests --real --trade --out reports\mt5\trade.md
Write-Host "== spec-dump" -ForegroundColor Cyan
& $g spec-dump --real --db state\demo.sqlite | Tee-Object reports\mt5\spec.json
Write-Host "`nSend the developer the files in reports\mt5\ (doctor.txt, llm-check.txt, readonly.md, trade.md, spec.json)." -ForegroundColor Green
