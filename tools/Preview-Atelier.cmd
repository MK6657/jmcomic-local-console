@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0JMComic.exe" (
    "%~dp0JMComic.exe" demo
) else if exist "%~dp0..\start-demo-preview.bat" (
    call "%~dp0..\start-demo-preview.bat"
    exit /b %errorlevel%
) else (
    echo JMComic.exe not found. Run start-demo-preview.bat from the source root.
    exit /b 1
)
if errorlevel 1 pause
