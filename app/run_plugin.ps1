$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $AppDir

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  Write-Host "Python not found. Please install Python 3.11+ and rerun." -ForegroundColor Red
  exit 1
}

Write-Host "Starting Temu DXM backend plugin UI..."
Write-Host "If dependencies are missing, run: pip install -r requirements.txt"
python webui.py
