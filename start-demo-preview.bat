@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0" || exit /b 1
for /f "tokens=1 delims==" %%K in ('set JMCONSOLE_ 2^>nul') do set "%%K="

set "PY_EXE="
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -I -B -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY_EXE=.venv\Scripts\python.exe"
)
if not defined PY_EXE (
    if exist "..\.venv-build\Scripts\python.exe" (
        set "PY_EXE=..\.venv-build\Scripts\python.exe"
    ) else if exist "..\.venv\Scripts\python.exe" (
        set "PY_EXE=..\.venv\Scripts\python.exe"
    ) else (
        set "PY_EXE=python"
    )
)

"%PY_EXE%" -B desktop.py demo
exit /b %errorlevel%
