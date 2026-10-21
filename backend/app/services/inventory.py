"""库存服务：批次入库、先进先出出库、调拨、售后备件出库（销售/维修领用分开记）、盘点调整。"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import BizError
from ..models import (Material, StockLot, StockTxn, TxnType, Warehouse, WarehousePurpose, ZERO, now)
from .common import D, get_or_404


def stock_in(db: Session, *, material_id: int, warehouse_id: int, qty: Decimal, txn_type: str,
             lot_no: str = "", location_id: int | None = None, unit_cost: Decimal = ZERO,
             ref_type: str = "", ref_id: int | None = None, supplier_id: int | None = None,
             order_id: int | None = None, user_id: int | None = None, remark: str = "") -> StockLot:
    qty = D(qty)
    if qty <= 0:
        raise BizError("入库数量必须大于 0")
    ts = now()
    lot = StockLot(material_id=material_id, warehouse_id=warehouse_id, location_id=location_id,
                   lot_no=lot_no or ts.strftime("L%y%m%d%H%M%S"), qty=qty, unit_cost=D(unit_cost),
                   received_at=ts, last_move_at=ts, source_type=ref_type, source_id=ref_id,
                   supplier_id=supplier_id)
    db.add(lot)
    db.flush()
    db.add(StockTxn(txn_type=txn_type, material_id=material_id, warehouse_id=warehouse_id, lot_id=lot.id,
                    qty=qty, ref_type=ref_type, ref_id=ref_id, order_id=order_id, user_id=user_id,
                    remark=remark, created_at=ts))
    return lot


def available(db: Session, material_id: int, warehouse_id: int) -> Decimal:
    v = db.scalar(select(func.coalesce(func.sum(StockLot.qty), 0)).where(
        StockLot.material_id == material_id, StockLot.warehouse_id == warehouse_id, StockLot.qty > 0))
    return D(v)


def fifo_out(db: Session, *, material_id: int, warehouse_id: int, qty: Decimal, txn_type: str,
             ref_type: str = "", ref_id: int | None = None, order_id: int | None = None,
             customer: str = "", user_id: int | None = None, remark: str = "",
             lot_id: int | None = None) -> list[dict]:
    """先进先出扣减；指定 lot_id 时只扣该批次（用于冲销/退货）。返回扣减明细。"""
    qty = D(qty)
    if qty <= 0:
        raise BizError("出库数量必须大于 0")
    q = select(StockLot).where(StockLot.material_id == material_id, StockLot.warehouse_id == warehouse_id,
                               StockLot.qty > 0)
    if lot_id:
        q = q.where(StockLot.id == lot_id)
    lots = db.scalars(q.order_by(StockLot.received_at, StockLot.id).with_for_update()).all()
    have = sum((D(x.qty) for x in lots), ZERO)
    if have < qty:
        m = db.get(Material, material_id)
        raise BizError(f"{m.code} {m.name} 库存不足：需要 {qty:g}，可用 {have:g}")
    ts, remain, used = now(), qty, []
    for lot in lots:
        if remain <= 0:
            break
        t = min(D(lot.qty), remain)
        lot.qty = D(lot.qty) - t
        lot.last_move_at = ts
        remain -= t
        used.append({"lot_id": lot.id, "lot_no": lot.lot_no, "qty": float(t)})
        db.add(StockTxn(txn_type=txn_type, material_id=material_id, warehouse_id=warehouse_id, lot_id=lot.id,
                        qty=-t, ref_type=ref_type, ref_id=ref_id, order_id=order_id, customer=customer,
                        user_id=user_id, remark=remark, created_at=ts))
    return used


def transfer(db: Session, *, material_id: int, from_wh: int, to_wh: int, qty: Decimal,
             to_location_id: int | None = None, user_id: int | None = None, remark: str = "") -> None:
    """调拨（例如生产库 ↔ 售后备件库），保留原批次号与入库时间以维持先进先出。"""
    if from_wh == to_wh:
        raise BizError("调出和调入仓库不能相同")
    src_wh = db.get(Warehouse, from_wh)
    if src_wh is not None and src_wh.purpose == WarehousePurpose.PRODUCTION:
        # 从生产库转走的料，不能是别的订单已锁定/冻结、或在制任务认领的：与领料用同一套保护
        from . import protection
        from .kitting import acquire_mutex
        acquire_mutex(db)
        free = protection.free_to_issue(db, material_id, from_wh)
        if D(qty) > free:
            raise BizError(f"可调拨数量不足：生产库中有 {free:g} 未被订单锁定/冻结或被任务占用，本次要调 {D(qty):g}。"
                           "占用中的料请先走【调料申请】或释放占用", data={"free": float(free)})
    used = fifo_out(db, material_id=material_id, warehouse_id=from_wh, qty=qty, txn_type=TxnType.TRANSFER_OUT,
                    ref_type="TRANSFER", user_id=user_id, remark=remark)
    ts = now()
    for u in used:
        src = db.get(StockLot, u["lot_id"])
        lot = StockLot(material_id=material_id, warehouse_id=to_wh, location_id=to_location_id,
                       lot_no=src.lot_no, qty=D(u["qty"]), unit_cost=src.unit_cost, received_at=src.received_at,
                       last_move_at=ts, source_type="TRANSFER", source_id=src.id, supplier_id=src.supplier_id)
        db.add(lot)
        db.flush()
        db.add(StockTxn(txn_type=TxnType.TRANSFER_IN, material_id=material_id, warehouse_id=to_wh, lot_id=lot.id,
                        qty=D(u["qty"]), ref_type="TRANSFER", user_id=user_id, remark=remark, created_at=ts))


def spare_out(db: Session, *, material_id: int, warehouse_id: int, qty: Decimal, kind: str,
              customer: str = "", user_id: int | None = None, remark: str = "") -> list[dict]:
    """售后备件出库：SALE（对外销售）与 USE（售后维修领用）分开记账。"""
    wh = get_or_404(db, Warehouse, warehouse_id, "仓库")
    if wh.purpose != WarehousePurpose.AFTERSALES:
        raise BizError("售后出库只能从售后备件库出")
    t = TxnType.SPARE_SALE_OUT if kind == "SALE" else TxnType.SPARE_USE_OUT
    if kind == "SALE" and not customer:
        raise BizError("备件销售需填写客户")
    return fifo_out(db, material_id=material_id, warehouse_id=warehouse_id, qty=qty, txn_type=t,
                    ref_type="SPARE", customer=customer, user_id=user_id, remark=remark)


def adjust(db: Session, *, lot_id: int, new_qty: Decimal, user_id: int | None, remark: str) -> None:
    lot = get_or_404(db, StockLot, lot_id, "批次")
    diff = D(new_qty) - D(lot.qty)
    if diff == 0:
        return
    if not remark:
        raise BizError("盘点调整必须填写原因")
    lot.qty = D(new_qty)
    lot.last_move_at = now()
    db.add(StockTxn(txn_type=TxnType.ADJUST, material_id=lot.material_id, warehouse_id=lot.warehouse_id,
                    lot_id=lot.id, qty=diff, ref_type="ADJUST", user_id=user_id, remark=remark, created_at=now()))


def default_warehouse(db: Session, purpose: str = WarehousePurpose.PRODUCTION) -> Warehouse:
    wh = db.scalar(select(Warehouse).where(Warehouse.purpose == purpose, Warehouse.is_active.is_(True))
                   .order_by(Warehouse.id))
    if not wh:
        raise BizError(f"未配置{WarehousePurpose.LABELS.get(purpose, purpose)}仓库")
    return wh


def onhand_summary(db: Session, purpose: str | None = None, keyword: str = "") -> list[dict]:
    q = (select(Material, Warehouse, func.sum(StockLot.qty), func.min(StockLot.received_at),
                func.max(StockLot.last_move_at))
         .join(StockLot, StockLot.material_id == Material.id).join(Warehouse, Warehouse.id == StockLot.warehouse_id)
         .where(StockLot.qty > 0).group_by(Material.id, Warehouse.id))
    if purpose:
        q = q.where(Warehouse.purpose == purpose)
    if keyword:
        like = f"%{keyword}%"
        q = q.where(Material.code.like(like) | Material.name.like(like) | Material.drawing_no.like(like))
    out = []
    for m, wh, qty, oldest, last_move in db.execute(q.order_by(Material.code)).all():
        qty = D(qty)
        lo, hi = (m.spare_min, m.spare_max) if wh.purpose == WarehousePurpose.AFTERSALES else (m.safety_min, m.safety_max)
        lo, hi = D(lo), D(hi)
        flag = "LOW" if lo > 0 and qty < lo else ("HIGH" if hi > 0 and qty > hi else "")
        out.append({"material_id": m.id, "code": m.code, "name": m.name, "spec": m.spec, "unit": m.unit,
                    "type": m.type, "drawing_no": m.drawing_no, "warehouse_id": wh.id, "warehouse": wh.name,
                    "purpose": wh.purpose, "qty": qty, "safety_min": lo, "safety_max": hi, "flag": flag,
                    "oldest_lot": oldest, "last_move_at": last_move})
    return out
