@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "source_setup="
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -I -B -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        ".venv\Scripts\python.exe" launcher.py --source-setup --wait
        goto :done
    )
    set "source_setup=--source-setup"
)
if exist "..\.venv-build\Scripts\python.exe" (
    "..\.venv-build\Scripts\python.exe" launcher.py %source_setup% --wait
) else if exist "..\.venv\Scripts\python.exe" (
    "..\.venv\Scripts\python.exe" launcher.py %source_setup% --wait
) else (
    python launcher.py --source-setup --wait
)
:done
if errorlevel 1 goto :failed
exit /b 0
:failed
echo Startup failed. See runtime\logs\launcher.log or run python app.py
pause
exit /b 1
