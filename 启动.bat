@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo 未检测到 Python，请先安装 Python 3.10 及以上版本，并在安装时勾选 "Add python.exe to PATH"。
    pause
    exit /b 1
)

python -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo 首次运行，正在安装依赖 PySide6（需要联网，约 80MB）...
    python -m pip install PySide6
)

start "" pythonw "%~dp0main.py"
exit /b 0