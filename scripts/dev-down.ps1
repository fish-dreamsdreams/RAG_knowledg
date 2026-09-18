# Stop processes listening on 8000 (backend) and 5173 (frontend), then the
# Celery worker (no listening port, so it is tracked by its pid file).
# Usage: powershell -ExecutionPolicy Bypass -File scripts\dev-down.ps1
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 using the
# system ANSI codepage (GBK on zh-CN Windows), so non-ASCII text inside quotes
# breaks parsing unless the file is saved with a BOM.
$ErrorActionPreference = 'Continue'

foreach ($port in 8000, 5173) {
  $pids = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
  if (-not $pids) {
    Write-Host "port $port : no listener"
    continue
  }
  foreach ($targetPid in $pids) {
    Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
    Write-Host "port $port : stopped PID $targetPid"
  }
}

$celeryPidFile = Join-Path (Split-Path -Parent $PSScriptRoot) 'backend\var\celery.pid'
if (Test-Path $celeryPidFile) {
  foreach ($line in Get-Content $celeryPidFile) {
    $workerPid = 0
    if ([int]::TryParse($line.Trim(), [ref]$workerPid)) {
      Stop-Process -Id $workerPid -Force -ErrorAction SilentlyContinue
      Write-Host "celery worker : stopped PID $workerPid"
    }
  }
  Remove-Item $celeryPidFile -ErrorAction SilentlyContinue
} else {
  Write-Host "celery worker : no pid file"
}
