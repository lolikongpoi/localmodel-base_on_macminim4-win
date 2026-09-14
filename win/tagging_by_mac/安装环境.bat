@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  if exist "..\.venv\Scripts\python.exe" (
    "..\.venv\Scripts\python.exe" -m venv .venv
  ) else (
    py -3 -m venv .venv
  )
)
.venv\Scripts\python.exe -m pip install --upgrade pip
echo.
echo Setup complete. The launcher uses this directory's virtual environment.
pause
