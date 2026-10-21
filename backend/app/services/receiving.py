"""到货 → 检验 → 合格入库 / 不合格转来料异常单（线上审批闭环）。

来料异常状态机：
    DRAFT ──提交──▶ SUBMITTED ──驳回──▶ REJECTED ──重新提交──▶ SUBMITTED
                      │
                      └─审核处置──▶ DISPOSED ──(换货/返修)──▶ AWAIT_REINSPECT ──复检──▶ REINSPECTED
                                       │                                                   │
                                       └────────────────────关闭────────────────────────────┴──▶ CLOSED
处置方式：
    质量异常  RETURN 退货 / EXCHANGE 换货 / REWORK 返修挑选 / CONCESSION 让步接收
    数量异常  SUPPLEMENT 供应商补发 / CLOSE_SHORT 按实收关闭余量
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import BizError
from ..models import (Arrival, ExceptionLog, IncomingException, OutsourceOrder, PurchaseOrderLine, Role,
                      TxnType, User, WarehousePurpose, ZERO, now)
from .common import D, audit, get_or_404, next_no
from .inventory import default_warehouse, stock_in
from .purchase import refresh_line_status, refresh_po_status

QUALITY_DISPOSITIONS = {"RETURN": "退货", "EXCHANGE": "换货", "REWORK": "返修/挑选", "CONCESSION": "让步接收"}
QTY_DISPOSITIONS = {"SUPPLEMENT": "供应商补发", "CLOSE_SHORT": "按实收关闭余量"}
STATUS_LABELS = {"DRAFT": "草稿", "SUBMITTED": "待审核", "REJECTED": "已驳回", "DISPOSED": "处置中",
                 "AWAIT_REINSPECT": "待复检", "REINSPECTED": "已复检", "CLOSED": "已关闭"}
RESPONSIBILITY = {"SUPPLIER": "供应商", "INTERNAL": "内部", "LOGISTICS": "物流"}


def _source(db: Session, source_type: str, source_id: int, lock: bool = False):
    model, label = {"PO": (PurchaseOrderLine, "采购行"), "OUTSOURCE": (OutsourceOrder, "外协单")}.get(
        source_type, (None, None))
    if model is None:
        raise BizError("来源类型错误")
    if not lock:
        return get_or_404(db, model, source_id, label)
    obj = db.scalar(select(model).where(model.id == source_id).with_for_update())  # 行锁：串行化同一来源的收货
    if obj is None:
        raise BizError(f"{label}不存在", 404)
    return obj


def _refresh_source(src) -> None:
    if isinstance(src, PurchaseOrderLine):
        refresh_line_status(src)
        refresh_po_status(src.po)
    else:
        if src.status == "CANCELLED":
            return
        if D(src.received_qty) >= D(src.qty):
            src.status = "RETURNED"
        elif D(src.arrived_qty) > 0:
            src.status = "PARTIAL"


def register_arrival(db: Session, user: User, source_type: str, source_id: int, qty: Decimal,
                     expected_qty: Decimal | None = None, note: str = "",
                     client_token: str | None = None) -> tuple[Arrival, IncomingException | None]:
    # 先锁来源行，再查幂等键（用加锁读取拿“最新已提交”的数据：MySQL 默认可重复读，
    # 普通查询会看到事务开始时的旧快照，看不到并发事务刚提交的那张到货单）。
    # 同一个 client_token 重复提交（双击、网络重试）只登记一次，返回第一次的结果。
    src = _source(db, source_type, source_id, lock=True)
    if client_token:
        dup = db.scalar(select(Arrival).where(Arrival.client_token == client_token).with_for_update())
        if dup:
            exp_new = D(expected_qty) if expected_qty is not None else D(qty)
            if (dup.source_type, dup.source_line_id, D(dup.qty), D(dup.expected_qty)) != \
                    (source_type, src.id, D(qty), exp_new):
                raise BizError("同一幂等键对应的请求内容不同，请换新的键", 409)
            return dup, None
    qty = D(qty)
    if qty <= 0:
        raise BizError("到货数量必须大于 0")
    if isinstance(src, PurchaseOrderLine):
        if src.po.status != "APPROVED":
            raise BizError("采购单未审核，不能登记到货")
        supplier_id, unit_cost = src.po.supplier_id, src.price
    else:
        if src.status == "PLANNED":
            raise BizError("外协单尚未发出")
        supplier_id, unit_cost = src.supplier_id, src.price
    open_qty = src.open_qty
    # expected_qty = 送货单上的数量；不填视为与实到一致（分批送货是常态，不算异常）
    exp = D(expected_qty) if expected_qty is not None else qty
    if qty > open_qty:
        raise BizError(f"到货数量 {qty:g} 超过未到数量 {open_qty:g}")
    a = Arrival(arrival_no=next_no(db, Arrival, "arrival_no", "AR"), source_type=source_type,
                source_line_id=src.id, supplier_id=supplier_id, material_id=src.material_id, qty=qty,
                expected_qty=exp, arrived_at=now(), inspect_note=note, unit_cost=D(unit_cost),
                client_token=client_token or None)
    db.add(a)
    src.arrived_qty = D(src.arrived_qty) + qty
    _refresh_source(src)
    db.flush()
    exc = None
    if exp > qty:  # 少到 → 自动生成数量异常（待提交）
        exc = _new_exception(db, user, "QTY", src.material_id, exp - qty,
                             f"到货 {a.arrival_no} 应到 {exp:g}，实到 {qty:g}，短少 {exp - qty:g}",
                             arrival=a, supplier_id=supplier_id, status="SUBMITTED")
    audit(db, user.id, "ARRIVE", "Arrival", a.id, {"qty": qty, "source": source_type})
    return a, exc


def inspect(db: Session, user: User, arrival_id: int, qualified: Decimal, rejected: Decimal,
            warehouse_id: int | None = None, location_id: int | None = None, lot_no: str = "",
            note: str = "", reject_reason: str = "") -> tuple[Arrival, IncomingException | None]:
    # 行锁：同一到货单并发检验，后到者看到已检验状态被拒，不会重复入库
    a = db.scalar(select(Arrival).where(Arrival.id == arrival_id).with_for_update())
    if a is None:
        raise BizError("到货单不存在", 404)
    if a.status != "PENDING":
        raise BizError("该到货单已检验")
    qualified, rejected = D(qualified), D(rejected)
    if qualified < 0 or rejected < 0 or qualified + rejected != D(a.qty):
        raise BizError(f"合格 + 不合格 必须等于到货数量 {D(a.qty):g}")
    if rejected > 0 and not reject_reason:
        raise BizError("有不合格品时请填写不合格原因")
    src = _source(db, a.source_type, a.source_line_id)
    wh_id = warehouse_id or default_warehouse(db, WarehousePurpose.PRODUCTION).id
    a.qualified_qty, a.rejected_qty = qualified, rejected
    a.inspector_id, a.inspected_at, a.status = user.id, now(), "INSPECTED"
    a.warehouse_id, a.location_id = wh_id, location_id
    if note:
        a.inspect_note = (a.inspect_note + "；" if a.inspect_note else "") + note
    if qualified > 0:
        lot = stock_in(db, material_id=a.material_id, warehouse_id=wh_id, qty=qualified,
                       txn_type=TxnType.PURCHASE_IN if a.source_type == "PO" else TxnType.OUTSOURCE_IN,
                       lot_no=lot_no or a.arrival_no, location_id=location_id, unit_cost=a.unit_cost,
                       ref_type="ARRIVAL", ref_id=a.id, supplier_id=a.supplier_id,
                       order_id=getattr(src, "order_id", None), user_id=user.id)
        a.lot_id = lot.id
        src.received_qty = D(src.received_qty) + qualified
    _refresh_source(src)
    exc = None
    if rejected > 0:
        exc = _new_exception(db, user, "QUALITY", a.material_id, rejected, reject_reason, arrival=a,
                             supplier_id=a.supplier_id, status="SUBMITTED")
    audit(db, user.id, "INSPECT", "Arrival", a.id, {"qualified": qualified, "rejected": rejected})
    return a, exc


# ------------------------------------------------------------------ 来料异常单


def _log(e: IncomingException, user: User | None, frm: str, to: str, action: str, comment: str = "") -> None:
    e.logs.append(ExceptionLog(from_status=frm, to_status=to, action=action, comment=comment,
                               user_id=user.id if user else None, created_at=now()))


def _new_exception(db: Session, user: User, category: str, material_id: int, qty: Decimal, description: str,
                   arrival: Arrival | None = None, supplier_id: int | None = None, status: str = "DRAFT",
                   deadline_days: int = 3) -> IncomingException:
    e = IncomingException(exc_no=next_no(db, IncomingException, "exc_no", "QE"), category=category,
                          arrival_id=arrival.id if arrival else None, supplier_id=supplier_id,
                          material_id=material_id, qty=D(qty), description=description, status=status,
                          deadline=date.today() + timedelta(days=deadline_days), created_by=user.id)
    db.add(e)
    _log(e, user, "", status, "CREATE", "检验自动生成" if arrival else "")
    db.flush()
    return e


def create_exception(db: Session, user: User, data: dict) -> IncomingException:
    if data.get("category") not in ("QTY", "QUALITY"):
        raise BizError("异常类别必须是 QTY 或 QUALITY")
    arrival = db.get(Arrival, data["arrival_id"]) if data.get("arrival_id") else None
    e = _new_exception(db, user, data["category"], data["material_id"], data["qty"], data["description"],
                       arrival=arrival, supplier_id=data.get("supplier_id") or (arrival.supplier_id if arrival else None),
                       status="DRAFT", deadline_days=int(data.get("deadline_days", 3)))
    return e


def _expect(e: IncomingException, *states: str) -> None:
    if e.status not in states:
        raise BizError(f"当前状态“{STATUS_LABELS.get(e.status, e.status)}”不能执行该操作")


def submit(db: Session, user: User, exc_id: int, comment: str = "") -> IncomingException:
    e = get_or_404(db, IncomingException, exc_id, "异常单")
    _expect(e, "DRAFT", "REJECTED")
    frm = e.status
    e.status = "SUBMITTED"
    _log(e, user, frm, e.status, "SUBMIT", comment)
    return e


def reject(db: Session, user: User, exc_id: int, comment: str) -> IncomingException:
    e = get_or_404(db, IncomingException, exc_id, "异常单")
    _expect(e, "SUBMITTED")
    if not comment:
        raise BizError("驳回请填写原因")
    e.status = "REJECTED"
    _log(e, user, "SUBMITTED", "REJECTED", "REJECT", comment)
    return e


def dispose(db: Session, user: User, exc_id: int, disposition: str, responsibility: str,
            comment: str = "", owner_id: int | None = None, deadline: date | None = None) -> IncomingException:
    """审核并确定处置方式与责任方，立即执行库存/采购联动。"""
    e = get_or_404(db, IncomingException, exc_id, "异常单")
    _expect(e, "SUBMITTED")
    valid = QUALITY_DISPOSITIONS if e.category == "QUALITY" else QTY_DISPOSITIONS
    if disposition not in valid:
        raise BizError(f"处置方式无效，可选：{'、'.join(valid.values())}")
    if responsibility not in RESPONSIBILITY:
        raise BizError("请选择责任方")
    e.disposition, e.responsibility = disposition, responsibility
    e.owner_id = owner_id or user.id
    if deadline:
        e.deadline = deadline
    arrival = db.get(Arrival, e.arrival_id) if e.arrival_id else None
    src = _source(db, arrival.source_type, arrival.source_line_id) if arrival else None
    if src is not None:
        if disposition == "RETURN":  # 退货：释放到货占用，余量重新变为“未到”，采购可补单或关闭
            src.arrived_qty = max(D(src.arrived_qty) - D(e.qty), ZERO)
        elif disposition == "CONCESSION":  # 让步接收：直接入库
            wh = arrival.warehouse_id or default_warehouse(db).id
            stock_in(db, material_id=e.material_id, warehouse_id=wh, qty=e.qty, txn_type=TxnType.CONCESSION_IN,
                     lot_no=f"{arrival.arrival_no}-RB", unit_cost=arrival.unit_cost, ref_type="EXCEPTION",
                     ref_id=e.id, supplier_id=arrival.supplier_id, user_id=user.id, remark="让步接收")
            src.received_qty = D(src.received_qty) + D(e.qty)
        elif disposition == "SUPPLEMENT":
            pass  # 少到部分本来就仍是“未到量”，等待补发即可
        elif disposition == "CLOSE_SHORT" and isinstance(src, PurchaseOrderLine):
            src.qty = D(src.arrived_qty)
        _refresh_source(src)
    e.status = "AWAIT_REINSPECT" if disposition in ("EXCHANGE", "REWORK") else "DISPOSED"
    _log(e, user, "SUBMITTED", e.status, "DISPOSE",
         f"{valid[disposition]}；责任方：{RESPONSIBILITY[responsibility]}。{comment}")
    return e


def reinspect(db: Session, user: User, exc_id: int, qualified: Decimal, warehouse_id: int | None = None,
              comment: str = "") -> IncomingException:
    e = get_or_404(db, IncomingException, exc_id, "异常单")
    _expect(e, "AWAIT_REINSPECT")
    qualified = D(qualified)
    if qualified < 0 or qualified > D(e.qty):
        raise BizError(f"复检合格数应在 0 ~ {D(e.qty):g} 之间")
    arrival = db.get(Arrival, e.arrival_id) if e.arrival_id else None
    if qualified > 0:
        wh = warehouse_id or (arrival.warehouse_id if arrival and arrival.warehouse_id else default_warehouse(db).id)
        stock_in(db, material_id=e.material_id, warehouse_id=wh, qty=qualified,
                 txn_type=TxnType.PURCHASE_IN if not arrival or arrival.source_type == "PO" else TxnType.OUTSOURCE_IN,
                 lot_no=f"{arrival.arrival_no if arrival else e.exc_no}-FJ", unit_cost=arrival.unit_cost if arrival else ZERO,
                 ref_type="EXCEPTION", ref_id=e.id, supplier_id=e.supplier_id, user_id=user.id, remark="复检合格入库")
        if arrival:
            src = _source(db, arrival.source_type, arrival.source_line_id)
            src.received_qty = D(src.received_qty) + qualified
            _refresh_source(src)
    e.reinspect_qualified = qualified
    e.status = "REINSPECTED"
    _log(e, user, "AWAIT_REINSPECT", "REINSPECTED", "REINSPECT",
         f"复检合格 {qualified:g} / {D(e.qty):g}。{comment}")
    return e


def close(db: Session, user: User, exc_id: int, result: str) -> IncomingException:
    e = get_or_404(db, IncomingException, exc_id, "异常单")
    _expect(e, "DISPOSED", "REINSPECTED")
    if not result:
        raise BizError("关闭请填写处理结果")
    frm = e.status
    e.status, e.result, e.closed_at = "CLOSED", result, now()
    _log(e, user, frm, "CLOSED", "CLOSE", result)
    return e


EXC_ROLES = {"dispose": (Role.QUALITY, Role.MANAGER), "reject": (Role.QUALITY, Role.MANAGER),
             "reinspect": (Role.QUALITY,), "close": (Role.QUALITY, Role.MANAGER)}
