@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_tests.ps1" %*
set "code=%ERRORLEVEL%"
if not "%BOUNCYBOT_OPTIMIZER_NO_PAUSE%"=="1" pause
exit /b %code%
