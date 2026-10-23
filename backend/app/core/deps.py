from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ApiKey, Role, User
from .security import decode_token, hash_api_key

bearer = HTTPBearer(auto_error=False)

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
NO_LOCK_PREFIXES = ("/api/auth/", "/api/ai/")


def lock_for_write(request: Request, db: Session) -> None:
    """写请求在身份校验通过后、读取/修改业务单据之前，先拿齐套全局锁（顺序：全局锁 → 单据行锁 → 重算），
    避免“完工先锁订单、收货先锁来源、最后重算再争全局锁”的相反顺序。只读请求、登录和 AI 辅助查询不拿锁。"""
    if request.method in WRITE_METHODS and not request.url.path.startswith(NO_LOCK_PREFIXES):
        from ..services.kitting import acquire_mutex
        acquire_mutex(db)


def current_user(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(bearer),
                 db: Session = Depends(get_db)) -> User:
    if not cred:
        raise HTTPException(401, "未登录")
    uid = decode_token(cred.credentials)
    user = db.get(User, uid) if uid else None
    if not user or not user.is_active:
        raise HTTPException(401, "登录已失效，请重新登录")
    lock_for_write(request, db)
    return user


def require(*roles: str) -> Callable[..., User]:
    """角色校验：管理员拥有全部权限；一个账号可兼多个角色（减少多账号切换）。"""
    def dep(user: User = Depends(current_user)) -> User:
        mine = set(user.role_list)
        if Role.ADMIN in mine or mine.intersection(roles):
            return user
        need = "、".join(Role.LABELS.get(r, r) for r in roles)
        raise HTTPException(403, f"无权限，需要角色：{need}")
    return dep


def api_key_auth(scope: str = "read") -> Callable[..., ApiKey]:
    def dep(request: Request, x_api_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> ApiKey:
        if not x_api_key:
            raise HTTPException(401, "缺少 X-API-Key")
        key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(x_api_key)))
        if not key or not key.is_active:
            raise HTTPException(401, "API Key 无效")
        if scope not in key.scopes.split(","):
            raise HTTPException(403, f"API Key 无 {scope} 权限")
        key.last_used_at = datetime.now().replace(microsecond=0)
        db.commit()  # 提交会释放事务；全局锁必须在它之后才拿
        lock_for_write(request, db)
        return key
    return dep
