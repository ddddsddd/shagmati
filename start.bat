@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "Chess AI server" cmd /k python ai_server.py
start "Chess game server" cmd /k python server.py
timeout /t 3 >nul
start "" http://127.0.0.1:8000
