# Nightly SQLite backup with 14-day retention (ARCHITECTURE §4). Schedule daily at 23:30 local.
param([string]$Db = "state\demo.sqlite", [string]$Dir = "backups")
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$dest = Join-Path $Dir ("demo-" + $stamp + ".sqlite")
# Use sqlite's online backup through Python so a live WAL database copies consistently
$code = "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()"
& .venv\Scripts\python.exe -c $code $Db $dest
Get-ChildItem $Dir -Filter "demo-*.sqlite" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } | Remove-Item
Write-Host "backup written to $dest"
