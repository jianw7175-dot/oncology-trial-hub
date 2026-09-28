#!/bin/zsh
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo '请先安装 Python 3.10 或更新版本：https://www.python.org/downloads/'
  read '?按回车退出'
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if [ ! -f .venv/setup-complete ]; then
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m playwright install chromium
  touch .venv/setup-complete
fi
echo '开始采集。Ctrl+C 可停止，下次启动自动继续。结果保存在 data 文件夹。'
.venv/bin/python crawler.py
read '?采集结束，按回车退出'
