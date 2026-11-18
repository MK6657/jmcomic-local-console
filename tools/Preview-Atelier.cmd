@echo off
setlocal
cd /d "%~dp0"
"%~dp0JMComic.exe" demo
if errorlevel 1 pause
