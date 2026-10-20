"""生产启动检查：正式环境拒绝默认密钥、默认账号、演示数据、通配 CORS、自动建表。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import DEFAULT_SECRET, settings
from .security import verify_password

# 演示库里出现过的账号和密码；正式库里只要还有任何一组能登录，就拒绝启动
DEMO_CREDENTIALS = [("admin", "admin123")] + [(u, "123456") for u in
                    ("zhuguan", "jihua", "caigou", "cangku", "zhijian", "chejian", "jishu", "shouhou", "xiaoshou")]
DEMO_BOARD_KEYS = {"board-demo"}
DEMO_PORTAL_TOKENS = {"demo-supplier-s001"}


def problems(db: Session | None = None) -> list[str]:
    out: list[str] = []
    if len(settings.secret_key) < 32 or settings.secret_key in {DEFAULT_SECRET, "please-change-this-secret"}:
        out.append("SECRET_KEY 未设置或太弱（至少 32 位随机字符，例如 openssl rand -hex 32）")
    if "*" in settings.cors_origins:
        out.append("CORS_ORIGINS 不能是 *，请填写实际访问域名")
    if settings.auto_create_tables:
        out.append("AUTO_CREATE_TABLES 必须为 0，表结构只通过 alembic upgrade head 变更")
    if db is not None:
        from ..models import Setting, Supplier, User
        for un, pw in DEMO_CREDENTIALS:
            u = db.scalar(select(User).where(User.username == un))
            if u and u.is_active and verify_password(pw, u.password_hash):
                out.append(f"账号 {un} 仍在使用演示默认密码，请改密或停用")
        k = db.get(Setting, "board_key")
        if k and k.value in DEMO_BOARD_KEYS:
            out.append("看板密钥仍是演示值 board-demo")
        for sp in db.scalars(select(Supplier)).all():
            if getattr(sp, "portal_token", "") in DEMO_PORTAL_TOKENS:
                out.append(f"供应商 {sp.name} 的门户令牌仍是演示值")
    return out


def enforce(db: Session | None = None) -> None:
    if not settings.is_production:
        return
    errs = problems(db)
    if errs:
        raise RuntimeError("生产环境启动检查未通过（APP_ENV=production）：\n  - " + "\n  - ".join(errs)
                           + "\n演示/开发请设置 APP_ENV=demo；正式环境请先用 python -m app.cli create-admin 创建管理员。")
