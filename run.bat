@echo off
chcp 65001 >nul
cd /d %~dp0
where python >nul 2>nul || (echo 没找到 Python，请先安装 Python 3.11+（安装时勾选 Add to PATH）& pause & exit /b 1)
if not exist .venv (
    echo 正在创建虚拟环境…
    python -m venv .venv
)
call .venv\Scripts\activate
echo 正在安装依赖（第一次较慢）…
pip install -q -r requirements.txt
set PYTHONPATH=src
echo 启动播放器…
python -m player.app
pause
