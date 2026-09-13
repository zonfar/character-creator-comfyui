@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3 was not found. Install it from https://python.org and try again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if not exist "config.json" copy "config.example.json" "config.json" >nul
echo Setup complete. Edit config.json if ComfyUI is installed somewhere else.
pause
