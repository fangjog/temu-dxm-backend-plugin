@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Please install Python 3.11+ and rerun.
  pause
  exit /b 1
)
echo Starting Temu DXM backend plugin UI...
echo If dependencies are missing, run: pip install -r requirements.txt
python webui.py
pause
