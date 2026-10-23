"""自制件加工任务 + 外协单。二者都是齐套计算中的“在途供应”。"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import BizError
from ..models import (Alert, Bom, Material, MaterialType, OrderStatus, OutsourceOrder, SalesOrder, PickLine, PickList, Supplier,
                      TxnType, User, WorkOrder, ZERO, now)
from .common import D, audit, get_or_404, next_no
from .drawings import assert_confirmed, assert_not_held
from . import protection as prot
from .inventory import default_warehouse, stock_in
from .kitting import acquire_mutex

TASK_SOURCE_MODES = {"ORDER", "STOCK"}


def _validate_task_source(db: Session, data: dict, material_id: int) -> tuple[str, int | None]:
    """任务来源必须显式选择；按订单生产时校验订单状态和冻结 BOM 归属。"""
    mode, order_id = data.get("source_mode"), data.get("order_id")
    if mode not in TASK_SOURCE_MODES:
        raise BizError("请选择任务来源：按整机订单生产或备库生产")
    if mode == "STOCK":
        if order_id:
            raise BizError("备库生产不能同时关联整机订单")
        return mode, None
    if not order_id:
        raise BizError("按整机订单生产必须选择整机订单")
    order = get_or_404(db, SalesOrder, order_id, "整机订单")
    if order.status not in OrderStatus.ACTIVE_FOR_KIT:
        raise BizError(f"订单 {order.order_no} 已结束或取消，不能再创建生产任务")
    if order.bom_snapshot:
        tree = json.loads(order.bom_snapshot)
    else:
        from .orders import active_bom, bom_snapshot
        top = active_bom(db, order.product_id)
        tree = bom_snapshot(db, top) if top else {}
    if str(material_id) not in tree:
        raise BizError(f"该物料不在订单 {order.order_no} 的批准 BOM 中，不能错误关联")
    return mode, order.id


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


def bom_state(doc) -> str:
    """CONFIRMED：BOM 快照已冻结（建单时取自订单批准版/当前生效版，或技术已确认）；
    PENDING_CONFIRM：没有快照（迁移前的旧单据，或建单时还没有 BOM），须技术核对后确认。"""
    return "PENDING_CONFIRM" if doc.bom_snapshot is None else "CONFIRMED"


def require_bom_confirmed(doc, what: str) -> None:
    """未经确认的单据不得开工/领料/过账/报工/退料：系统绝不替技术自动选一版 BOM。"""
    if doc.bom_snapshot is None:
        kind = "WORK_ORDER" if isinstance(doc, WorkOrder) else "OUTSOURCE"
        raise BizError(f"{what}的 BOM 还没有经过技术确认（旧单据或建单时尚无 BOM）。"
                       "请技术在单据上核对并【确认 BOM】（选定采用哪一版）后再操作",
                       data={"need_bom_confirm": True, "kind": kind, "id": doc.id})


def _ensure_bom(db: Session, doc, what: str) -> list[tuple[int, Decimal]]:
    """取该单据冻结的 BOM。没有快照 → 阻断并提示技术确认（不再自动冻结当前生效版）；
    快照为空列表也不等于免投料，除非物料上明确勾选“免 BOM”。"""
    require_bom_confirmed(doc, what)
    lines = prot.bom_lines(db, doc)
    m = db.get(Material, doc.material_id)
    if not lines and not (m and m.bom_exempt):
        raise BizError(f"{what}没有有效 BOM，空快照不代表免投料；请技术维护 BOM 或明确批准免 BOM")
    return lines


def _bom_lines_view(db: Session, bom: Bom) -> list[dict]:
    return [{"child_id": ln.child_id, "code": ln.child.code, "name": ln.child.name, "unit": ln.child.unit,
             "qty_per": ln.qty_per, "scrap_rate": ln.scrap_rate,
             "per_unit": D(ln.qty_per) * (1 + D(ln.scrap_rate))} for ln in bom.lines]


def _doc_kind(doc) -> tuple[str, object]:
    return ("WORK_ORDER", PickList.work_order_id) if isinstance(doc, WorkOrder) else ("OUTSOURCE", PickList.outsource_id)


def bom_candidates(db: Session, doc) -> dict:
    """给技术确认用：当前状态、该物料各版本 BOM、订单批准版（若关联订单）、单据已领料/已报工情况。"""
    kind, col = _doc_kind(doc)
    boms = db.scalars(select(Bom).where(Bom.material_id == doc.material_id, Bom.status != "DRAFT")
                      .order_by(Bom.id.desc())).all()
    order_lines = None
    if getattr(doc, "order_id", None):
        o = db.get(SalesOrder, doc.order_id)
        tree = json.loads(o.bom_snapshot) if o and o.bom_snapshot else {}
        if str(doc.material_id) in tree:
            order_lines = [{"child_id": int(c), "code": db.get(Material, int(c)).code, "per_unit": D(p)}
                           for c, p in tree[str(doc.material_id)]]
    issued = []
    for mid in {pl.material_id for pl in db.scalars(select(PickLine).join(PickList).where(
            col == doc.id, PickList.status == "POSTED")).all()}:
        net = prot.issued_net(db, col, doc.id, mid)
        issued.append({"material_id": mid, "code": db.get(Material, mid).code, "issued_net": net})
    return {"kind": kind, "id": doc.id, "state": bom_state(doc), "qty": doc.qty,
            "done_qty": getattr(doc, "done_qty", None) if kind == "WORK_ORDER" else doc.arrived_qty,
            "confirmed_at": doc.bom_confirmed_at, "confirm_note": doc.bom_confirm_note,
            "boms": [{"id": b.id, "version": b.version, "status": b.status, "lines": _bom_lines_view(db, b)}
                     for b in boms],
            "order_bom": order_lines, "issued": issued}


def confirm_bom(db: Session, user: User, kind: str, doc_id: int, *, bom_id: int | None = None,
                use_order_bom: bool = False, reason: str = "") -> dict:
    """技术确认：为没有快照的旧单据选定采用的 BOM 版本并冻结。必须填写依据；留痕（谁、何时、选了哪一版、已领料情况）。
    可以选择非当前生效的旧版本（旧单据当初是按旧版开的），也可以直接沿用关联订单的批准版。"""
    if not (reason or "").strip():
        raise BizError("确认 BOM 必须填写依据（例如：按图纸/工艺卡核对，采用 V1）")
    model = WorkOrder if kind == "WORK_ORDER" else OutsourceOrder
    db.flush()
    acquire_mutex(db)
    doc = db.scalar(select(model).where(model.id == doc_id).with_for_update().execution_options(populate_existing=True))
    if doc is None:
        raise BizError("单据不存在", 404)
    if doc.bom_snapshot is not None:
        raise BizError("该单据的 BOM 已确认；如需换版本请走变更，不能直接改写已冻结的快照")
    if use_order_bom == (bom_id is not None):
        raise BizError("请二选一：指定 BOM 版本 bom_id，或沿用关联订单的批准版 use_order_bom")
    if use_order_bom:
        o = db.get(SalesOrder, doc.order_id) if doc.order_id else None
        tree = json.loads(o.bom_snapshot) if o and o.bom_snapshot else {}
        if str(doc.material_id) not in tree or not tree[str(doc.material_id)]:
            raise BizError("该单据没有关联订单，或订单批准 BOM 中没有这个件，请改选 BOM 版本")
        lines = tree[str(doc.material_id)]
        picked = {"source": "ORDER", "order_id": doc.order_id}
    else:
        b = db.get(Bom, bom_id)
        if b is None or b.material_id != doc.material_id or b.status == "DRAFT":
            raise BizError("BOM 版本无效：必须是该物料已发布（生效或已作废）的版本")
        if not b.lines:
            raise BizError("所选 BOM 没有子件")
        lines = [[ln.child_id, str(D(ln.qty_per) * (1 + D(ln.scrap_rate)))] for ln in b.lines]
        picked = {"source": "BOM", "bom_id": b.id, "version": b.version, "status": b.status}
    # 与已发生的领料/报工核对：只提示，不替技术做决定
    kind_, col = _doc_kind(doc)
    need = {int(c): Decimal(p) for c, p in lines}
    done = D(getattr(doc, "done_qty", 0)) if kind_ == "WORK_ORDER" else ZERO
    warnings = []
    for mid in {pl.material_id for pl in db.scalars(select(PickLine).join(PickList).where(
            col == doc.id, PickList.status == "POSTED")).all()}:
        net = prot.issued_net(db, col, doc.id, mid)
        if mid not in need and net > 0:
            warnings.append(f"已领 {db.get(Material, mid).code} {net:g}，但所选 BOM 里没有这个子件")
        elif done > 0 and net < need.get(mid, ZERO) * done:
            warnings.append(f"{db.get(Material, mid).code} 已领未退 {net:g}，不够所选 BOM 对已完工 {done:g} 的用量"
                            f" {need[mid] * done:g}")
    doc.bom_snapshot = json.dumps(lines, ensure_ascii=False)
    doc.bom_confirmed_by, doc.bom_confirmed_at, doc.bom_confirm_note = user.id, now(), reason.strip()[:300]
    for a in db.scalars(select(Alert).where(Alert.dedupe_key == f"BOM_CONFIRM:{kind}:{doc.id}", Alert.status != "RESOLVED")).all():
        a.status, a.resolved_at = "RESOLVED", now()
    audit(db, user.id, "BOM_CONFIRM", model.__name__, doc.id, {**picked, "reason": reason, "warnings": warnings})
    return {"doc": doc, "warnings": warnings}


def create_wo(db: Session, user: User, data: dict) -> WorkOrder:
    m = get_or_404(db, Material, data["material_id"], "物料")
    if m.type != MaterialType.MAKE:
        raise BizError("自制任务只能针对自制件")
    source_mode, order_id = _validate_task_source(db, data, m.id)
    w = WorkOrder(wo_no=next_no(db, WorkOrder, "wo_no", "WO"), material_id=m.id, qty=D(data["qty"]),
                  planned_start=data.get("planned_start"), planned_finish=data.get("planned_finish"),
                  source_mode=source_mode, order_id=order_id, owner=data.get("owner", ""), remark=data.get("remark", ""))
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
    source_mode, order_id = _validate_task_source(db, data, m.id)
    o = OutsourceOrder(os_no=next_no(db, OutsourceOrder, "os_no", "OS"), supplier_id=s.id, material_id=m.id,
                       drawing_no=data.get("drawing_no") or m.drawing_no, qty=D(data["qty"]),
                       price=D(data.get("price") or 0), expected_return=data.get("expected_return"),
                       source_mode=source_mode, order_id=order_id, supply_mode=data.get("supply_mode") or "TURNKEY",
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
    """取消外协单。只有“计划”状态（还没发出）才能直接取消：
    已发出的外协单，料和图纸已经在外协厂，直接取消会让已出库的料悬空（退料口径把它当成已消耗）。
    确实未加工的，先【撤回发出】把单据退回计划状态，料才能退回，再取消。"""
    db.flush()
    acquire_mutex(db)
    o = db.scalar(select(OutsourceOrder).where(OutsourceOrder.id == os_id).with_for_update()
                  .execution_options(populate_existing=True))
    if o is None:
        raise BizError("外协单不存在", 404)
    if o.status in ("RETURNED", "CANCELLED"):
        raise BizError("外协单已结束")
    if D(o.arrived_qty) > 0:
        raise BizError("已有回厂记录，不能取消")
    if o.status != "PLANNED":
        raise BizError("外协单已发出（料/图纸已在外协厂），不能直接取消。确认外协厂尚未加工的，请先【撤回发出】"
                       "（本厂供料的原料随后可在领料单上退回），再取消", data={"need_recall": True})
    o.status = "CANCELLED"
    for pk in db.scalars(select(PickList).where(PickList.outsource_id == o.id, PickList.status == "DRAFT")).all():
        pk.status = "CANCELLED"
    audit(db, user.id, "CANCEL", "OutsourceOrder", o.id)
    return o


def recall_outsource(db: Session, user: User, os_id: int, reason: str) -> OutsourceOrder:
    """撤回发出：已发出但外协厂尚无任何回厂记录的外协单退回“计划”状态。
    之后本厂供料的原料不再算已消耗，可退料；也可以取消或重新发出。"""
    if not (reason or "").strip():
        raise BizError("撤回发出必须填写原因")
    db.flush()
    acquire_mutex(db)
    o = db.scalar(select(OutsourceOrder).where(OutsourceOrder.id == os_id).with_for_update()
                  .execution_options(populate_existing=True))
    if o is None:
        raise BizError("外协单不存在", 404)
    if o.status != "SENT":
        raise BizError("只有“已发出”的外协单可以撤回")
    if D(o.arrived_qty) > 0:
        raise BizError("已有回厂记录，不能撤回发出")
    before = o.sent_date
    o.status, o.sent_date = "PLANNED", None
    audit(db, user.id, "RECALL", "OutsourceOrder", o.id, {"reason": reason, "sent_date": before})
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
