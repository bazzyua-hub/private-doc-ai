@echo off
rem LocalDoc AI: build the distribution zip (Windows). Needs internet (downloads llama-server).
rem   build.bat                -> LocalDocAI-x.y.z-win64.zip (without model)
rem   build.bat --with-model   -> also bundles the model (about 2GB)
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo .venv not found. Run setup.bat first.
  exit /b 1
)
.venv\Scripts\python.exe -m pip install -r requirements-build.txt || exit /b 1
.venv\Scripts\python.exe tools\build_package.py %*
if errorlevel 1 exit /b 1
echo.
echo Done. See the build folder.
