from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import NotFound
from ..models import AuditLog, now


def D(v: Any) -> Decimal:
    if v is None:
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def get_or_404(db: Session, model, obj_id: int, what: str):
    obj = db.get(model, obj_id)
    if obj is None:
        raise NotFound(what)
    return obj


def next_no(db: Session, model, field: str, prefix: str) -> str:
    """生成单号：前缀 + yyMMdd + 3 位流水，例如 PO251020001。"""
    day = date.today().strftime("%y%m%d")
    head = f"{prefix}{day}"
    col = getattr(model, field)
    last = db.scalar(select(func.max(col)).where(col.like(f"{head}%")))
    seq = int(last[len(head):]) + 1 if last else 1
    return f"{head}{seq:03d}"


def _default(o: Any):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, date):
        return o.isoformat()
    return str(o)


def dumps(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, default=_default)


def audit(db: Session, user_id: int | None, action: str, entity: str, entity_id: int | None,
          detail: Any = "") -> None:
    db.add(AuditLog(user_id=user_id, action=action, entity=entity, entity_id=entity_id,
                    detail=detail if isinstance(detail, str) else dumps(detail), created_at=now()))
