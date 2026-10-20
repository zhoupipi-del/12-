"""车间看板（大屏）、部门周计划（小屏）、展示模式（客户参观时切换，隐藏内部信息）。"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (Arrival, AssemblyNote, KitLine, KitStatus, Material, OrderStatus, OutsourceOrder,
                      PurchaseOrder, PurchaseOrderLine, SalesOrder, Setting, ShipmentPlan, WeeklyPlanItem,
                      Alert)
from .common import D

MODES = {"WORKSHOP": "车间模式", "SHOWCASE": "展示模式"}
DEFAULTS = {
    "board_mode": "WORKSHOP",
    "showcase_title": "湖南一二矿山科技有限公司",
    "showcase_slogan": "专注矿山装备，品质交付每一台",
    "showcase_intro": "非标定制，全流程数字化生产协同，订单进度实时可见",
    "board_refresh_seconds": "30",
}


def get_setting(db: Session, key: str) -> str:
    s = db.get(Setting, key)
    return s.value if s else DEFAULTS.get(key, "")


def set_setting(db: Session, key: str, value: str) -> None:
    s = db.get(Setting, key)
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))


def _mask(name: str) -> str:
    if len(name) <= 2:
        return name[0] + "*"
    return name[:2] + "*" * min(len(name) - 3, 4) + name[-1]


def workshop(db: Session, today: date | None = None, force_mode: str | None = None) -> dict:
    today = today or date.today()
    mode = force_mode or get_setting(db, "board_mode")
    base = {"mode": mode, "now": date.today().isoformat(),
            "refresh": int(get_setting(db, "board_refresh_seconds") or 30)}
    assembling = db.scalars(select(SalesOrder).where(SalesOrder.status == OrderStatus.ASSEMBLING)
                            .order_by(SalesOrder.due_date)).all()
    if mode == "SHOWCASE":
        year0 = date(today.year, 1, 1)
        shipped = db.scalars(select(ShipmentPlan).where(ShipmentPlan.status == "SHIPPED",
                                                        ShipmentPlan.shipped_at >= year0)).all()
        on_time = sum(1 for s in shipped if s.shipped_at.date() <= s.order.due_date)
        return {**base,
                "title": get_setting(db, "showcase_title"), "slogan": get_setting(db, "showcase_slogan"),
                "intro": get_setting(db, "showcase_intro"),
                "stats": {"assembling": len(assembling), "shipped_year": len(shipped),
                          "on_time_rate": round(on_time / len(shipped), 3) if shipped else None},
                "assembling": [{"order_no": o.order_no, "customer": _mask(o.customer), "product": o.product.name,
                                "qty": D(o.qty), "progress": o.progress, "station": o.station} for o in assembling]}

    strips: dict[int, list] = {}
    for k in db.scalars(select(KitLine).where(KitLine.level == 1).order_by(KitLine.id)).all():
        strips.setdefault(k.order_id, []).append({"level": 1, "name": k.material.name, "status": k.status,
                                                  "short_qty": k.short_qty, "expected_date": k.expected_date})

    def order_row(o: SalesOrder) -> dict:
        return {"strip": strips.get(o.id, []),"id": o.id, "order_no": o.order_no, "customer": o.customer, "product": o.product.name,
                "qty": D(o.qty), "due_date": o.due_date, "planned_start": o.planned_start, "station": o.station,
                "progress": o.progress, "priority": o.priority, "kit_status": o.kit_status, "kit_color": o.kit_color,
                "kit_rate": D(o.kit_rate), "kit_ready_date": o.kit_ready_date,
                "ready_in_2_days": bool(o.kit_ready_date and o.kit_ready_date <= today + timedelta(days=2)),
                "overdue": o.due_date < today}

    plan = sorted(db.scalars(select(SalesOrder).where(SalesOrder.status.in_([OrderStatus.RELEASED, OrderStatus.PLANNED]))).all(),
                  key=lambda o: (o.status != OrderStatus.RELEASED, o.planned_start or o.due_date, o.due_date))
    short_rows = db.execute(
        select(KitLine, Material, SalesOrder).join(Material, Material.id == KitLine.material_id)
        .join(SalesOrder, SalesOrder.id == KitLine.order_id)
        .where(KitLine.status.in_([KitStatus.SHORT, KitStatus.INCOMING_LATE]), KitLine.from_children == 0)
        .order_by(KitLine.need_date).limit(40)).all()
    shortages = [{"order_no": o.order_no, "code": m.code, "name": m.name, "drawing_no": m.drawing_no,
                  "type": m.type, "short_qty": D(k.short_qty) or (D(k.required) - D(k.from_stock)),
                  "need_date": k.need_date, "expected_date": k.expected_date, "status": k.status}
                 for k, m, o in short_rows]
    horizon = today + timedelta(days=7)
    incoming = []
    for ln, po in db.execute(select(PurchaseOrderLine, PurchaseOrder).join(PurchaseOrder)
                             .where(PurchaseOrder.status == "APPROVED",
                                    PurchaseOrderLine.status.in_(["OPEN", "PARTIAL"]))).all():
        d = ln.promised_date or ln.need_date
        if d and d <= horizon and ln.open_qty > 0:
            incoming.append({"type": "采购", "no": po.po_no, "supplier": po.supplier.name, "code": ln.material.code,
                             "name": ln.material.name, "qty": ln.open_qty, "date": d,
                             "confirmed": ln.promised_date is not None, "late": d < today})
    for x in db.scalars(select(OutsourceOrder).where(OutsourceOrder.status.in_(["SENT", "PARTIAL"]))).all():
        if x.expected_return and x.expected_return <= horizon and x.open_qty > 0:
            incoming.append({"type": "外协", "no": x.os_no, "supplier": x.supplier.name, "code": x.material.code,
                             "name": x.material.name, "qty": x.open_qty, "date": x.expected_return,
                             "confirmed": True, "late": x.expected_return < today})
    arrived = [{"no": a.arrival_no, "code": a.material.code, "name": a.material.name, "qty": D(a.qty),
                "supplier": a.supplier.name, "arrived_at": a.arrived_at}
               for a in db.scalars(select(Arrival).where(Arrival.status == "PENDING").order_by(Arrival.arrived_at)).all()]
    incoming.sort(key=lambda x: x["date"])
    notes = [{"id": n.id, "content": n.content, "level": n.level,
              "order_no": db.get(SalesOrder, n.order_id).order_no if n.order_id else ""}
             for n in db.scalars(select(AssemblyNote).where(AssemblyNote.is_active.is_(True))
                                 .order_by(AssemblyNote.level.desc(), AssemblyNote.id.desc())).all()]
    open_alerts = db.scalar(select(func.count()).select_from(Alert).where(Alert.status == "OPEN",
                                                                          Alert.level == "CRITICAL"))
    plan_rows = [order_row(o) for o in plan]
    return {**base,
            "stats": {"assembling": len(assembling), "planned": len(plan_rows),
                      "ready": sum(1 for r in plan_rows if r["kit_status"] == KitStatus.READY),
                      "ready_in_2_days": sum(1 for r in plan_rows if r["ready_in_2_days"]),
                      "short_orders": sum(1 for r in plan_rows if r["kit_color"] == "RED"),
                      "pending_inspection": len(arrived), "critical_alerts": open_alerts},
            "assembling": [order_row(o) for o in assembling], "plan": plan_rows[:20], "shortages": shortages,
            "incoming": incoming[:30], "arrived": arrived[:20], "notes": notes}


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def weekly(db: Session, ws: date | None = None) -> dict:
    ws = week_start(ws or date.today())
    items = db.scalars(select(WeeklyPlanItem).where(WeeklyPlanItem.week_start == ws)
                       .order_by(WeeklyPlanItem.department, WeeklyPlanItem.due_date)).all()
    groups: dict[str, list] = {}
    for it in items:
        groups.setdefault(it.department, []).append(
            {"id": it.id, "content": it.content, "owner": it.owner, "due_date": it.due_date, "status": it.status,
             "order_no": db.get(SalesOrder, it.order_id).order_no if it.order_id else ""})
    we = ws + timedelta(days=6)
    due = db.scalars(select(SalesOrder).where(SalesOrder.due_date.between(ws, we),
                                              SalesOrder.status.notin_([OrderStatus.CANCELLED, OrderStatus.CLOSED]))).all()
    ships = db.scalars(select(ShipmentPlan).where(ShipmentPlan.plan_date.between(ws, we),
                                                  ShipmentPlan.status != "CANCELLED")).all()
    return {"week_start": ws, "week_end": we, "mode": get_setting(db, "board_mode"), "groups": groups,
            "due_orders": [{"order_no": o.order_no, "customer": o.customer, "product": o.product.name,
                            "due_date": o.due_date, "status": o.status, "kit_color": o.kit_color} for o in due],
            "shipments": [{"ship_no": s.ship_no, "order_no": s.order.order_no, "plan_date": s.plan_date,
                           "qty": D(s.qty), "status": s.status} for s in ships]}
