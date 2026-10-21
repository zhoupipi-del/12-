"""预警中心：缺料、在途延误、交期风险、库存高低、呆滞、异常超时、图纸变更。

每类预警有去重键：同一问题只产生一条 OPEN 预警；问题消失后自动标记 RESOLVED。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (Alert, IncomingException, KitStatus, Material, OrderStatus, Role, SalesOrder,
                      StockLot, Warehouse, WarehousePurpose, ZERO, now)
from .common import D

def fq(v) -> str:
    """数量显示：去掉多余的 0（2.6000 → 2.6）。"""
    t = format(D(v).normalize(), "f")
    return t


KIT_TYPES = ("SHORTAGE", "LATE_SUPPLY", "DUE_RISK", "PRESTART_CHECK")
STOCK_TYPES = ("STOCK_LOW", "STOCK_HIGH", "SPARE_LOW", "SPARE_HIGH", "STAGNANT")


def raise_alert(db: Session, type_: str, key: str, title: str, detail: str = "", level: str = "WARN",
                role: str = "", ref_type: str = "", ref_id: int | None = None) -> Alert:
    a = db.scalar(select(Alert).where(Alert.dedupe_key == key, Alert.status != "RESOLVED"))
    if a:
        a.title, a.detail, a.level = title, detail, level
        return a
    a = Alert(type=type_, level=level, title=title, detail=detail, dedupe_key=key, target_role=role,
              ref_type=ref_type, ref_id=ref_id, created_at=now())
    db.add(a)
    return a


def _resolve_missing(db: Session, types: tuple[str, ...], keep: set[str]) -> None:
    for a in db.scalars(select(Alert).where(Alert.type.in_(types), Alert.status != "RESOLVED")).all():
        if a.dedupe_key not in keep:
            a.status, a.resolved_at = "RESOLVED", now()


def sync_kit_alerts(db: Session, res) -> None:  # res: KitResult
    keep: set[str] = set()
    for mid, s in res.shortages.items():
        m = db.get(Material, mid)
        key = f"SHORTAGE:{mid}"
        keep.add(key)
        nos = "、".join(o["order_no"] for o in s["orders"][:5])
        raise_alert(db, "SHORTAGE", key, f"缺料：{m.code} {m.name} 缺 {fq(s['qty'])}{m.unit}",
                    f"最早需求日 {s['need_date']}，影响订单：{nos}", "CRITICAL", Role.BUYER, "MATERIAL", mid)
    for oid, out in res.orders.items():
        so = db.get(SalesOrder, oid)
        late = [ln for ln in out.lines if ln.status == KitStatus.INCOMING_LATE and not ln.exploded]
        for ln in late:
            key = f"LATE:{oid}:{ln.material_id}"
            keep.add(key)
            m = db.get(Material, ln.material_id)
            raise_alert(db, "LATE_SUPPLY", key, f"在途延误：{so.order_no} 的 {m.name}",
                        f"需求日 {ln.need_date}，预计 {ln.expected_date or '无法确定'} 可用（到货 + 检验转运），晚于需求日",
                        "WARN", Role.BUYER, "ORDER", oid)
        if out.check_due:  # 到开工前检查节点仍未齐套：提醒去冲填/催料（不是到货要求）
            key = f"PRECHECK:{oid}"
            keep.add(key)
            raise_alert(db, "PRESTART_CHECK", key, f"开工前检查：{so.order_no}（{so.customer}）尚未齐套",
                        f"距需求日不足 {settings.kit_buffer_days} 天，状态 {KitStatus.LABELS.get(out.status, out.status)}，"
                        f"预计齐套 {out.ready_date or '无法确定'}；请确认冲填/催料",
                        "WARN", Role.PLANNER, "ORDER", oid)
        if out.status != KitStatus.READY:
            if out.ready_date is None or out.ready_date > so.due_date:
                key = f"DUE:{oid}"
                keep.add(key)
                why = "存在缺料，无法确定齐套日" if out.ready_date is None else f"预计齐套日 {out.ready_date}"
                lvl = "CRITICAL" if so.status != OrderStatus.PLANNED else "WARN"
                raise_alert(db, "DUE_RISK", key, f"交期风险：{so.order_no}（{so.customer}）交期 {so.due_date}",
                            why, lvl, Role.PLANNER, "ORDER", oid)
    _resolve_missing(db, KIT_TYPES, keep)


def scan_stock(db: Session, today: date | None = None) -> int:
    """库存高低 + 呆滞扫描（生产库与售后库分开判断）。"""
    today = today or date.today()
    keep: set[str] = set()
    qty_by: dict[tuple[int, str], Decimal] = defaultdict(lambda: ZERO)
    for mid, purpose, q in db.execute(
            select(StockLot.material_id, Warehouse.purpose, func.sum(StockLot.qty)).join(Warehouse)
            .group_by(StockLot.material_id, Warehouse.purpose)).all():
        qty_by[(mid, purpose)] = D(q)
    for m in db.scalars(select(Material).where(Material.is_active.is_(True))).all():
        for purpose, lo, hi, tl, th, label in (
                (WarehousePurpose.PRODUCTION, m.safety_min, m.safety_max, "STOCK_LOW", "STOCK_HIGH", "生产库"),
                (WarehousePurpose.AFTERSALES, m.spare_min, m.spare_max, "SPARE_LOW", "SPARE_HIGH", "售后备件库")):
            qty = qty_by[(m.id, purpose)]
            lo, hi = D(lo), D(hi)
            if lo > 0 and qty < lo:
                key = f"{tl}:{m.id}"
                keep.add(key)
                raise_alert(db, tl, key, f"{label}低于安全库存：{m.code} {m.name}",
                            f"现存 {fq(qty)}，下限 {fq(lo)}", "WARN",
                            Role.BUYER if purpose == WarehousePurpose.PRODUCTION else Role.AFTERSALES,
                            "MATERIAL", m.id)
            if hi > 0 and qty > hi:
                key = f"{th}:{m.id}"
                keep.add(key)
                raise_alert(db, th, key, f"{label}库存积压：{m.code} {m.name}",
                            f"现存 {fq(qty)}，上限 {fq(hi)}", "INFO", Role.WAREHOUSE, "MATERIAL", m.id)
    limit = today - timedelta(days=settings.stagnant_days)
    for lot in db.scalars(select(StockLot).where(StockLot.qty > 0, StockLot.last_move_at < limit)).all():
        key = f"STAGNANT:{lot.id}"
        keep.add(key)
        raise_alert(db, "STAGNANT", key, f"呆滞：{lot.material.code} {lot.material.name} 批次 {lot.lot_no}",
                    f"{settings.stagnant_days} 天无出入库，数量 {fq(lot.qty)}", "INFO", Role.WAREHOUSE, "LOT", lot.id)
    _resolve_missing(db, STOCK_TYPES, keep)
    return len(keep)


def scan_exceptions(db: Session, today: date | None = None) -> int:
    today = today or date.today()
    keep: set[str] = set()
    for e in db.scalars(select(IncomingException).where(
            IncomingException.status.notin_(["CLOSED", "DRAFT"]), IncomingException.deadline.is_not(None))).all():
        if e.deadline < today:
            key = f"EXC_OVERDUE:{e.id}"
            keep.add(key)
            raise_alert(db, "EXC_OVERDUE", key, f"来料异常超时未处理：{e.exc_no}",
                        f"处理时限 {e.deadline}", "WARN", Role.QUALITY, "EXCEPTION", e.id)
    _resolve_missing(db, ("EXC_OVERDUE",), keep)
    return len(keep)


def scan_overdue_orders(db: Session, today: date | None = None) -> int:
    today = today or date.today()
    keep: set[str] = set()
    for o in db.scalars(select(SalesOrder).where(
            SalesOrder.status.in_(OrderStatus.ACTIVE_FOR_KIT + [OrderStatus.COMPLETED]),
            SalesOrder.due_date < today)).all():
        key = f"OVERDUE:{o.id}"
        keep.add(key)
        raise_alert(db, "ORDER_OVERDUE", key, f"订单已逾期：{o.order_no}（{o.customer}）",
                    f"交期 {o.due_date}，当前状态 {OrderStatus.LABELS.get(o.status, o.status)}",
                    "CRITICAL", Role.MANAGER, "ORDER", o.id)
    _resolve_missing(db, ("ORDER_OVERDUE",), keep)
    return len(keep)


def scan_all(db: Session, commit: bool = True) -> dict:
    r = {"stock": scan_stock(db), "exceptions": scan_exceptions(db), "overdue": scan_overdue_orders(db)}
    if commit:
        db.commit()
    return r
