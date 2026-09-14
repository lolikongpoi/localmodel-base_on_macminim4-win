@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo 尚未安装独立环境，请先双击“安装环境.bat”。
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" mac_tag_app.py
