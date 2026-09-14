@echo off
chcp 65001 >nul
cd /d "%~dp0"
.venv\Scripts\python.exe mock_mac_tag_service.py
