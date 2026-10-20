"""库存出口的统一保护：谁占着料，谁才能领/转走。

占用来源（同一套口径，领料、调拨都用它）：
  1. 订单的库存预占（Reservation，source=STOCK：人工锁定 + 冻结）
  2. 在制自制任务（计划/开工）和本厂供料外协单的【未领原料】需求
自己的占用不挡自己；别人的占用，要走“调料申请”审批腾出，不能从出库口绕过。
"""
from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (Bom, OutsourceOrder, PickLine, PickList, Reservation, StockLot, Warehouse, WarehousePurpose,
                      WorkOrder, ZERO)
from .common import D


def bom_lines(db: Session, doc) -> list[tuple[int, Decimal]]:
    """任务/外协单的批准 BOM：优先用建单时冻结的快照；老单据没有快照才读当前生效 BOM。"""
    if doc.bom_snapshot:
        return [(int(c), Decimal(p)) for c, p in json.loads(doc.bom_snapshot)]
    b = db.scalar(select(Bom).where(Bom.material_id == doc.material_id, Bom.status == "ACTIVE"))
    return [(ln.child_id, D(ln.qty_per) * (1 + D(ln.scrap_rate))) for ln in b.lines] if b else []


def issued_net(db: Session, col, ref_id: int, material_id: int) -> Decimal:
    """已过账领料 − 已退料。"""
    return D(db.scalar(select(func.coalesce(func.sum(PickLine.qty - PickLine.returned_qty), 0))
                       .join(PickList, PickLine.pick_id == PickList.id)
                       .where(PickList.status == "POSTED", col == ref_id, PickLine.material_id == material_id)))


def reserved_by_others(db: Session, material_id: int, order_id: int | None) -> Decimal:
    q = select(func.coalesce(func.sum(Reservation.qty), 0)).where(Reservation.material_id == material_id,
                                                                  Reservation.source_type == "STOCK")
    if order_id:
        q = q.where(Reservation.order_id != order_id)
    return D(db.scalar(q))


def own_reserved(db: Session, material_id: int, order_id: int | None) -> Decimal:
    if not order_id:
        return ZERO
    return D(db.scalar(select(func.coalesce(func.sum(Reservation.qty), 0)).where(
        Reservation.material_id == material_id, Reservation.source_type == "STOCK", Reservation.order_id == order_id)))


def task_demand_others(db: Session, material_id: int, wo_id: int | None = None, os_id: int | None = None) -> Decimal:
    """在制自制任务 / 本厂供料外协单对该物料的未领需求（排除自己）。"""
    total = ZERO
    for w in db.scalars(select(WorkOrder).where(WorkOrder.status.in_(["PLANNED", "STARTED"]))).all():
        if wo_id and w.id == wo_id:
            continue
        for cid, per in bom_lines(db, w):
            if cid == material_id:
                total += max(per * D(w.qty) - issued_net(db, PickList.work_order_id, w.id, cid), ZERO)
    for o in db.scalars(select(OutsourceOrder).where(OutsourceOrder.status == "PLANNED",
                                                      OutsourceOrder.supply_mode == "CONSIGNED")).all():
        if os_id and o.id == os_id:
            continue
        for cid, per in bom_lines(db, o):
            if cid == material_id:
                total += max(per * D(o.qty) - issued_net(db, PickList.outsource_id, o.id, cid), ZERO)
    return total


def onhand_production(db: Session, material_id: int, warehouse_id: int | None = None) -> Decimal:
    q = select(func.coalesce(func.sum(StockLot.qty), 0)).join(Warehouse).where(StockLot.material_id == material_id,
                                                                              StockLot.qty > 0)
    q = q.where(StockLot.warehouse_id == warehouse_id) if warehouse_id else q.where(
        Warehouse.purpose == WarehousePurpose.PRODUCTION)
    return D(db.scalar(q))


def free_to_issue(db: Session, material_id: int, warehouse_id: int, *, order_id: int | None = None,
                  wo_id: int | None = None, os_id: int | None = None) -> Decimal:
    """某个单据此刻最多能从该仓领/转走多少：实存 − 他人占用；自己的预占至少可用。"""
    on = onhand_production(db, material_id, warehouse_id)
    others = reserved_by_others(db, material_id, order_id) + task_demand_others(db, material_id, wo_id, os_id)
    return max(on - others, own_reserved(db, material_id, order_id), ZERO) if order_id else max(on - others, ZERO)
