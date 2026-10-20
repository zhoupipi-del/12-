"""图纸、销售订单、齐套中心、领料、发货。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..core.deps import current_user, require
from ..core.errors import BizError
from ..db import get_db
from ..models import (ReallocationRequest, Drawing, DrawingVersion, KitLine, Material, OrderStatus, PickList, Reservation, Role,
                      SalesOrder, ShipmentPlan, User)
from ..services import drawings as dsvc
from ..services import kitting
from ..services import reallocation as ra
from ..services import orders as osvc
from ..services.common import get_or_404
from ..services.purchase import requisition_from_shortages
from . import schemas as S
from . import serial
from .util import commit_recalc, page

router = APIRouter()


# ---------------------------------------------------------------- 图纸
@router.get("/drawings")
def drawings(keyword: str = "", db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = select(Drawing)
    if keyword:
        q = q.where(or_(Drawing.drawing_no.like(f"%{keyword}%"), Drawing.name.like(f"%{keyword}%")))
    out = []
    for d in db.scalars(q.order_by(Drawing.drawing_no)).all():
        cur = next((v for v in d.versions if v.status == "RELEASED"), None)
        out.append({**serial.pick(d, "id", "drawing_no", "name", "material_id"),
                    "current_version": cur.version if cur else "", "current_version_id": cur.id if cur else None,
                    "released_at": cur.released_at if cur else None, "version_count": len(d.versions),
                    "has_draft": any(v.status == "DRAFT" for v in d.versions)})
    return out


@router.post("/drawings")
def create_drawing(body: S.DrawingIn, db: Session = Depends(get_db), u: User = Depends(require(Role.TECH))):
    if db.scalar(select(Drawing).where(Drawing.drawing_no == body.drawing_no)):
        raise BizError("图号已存在")
    d = Drawing(**body.model_dump())
    db.add(d)
    db.commit()
    return {"id": d.id}


@router.get("/drawings/{did}")
def drawing_detail(did: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    d = get_or_404(db, Drawing, did, "图纸")
    return {**serial.pick(d, "id", "drawing_no", "name", "material_id"),
            "versions": [{**serial.pick(v, "id", "version", "status", "change_note", "file_name", "file_path",
                                        "pdf_path", "released_at", "created_at"),
                          "uploader": serial.user_name(db, v.uploaded_by),
                          "releaser": serial.user_name(db, v.released_by)} for v in reversed(d.versions)],
            "affected": dsvc.affected_docs(db, d.drawing_no)}


@router.post("/drawings/{did}/versions")
def upload_version(did: int, version: str = Form(...), change_note: str = Form(""),
                   file: UploadFile | None = File(None), pdf: UploadFile | None = File(None),
                   db: Session = Depends(get_db), u: User = Depends(require(Role.TECH))):
    v = dsvc.upload_version(db, u, did, version, change_note, file, pdf)
    db.commit()
    return {"id": v.id}


@router.post("/drawings/versions/{vid}/release")
def release_version(vid: int, body: S.ReleaseIn | None = None, db: Session = Depends(get_db),
                    u: User = Depends(require(Role.TECH))):
    body = body or S.ReleaseIn()
    r = dsvc.release(db, u, vid, body.change_level, body.hold_reason)
    db.commit()
    return {"ok": True, "affected": r["affected"], "held": r["held"]}


@router.get("/drawings/{did}/bindings")
def drawing_bindings(did: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    """升版影响评估：哪些在制单据绑定了哪个版本。"""
    return dsvc.bindings_of(db, did)


@router.post("/drawings-switch")
def drawings_switch(body: S.SwitchIn, db: Session = Depends(get_db), u: User = Depends(require(Role.TECH))):
    r = dsvc.switch_binding(db, u, body.ref_type, body.ref_id, body.drawing_id, body.new_version_id, body.reason)
    db.commit()
    return r


@router.post("/drawings-resolve-hold")
def drawings_resolve_hold(body: S.ResolveHoldIn, db: Session = Depends(get_db), u: User = Depends(require(Role.TECH))):
    r = dsvc.resolve_hold(db, u, body.ref_type, body.ref_id, body.drawing_id, body.reason)
    db.commit()
    return r


@router.get("/files/{path:path}")
def download(path: str, _: User = Depends(current_user)):
    p = (settings.upload_dir / path).resolve()
    if settings.upload_dir.resolve() not in p.parents or not p.is_file():
        raise BizError("文件不存在", 404)
    return FileResponse(p)


@router.get("/drawings-required")
def drawings_required(ref_type: str, ref_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return dsvc.required_drawings(db, ref_type, ref_id)


@router.post("/drawings-confirm")
def drawings_confirm(body: S.ConfirmIn, db: Session = Depends(get_db),
                     u: User = Depends(require(Role.WORKSHOP, Role.TECH, Role.PLANNER))):
    n = dsvc.confirm(db, u, body.ref_type, body.ref_id, body.version_ids)
    db.commit()
    return {"confirmed": n}


@router.post("/drawings-change-no")
def drawings_change_no(body: S.DrawingNoChangeIn, db: Session = Depends(get_db), u: User = Depends(require(Role.TECH))):
    r = dsvc.change_drawing_no(db, u, body.material_id, body.new_drawing_no, body.reason)
    db.commit()
    return r


# ---------------------------------------------------------------- 销售订单
@router.get("/orders")
def orders(keyword: str = "", status: str = "", kit_color: str = "", page_no: int = 1, size: int = 50,
           db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = select(SalesOrder)
    if keyword:
        q = q.join(Material, Material.id == SalesOrder.product_id).where(or_(
            SalesOrder.order_no.like(f"%{keyword}%"), SalesOrder.customer.like(f"%{keyword}%"),
            Material.name.like(f"%{keyword}%")))
    if status:
        q = q.where(SalesOrder.status.in_(status.split(",")))
    if kit_color:
        q = q.where(SalesOrder.kit_color == kit_color)
    return page(db, q.order_by(SalesOrder.due_date, SalesOrder.id), page_no, size, serial.order)


@router.post("/orders")
def create_order(body: S.OrderIn, db: Session = Depends(get_db), u: User = Depends(require(Role.SALES, Role.PLANNER))):
    o = osvc.create_order(db, u, body.model_dump())
    commit_recalc(db)
    return serial.order(o)


@router.get("/orders/{oid}")
def order_detail(oid: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    o = get_or_404(db, SalesOrder, oid, "订单")
    lines = db.scalars(select(KitLine).where(KitLine.order_id == oid).order_by(KitLine.id)).all()
    res = db.scalars(select(Reservation).where(Reservation.order_id == oid)).all()
    picks = db.scalars(select(PickList).where(PickList.order_id == oid).order_by(PickList.id.desc())).all()
    ships = db.scalars(select(ShipmentPlan).where(ShipmentPlan.order_id == oid)).all()
    return {**serial.order(o), "kit_lines": [serial.kit_line(k) for k in lines],
            "reservations": [serial.reservation(db, r) for r in res],
            "drawings": dsvc.required_drawings(db, "SALES_ORDER", oid),
            "picks": [serial.pick_list(db, p) for p in picks], "shipments": [serial.shipment(db, s) for s in ships]}


@router.put("/orders/{oid}")
def update_order(oid: int, body: dict, db: Session = Depends(get_db),
                 u: User = Depends(require(Role.PLANNER, Role.SALES, Role.WORKSHOP))):
    if set(body) - {"progress", "station"} and not ({Role.PLANNER, Role.SALES, Role.ADMIN} & set(u.role_list)):
        raise BizError("车间只能更新进度和工位", 403)
    for k in ("due_date", "planned_start"):
        if body.get(k):
            body[k] = date.fromisoformat(body[k])
    o = osvc.update_order(db, u, oid, body)
    commit_recalc(db)
    return serial.order(o)


def _order_action(fn, *roles):
    def ep(oid: int, body: S.ReasonIn | None = None, db: Session = Depends(get_db), u: User = Depends(require(*roles))):
        args = (body.reason,) if fn in (osvc.cancel,) else ()
        o = fn(db, u, oid, *args)
        commit_recalc(db)
        return serial.order(o)
    ep.__name__ = f"order_{fn.__name__}"
    return ep


router.post("/orders/{oid}/release")(_order_action(osvc.release, Role.PLANNER))
router.post("/orders/{oid}/cancel")(_order_action(osvc.cancel, Role.PLANNER, Role.SALES))


@router.post("/orders/{oid}/start")
def start_order(oid: int, body: S.StartIn, db: Session = Depends(get_db),
                u: User = Depends(require(Role.WORKSHOP, Role.PLANNER, Role.MANAGER))):
    if body.override_reason and not ({Role.MANAGER, Role.ADMIN} & set(u.role_list)):
        raise BizError("未齐套强制开工需要生产主管操作", 403)
    o = osvc.start_assembly(db, u, oid, body.override_reason)
    commit_recalc(db)
    return serial.order(o)


@router.post("/orders/{oid}/complete")
def complete_order(oid: int, db: Session = Depends(get_db), u: User = Depends(require(Role.WORKSHOP, Role.WAREHOUSE))):
    o = osvc.complete(db, u, oid)
    commit_recalc(db)
    return serial.order(o)


@router.post("/orders/{oid}/lock")
def lock(oid: int, body: S.LockIn, db: Session = Depends(get_db), u: User = Depends(require(Role.PLANNER, Role.MANAGER))):
    osvc.lock_reservation(db, u, oid, body.material_id, body.qty, body.reason)
    commit_recalc(db)
    return {"ok": True}


@router.delete("/reservations/{rid}")
def unlock(rid: int, db: Session = Depends(get_db), u: User = Depends(require(Role.PLANNER, Role.MANAGER))):
    osvc.unlock_reservation(db, u, rid)
    commit_recalc(db)
    return {"ok": True}


@router.get("/orders/{oid}/simulate-top")
def simulate(oid: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    """模拟把该订单提为最高优先级，返回受影响订单（不落库）。"""
    return kitting.simulate_pin(db, oid)


# ---------------------------------------------------------------- 调料申请（动他单已冻结的料：先预演、再审批）
@router.post("/realloc/preview")
def realloc_preview(body: S.ReallocPreviewIn, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return ra.preview(db, body.to_order_id, body.material_id, body.qty, body.from_order_ids)


@router.post("/realloc")
def realloc_create(body: S.ReallocRequestIn, db: Session = Depends(get_db),
                   u: User = Depends(require(Role.PLANNER, Role.MANAGER))):
    r = ra.create_request(db, u, body.to_order_id, body.material_id, body.qty, body.reason, body.from_order_ids)
    db.commit()
    return serial.realloc(db, r)


@router.get("/realloc")
def realloc_list(status: str = "", db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = select(ReallocationRequest)
    if status:
        q = q.where(ReallocationRequest.status == status)
    return [serial.realloc(db, r) for r in db.scalars(q.order_by(ReallocationRequest.id.desc()).limit(200)).all()]


@router.post("/realloc/{rid}/approve")
def realloc_approve(rid: int, body: S.DecisionIn, db: Session = Depends(get_db),
                    u: User = Depends(require(Role.MANAGER))):
    try:
        r = ra.approve(db, u, rid, body.comment)
    except BizError:
        db.commit()  # 过期状态要落库
        raise
    db.commit()
    return serial.realloc(db, r)


@router.post("/realloc/{rid}/reject")
def realloc_reject(rid: int, body: S.DecisionIn, db: Session = Depends(get_db),
                   u: User = Depends(require(Role.MANAGER))):
    r = ra.reject(db, u, rid, body.comment)
    db.commit()
    return serial.realloc(db, r)


# ---------------------------------------------------------------- 齐套中心
@router.post("/kitting/recalc")
def recalc(db: Session = Depends(get_db), _: User = Depends(current_user)):
    r = kitting.recalc(db)
    return {"orders": len(r.orders), "shortage_materials": len(r.shortages)}


@router.get("/kitting/overview")
def overview(db: Session = Depends(get_db), _: User = Depends(current_user)):
    rows = db.scalars(select(SalesOrder).where(SalesOrder.status.in_(OrderStatus.ACTIVE_FOR_KIT))).all()
    lead = {m.id: m.lead_time_days for m in db.scalars(select(Material)).all()}
    rows = sorted(rows, key=lambda o: kitting.order_sort_key(o, kitting.order_need_date(o, lead)))
    strips: dict[int, list] = {}
    for k in db.scalars(select(KitLine).where(KitLine.level == 1).order_by(KitLine.id)).all():
        strips.setdefault(k.order_id, []).append({"level": 1, "name": k.material.name, "status": k.status,
                                                  "short_qty": k.short_qty, "expected_date": k.expected_date})
    items = []
    for seq, o in enumerate(rows, 1):
        d = serial.order(o)
        d["strip"] = strips.get(o.id, [])
        d["seq"] = seq
        d["need_date"] = kitting.order_need_date(o, lead)
        d["ready_in_2_days"] = bool(o.kit_ready_date and (o.kit_ready_date - date.today()).days <= settings.kit_buffer_days)
        items.append(d)
    summary = {c: sum(1 for i in items if i["kit_color"] == c) for c in ("GREEN", "YELLOW", "RED")}
    return {"buffer_days": settings.kit_buffer_days, "summary": summary, "items": items}


@router.get("/kitting/shortages")
def shortages(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return kitting.shortage_summary(db)


@router.post("/kitting/shortages/to-requisition")
def to_requisition(material_ids: list[int] | None = None, db: Session = Depends(get_db),
                   u: User = Depends(require(Role.PLANNER, Role.BUYER))):
    sh = kitting.shortage_summary(db)
    if material_ids:
        sh = [s for s in sh if s["material_id"] in material_ids]
    if not sh:
        raise BizError("当前没有缺料")
    r = requisition_from_shortages(db, u, sh)
    db.commit()
    return r


# ---------------------------------------------------------------- 领料
@router.get("/orders/{oid}/pick-plan")
def pick_plan(oid: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return osvc.pick_plan(db, get_or_404(db, SalesOrder, oid, "订单"))


@router.post("/picks")
def create_pick(body: S.PickIn, db: Session = Depends(get_db), u: User = Depends(require(Role.WAREHOUSE, Role.WORKSHOP))):
    p = osvc.create_pick(db, u, body.order_id, [x.model_dump() for x in body.lines] if body.lines else None,
                         body.warehouse_id, body.picker)
    db.commit()
    return serial.pick_list(db, p)


@router.put("/picks/{pid}")
def update_pick(pid: int, body: list[S.PickLineIn], db: Session = Depends(get_db),
                u: User = Depends(require(Role.WAREHOUSE, Role.WORKSHOP))):
    p = get_or_404(db, PickList, pid, "领料单")
    if p.status != "DRAFT":
        raise BizError("已过账")
    qty = {x.material_id: x.qty for x in body}
    for ln in p.lines:
        if ln.material_id in qty:
            ln.qty = qty[ln.material_id]
    db.commit()
    return serial.pick_list(db, p)


@router.post("/picks/{pid}/post")
def post_pick(pid: int, db: Session = Depends(get_db), u: User = Depends(require(Role.WAREHOUSE))):
    p = osvc.post_pick(db, u, pid)
    commit_recalc(db)
    return serial.pick_list(db, p)


@router.post("/picks/{pid}/return")
def return_pick(pid: int, body: S.ReturnPickIn, db: Session = Depends(get_db),
                u: User = Depends(require(Role.WAREHOUSE))):
    p = osvc.return_pick(db, u, pid, [x.model_dump() for x in body.lines], body.reason)
    commit_recalc(db)
    return serial.pick_list(db, p)


@router.get("/picks")
def picks(status: str = "", page_no: int = 1, size: int = 50, db: Session = Depends(get_db),
          _: User = Depends(current_user)):
    q = select(PickList)
    if status:
        q = q.where(PickList.status == status)
    return page(db, q.order_by(PickList.id.desc()), page_no, size, lambda p: serial.pick_list(db, p))


# ---------------------------------------------------------------- 发货
@router.get("/shipments")
def shipments(status: str = "", page_no: int = 1, size: int = 50, db: Session = Depends(get_db),
              _: User = Depends(current_user)):
    q = select(ShipmentPlan)
    if status:
        q = q.where(ShipmentPlan.status.in_(status.split(",")))
    return page(db, q.order_by(ShipmentPlan.plan_date.desc()), page_no, size, lambda s: serial.shipment(db, s))


@router.post("/shipments")
def create_shipment(body: S.ShipmentIn, db: Session = Depends(get_db), u: User = Depends(require(Role.SALES, Role.PLANNER))):
    s = osvc.create_shipment(db, u, body.model_dump())
    db.commit()
    return serial.shipment(db, s)


SHIP_ROLES = {"submit": (Role.SALES, Role.PLANNER), "approve": (Role.MANAGER,), "reject": (Role.MANAGER,),
              "ship": (Role.WAREHOUSE,), "cancel": (Role.SALES, Role.PLANNER)}


@router.post("/shipments/{sid}/{action}")
def shipment_action(sid: int, action: str, body: S.CommentIn | None = None, db: Session = Depends(get_db),
                    u: User = Depends(current_user)):
    roles = SHIP_ROLES.get(action)
    if roles is None:
        raise BizError("未知操作")
    if not (set(roles) | {Role.ADMIN}) & set(u.role_list):
        raise BizError("无权限执行该操作", 403)
    s = osvc.shipment_action(db, u, sid, action, body.comment if body else "")
    db.commit()
    return serial.shipment(db, s)


__all__ = ["router", "DrawingVersion"]
