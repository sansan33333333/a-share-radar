@echo off
chcp 65001 >nul
cd /d "%~dp0"
python pipeline.py --force
echo.
echo 报告已更新：http://127.0.0.1:8765
pause
