#!/bin/bash
set -e

echo "==> 正在检查环境..."
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到 python3，请先安装 Python 3。"
    exit 1
fi

echo "==> 正在安装依赖..."
pip3 install -r requirements.txt

echo "==> 开始打包..."
rm -rf build dist

# 关键点：--add-data 必须包含 templates 和 static 两个目录
# macOS 使用冒号 ":" 分隔
pyinstaller --windowed --onefile \
    --name "Shawro AI" \
    --icon "static/icon.icns" \
    --add-data "templates:templates" \
    --add-data "static:static" \
    app.py

echo "==> 打包完成！"
echo "你可以在 dist 文件夹里找到 'Shawro AI.app' 文件，双击即可运行。"