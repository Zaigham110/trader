# Packs the databases of one instance into a zip, to move the instance to another machine
# (for example a cloud server). Each database is copied with SQLite's own backup, so the copy is
# complete even while the services are running. For the final move, stop the brain and the gateway
# first, so nothing is written here after the pack is made.
#   .\scripts\windows\pack-state.ps1                      -> state-pack-<date>.zip in the repository root
#   .\scripts\windows\pack-state.ps1 -Out D:\move.zip
# On the other machine: unzip into the repository's state folder BEFORE the first start.
# Not packed: logs, and the Telegram signal login (run `goldctl signals-login` again there).
# Secrets are in Windows Credential Manager and are never packed: store them again with `goldctl secret-set`.
param(
  [string]$Repo = (Get-Location).Path,
  [string]$StateDir = "state",
  [string]$Out = ""
)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$py = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "python not found at $py" }
$src = Join-Path $Repo $StateDir
if (-not (Test-Path $src)) { throw "state folder $src not found" }
if (-not $Out) { $Out = Join-Path $Repo ("state-pack-" + (Get-Date -Format "yyyyMMdd-HHmm") + ".zip") }
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("goldtrader-pack-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  $code = @'
import sqlite3, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
for f in sorted(src.glob("*.sqlite")):
    a = sqlite3.connect(f"{f.resolve().as_uri()}?mode=ro", uri=True, timeout=60)
    b = sqlite3.connect(dst / f.name)
    a.backup(b)
    ok = b.execute("PRAGMA integrity_check").fetchone()[0]
    a.close(); b.close()
    if ok != "ok":
        sys.exit(f"{f.name}: copy failed the integrity check ({ok})")
    print(f"{f.name:28} {(dst / f.name).stat().st_size / 1e6:8.1f} MB  ok")
'@
  $script = Join-Path $tmp "_pack.py"
  Set-Content -Path $script -Value $code -Encoding UTF8
  & $py $script $src $tmp
  if ($LASTEXITCODE -ne 0) { throw "packing failed; nothing was written to $Out" }
  Remove-Item $script
  if (Test-Path $Out) { Remove-Item $Out }
  Compress-Archive -Path (Join-Path $tmp "*.sqlite") -DestinationPath $Out
  Write-Host ("Packed to {0} ({1:N1} MB)" -f $Out, ((Get-Item $Out).Length / 1MB))
} finally {
  Remove-Item -Recurse -Force $tmp
}
