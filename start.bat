@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" launcher.py --source-setup --wait
) else (
    python launcher.py --source-setup --wait
)
if errorlevel 1 goto :failed
exit /b 0
:failed
echo Startup failed. See runtime\logs\launcher.log or run .venv\Scripts\python.exe app.py
pause
exit /b 1
