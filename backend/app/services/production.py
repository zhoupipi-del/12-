"""自制件加工任务 + 外协单。二者都是齐套计算中的“在途供应”。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import BizError
from ..models import (Bom, Material, MaterialType, OutsourceOrder, PickLine, PickList, Supplier, TxnType, User,
                      WorkOrder, ZERO)
from .common import D, audit, get_or_404, next_no
from .drawings import assert_confirmed
from .inventory import default_warehouse, stock_in


def create_wo(db: Session, user: User, data: dict) -> WorkOrder:
    m = get_or_404(db, Material, data["material_id"], "物料")
    if m.type != MaterialType.MAKE:
        raise BizError("自制任务只能针对自制件")
    w = WorkOrder(wo_no=next_no(db, WorkOrder, "wo_no", "WO"), material_id=m.id, qty=D(data["qty"]),
                  planned_start=data.get("planned_start"), planned_finish=data.get("planned_finish"),
                  order_id=data.get("order_id"), owner=data.get("owner", ""), remark=data.get("remark", ""))
    db.add(w)
    db.flush()
    audit(db, user.id, "CREATE", "WorkOrder", w.id, data)
    return w


def update_wo(db: Session, user: User, wo_id: int, data: dict) -> WorkOrder:
    w = get_or_404(db, WorkOrder, wo_id, "自制任务")
    if w.status in ("DONE", "CANCELLED"):
        raise BizError("任务已结束")
    for k in ("qty", "planned_start", "planned_finish", "owner", "remark"):
        if k in data:
            setattr(w, k, D(data[k]) if k == "qty" else data[k])
    if D(w.qty) < D(w.done_qty):
        raise BizError("数量不能小于已完工数量")
    return w


def start_wo(db: Session, user: User, wo_id: int) -> WorkOrder:
    w = get_or_404(db, WorkOrder, wo_id, "自制任务")
    if w.status != "PLANNED":
        raise BizError("任务不是计划状态")
    assert_confirmed(db, "WORK_ORDER", w.id)  # 加工前确认图纸版本
    w.status = "STARTED"
    return w


def report_wo(db: Session, user: User, wo_id: int, qty: Decimal, warehouse_id: int | None = None) -> WorkOrder:
    """报工完工：完工数量入生产库，成为可预占库存。"""
    w = db.scalar(select(WorkOrder).where(WorkOrder.id == wo_id).with_for_update())  # 行锁：并发报工不超量
    if w is None:
        raise BizError("自制任务不存在", 404)
    if w.status != "STARTED":
        raise BizError("任务需先开工")
    qty = D(qty)
    if qty <= 0 or D(w.done_qty) + qty > D(w.qty):
        raise BizError("报工数量无效")
    # 完工必须有对应的原料消耗：按 BOM 核对“累计完工量所需原料 ≤ 已领未退原料”，不足不能报工（先领料）
    db.flush()
    bom = db.scalar(select(Bom).where(Bom.material_id == w.material_id, Bom.status == "ACTIVE"))
    if bom:
        short = []
        for ln in bom.lines:
            need = D(ln.qty_per) * (1 + D(ln.scrap_rate)) * (D(w.done_qty) + qty)
            have = _issued_total(db, PickList.work_order_id, w.id, ln.child_id)
            if have < need:
                short.append(f"{ln.child.code} 需 {need:g}、已领 {have:g}")
        if short:
            raise BizError("原料领用不足，不能报工（请先在本任务上【领原料】并过账）：" + "；".join(short))
    wh = warehouse_id or default_warehouse(db).id
    stock_in(db, material_id=w.material_id, warehouse_id=wh, qty=qty, txn_type=TxnType.PRODUCE_IN,
             lot_no=w.wo_no, ref_type="WORK_ORDER", ref_id=w.id, order_id=w.order_id, user_id=user.id)
    w.done_qty = D(w.done_qty) + qty
    if D(w.done_qty) >= D(w.qty):
        w.status = "DONE"
    audit(db, user.id, "REPORT", "WorkOrder", w.id, {"qty": qty})
    return w


def cancel_wo(db: Session, user: User, wo_id: int) -> WorkOrder:
    w = get_or_404(db, WorkOrder, wo_id, "自制任务")
    if w.status == "DONE":
        raise BizError("已完工任务不能取消")
    w.status = "CANCELLED"
    return w


def create_outsource(db: Session, user: User, data: dict) -> OutsourceOrder:
    m = get_or_404(db, Material, data["material_id"], "物料")
    s = get_or_404(db, Supplier, data["supplier_id"], "外协厂")
    if m.type != MaterialType.OUTSOURCE:
        raise BizError("外协单只能针对外协件")
    o = OutsourceOrder(os_no=next_no(db, OutsourceOrder, "os_no", "OS"), supplier_id=s.id, material_id=m.id,
                       drawing_no=data.get("drawing_no") or m.drawing_no, qty=D(data["qty"]),
                       price=D(data.get("price") or 0), expected_return=data.get("expected_return"),
                       order_id=data.get("order_id"), supply_mode=data.get("supply_mode") or "TURNKEY",
                       remark=data.get("remark", ""))
    db.add(o)
    db.flush()
    audit(db, user.id, "CREATE", "OutsourceOrder", o.id, data)
    return o


def update_outsource(db: Session, user: User, os_id: int, data: dict) -> OutsourceOrder:
    o = get_or_404(db, OutsourceOrder, os_id, "外协单")
    if o.status in ("RETURNED", "CANCELLED"):
        raise BizError("外协单已结束")
    for k in ("expected_return", "remark", "price"):
        if k in data:
            setattr(o, k, D(data[k]) if k == "price" else data[k])
    if "qty" in data:
        if D(data["qty"]) < D(o.arrived_qty):
            raise BizError("数量不能小于已回厂数量")
        o.qty = D(data["qty"])
    return o


def send_outsource(db: Session, user: User, os_id: int, sent_date: date | None = None) -> OutsourceOrder:
    o = get_or_404(db, OutsourceOrder, os_id, "外协单")
    if o.status != "PLANNED":
        raise BizError("外协单已发出")
    assert_confirmed(db, "OUTSOURCE", o.id)  # 发外协前确认图纸版本
    if o.supply_mode == "CONSIGNED":  # 本厂供料：原料必须先领料发出
        for ln in _bom_for(db, o.material_id).lines:
            need = D(ln.qty_per) * (1 + D(ln.scrap_rate)) * D(o.qty)
            if _issued_total(db, PickList.outsource_id, o.id, ln.child_id) < need:
                raise BizError("本厂供料外协需先把原料领料发出（外协领料单过账）后才能发出")
    o.status, o.sent_date = "SENT", sent_date or date.today()
    audit(db, user.id, "SEND", "OutsourceOrder", o.id)
    return o


def cancel_outsource(db: Session, user: User, os_id: int) -> OutsourceOrder:
    o = get_or_404(db, OutsourceOrder, os_id, "外协单")
    if D(o.arrived_qty) > 0:
        raise BizError("已有回厂记录，不能取消")
    o.status = "CANCELLED"
    return o


# ------------------------------------------------------------------ 为在制任务 / 本厂供料外协领料


def _issued_total(db: Session, col, ref_id: int, material_id: int) -> Decimal:
    return D(db.scalar(select(func.coalesce(func.sum(PickLine.qty - PickLine.returned_qty), 0)).join(PickList, PickLine.pick_id == PickList.id)
                       .where(PickList.status == "POSTED", col == ref_id, PickLine.material_id == material_id)))


def _bom_for(db: Session, material_id: int) -> Bom:
    b = db.scalar(select(Bom).where(Bom.material_id == material_id, Bom.status == "ACTIVE"))
    if not b:
        raise BizError("该物料没有生效的 BOM，无法领料")
    return b


def create_wo_pick(db: Session, user: User, wo_id: int, warehouse_id: int | None = None) -> PickList:
    """自制任务领原料：按 BOM × 任务数量 − 已领，生成领料单（过账走 orders.post_pick）。"""
    w = get_or_404(db, WorkOrder, wo_id, "自制任务")
    if w.status not in ("PLANNED", "STARTED"):
        raise BizError("任务已结束，不能领料")
    assert_confirmed(db, "WORK_ORDER", w.id)
    wh = warehouse_id or default_warehouse(db).id
    pk = PickList(pick_no=next_no(db, PickList, "pick_no", "PK"), work_order_id=w.id, warehouse_id=wh, created_by=user.id)
    for ln in _bom_for(db, w.material_id).lines:
        gross = D(ln.qty_per) * (1 + D(ln.scrap_rate)) * D(w.qty)
        remain = max(gross - _issued_total(db, PickList.work_order_id, w.id, ln.child_id), ZERO)
        if remain > 0:
            pk.lines.append(PickLine(material_id=ln.child_id, plan_qty=remain, qty=remain))
    if not pk.lines:
        raise BizError("原料已领齐")
    db.add(pk)
    db.flush()
    return pk


def create_outsource_pick(db: Session, user: User, os_id: int, warehouse_id: int | None = None) -> PickList:
    """本厂供料外协：发出前按 BOM 领料给外协厂。包工包料的外协单不需要。"""
    o = get_or_404(db, OutsourceOrder, os_id, "外协单")
    if o.supply_mode != "CONSIGNED":
        raise BizError("该外协单为包工包料，无需本厂发料")
    if o.status in ("RETURNED", "CANCELLED"):
        raise BizError("外协单已结束")
    wh = warehouse_id or default_warehouse(db).id
    pk = PickList(pick_no=next_no(db, PickList, "pick_no", "PK"), outsource_id=o.id, warehouse_id=wh, created_by=user.id)
    for ln in _bom_for(db, o.material_id).lines:
        gross = D(ln.qty_per) * (1 + D(ln.scrap_rate)) * D(o.qty)
        remain = max(gross - _issued_total(db, PickList.outsource_id, o.id, ln.child_id), ZERO)
        if remain > 0:
            pk.lines.append(PickLine(material_id=ln.child_id, plan_qty=remain, qty=remain))
    if not pk.lines:
        raise BizError("原料已发齐")
    db.add(pk)
    db.flush()
    return pk
