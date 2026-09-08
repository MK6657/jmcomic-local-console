@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto :failed
)
echo Installing / checking project dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" launcher.py --wait
if errorlevel 1 goto :failed
exit /b 0
:failed
echo Startup failed. See runtime\logs\launcher.log or run .venv\Scripts\python.exe app.py
pause
exit /b 1
