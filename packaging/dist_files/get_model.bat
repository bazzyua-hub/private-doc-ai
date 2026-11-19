@echo off
rem LocalDoc AI: download the model (the only step that uses the network). See get_model.ps1
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get_model.ps1"
echo.
pause
