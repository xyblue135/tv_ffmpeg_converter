@echo off
rem 老电视兼容模式 · 视频转换器 启动脚本
chcp 65001 >nul
set "DIR=%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%DIR%gui.py"
    exit /b
)

python "%DIR%gui.py"
if %errorlevel% neq 0 pause
