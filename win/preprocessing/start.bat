@echo off
setlocal
set "APP_DIR=%~dp0"
set "TCL_LIBRARY=%APP_DIR%tcl_runtime\tcl8.6"
set "TK_LIBRARY=%APP_DIR%tcl_runtime\tk8.6"
set "PYTHONW=%APP_DIR%..\.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=%APP_DIR%.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" (
  echo Python environment is missing. Run setup.bat first.
  pause
  exit /b 1
)
start "" "%PYTHONW%" "%APP_DIR%qq_cache_preclassifier.py" --gui
endlocal
