@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3.11 -m venv .venv
)
.venv\Scripts\python.exe -m pip install --upgrade pip
echo.
echo 环境安装完成。此客户端不需要额外的第三方 Python 包。
pause
