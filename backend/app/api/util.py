from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..services.kitting import recalc


def page(db: Session, q, page: int, size: int, conv) -> dict:
    total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery()))
    rows = db.scalars(q.offset((max(page, 1) - 1) * size).limit(min(size, 500))).all()
    return {"total": total, "items": [conv(r) for r in rows]}


def commit_recalc(db: Session) -> None:
    """影响供需的操作完成后：在【同一个事务】里重算齐套再一次提交（入库、到货、改单、改交期都会触发冲填）。
    重算失败则整笔业务回滚（库存和齐套不会出现“库存已变、齐套没跟上”的半成功状态），接口返回错误，可安全重试。"""
    try:
        db.flush()  # 会话不自动 flush：先把本次业务写入刷进事务，重算读到的才是最新状态
        recalc(db, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
