@echo off
setlocal
set "APP_DIR=%~dp0"
set "PYTHONW=%APP_DIR%..\.venv\Scripts\pythonw.exe"
set "APP_SCRIPT=%APP_DIR%tag_move_app.py"

if not exist "%PYTHONW%" goto :missing_runtime
if not exist "%APP_SCRIPT%" goto :missing_script

start "" /D "%APP_DIR%" "%PYTHONW%" "%APP_SCRIPT%"
exit /b 0

:missing_runtime
echo Python runtime was not found:
echo %PYTHONW%
echo Create it from the repository root with:
echo cd win
echo py -3 -m venv .venv
pause
exit /b 1

:missing_script
echo Application script was not found:
echo %APP_SCRIPT%
pause
exit /b 1
