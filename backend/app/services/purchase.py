"""采购服务。

关键设计（对应录音痛点“任一行入库审核整单锁定，纠错需整单弃审并作废下游单据”）：
  * 锁定粒度是【行】：某行有到货/入库后只锁该行，同单其他行照常可改、可删、可新增行。
  * 已锁定行的纠错走【行级红字冲销】：先做影响分析（关联到货、检验、批次、异常单），
    批次未被领用时自动冲销该行库存并解锁该行，不需要弃审整单、不影响其他行。
  * 每次修改都留修订记录（谁、何时、改前改后、原因）。
  * 编辑锁 + 版本号：防止多人同时改同一张多供应商单据。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..core.errors import BizError, Conflict
from ..models import (Arrival, IncomingException, KitStatus, Material, MaterialType, OrderStatus, PoLineRevision, PurchaseOrder, PurchaseOrderLine, Requisition, RequisitionLine, Reservation, Role, SalesOrder, StockLot, StockTxn, Supplier, TxnType, User, Warehouse, WarehousePurpose, ZERO, now)
from .alerts import raise_alert
from .common import D, audit, dumps, get_or_404, next_no
from .inventory import fifo_out

# ------------------------------------------------------------ 历史供应商 / 价格 / 采购员自动带出


def price_history(db: Session, material_id: int, limit: int = 10) -> list[dict]:
    rows = db.execute(
        select(PurchaseOrderLine, PurchaseOrder, Supplier)
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
        .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
        .where(PurchaseOrderLine.material_id == material_id, PurchaseOrder.status.in_(["APPROVED", "CLOSED"]))
        .order_by(PurchaseOrder.order_date.desc(), PurchaseOrderLine.id.desc()).limit(limit)).all()
    return [{"po_no": po.po_no, "order_date": po.order_date, "supplier_id": s.id, "supplier": s.name,
             "price": ln.price, "qty": ln.qty, "buyer_id": po.buyer_id,
             "on_time": (ln.promised_date is None or ln.need_date is None or ln.promised_date <= ln.need_date)}
            for ln, po, s in rows]


def suggest(db: Session, material_id: int, supplier_id: int | None = None) -> dict:
    """带出：供应商（指定 > 最近一次 > 物料默认）、该供应商最近价格、采购员（物料默认 > 最近一次）。"""
    m = get_or_404(db, Material, material_id, "物料")
    hist = price_history(db, material_id, 20)
    sid = supplier_id or (hist[0]["supplier_id"] if hist else m.default_supplier_id)
    price = next((h["price"] for h in hist if h["supplier_id"] == sid), None)
    buyer = m.default_buyer_id or (hist[0]["buyer_id"] if hist else None)
    sup = db.get(Supplier, sid) if sid else None
    bu = db.get(User, buyer) if buyer else None
    return {"material_id": m.id, "drawing_no": m.drawing_no, "supplier_id": sid,
            "supplier": sup.name if sup else "", "price": price, "buyer_id": buyer,
            "buyer": bu.name if bu else "", "history": hist[:5]}


# ------------------------------------------------------------ 请购计划单


def create_requisition(db: Session, user: User, lines: list[dict], source: str = "MANUAL",
                       remark: str = "") -> Requisition:
    if not lines:
        raise BizError("请购单至少一行")
    req = Requisition(req_no=next_no(db, Requisition, "req_no", "PR"), source=source, created_by=user.id,
                      remark=remark)
    for ln in lines:
        s = suggest(db, ln["material_id"], ln.get("supplier_id"))
        req.lines.append(RequisitionLine(
            material_id=ln["material_id"], qty=D(ln["qty"]), need_date=ln.get("need_date"),
            drawing_no=ln.get("drawing_no") or s["drawing_no"], supplier_id=s["supplier_id"],
            price=ln.get("price") if ln.get("price") is not None else s["price"],
            buyer_id=ln.get("buyer_id") or s["buyer_id"], order_id=ln.get("order_id"),
            remark=ln.get("remark", "")))
    db.add(req)
    db.flush()
    audit(db, user.id, "CREATE", "Requisition", req.id, {"lines": len(lines), "source": source})
    return req


def requisition_from_shortages(db: Session, user: User, shortages: list[dict]) -> dict:
    """缺料 → 外购件生成请购单；自制件/外协件返回建议（由计划员确认后建自制任务/外协单）。"""
    buy, make, outsource = [], [], []
    for s in shortages:
        item = {"material_id": s["material_id"], "qty": s["short_qty"], "need_date": s["need_date"],
                "order_id": s["orders"][0]["order_id"] if len(s["orders"]) == 1 else None,
                "remark": "齐套缺料：" + "、".join(o["order_no"] for o in s["orders"][:5])}
        if s["type"] == MaterialType.BUY:
            buy.append(item)
        elif s["type"] == MaterialType.OUTSOURCE:
            outsource.append({**item, **{k: s[k] for k in ("code", "name")}})
        else:
            make.append({**item, **{k: s[k] for k in ("code", "name")}})
    req = create_requisition(db, user, buy, "KITTING") if buy else None
    return {"requisition_id": req.id if req else None, "requisition_no": req.req_no if req else None,
            "make_suggestions": make, "outsource_suggestions": outsource}


def update_requisition_line(db: Session, user: User, line_id: int, data: dict) -> RequisitionLine:
    ln = get_or_404(db, RequisitionLine, line_id, "请购行")
    if ln.req.status not in ("DRAFT", "CONFIRMED"):
        raise BizError("请购单已转采购，不能再修改；请在采购订单行上修改")
    before = {k: getattr(ln, k) for k in data}
    for k in ("drawing_no", "qty", "need_date", "supplier_id", "price", "buyer_id", "remark"):
        if k in data:
            setattr(ln, k, D(data[k]) if k in ("qty", "price") and data[k] is not None else data[k])
    if "supplier_id" in data and "price" not in data:  # 换供应商时自动带出该供应商历史价
        ln.price = suggest(db, ln.material_id, data["supplier_id"])["price"]
    audit(db, user.id, "EDIT", "RequisitionLine", ln.id, {"before": before, "after": data})
    return ln


def convert_requisition(db: Session, user: User, req_id: int) -> list[PurchaseOrder]:
    req = get_or_404(db, Requisition, req_id, "请购单")
    if req.status not in ("DRAFT", "CONFIRMED"):
        raise BizError("该请购单已处理")
    missing = [ln.material.code for ln in req.lines if not ln.supplier_id]
    if missing:
        raise BizError(f"以下物料未指定供应商：{'、'.join(missing)}")
    groups: dict[tuple[int, int | None], list[RequisitionLine]] = defaultdict(list)
    for ln in req.lines:
        groups[(ln.supplier_id, ln.buyer_id)].append(ln)
    pos = []
    for (sid, bid), lines in groups.items():
        po = PurchaseOrder(po_no=next_no(db, PurchaseOrder, "po_no", "PO"), supplier_id=sid,
                           buyer_id=bid or user.id, remark=f"由请购单 {req.req_no} 生成")
        for i, ln in enumerate(lines, 1):
            po.lines.append(PurchaseOrderLine(line_no=i * 10, material_id=ln.material_id, drawing_no=ln.drawing_no,
                                              qty=ln.qty, price=D(ln.price), need_date=ln.need_date,
                                              order_id=ln.order_id))
        db.add(po)
        db.flush()
        for ln, pl in zip(lines, po.lines):
            ln.po_line_id = pl.id
        pos.append(po)
    req.status = "CONVERTED"
    audit(db, user.id, "CONVERT", "Requisition", req.id, [p.po_no for p in pos])
    return pos


# ------------------------------------------------------------ 采购订单：编辑锁 / 版本


def acquire_edit_lock(db: Session, user: User, po: PurchaseOrder, force: bool = False) -> None:
    ts = now()
    if (po.edit_lock_user and po.edit_lock_user != user.id and po.edit_lock_until and po.edit_lock_until > ts
            and not force):
        who = db.get(User, po.edit_lock_user)
        raise Conflict(f"{who.name if who else '其他用户'} 正在编辑该采购单（{po.edit_lock_until:%H:%M} 前），请稍后",
                       {"locked_by": who.name if who else ""})
    po.edit_lock_user = user.id
    po.edit_lock_until = ts + timedelta(minutes=settings.edit_lock_minutes)


def release_edit_lock(db: Session, user: User, po: PurchaseOrder) -> None:
    if po.edit_lock_user == user.id:
        po.edit_lock_user, po.edit_lock_until = None, None


def _check_editable(db: Session, user: User, po: PurchaseOrder, version: int | None,
                    allow_closed: bool = False) -> None:
    if po.status == "CANCELLED" or (po.status == "CLOSED" and not allow_closed):
        raise BizError("采购单已关闭/取消")
    if version is not None and version != po.version:
        raise Conflict("采购单已被他人修改，请刷新后再操作")
    ts = now()
    if po.edit_lock_user and po.edit_lock_user != user.id and po.edit_lock_until and po.edit_lock_until > ts:
        who = db.get(User, po.edit_lock_user)
        raise Conflict(f"{who.name if who else '其他用户'} 正在编辑该采购单")


def _bump(po: PurchaseOrder) -> None:
    po.version = (po.version or 1) + 1


def _rev(db: Session, user: User | None, line: PurchaseOrderLine, action: str, before: dict, after: dict,
         reason: str = "") -> None:
    db.add(PoLineRevision(po_line_id=line.id, action=action, before=dumps(before), after=dumps(after),
                          reason=reason, user_id=user.id if user else None, created_at=now()))


def refresh_line_status(line: PurchaseOrderLine) -> None:
    if line.status in ("CLOSED", "CANCELLED"):
        return
    if D(line.received_qty) >= D(line.qty) and D(line.qty) > 0:
        line.status = "RECEIVED"
    elif D(line.arrived_qty) > 0:
        line.status = "PARTIAL"
    else:
        line.status = "OPEN"


def refresh_po_status(po: PurchaseOrder) -> None:
    if po.status == "APPROVED" and po.lines and all(ln.status in ("RECEIVED", "CLOSED", "CANCELLED") for ln in po.lines):
        po.status = "CLOSED"
    elif po.status == "CLOSED" and any(ln.status in ("OPEN", "PARTIAL") for ln in po.lines):
        po.status = "APPROVED"


def create_po(db: Session, user: User, supplier_id: int, lines: list[dict], remark: str = "",
              buyer_id: int | None = None) -> PurchaseOrder:
    get_or_404(db, Supplier, supplier_id, "供应商")
    po = PurchaseOrder(po_no=next_no(db, PurchaseOrder, "po_no", "PO"), supplier_id=supplier_id,
                       buyer_id=buyer_id or user.id, remark=remark)
    for i, ln in enumerate(lines, 1):
        s = suggest(db, ln["material_id"], supplier_id)
        po.lines.append(PurchaseOrderLine(
            line_no=i * 10, material_id=ln["material_id"], drawing_no=ln.get("drawing_no") or s["drawing_no"],
            qty=D(ln["qty"]), price=D(ln["price"]) if ln.get("price") is not None else D(s["price"]),
            need_date=ln.get("need_date"), promised_date=ln.get("promised_date"), order_id=ln.get("order_id")))
    db.add(po)
    db.flush()
    audit(db, user.id, "CREATE", "PurchaseOrder", po.id, {"lines": len(lines)})
    return po


def approve_po(db: Session, user: User, po_id: int) -> PurchaseOrder:
    po = get_or_404(db, PurchaseOrder, po_id, "采购单")
    if po.status != "DRAFT":
        raise BizError("只有草稿状态可以审核")
    if not po.lines:
        raise BizError("采购单没有明细行")
    po.status = "APPROVED"
    _bump(po)
    audit(db, user.id, "APPROVE", "PurchaseOrder", po.id)
    return po


def add_line(db: Session, user: User, po_id: int, data: dict, version: int | None = None) -> PurchaseOrderLine:
    po = get_or_404(db, PurchaseOrder, po_id, "采购单")
    _check_editable(db, user, po, version)
    s = suggest(db, data["material_id"], po.supplier_id)
    no = max((ln.line_no for ln in po.lines), default=0) + 10
    ln = PurchaseOrderLine(line_no=no, material_id=data["material_id"],
                           drawing_no=data.get("drawing_no") or s["drawing_no"], qty=D(data["qty"]),
                           price=D(data["price"]) if data.get("price") is not None else D(s["price"]),
                           need_date=data.get("need_date"), promised_date=data.get("promised_date"),
                           order_id=data.get("order_id"))
    po.lines.append(ln)
    refresh_po_status(po)
    _bump(po)
    db.flush()
    _rev(db, user, ln, "ADD", {}, data)
    return ln


FREE_FIELDS = ("material_id", "drawing_no", "qty", "price", "need_date", "promised_date", "supplier_note")
LOCKED_FIELDS = ("qty", "need_date", "promised_date", "supplier_note")


def edit_line(db: Session, user: User, line_id: int, data: dict, reason: str = "",
              version: int | None = None) -> PurchaseOrderLine:
    ln = get_or_404(db, PurchaseOrderLine, line_id, "采购行")
    po = ln.po
    _check_editable(db, user, po, version)
    if ln.status in ("CLOSED", "CANCELLED"):
        raise BizError("该行已关闭/取消")
    allowed = LOCKED_FIELDS if ln.locked else FREE_FIELDS
    bad = [k for k in data if k not in allowed]
    if bad:
        raise BizError(f"第 {ln.line_no} 行已有到货/入库，已锁定字段：{'、'.join(bad)}。"
                       f"如需更正，请对该行做“行级冲销”（不影响其他行）",
                       data={"locked": True, "fields": bad})
    if "qty" in data and D(data["qty"]) < D(ln.arrived_qty):
        raise BizError(f"数量不能小于已到货数量 {D(ln.arrived_qty):g}")
    if po.status == "APPROVED" and not reason:
        raise BizError("已审核的采购单修改需要填写原因")
    before = {k: getattr(ln, k) for k in data}
    for k, v in data.items():
        setattr(ln, k, D(v) if k in ("qty", "price") else v)
    refresh_line_status(ln)
    refresh_po_status(po)
    _bump(po)
    _rev(db, user, ln, "EDIT", before, data, reason)
    return ln


def _lot_inflow(db: Session, lot_id: int) -> Decimal:
    return D(db.scalar(select(func.coalesce(func.sum(StockTxn.qty), 0)).where(StockTxn.lot_id == lot_id, StockTxn.qty > 0)))


def _derived_lots(db: Session, arrivals: list, lock: bool = False) -> list[tuple]:
    """到货 → 来料异常 → 让步接收/复检合格 派生出的入库批次（谱系）。返回 [(批次, 异常单)]。"""
    out = []
    for a in arrivals:
        for e in db.scalars(select(IncomingException).where(IncomingException.arrival_id == a.id)).all():
            q = select(StockLot).where(StockLot.source_type == "EXCEPTION", StockLot.source_id == e.id)
            for lot in db.scalars(q.with_for_update() if lock else q).all():
                out.append((lot, e))
    return out


def line_impact(db: Session, line_id: int) -> dict:
    """行级冲销前的影响分析：列出下游单据，判断能否自动冲销。"""
    ln = get_or_404(db, PurchaseOrderLine, line_id, "采购行")
    arrivals = db.scalars(select(Arrival).where(Arrival.source_type == "PO", Arrival.source_line_id == ln.id,
                                                Arrival.status != "REVERSED")).all()
    items, blockers = [], []
    a_lbl = {"PENDING": "待检验", "INSPECTED": "已检验入库"}
    e_lbl = {"DRAFT": "草稿", "SUBMITTED": "待审核", "REJECTED": "已驳回", "DISPOSED": "处置中",
             "AWAIT_REINSPECT": "待复检", "REINSPECTED": "已复检", "CLOSED": "已关闭"}
    for a in arrivals:
        items.append({"type": "到货单", "no": a.arrival_no, "qty": a.qty, "status": a_lbl.get(a.status, a.status)})
        if a.lot_id:
            lot = db.get(StockLot, a.lot_id)
            consumed = D(a.qualified_qty) - D(lot.qty) if lot else D(a.qualified_qty)
            items.append({"type": "入库批次", "no": lot.lot_no if lot else "-", "qty": a.qualified_qty,
                          "remain": lot.qty if lot else 0})
            if consumed > 0:
                blockers.append(f"批次 {lot.lot_no if lot else '-'} 已被领用/出库 {consumed:g}，需先退料")
        for e in db.scalars(select(IncomingException).where(IncomingException.arrival_id == a.id)).all():
            items.append({"type": "来料异常单", "no": e.exc_no, "qty": e.qty, "status": e_lbl.get(e.status, e.status)})
            if e.status != "CLOSED":
                blockers.append(f"来料异常单 {e.exc_no} 未关闭")
    # 异常处置派生的批次（让步接收、复检合格）同属该采购行，冲销必须一并处理，已被领用的同样阻断
    derived = _derived_lots(db, arrivals)
    for lot, e in derived:
        consumed = _lot_inflow(db, lot.id) - D(lot.qty)
        items.append({"type": "异常处置入库批次", "no": lot.lot_no, "qty": _lot_inflow(db, lot.id), "remain": lot.qty})
        if consumed > 0:
            blockers.append(f"异常单 {e.exc_no} 派生的批次 {lot.lot_no} 已被领用/出库 {consumed:g}，需先退料")
    # 冲回后预演：把这批库存拿掉，看哪些订单的齐套会变差。
    # 已开始装配的订单、或有人工锁定预占的订单受影响 → 阻断；其余只列出影响（冻结量由系统按优先级重分）
    reversible = sum((D(db.get(StockLot, a.lot_id).qty) for a in arrivals if a.lot_id and db.get(StockLot, a.lot_id)), ZERO)
    reversible += sum((D(lot.qty) for lot, _e in derived), ZERO)
    if reversible > 0:
        from dataclasses import replace
        from . import kitting
        snap = kitting.load_snapshot(db)
        base = kitting.compute(snap)
        oh = dict(snap.on_hand)
        oh[ln.material_id] = max(oh.get(ln.material_id, ZERO) - reversible, ZERO)
        sim = kitting.compute(replace(snap, on_hand=oh))
        hard = {oid for oid, _m, _q, _r in snap.locked if _m == ln.material_id}
        for oid, b in base.orders.items():
            sm = sim.orders.get(oid)
            if sm and (sm.status != b.status or sm.ready_date != b.ready_date):
                so = db.get(SalesOrder, oid)
                items.append({"type": "受影响订单", "no": so.order_no, "qty": 0,
                              "status": f"{KitStatus.LABELS.get(b.status, b.status)} → {KitStatus.LABELS.get(sm.status, sm.status)}"})
                if so.status == OrderStatus.ASSEMBLING or oid in hard:
                    blockers.append(f"订单 {so.order_no}（{'装配中' if so.status == OrderStatus.ASSEMBLING else '有人工锁定预占'}）"
                                    "会因冲销失去已保障的料，请先走调料申请/退补")
    actions = []
    if blockers:
        actions = ["已领用的料：先在领料单上【退料】，再冲销", "质量问题：走【来料异常单】的退货/换货处置",
                   "数量或价格有误：用【关闭剩余】+ 补行，原到货/入库记录保留", "涉及他单预占：先走【调料申请】腾出库存"]
    return {"line_id": ln.id, "line_no": ln.line_no, "locked": ln.locked, "items": items,
            "can_reverse": ln.locked and not blockers, "blockers": blockers, "next_steps": actions,
            "other_lines_affected": 0}


def reverse_line(db: Session, user: User, line_id: int, reason: str, version: int | None = None) -> dict:
    """行级红字冲销：冲回该行入库批次、作废该行到货，解锁该行。同单其他行不动。"""
    if not reason:
        raise BizError("冲销必须填写原因")
    ln = get_or_404(db, PurchaseOrderLine, line_id, "采购行")
    _check_editable(db, user, ln.po, version, allow_closed=True)  # 收满后发现录错，正是要冲销的场景：冲销后重新打开
    # 先锁住该行的到货单与批次，再做影响分析：分析与执行之间不会被并发领用/冲销打断
    for a in db.scalars(select(Arrival).where(Arrival.source_type == "PO", Arrival.source_line_id == ln.id)
                        .with_for_update()).all():
        if a.lot_id:
            db.scalar(select(StockLot).where(StockLot.id == a.lot_id).with_for_update())
    _arrs = db.scalars(select(Arrival).where(Arrival.source_type == "PO", Arrival.source_line_id == ln.id)).all()
    _derived_lots(db, _arrs, lock=True)
    imp = line_impact(db, line_id)
    if not ln.locked:
        raise BizError("该行未锁定，可直接修改，无需冲销")
    if imp["blockers"]:
        raise Conflict("无法自动冲销：" + "；".join(imp["blockers"]), imp)
    before = {"arrived_qty": ln.arrived_qty, "received_qty": ln.received_qty, "status": ln.status}
    for a in db.scalars(select(Arrival).where(Arrival.source_type == "PO", Arrival.source_line_id == ln.id,
                                              Arrival.status != "REVERSED").with_for_update()).all():
        if a.lot_id:
            lot = db.scalar(select(StockLot).where(StockLot.id == a.lot_id).with_for_update())
            if lot and D(lot.qty) > 0:
                fifo_out(db, material_id=lot.material_id, warehouse_id=lot.warehouse_id, qty=D(lot.qty),
                         txn_type=TxnType.REVERSAL, ref_type="PO_LINE", ref_id=ln.id, user_id=user.id,
                         remark=f"采购行冲销：{reason}", lot_id=lot.id)
        a.status = "REVERSED"
    for lot, e in _derived_lots(db, db.scalars(select(Arrival).where(
            Arrival.source_type == "PO", Arrival.source_line_id == ln.id)).all(), lock=True):
        if D(lot.qty) > 0:
            fifo_out(db, material_id=lot.material_id, warehouse_id=lot.warehouse_id, qty=D(lot.qty),
                     txn_type=TxnType.REVERSAL, ref_type="PO_LINE", ref_id=ln.id, user_id=user.id,
                     remark=f"采购行冲销（异常单 {e.exc_no} 派生批次）：{reason}", lot_id=lot.id)
    ln.arrived_qty, ln.received_qty = ZERO, ZERO
    ln.status = "OPEN"
    refresh_po_status(ln.po)
    _bump(ln.po)
    _rev(db, user, ln, "REVERSE", before, {"arrived_qty": 0, "received_qty": 0, "status": "OPEN"}, reason)
    audit(db, user.id, "REVERSE", "PurchaseOrderLine", ln.id, {"reason": reason, "impact": imp["items"]})
    return imp


def close_remaining(db: Session, user: User, line_id: int, reason: str, version: int | None = None) -> PurchaseOrderLine:
    """关闭行剩余未到量（例如供应商只能供部分，余量转其他供应商）。"""
    ln = get_or_404(db, PurchaseOrderLine, line_id, "采购行")
    _check_editable(db, user, ln.po, version)
    if not reason:
        raise BizError("请填写关闭原因")
    before = {"status": ln.status, "qty": ln.qty}
    ln.status = "CLOSED" if D(ln.arrived_qty) > 0 else "CANCELLED"
    refresh_po_status(ln.po)
    _bump(ln.po)
    _rev(db, user, ln, "CLOSE_REMAIN", before, {"status": ln.status}, reason)
    return ln


def delete_line(db: Session, user: User, line_id: int, version: int | None = None) -> None:
    ln = get_or_404(db, PurchaseOrderLine, line_id, "采购行")
    po = ln.po
    _check_editable(db, user, po, version)
    if ln.locked:
        raise BizError("该行已有到货，不能删除；可先行级冲销或关闭剩余")
    if po.status == "DRAFT":
        po.lines.remove(ln)
        db.delete(ln)
    else:
        before = {"status": ln.status}
        ln.status = "CANCELLED"
        _rev(db, user, ln, "CANCEL", before, {"status": "CANCELLED"})
    refresh_po_status(po)
    _bump(po)


# ------------------------------------------------------------ 供应商门户（线上跟单）


def portal_lines(db: Session, supplier: Supplier) -> list[PurchaseOrderLine]:
    return db.scalars(select(PurchaseOrderLine).join(PurchaseOrder)
                      .where(PurchaseOrder.supplier_id == supplier.id, PurchaseOrder.status == "APPROVED",
                             PurchaseOrderLine.status.in_(["OPEN", "PARTIAL"]))
                      .order_by(PurchaseOrderLine.need_date, PurchaseOrder.po_no)).all()


def supplier_confirm(db: Session, supplier: Supplier, line_id: int, promised_date: date, note: str = "") -> PurchaseOrderLine:
    ln = get_or_404(db, PurchaseOrderLine, line_id, "采购行")
    if ln.po.supplier_id != supplier.id:
        raise BizError("无权操作该订单", 403)
    before = {"promised_date": ln.promised_date, "supplier_note": ln.supplier_note}
    ln.promised_date, ln.supplier_note = promised_date, note[:300]
    _rev(db, None, ln, "SUPPLIER_DATE", before, {"promised_date": promised_date, "supplier_note": note},
         f"供应商 {supplier.name} 回复")
    avail = promised_date + timedelta(days=max(ln.material.inbound_days or 0, 0))  # 可用日 = 承诺到货 + 检验转运
    cutoff = ln.need_date
    if cutoff and avail > cutoff:
        raise_alert(db, "SUPPLIER_LATE", f"SUPLATE:{ln.id}",
                    f"供应商交期晚于需求：{ln.po.po_no} 第{ln.line_no}行 {ln.material.name}",
                    f"{supplier.name} 承诺 {promised_date}，加检验转运后 {avail} 可用，晚于需求日 {ln.need_date}",
                    "WARN", Role.BUYER, "PO", ln.po_id)
    return ln
