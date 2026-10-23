@echo off
rem Double-click to install SullyTranslate (runs install.ps1 without changing PowerShell's system policy).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
