@echo off
rem LocalDoc AI - first-time setup (creates .venv and installs packages). Needs internet.
cd /d "%~dp0"
set PY=
for %%V in (3.12 3.11 3.13) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set PY=py -%%V
  )
)
if not defined PY (
  where py >nul 2>nul && set PY=py -3
)
if not defined PY set PY=python
echo Using: %PY%
if exist .venv\Scripts\python.exe goto :have_venv
echo Creating virtual environment...
%PY% -m venv .venv || goto :error
:have_venv
.venv\Scripts\python.exe --version
.venv\Scripts\python.exe -m pip install --upgrade pip || goto :error
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt || goto :error
echo.
echo Done. Next (PowerShell):
echo   .\.venv\Scripts\python.exe tools\setup_assets.py llama
echo   .\.venv\Scripts\python.exe tools\setup_assets.py model 1.7b
echo   .\start.bat --check
echo Then start the GUI by double-clicking start_gui.bat
goto :eof
:error
echo Setup failed. See the messages above.
exit /b 1
