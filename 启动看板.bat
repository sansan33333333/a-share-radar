@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "data\latest.json" (
  echo 首次运行，正在生成首份报告，请稍候...
  python pipeline.py --force
)
start "" pythonw server.py
timeout /t 2 >nul
start "" http://127.0.0.1:8765
