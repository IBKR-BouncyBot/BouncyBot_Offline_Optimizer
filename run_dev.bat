@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Create .venv and install requirements first.
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0main.py" %*
exit /b %ERRORLEVEL%
