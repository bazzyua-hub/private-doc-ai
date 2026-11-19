@echo off
rem LocalDoc AI launcher (Phase 1: command line). Usage: start.bat file.pdf [--mode detail]
cd /d "%~dp0"
chcp 65001 >nul
if not exist .venv\Scripts\python.exe (
  echo .venv not found. Run setup.bat first.
  exit /b 1
)
.venv\Scripts\python.exe main.py %*
