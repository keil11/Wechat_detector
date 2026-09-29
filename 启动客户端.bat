@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [错误] 未找到项目虚拟环境 .venv
  echo 请先运行: uv venv .venv --python 3.12
  echo 然后运行: uv pip install --python .venv\Scripts\python.exe -r requirements.txt
  pause
  exit /b 1
)
".venv\Scripts\python.exe" server.py
if errorlevel 1 pause
