@echo off
chcp 65001 >nul
echo =======================================
echo   正在准备打包 Shawro AI 桌面应用
echo =======================================

echo.
echo [1/3] 正在安装依赖...
pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo 依赖安装失败，请检查你的 Python 环境或网络。
    pause
    exit /b
)

echo.
echo [2/3] 正在清理旧文件...
if exist build rd /s /q build
if exist dist rd /s /q dist

echo.
echo [3/3] 开始打包...
:: 关键点：--add-data 必须包含 templates 和 static 两个目录
:: Windows 使用分号 ";" 分隔
pyinstaller --noconsole --onefile --name "Shawro AI" --icon "static\icon.ico" --add-data "templates;templates" --add-data "static;static" app.py

if %ERRORLEVEL% NEQ 0 (
    echo 打包失败，请检查上方报错信息。
    pause
    exit /b
)

echo.
echo =======================================
echo   打包完成！
echo   请前往 dist 文件夹查看 "Shawro AI.exe"
echo =======================================
pause