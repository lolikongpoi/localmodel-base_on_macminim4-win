@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo Python runtime not found. Run the setup batch file first.
  pause
  exit /b 1
)
"%PYTHON%" "%~dp0mac_tag_app.py"
if errorlevel 1 (
  echo.
  echo Application failed. Keep this window open and copy the error above.
  pause
)
