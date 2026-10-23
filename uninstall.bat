@echo off
rem Double-click to uninstall SullyTranslate. Leaves this folder first so it can be deleted, all on one line
rem because this file is deleted while it runs.
cd /d "%TEMP%" & powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" & exit /b
