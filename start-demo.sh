#!/usr/bin/env sh
# Linux/macOS 演示启动：需要 Python 3.12+
export APP_ENV=demo
cd "$(dirname "$0")/backend"
pip install -r requirements.txt  # 建议在虚拟环境里：python3.12 -m venv .venv && . .venv/bin/activate
[ -f prodsync.db ] || python -m app.seed
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
