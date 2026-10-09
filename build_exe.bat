@echo off
rem Phase 5 正式打包用；Phase 0 也可以先打一个尝鲜版 exe
chcp 65001 >nul
cd /d %~dp0
call .venv\Scripts\activate 2>nul || (echo 请先运行一次 run.bat & pause & exit /b 1)
pip install -q pyinstaller
pyinstaller --noconfirm --clean --windowed --name 音乐播放器 ^
    --add-data "resources;resources" ^
    src/player/app.py
echo.
echo 打包完成，exe 在 dist\音乐播放器\ 目录里
pause
