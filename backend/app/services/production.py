"""自制件加工任务 + 外协单。二者都是齐套计算中的“在途供应”。"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import BizError
from ..models import (Bom, Material, MaterialType, OutsourceOrder, SalesOrder, PickLine, PickList, Supplier, TxnType, User,
                      WorkOrder, ZERO)
from .common import D, audit, get_or_404, next_no
from .drawings import assert_confirmed, assert_not_held
from . import protection as prot
from .inventory import default_warehouse, stock_in
from .kitting import acquire_mutex


def freeze_bom(db: Session, material_id: int, order_id: int | None = None) -> str | None:
    """建单时冻结批准 BOM。关联订单且订单快照里有这个自制/外协件 → 用订单下达时冻结的那一版（与订单批准的版本一致）；
    否则取当前生效 BOM。找不到 BOM 返回 None（开工前必须补齐，空 BOM 仍须物料明确批准免投料）。"""
    if order_id:
        o = db.get(SalesOrder, order_id)
        if o and o.bom_snapshot:
            tree = json.loads(o.bom_snapshot)
            if str(material_id) in tree:
                lines = tree[str(material_id)]
                # 订单树把无下级的节点也记作 []；它不是“批准免投料”的凭据。
                m = db.get(Material, material_id)
                if not lines and not (m and m.bom_exempt):
                    return None
                return json.dumps(lines, ensure_ascii=False)
    b = db.scalar(select(Bom).where(Bom.material_id == material_id, Bom.status == "ACTIVE"))
    if not b or not b.lines:
        m = db.get(Material, material_id)
        return "[]" if m is not None and m.bom_exempt else None  # 只有明确勾选“免 BOM”的工艺才允许无物料投入
    return json.dumps([[ln.child_id, str(D(ln.qty_per) * (1 + D(ln.scrap_rate)))] for ln in b.lines], ensure_ascii=False)


def _ensure_bom(db: Session, doc, what: str) -> list[tuple[int, Decimal]]:
    """取该单据冻结的 BOM；老单据没有快照则现冻结一次；仍没有 BOM 就阻断（资料缺失不等于免投料）。"""
    if doc.bom_snapshot is None:
        doc.bom_snapshot = freeze_bom(db, doc.material_id, getattr(doc, "order_id", None))
        if doc.bom_snapshot is None:
            raise BizError(f"{what}的物料没有生效的 BOM，不能开工/领料/报工。请技术先维护 BOM"
                           "（确无物料投入的工艺，请技术在物料上明确勾选“免 BOM（无物料投入）”）")
    lines = prot.bom_lines(db, doc)
    m = db.get(Material, doc.material_id)
    if not lines and not (m and m.bom_exempt):
        raise BizError(f"{what}没有有效 BOM，空快照不代表免投料；请技术维护 BOM 或明确批准免 BOM")
    return lines


def create_wo(db: Session, user: User, data: dict) -> WorkOrder:
    m = get_or_404(db, Material, data["material_id"], "物料")
    if m.type != MaterialType.MAKE:
        raise BizError("自制任务只能针对自制件")
    w = WorkOrder(wo_no=next_no(db, WorkOrder, "wo_no", "WO"), material_id=m.id, qty=D(data["qty"]),
                  planned_start=data.get("planned_start"), planned_finish=data.get("planned_finish"),
                  order_id=data.get("order_id"), owner=data.get("owner", ""), remark=data.get("remark", ""))
    w.bom_snapshot = freeze_bom(db, m.id, w.order_id)
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
    _ensure_bom(db, w, "自制任务")
    w.status = "STARTED"
    return w


def report_wo(db: Session, user: User, wo_id: int, qty: Decimal, warehouse_id: int | None = None) -> WorkOrder:
    """报工完工：完工数量入生产库，成为可预占库存。"""
    db.flush()  # 保留同一事务前一步服务调用的合法变更，再刷新锁定状态
    acquire_mutex(db)  # 与领料、退料、完工统一：先全局互斥，再锁业务单据
    w = db.scalar(select(WorkOrder).where(WorkOrder.id == wo_id).with_for_update().execution_options(populate_existing=True))  # 行锁：并发报工不超量
    if w is None:
        raise BizError("自制任务不存在", 404)
    if w.status != "STARTED":
        raise BizError("任务需先开工")
    qty = D(qty)
    if qty <= 0 or D(w.done_qty) + qty > D(w.qty):
        raise BizError("报工数量无效")
    assert_not_held(db, "WORK_ORDER", w.id)  # 图纸重大变更暂停期间不得继续产出入库
    # 完工必须有对应的原料消耗：按冻结的批准 BOM 核对“累计完工量所需原料 ≤ 已领未退原料”，不足不能报工（先领料）
    db.flush()
    short = []
    for cid, per in _ensure_bom(db, w, "自制任务"):
        need = per * (D(w.done_qty) + qty)
        have = prot.issued_net(db, PickList.work_order_id, w.id, cid)
        if have < need:
            short.append(f"{db.get(Material, cid).code} 需 {need:g}、已领未退 {have:g}")
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
    for pk in db.scalars(select(PickList).where(PickList.work_order_id == w.id, PickList.status == "DRAFT")).all():
        pk.status = "CANCELLED"
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
    o.bom_snapshot = freeze_bom(db, m.id, o.order_id)
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
    db.flush()  # 保留同一事务前一步服务调用的合法变更，再刷新锁定状态
    acquire_mutex(db)
    o = db.scalar(select(OutsourceOrder).where(OutsourceOrder.id == os_id).with_for_update().execution_options(populate_existing=True))
    if o is None:
        raise BizError("外协单不存在", 404)
    if o.status != "PLANNED":
        raise BizError("外协单已发出")
    assert_confirmed(db, "OUTSOURCE", o.id)  # 发外协前确认图纸版本
    if o.supply_mode == "CONSIGNED":  # 本厂供料：原料必须先领料发出
        for cid, per in _ensure_bom(db, o, "外协单"):
            need = per * D(o.qty)
            if _issued_total(db, PickList.outsource_id, o.id, cid) < need:
                raise BizError("本厂供料外协需先把原料领料发出（外协领料单过账）后才能发出")
    o.status, o.sent_date = "SENT", sent_date or date.today()
    audit(db, user.id, "SEND", "OutsourceOrder", o.id)
    return o


def cancel_outsource(db: Session, user: User, os_id: int) -> OutsourceOrder:
    o = get_or_404(db, OutsourceOrder, os_id, "外协单")
    if D(o.arrived_qty) > 0:
        raise BizError("已有回厂记录，不能取消")
    o.status = "CANCELLED"
    for pk in db.scalars(select(PickList).where(PickList.outsource_id == o.id, PickList.status == "DRAFT")).all():
        pk.status = "CANCELLED"
    return o


# ------------------------------------------------------------------ 为在制任务 / 本厂供料外协领料


def _issued_total(db: Session, col, ref_id: int, material_id: int) -> Decimal:
    return prot.issued_net(db, col, ref_id, material_id)


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
    for cid, per in _ensure_bom(db, w, "自制任务"):
        remain = max(per * D(w.qty) - _issued_total(db, PickList.work_order_id, w.id, cid), ZERO)
        if remain > 0:
            pk.lines.append(PickLine(material_id=cid, plan_qty=remain, qty=remain))
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
    for cid, per in _ensure_bom(db, o, "外协单"):
        remain = max(per * D(o.qty) - _issued_total(db, PickList.outsource_id, o.id, cid), ZERO)
        if remain > 0:
            pk.lines.append(PickLine(material_id=cid, plan_qty=remain, qty=remain))
    if not pk.lines:
        raise BizError("原料已发齐")
    db.add(pk)
    db.flush()
    return pk
