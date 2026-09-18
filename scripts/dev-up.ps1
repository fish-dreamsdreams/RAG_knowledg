# Start backend API and frontend dev server as hidden background processes.
# PIDs and logs go to backend/var/.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\dev-up.ps1
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 using the
# system ANSI codepage (GBK on zh-CN Windows), so non-ASCII text inside quotes
# breaks parsing unless the file is saved with a BOM.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$varDir = Join-Path $root 'backend\var'
New-Item -ItemType Directory -Force -Path $varDir | Out-Null

$backend = Start-Process -FilePath (Join-Path $root 'backend\.venv\Scripts\python.exe') `
  -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000' `
  -WorkingDirectory (Join-Path $root 'backend') -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $varDir 'uvicorn.log') `
  -RedirectStandardError (Join-Path $varDir 'uvicorn.err.log')
$backend.Id | Out-File (Join-Path $varDir 'uvicorn.pid') -Encoding ascii

$frontend = Start-Process -FilePath 'npm.cmd' -ArgumentList 'run', 'dev' `
  -WorkingDirectory (Join-Path $root 'frontend') -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $varDir 'vite.log') `
  -RedirectStandardError (Join-Path $varDir 'vite.err.log')
$frontend.Id | Out-File (Join-Path $varDir 'vite.pid') -Encoding ascii

# Celery worker: document import/indexing and FAQ mining run here. Without it the
# import API returns a task_id but nothing ever parses, and the progress stays at
# "queued" forever - so it belongs to the dev stack, not to a manual step.
# --pool=threads: prefork is not usable on Windows (TECH_SPEC section 3).
$worker = Start-Process -FilePath (Join-Path $root 'backend\.venv\Scripts\python.exe') `
  -ArgumentList '-m', 'celery', '-A', 'app.engines.celery.celery_app', 'worker', '--pool=threads', '--concurrency=4', '--loglevel=info' `
  -WorkingDirectory (Join-Path $root 'backend') -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $varDir 'celery.log') `
  -RedirectStandardError (Join-Path $varDir 'celery.err.log')
$worker.Id | Out-File (Join-Path $varDir 'celery.pid') -Encoding ascii

Write-Host "backend PID=$($backend.Id)  url=http://127.0.0.1:8000/api/v1/health"
Write-Host "frontend PID=$($frontend.Id)  url=http://127.0.0.1:5173"
Write-Host "celery  PID=$($worker.Id)  log=backend\var\celery.log"
Write-Host "logs: backend\var\uvicorn*.log, backend\var\vite*.log, backend\var\celery*.log"
