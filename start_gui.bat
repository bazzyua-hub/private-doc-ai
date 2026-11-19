@echo off
rem LocalDoc AI launcher (Phase 3: GUI). Double-click, or: start_gui.bat file.pdf
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
  echo .venv not found. Run setup.bat first.
  pause
  exit /b 1
)
start "" .venv\Scripts\pythonw.exe gui.py %*
