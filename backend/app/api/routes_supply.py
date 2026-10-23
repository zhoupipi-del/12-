"""采购（请购 / 采购单行级锁定）、供应商门户、自制任务、外协、到货检验、来料异常。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..core.deps import current_user, require
from ..core.errors import BizError
from ..db import get_db
from ..models import (Arrival, IncomingException, Material, OutsourceOrder, PoLineRevision, PurchaseOrder,
                      PurchaseOrderLine, Requisition, RequisitionLine, Role, Supplier, User, WorkOrder)
from ..services import production as psvc
from ..services.kitting import acquire_mutex
from ..services import purchase as pu
from ..services import receiving as rc
from ..services.ai import classify_exception
from ..services.common import get_or_404
from . import schemas as S
from . import serial
from .util import commit_recalc, page

router = APIRouter()
BUY = (Role.BUYER, Role.PLANNER)


# ---------------------------------------------------------------- 请购计划单
@router.get("/requisitions")
def requisitions(status: str = "", page_no: int = 1, size: int = 50, db: Session = Depends(get_db),
                 _: User = Depends(current_user)):
    q = select(Requisition)
    if status:
        q = q.where(Requisition.status.in_(status.split(",")))
    return page(db, q.order_by(Requisition.id.desc()), page_no, size, lambda r: serial.req(db, r))


@router.post("/requisitions")
def create_req(body: S.ReqIn, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    r = pu.create_requisition(db, u, [x.model_dump() for x in body.lines], remark=body.remark)
    db.commit()
    return serial.req(db, r)


@router.get("/requisitions/{rid}")
def get_req(rid: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return serial.req(db, get_or_404(db, Requisition, rid, "请购单"))


@router.put("/requisition-lines/{lid}")
def edit_req_line(lid: int, body: dict, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    """计划单可直接改图号、供应商、数量、价格（转采购前）。"""
    ln = pu.update_requisition_line(db, u, lid, body)
    db.commit()
    return serial.req(db, ln.req)


@router.post("/requisitions/{rid}/lines")
def add_req_line(rid: int, body: S.ReqLineIn, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    r = get_or_404(db, Requisition, rid, "请购单")
    if r.status not in ("DRAFT", "CONFIRMED"):
        raise BizError("请购单已处理")
    s = pu.suggest(db, body.material_id, body.supplier_id)
    r.lines.append(RequisitionLine(material_id=body.material_id, qty=body.qty, need_date=body.need_date,
                                   drawing_no=body.drawing_no or s["drawing_no"], supplier_id=s["supplier_id"],
                                   price=body.price if body.price is not None else s["price"],
                                   buyer_id=body.buyer_id or s["buyer_id"], order_id=body.order_id, remark=body.remark))
    db.commit()
    return serial.req(db, r)


@router.delete("/requisition-lines/{lid}")
def del_req_line(lid: int, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    ln = get_or_404(db, RequisitionLine, lid, "请购行")
    r = ln.req
    if r.status not in ("DRAFT", "CONFIRMED"):
        raise BizError("请购单已处理")
    r.lines.remove(ln)
    db.commit()
    return serial.req(db, r)


@router.post("/requisitions/{rid}/convert")
def convert_req(rid: int, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    pos = pu.convert_requisition(db, u, rid)
    db.commit()
    return [serial.po(db, p, False) for p in pos]


@router.post("/requisitions/{rid}/cancel")
def cancel_req(rid: int, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    r = get_or_404(db, Requisition, rid, "请购单")
    if r.status == "CONVERTED":
        raise BizError("已转采购，不能取消")
    r.status = "CANCELLED"
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 采购订单
@router.get("/purchase-orders")
def pos(keyword: str = "", supplier_id: int | None = None, status: str = "", material_id: int | None = None,
        page_no: int = 1, size: int = 50, db: Session = Depends(get_db), _: User = Depends(current_user)):
    """多供应商订单快速查找：按供应商 / 物料 / 单号 / 状态。"""
    q = select(PurchaseOrder)
    if keyword:
        q = q.join(Supplier).where(or_(PurchaseOrder.po_no.like(f"%{keyword}%"), Supplier.name.like(f"%{keyword}%")))
    if supplier_id:
        q = q.where(PurchaseOrder.supplier_id == supplier_id)
    if status:
        q = q.where(PurchaseOrder.status.in_(status.split(",")))
    if material_id:
        q = q.where(PurchaseOrder.lines.any(PurchaseOrderLine.material_id == material_id))
    return page(db, q.order_by(PurchaseOrder.id.desc()), page_no, size, lambda p: serial.po(db, p, False))


@router.get("/po-lines")
def po_lines(open_only: bool = True, material_id: int | None = None, supplier_id: int | None = None,
             db: Session = Depends(get_db), _: User = Depends(current_user)):
    """采购跟单视图：所有未到货行，按需求日排序。"""
    q = select(PurchaseOrderLine).join(PurchaseOrder)
    if open_only:
        q = q.where(PurchaseOrder.status == "APPROVED", PurchaseOrderLine.status.in_(["OPEN", "PARTIAL"]))
    if material_id:
        q = q.where(PurchaseOrderLine.material_id == material_id)
    if supplier_id:
        q = q.where(PurchaseOrder.supplier_id == supplier_id)
    rows = db.scalars(q.order_by(PurchaseOrderLine.need_date).limit(500)).all()
    return [{**serial.po_line(x), "po_no": x.po.po_no, "supplier": x.po.supplier.name,
             "buyer": serial.user_name(db, x.po.buyer_id)} for x in rows]


@router.post("/purchase-orders")
def create_po(body: S.PoIn, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    p = pu.create_po(db, u, body.supplier_id, [x.model_dump() for x in body.lines], body.remark, body.buyer_id)
    db.commit()
    return serial.po(db, p)


@router.get("/purchase-orders/{pid}")
def get_po(pid: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    p = get_or_404(db, PurchaseOrder, pid, "采购单")
    d = serial.po(db, p)
    revs = db.scalars(select(PoLineRevision).where(PoLineRevision.po_line_id.in_([x.id for x in p.lines]))
                      .order_by(PoLineRevision.id.desc())).all()
    ln_no = {x.id: x.line_no for x in p.lines}
    d["revisions"] = [{**serial.pick(r, "id", "po_line_id", "action", "before", "after", "reason", "created_at"),
                       "line_no": ln_no.get(r.po_line_id), "user": serial.user_name(db, r.user_id) or "供应商"}
                      for r in revs]
    return d


@router.post("/purchase-orders/{pid}/approve")
def approve_po(pid: int, db: Session = Depends(get_db), u: User = Depends(require(Role.BUYER, Role.MANAGER))):
    p = pu.approve_po(db, u, pid)
    commit_recalc(db)
    return serial.po(db, p)


@router.post("/purchase-orders/{pid}/lock")
def edit_lock(pid: int, force: bool = False, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    p = get_or_404(db, PurchaseOrder, pid, "采购单")
    if force and Role.MANAGER not in u.role_list and Role.ADMIN not in u.role_list:
        raise BizError("强制解锁需要主管权限", 403)
    pu.acquire_edit_lock(db, u, p, force)
    db.commit()
    return {"locked_until": p.edit_lock_until, "version": p.version}


@router.delete("/purchase-orders/{pid}/lock")
def edit_unlock(pid: int, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    pu.release_edit_lock(db, u, get_or_404(db, PurchaseOrder, pid, "采购单"))
    db.commit()
    return {"ok": True}


@router.post("/purchase-orders/{pid}/lines")
def po_add_line(pid: int, body: S.PoLineIn, version: int | None = None, db: Session = Depends(get_db),
                u: User = Depends(require(*BUY))):
    pu.add_line(db, u, pid, body.model_dump(), version)
    commit_recalc(db)
    return serial.po(db, get_or_404(db, PurchaseOrder, pid, "采购单"))


@router.put("/po-lines/{lid}")
def po_edit_line(lid: int, body: S.PoLineEditIn, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    ch = dict(body.changes)
    from datetime import date
    for k in ("need_date", "promised_date"):
        if ch.get(k):
            ch[k] = date.fromisoformat(ch[k])
    ln = pu.edit_line(db, u, lid, ch, body.reason, body.version)
    commit_recalc(db)
    return serial.po(db, ln.po)


@router.get("/po-lines/{lid}/impact")
def po_line_impact(lid: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return pu.line_impact(db, lid)


@router.post("/po-lines/{lid}/reverse")
def po_reverse_line(lid: int, body: S.VersionReasonIn, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    pu.reverse_line(db, u, lid, body.reason, body.version)
    commit_recalc(db)
    return serial.po(db, get_or_404(db, PurchaseOrderLine, lid, "采购行").po)


@router.post("/po-lines/{lid}/close")
def po_close_line(lid: int, body: S.VersionReasonIn, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    ln = pu.close_remaining(db, u, lid, body.reason, body.version)
    commit_recalc(db)
    return serial.po(db, ln.po)


@router.delete("/po-lines/{lid}")
def po_delete_line(lid: int, version: int | None = None, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    po_id = get_or_404(db, PurchaseOrderLine, lid, "采购行").po_id
    pu.delete_line(db, u, lid, version)
    commit_recalc(db)
    return serial.po(db, get_or_404(db, PurchaseOrder, po_id, "采购单"))


# ---------------------------------------------------------------- 供应商门户（免登录，凭链接令牌）
def _portal_supplier(db: Session, token: str) -> Supplier:
    s = db.scalar(select(Supplier).where(Supplier.portal_token == token, Supplier.is_active.is_(True)))
    if not s or not token:
        raise BizError("链接无效或已失效，请联系采购员", 404)
    return s


@router.get("/portal/{token}")
def portal(token: str, db: Session = Depends(get_db)):
    s = _portal_supplier(db, token)
    return {"supplier": s.name,
            "lines": [{**serial.pick(x, "id", "line_no", "drawing_no", "qty", "need_date", "promised_date",
                                     "arrived_qty", "status", "supplier_note"),
                       "po_no": x.po.po_no, "order_date": x.po.order_date, "code": x.material.code,
                       "name": x.material.name, "spec": x.material.spec, "unit": x.material.unit,
                       "open_qty": x.open_qty} for x in pu.portal_lines(db, s)]}


@router.post("/portal/{token}/lines/{lid}/confirm")
def portal_confirm(token: str, lid: int, body: S.PortalConfirmIn, db: Session = Depends(get_db)):
    s = _portal_supplier(db, token)
    acquire_mutex(db)  # 令牌有效后才拿全局锁（免登录入口，无效令牌不应排队）
    pu.supplier_confirm(db, s, lid, body.promised_date, body.note)
    commit_recalc(db)
    return {"ok": True}


# ---------------------------------------------------------------- 自制任务
@router.get("/work-orders")
def work_orders(status: str = "", keyword: str = "", bom_pending: bool = False, page_no: int = 1, size: int = 50,
                db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = select(WorkOrder).join(Material)
    if bom_pending:  # 待技术确认 BOM 的单据
        q = q.where(WorkOrder.bom_snapshot.is_(None), WorkOrder.status.in_(["PLANNED", "STARTED"]))
    if status:
        q = q.where(WorkOrder.status.in_(status.split(",")))
    if keyword:
        q = q.where(or_(WorkOrder.wo_no.like(f"%{keyword}%"), Material.name.like(f"%{keyword}%"),
                        Material.code.like(f"%{keyword}%")))
    return page(db, q.order_by(WorkOrder.planned_finish, WorkOrder.id), page_no, size, serial.wo)


@router.post("/work-orders")
def create_wo(body: S.WorkOrderIn, db: Session = Depends(get_db), u: User = Depends(require(Role.PLANNER, Role.WORKSHOP))):
    w = psvc.create_wo(db, u, body.model_dump())
    commit_recalc(db)
    return serial.wo(w)


@router.put("/work-orders/{wid}")
def update_wo(wid: int, body: dict, db: Session = Depends(get_db), u: User = Depends(require(Role.PLANNER, Role.WORKSHOP))):
    from datetime import date
    for k in ("planned_start", "planned_finish"):
        if body.get(k):
            body[k] = date.fromisoformat(body[k])
    w = psvc.update_wo(db, u, wid, body)
    commit_recalc(db)
    return serial.wo(w)


@router.post("/work-orders/{wid}/start")
def start_wo(wid: int, db: Session = Depends(get_db), u: User = Depends(require(Role.WORKSHOP, Role.PLANNER))):
    w = psvc.start_wo(db, u, wid)
    db.commit()
    return serial.wo(w)


@router.post("/work-orders/{wid}/report")
def report_wo(wid: int, body: S.QtyIn, db: Session = Depends(get_db), u: User = Depends(require(Role.WORKSHOP))):
    w = psvc.report_wo(db, u, wid, body.qty, body.warehouse_id)
    commit_recalc(db)
    return serial.wo(w)


@router.post("/work-orders/{wid}/pick")
def wo_pick(wid: int, db: Session = Depends(get_db), u: User = Depends(require(Role.WAREHOUSE, Role.WORKSHOP))):
    from ..services.common import get_or_404 as _g  # noqa: F401
    p = psvc.create_wo_pick(db, u, wid)
    db.commit()
    return serial.pick_list(db, p)


@router.post("/outsource/{oid}/pick")
def os_pick(oid: int, db: Session = Depends(get_db), u: User = Depends(require(Role.WAREHOUSE, *BUY))):
    p = psvc.create_outsource_pick(db, u, oid)
    db.commit()
    return serial.pick_list(db, p)


@router.get("/work-orders/{wid}/bom")
def wo_bom(wid: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return psvc.bom_candidates(db, get_or_404(db, WorkOrder, wid, "自制任务"))


@router.post("/work-orders/{wid}/bom-confirm")
def wo_bom_confirm(wid: int, body: S.BomConfirmIn, db: Session = Depends(get_db), u: User = Depends(require(Role.TECH))):
    r = psvc.confirm_bom(db, u, "WORK_ORDER", wid, bom_id=body.bom_id, use_order_bom=body.use_order_bom, reason=body.reason)
    commit_recalc(db)
    return {**serial.wo(r["doc"]), "warnings": r["warnings"]}


@router.post("/work-orders/{wid}/cancel")
def cancel_wo(wid: int, db: Session = Depends(get_db), u: User = Depends(require(Role.PLANNER))):
    w = psvc.cancel_wo(db, u, wid)
    commit_recalc(db)
    return serial.wo(w)


# ---------------------------------------------------------------- 外协
@router.get("/outsource")
def outsource_list(status: str = "", bom_pending: bool = False, page_no: int = 1, size: int = 50,
                   db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = select(OutsourceOrder)
    if bom_pending:
        q = q.where(OutsourceOrder.bom_snapshot.is_(None), OutsourceOrder.supply_mode == "CONSIGNED",
                    OutsourceOrder.status.in_(["PLANNED", "SENT", "PARTIAL"]))
    if status:
        q = q.where(OutsourceOrder.status.in_(status.split(",")))
    return page(db, q.order_by(OutsourceOrder.id.desc()), page_no, size, serial.outsource)


@router.post("/outsource")
def create_os(body: S.OutsourceIn, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    o = psvc.create_outsource(db, u, body.model_dump())
    commit_recalc(db)
    return serial.outsource(o)


@router.put("/outsource/{oid}")
def update_os(oid: int, body: dict, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    from datetime import date
    if body.get("expected_return"):
        body["expected_return"] = date.fromisoformat(body["expected_return"])
    o = psvc.update_outsource(db, u, oid, body)
    commit_recalc(db)
    return serial.outsource(o)


@router.post("/outsource/{oid}/send")
def send_os(oid: int, db: Session = Depends(get_db), u: User = Depends(require(*BUY, Role.WAREHOUSE))):
    o = psvc.send_outsource(db, u, oid)
    commit_recalc(db)
    return serial.outsource(o)


@router.get("/outsource/{oid}/bom")
def os_bom(oid: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return psvc.bom_candidates(db, get_or_404(db, OutsourceOrder, oid, "外协单"))


@router.post("/outsource/{oid}/bom-confirm")
def os_bom_confirm(oid: int, body: S.BomConfirmIn, db: Session = Depends(get_db), u: User = Depends(require(Role.TECH))):
    r = psvc.confirm_bom(db, u, "OUTSOURCE", oid, bom_id=body.bom_id, use_order_bom=body.use_order_bom, reason=body.reason)
    commit_recalc(db)
    return {**serial.outsource(r["doc"]), "warnings": r["warnings"]}


@router.post("/outsource/{oid}/recall")
def recall_os(oid: int, body: S.ReasonIn, db: Session = Depends(get_db), u: User = Depends(require(*BUY, Role.MANAGER))):
    o = psvc.recall_outsource(db, u, oid, body.reason)
    commit_recalc(db)
    return serial.outsource(o)


@router.post("/outsource/{oid}/cancel")
def cancel_os(oid: int, db: Session = Depends(get_db), u: User = Depends(require(*BUY))):
    o = psvc.cancel_outsource(db, u, oid)
    commit_recalc(db)
    return serial.outsource(o)


# ---------------------------------------------------------------- 到货 / 检验
@router.get("/arrivals")
def arrivals(status: str = "", page_no: int = 1, size: int = 50, db: Session = Depends(get_db),
             _: User = Depends(current_user)):
    q = select(Arrival)
    if status:
        q = q.where(Arrival.status.in_(status.split(",")))
    return page(db, q.order_by(Arrival.id.desc()), page_no, size, lambda a: serial.arrival(db, a))


@router.get("/arrival-sources")
def arrival_sources(keyword: str = "", db: Session = Depends(get_db), _: User = Depends(current_user)):
    """可登记到货的来源：已审核采购行 + 已发出外协单。"""
    out = []
    q = select(PurchaseOrderLine).join(PurchaseOrder).join(Supplier).join(Material, Material.id == PurchaseOrderLine.material_id).where(
        PurchaseOrder.status == "APPROVED", PurchaseOrderLine.status.in_(["OPEN", "PARTIAL"]))
    if keyword:
        q = q.where(or_(PurchaseOrder.po_no.like(f"%{keyword}%"), Supplier.name.like(f"%{keyword}%"),
                        Material.code.like(f"%{keyword}%"), Material.name.like(f"%{keyword}%")))
    for x in db.scalars(q.limit(200)).all():
        out.append({"source_type": "PO", "source_id": x.id, "no": f"{x.po.po_no}-{x.line_no}",
                    "supplier": x.po.supplier.name, "code": x.material.code, "name": x.material.name,
                    "open_qty": x.open_qty, "date": x.promised_date or x.need_date})
    for x in db.scalars(select(OutsourceOrder).where(OutsourceOrder.status.in_(["SENT", "PARTIAL"]))).all():
        if keyword and keyword not in x.os_no + x.supplier.name + x.material.code + x.material.name:
            continue
        out.append({"source_type": "OUTSOURCE", "source_id": x.id, "no": x.os_no, "supplier": x.supplier.name,
                    "code": x.material.code, "name": x.material.name, "open_qty": x.open_qty,
                    "date": x.expected_return})
    return out


@router.post("/arrivals")
def register_arrival(body: S.ArrivalIn, db: Session = Depends(get_db), u: User = Depends(require(Role.WAREHOUSE)),
                     idempotency_key: str | None = Header(default=None, max_length=64)):
    # 幂等键：请求体 client_token 或标准请求头 Idempotency-Key，重复提交返回第一次的结果，不重复登记
    a, exc = rc.register_arrival(db, u, body.source_type, body.source_id, body.qty, body.expected_qty, body.note,
                                  body.client_token or idempotency_key)
    commit_recalc(db)
    return {**serial.arrival(db, a), "exception_no": exc.exc_no if exc else None}


@router.post("/arrivals/{aid}/inspect")
def inspect(aid: int, body: S.InspectIn, db: Session = Depends(get_db), u: User = Depends(require(Role.QUALITY, Role.WAREHOUSE))):
    a, exc = rc.inspect(db, u, aid, body.qualified, body.rejected, body.warehouse_id, body.location_id,
                        body.lot_no, body.note, body.reject_reason)
    commit_recalc(db)
    return {**serial.arrival(db, a), "exception_no": exc.exc_no if exc else None}


# ---------------------------------------------------------------- 来料异常
@router.get("/exceptions")
def exceptions(status: str = "", category: str = "", keyword: str = "", page_no: int = 1, size: int = 50,
               db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = select(IncomingException)
    if status:
        q = q.where(IncomingException.status.in_(status.split(",")))
    if category:
        q = q.where(IncomingException.category == category)
    if keyword:
        q = q.join(Material, Material.id == IncomingException.material_id).where(or_(
            IncomingException.exc_no.like(f"%{keyword}%"), Material.name.like(f"%{keyword}%"),
            IncomingException.description.like(f"%{keyword}%")))
    return page(db, q.order_by(IncomingException.id.desc()), page_no, size, lambda e: serial.exception(db, e))


@router.get("/exceptions/{eid}")
def get_exception(eid: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return serial.exception(db, get_or_404(db, IncomingException, eid, "异常单"), logs=True)


@router.post("/exceptions")
def create_exception(body: S.ExceptionIn, db: Session = Depends(get_db), u: User = Depends(current_user)):
    e = rc.create_exception(db, u, body.model_dump())
    db.commit()
    return serial.exception(db, e, logs=True)


@router.post("/exceptions/classify")
def classify(body: S.TextIn, db: Session = Depends(get_db), u: User = Depends(current_user)):
    """AI/规则 辅助判断异常类别与责任方（仅建议，不自动提交）。"""
    r = classify_exception(db, u.id, body.text)
    db.commit()
    return r


def _exc_guard(u: User, action: str) -> None:
    roles = rc.EXC_ROLES.get(action)
    if roles and not (set(roles) | {Role.ADMIN}) & set(u.role_list):
        raise BizError("无权限执行该操作", 403)


@router.post("/exceptions/{eid}/submit")
def exc_submit(eid: int, body: S.CommentIn, db: Session = Depends(get_db), u: User = Depends(current_user)):
    e = rc.submit(db, u, eid, body.comment)
    db.commit()
    return serial.exception(db, e, logs=True)


@router.post("/exceptions/{eid}/reject")
def exc_reject(eid: int, body: S.CommentIn, db: Session = Depends(get_db), u: User = Depends(current_user)):
    _exc_guard(u, "reject")
    e = rc.reject(db, u, eid, body.comment)
    db.commit()
    return serial.exception(db, e, logs=True)


@router.post("/exceptions/{eid}/dispose")
def exc_dispose(eid: int, body: S.DisposeIn, db: Session = Depends(get_db), u: User = Depends(current_user)):
    _exc_guard(u, "dispose")
    e = rc.dispose(db, u, eid, body.disposition, body.responsibility, body.comment, body.owner_id, body.deadline)
    commit_recalc(db)
    return serial.exception(db, e, logs=True)


@router.post("/exceptions/{eid}/reinspect")
def exc_reinspect(eid: int, body: S.ReinspectIn, db: Session = Depends(get_db), u: User = Depends(current_user)):
    _exc_guard(u, "reinspect")
    e = rc.reinspect(db, u, eid, body.qualified, body.warehouse_id, body.comment)
    commit_recalc(db)
    return serial.exception(db, e, logs=True)


@router.post("/exceptions/{eid}/close")
def exc_close(eid: int, body: S.CloseIn, db: Session = Depends(get_db), u: User = Depends(current_user)):
    _exc_guard(u, "close")
    e = rc.close(db, u, eid, body.result)
    db.commit()
    return serial.exception(db, e, logs=True)
