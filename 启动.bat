@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)
if not exist .venv\setup-complete (
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto failed
  .venv\Scripts\python.exe -m playwright install chromium
  if errorlevel 1 goto failed
  type nul > .venv\setup-complete
)
echo 开始采集。Ctrl+C 可停止，下次启动自动继续。结果保存在 data 文件夹。
.venv\Scripts\python.exe crawler.py
pause
exit /b
:failed
echo 安装未完成，请检查 Python 3.10 或更新版本及网络连接。
pause
