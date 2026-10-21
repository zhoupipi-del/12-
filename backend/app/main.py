"""应用入口。

开发：uvicorn app.main:app --reload
生产：AUTO_CREATE_TABLES=0 alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi.encoders import jsonable_encoder
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .api import routes_master, routes_ops, routes_plan, routes_supply
from .config import BASE_DIR, settings
from .core.errors import BizError
from .core.security import new_portal_token
from .db import Base, SessionLocal, engine
from . import models  # noqa: F401  确保模型注册

log = logging.getLogger(__name__)


def startup() -> None:
    from .core import guard
    guard.enforce(None)  # 先做不依赖数据库的检查（密钥、CORS、自动建表）
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    if settings.auto_create_tables:
        Base.metadata.create_all(engine)
    if settings.is_production:
        with SessionLocal() as _db:  # 再查库里有没有演示账号/演示密钥
            guard.enforce(_db)
    with SessionLocal() as db:
        from .services.board import get_setting, set_setting
        try:
            from .models import SysMutex
            from .models import now as _now
            for mutex_name in ("kitting", "alert_scan"):
                if db.get(SysMutex, mutex_name) is None:  # 预建互斥行，避免并发首次创建时的间隙锁死锁
                    db.add(SysMutex(name=mutex_name, touched_at=_now()))
            db.commit()
            if not get_setting(db, "board_key"):
                set_setting(db, "board_key", new_portal_token())
                db.commit()
        except Exception:  # noqa: BLE001  —— 表未建（生产环境未迁移）时不阻塞启动
            db.rollback()


def run_scheduled_alert_scan(force: bool = False) -> bool:
    """定时巡检的一次执行；数据库租约保证多进程部署时只有一个实例真正扫描。"""
    from .services import alerts, kitting
    from .models import SysMutex, now
    with SessionLocal() as db:
        try:
            lease = db.scalar(select(SysMutex).where(SysMutex.name == "alert_scan").with_for_update())
            if lease is None:
                lease = SysMutex(name="alert_scan")
                db.add(lease)
                db.flush()
            cutoff = now() - timedelta(minutes=max(settings.alert_scan_minutes, 1))
            if not force and lease.touched_at and lease.touched_at >= cutoff:
                db.rollback()
                return False
            kitting.recalc(db, commit=False)
            alerts.scan_all(db, commit=False)
            lease.touched_at = now()
            db.commit()
            return True
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("U9 自动预警巡检失败")
            return False


async def _alert_loop(stop: asyncio.Event) -> None:
    seconds = max(settings.alert_scan_minutes, 1) * 60
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            await asyncio.to_thread(run_scheduled_alert_scan)


@asynccontextmanager
async def lifespan(_: FastAPI):
    startup()
    stop = asyncio.Event()
    task = None
    if settings.app_env != "test" and settings.alert_scan_minutes > 0:
        task = asyncio.create_task(_alert_loop(stop))
    try:
        yield
    finally:
        stop.set()
        if task:
            await task


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan,
              description="订单 → BOM 展开 → 齐套冲填 → 采购/自制/外协跟单 → 到货检验/来料异常 → 领料装配 → 发货 的生产闭环")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(BizError)
def biz_error(_: Request, e: BizError):
    return JSONResponse(jsonable_encoder({"detail": e.message, "data": e.data}), status_code=e.status_code)


@app.exception_handler(IntegrityError)
def integrity_error(_: Request, e: IntegrityError):
    return JSONResponse({"detail": "数据冲突（编码重复或被引用），请检查后重试"}, status_code=409)


for r in (routes_master.router, routes_plan.router, routes_supply.router, routes_ops.router):
    app.include_router(r, prefix="/api")
app.include_router(routes_ops.open_router, prefix="/api")


@app.get("/api/health")
def health():
    return {"ok": True}


# 前端打包产物（frontend/dist）由后端直接托管，单端口部署
DIST = Path(BASE_DIR).parent / "frontend" / "dist"
if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        # 只允许返回前端目录内的文件：解析后（含符号链接、绝对路径、..）必须仍在 DIST 之下，否则回退到首页
        root = DIST.resolve()
        if full_path:
            try:
                f = (root / full_path).resolve()
                if f.is_file() and f.is_relative_to(root):
                    return FileResponse(f)
            except (OSError, ValueError):
                pass
        return FileResponse(root / "index.html")
