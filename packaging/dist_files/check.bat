@echo off
rem LocalDoc AI: self check (does not read or save any document)
cd /d "%~dp0"
chcp 65001 >nul
set OUT=%TEMP%\localdoc_check.txt
del "%OUT%" >nul 2>nul
start /wait "" LocalDocAI.exe --selftest "%OUT%"
echo exit code: %ERRORLEVEL%
if exist "%OUT%" (type "%OUT%") else (echo no report was written)
echo.
pause
