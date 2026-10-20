@echo off
chcp 65001 >nul
REM Windows 演示启动：需要 Python 3.12+；前端已打包在 frontend\dist
REM 演示模式（带演示账号和数据），不要用于正式环境；正式部署见 README
set APP_ENV=demo
cd /d %~dp0backend
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
if not exist prodsync.db python -m app.seed
start http://127.0.0.1:8000
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
