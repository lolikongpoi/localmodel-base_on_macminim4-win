@echo off
setlocal
set "APP_DIR=%~dp0"
set "PYTHONW=%APP_DIR%..\.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=%APP_DIR%..\tagging_by_mac\.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" (
  echo Python environment was not found. Please run the existing environment setup first.
  pause
  exit /b 1
)
start "" /D "%APP_DIR%" "%PYTHONW%" "%APP_DIR%aio_app.py"
endlocal
