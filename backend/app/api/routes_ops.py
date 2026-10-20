"""库存、看板、预警、系统设置（开放接口密钥 / AI / 审计）、工作台、开放接口 v1。"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core.deps import api_key_auth, current_user, require
from ..core.errors import BizError
from ..core.security import decode_token, new_api_key
from ..db import get_db
from ..models import (Alert, ApiKey, Arrival, ShipmentPlan, AssemblyNote, AuditLog, IncomingException, Material, OrderStatus,
                      PurchaseOrder, PurchaseOrderLine, Role, SalesOrder, StockLot, StockTxn, TxnType, User,
                      Warehouse, WeeklyPlanItem, AiCallLog)
from ..services import ai as aisvc
from ..services import importer
from ..services import alerts as alsvc
from ..services import board as bsvc
from ..services import inventory as inv
from ..services import kitting
from ..services import orders as osvc
from ..services.common import audit, get_or_404
from . import schemas as S
from . import serial
from .util import commit_recalc, page

router = APIRouter()


# ---------------------------------------------------------------- 库存
@router.get("/stock/onhand")
def onhand(purpose: str = "", keyword: str = "", db: Session = Depends(get_db), _: User = Depends(current_user)):
    return inv.onhand_summary(db, purpose or None, keyword)


@router.get("/stock/lots")
def lots(material_id: int | None = None, warehouse_id: int | None = None, db: Session = Depends(get_db),
         _: User = Depends(current_user)):
    q = select(StockLot).where(StockLot.qty > 0)
    if material_id:
        q = q.where(StockLot.material_id == material_id)
    if warehouse_id:
        q = q.where(StockLot.warehouse_id == warehouse_id)
    return [{**serial.pick(x, "id", "material_id", "warehouse_id", "location_id", "lot_no", "qty", "unit_cost",
                           "received_at", "last_move_at", "source_type"),
             "code": x.material.code, "name": x.material.name, "warehouse": x.warehouse.name,
             "location": x.location.code if x.location else ""}
            for x in db.scalars(q.order_by(StockLot.received_at).limit(1000)).all()]


@router.get("/stock/txns")
def txns(material_id: int | None = None, txn_type: str = "", warehouse_id: int | None = None,
         date_from: date | None = None, date_to: date | None = None, page_no: int = 1, size: int = 100,
         db: Session = Depends(get_db), _: User = Depends(current_user)):
    """出入库明细（实时）。"""
    q = select(StockTxn)
    if material_id:
        q = q.where(StockTxn.material_id == material_id)
    if txn_type:
        q = q.where(StockTxn.txn_type.in_(txn_type.split(",")))
    if warehouse_id:
        q = q.where(StockTxn.warehouse_id == warehouse_id)
    if date_from:
        q = q.where(StockTxn.created_at >= date_from)
    if date_to:
        q = q.where(StockTxn.created_at < date_to + timedelta(days=1))

    def conv(t: StockTxn) -> dict:
        return {**serial.pick(t, "id", "txn_type", "material_id", "warehouse_id", "lot_id", "qty", "ref_type",
                              "ref_id", "order_id", "customer", "remark", "created_at"),
                "type_label": TxnType.LABELS.get(t.txn_type, t.txn_type), "code": t.material.code,
                "name": t.material.name, "warehouse": t.warehouse.name, "user": serial.user_name(db, t.user_id),
                "order_no": db.get(SalesOrder, t.order_id).order_no if t.order_id else ""}
    return page(db, q.order_by(StockTxn.id.desc()), page_no, size, conv)


@router.post("/stock/transfer")
def transfer(body: S.TransferIn, db: Session = Depends(get_db), u: User = Depends(require(Role.WAREHOUSE))):
    inv.transfer(db, material_id=body.material_id, from_wh=body.from_warehouse_id, to_wh=body.to_warehouse_id,
                 qty=body.qty, to_location_id=body.to_location_id, user_id=u.id, remark=body.remark)
    commit_recalc(db)
    return {"ok": True}


@router.post("/stock/spare-out")
def spare_out(body: S.SpareOutIn, db: Session = Depends(get_db),
              u: User = Depends(require(Role.WAREHOUSE, Role.AFTERSALES))):
    used = inv.spare_out(db, material_id=body.material_id, warehouse_id=body.warehouse_id, qty=body.qty,
                         kind=body.kind, customer=body.customer, user_id=u.id, remark=body.remark)
    db.commit()
    alsvc.scan_stock(db)
    db.commit()
    return {"lots": used}


@router.post("/stock/adjust")
def adjust(body: S.AdjustIn, db: Session = Depends(get_db), u: User = Depends(require(Role.WAREHOUSE))):
    inv.adjust(db, lot_id=body.lot_id, new_qty=body.new_qty, user_id=u.id, remark=body.remark)
    commit_recalc(db)
    return {"ok": True}


@router.get("/stock/spare-report")
def spare_report(date_from: date | None = None, date_to: date | None = None, db: Session = Depends(get_db),
                 _: User = Depends(current_user)):
    """售后备件：销售 vs 维修领用 分开统计。"""
    q = select(StockTxn.material_id, StockTxn.txn_type, func.sum(StockTxn.qty)).where(
        StockTxn.txn_type.in_([TxnType.SPARE_SALE_OUT, TxnType.SPARE_USE_OUT]))
    if date_from:
        q = q.where(StockTxn.created_at >= date_from)
    if date_to:
        q = q.where(StockTxn.created_at < date_to + timedelta(days=1))
    agg: dict[int, dict] = {}
    for mid, t, s in db.execute(q.group_by(StockTxn.material_id, StockTxn.txn_type)).all():
        m = db.get(Material, mid)
        row = agg.setdefault(mid, {"material_id": mid, "code": m.code, "name": m.name, "sale": 0, "use": 0})
        row["sale" if t == TxnType.SPARE_SALE_OUT else "use"] = -float(s)
    return list(agg.values())


# ---------------------------------------------------------------- 预警
@router.get("/alerts")
def alerts(status: str = "OPEN,ACK", type: str = "", role: str = "", page_no: int = 1, size: int = 100,
           db: Session = Depends(get_db), u: User = Depends(current_user)):
    q = select(Alert)
    if status:
        q = q.where(Alert.status.in_(status.split(",")))
    if type:
        q = q.where(Alert.type.in_(type.split(",")))
    if role == "mine" and Role.ADMIN not in u.role_list:
        q = q.where(or_(Alert.target_role.in_(u.role_list), Alert.target_role == ""))
    lvl = {"CRITICAL": 0, "WARN": 1, "INFO": 2}
    res = page(db, q.order_by(Alert.id.desc()), page_no, size,
               lambda a: serial.pick(a, "id", "type", "level", "title", "detail", "target_role", "ref_type",
                                     "ref_id", "status", "created_at"))
    res["items"].sort(key=lambda a: (lvl.get(a["level"], 3), -a["id"]))
    return res


@router.post("/alerts/{aid}/{action}")
def alert_action(aid: int, action: str, db: Session = Depends(get_db), u: User = Depends(current_user)):
    a = get_or_404(db, Alert, aid, "预警")
    if action == "ack":
        a.status = "ACK"
    elif action == "resolve":
        a.status = "RESOLVED"
    else:
        raise BizError("未知操作")
    db.commit()
    return {"ok": True}


@router.post("/alerts-scan")
def alerts_scan(db: Session = Depends(get_db), _: User = Depends(current_user)):
    kitting.recalc(db)
    return alsvc.scan_all(db)


# ---------------------------------------------------------------- 工作台
@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), u: User = Depends(current_user)):
    today = date.today()
    active = db.scalars(select(SalesOrder).where(SalesOrder.status.in_(OrderStatus.ACTIVE_FOR_KIT))).all()
    cnt = lambda q: db.scalar(select(func.count()).select_from(q.subquery()))  # noqa: E731
    return {
        "orders": {"active": len(active), "assembling": sum(1 for o in active if o.status == OrderStatus.ASSEMBLING),
                   "green": sum(1 for o in active if o.kit_color == "GREEN"),
                   "yellow": sum(1 for o in active if o.kit_color == "YELLOW"),
                   "red": sum(1 for o in active if o.kit_color == "RED"),
                   "overdue": sum(1 for o in active if o.due_date < today),
                   "due_7d": sum(1 for o in active if today <= o.due_date <= today + timedelta(days=7))},
        "todo": {
            "pending_inspection": cnt(select(Arrival).where(Arrival.status == "PENDING")),
            "exceptions_open": cnt(select(IncomingException).where(IncomingException.status.notin_(["CLOSED"]))),
            "po_draft": cnt(select(PurchaseOrder).where(PurchaseOrder.status == "DRAFT")),
            "po_lines_unconfirmed": cnt(select(PurchaseOrderLine).join(PurchaseOrder).where(
                PurchaseOrder.status == "APPROVED", PurchaseOrderLine.status.in_(["OPEN", "PARTIAL"]),
                PurchaseOrderLine.promised_date.is_(None))),
            "shipments_pending": cnt(select(ShipmentPlan).where(ShipmentPlan.status == "SUBMITTED")),
            "alerts_critical": cnt(select(Alert).where(Alert.status == "OPEN", Alert.level == "CRITICAL")),
        },
        "shortages": kitting.shortage_summary(db)[:10],
    }


# ---------------------------------------------------------------- 看板
def _board_auth(db: Session, key: str | None, authorization: str | None) -> bool:
    """看板大屏可用登录令牌，也可用看板密钥（车间电视免登录）。返回是否为登录用户。"""
    if authorization and authorization.lower().startswith("bearer ") and decode_token(authorization[7:]):
        return True
    if key and key == bsvc.get_setting(db, "board_key"):
        return False
    raise HTTPException(401, "看板需要登录或有效的看板密钥")


@router.get("/board/workshop")
def board_workshop(key: str | None = None, mode: str | None = None, authorization: str | None = Header(None),
                   db: Session = Depends(get_db)):
    logged_in = _board_auth(db, key, authorization)
    # 只有登录用户可以预览别的模式；凭密钥访问的大屏永远按后台配置的模式出数据，
    # 展示模式下由后端只返回允许展示的数据，不能靠改参数绕开
    force = mode if (logged_in and mode in bsvc.MODES) else None
    return bsvc.workshop(db, force_mode=force)


@router.get("/board/weekly")
def board_weekly(key: str | None = None, week: date | None = None, authorization: str | None = Header(None),
                 db: Session = Depends(get_db)):
    logged_in = _board_auth(db, key, authorization)
    if not logged_in and bsvc.get_setting(db, "board_mode") == "SHOWCASE":
        raise HTTPException(403, "当前为客户展示模式，周计划仅登录用户可看")
    return bsvc.weekly(db, week)


@router.get("/board/settings")
def board_settings(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return {k: bsvc.get_setting(db, k) for k in list(bsvc.DEFAULTS) + ["board_key"]}


@router.put("/board/settings")
def save_board_settings(body: S.BoardSettingIn, db: Session = Depends(get_db),
                        u: User = Depends(require(Role.MANAGER, Role.PLANNER, Role.WORKSHOP))):
    """切换看板模式（车间模式 / 展示模式），大屏下次刷新即生效。"""
    for k, v in body.model_dump(exclude_none=True).items():
        if k == "board_mode" and v not in bsvc.MODES:
            raise BizError("模式无效")
        bsvc.set_setting(db, k, v)
    audit(db, u.id, "BOARD_SETTING", "Setting", None, body.model_dump(exclude_none=True))
    db.commit()
    return {"ok": True}


@router.post("/board/key")
def reset_board_key(db: Session = Depends(get_db), u: User = Depends(require(Role.ADMIN))):
    from ..core.security import new_portal_token
    k = new_portal_token()
    bsvc.set_setting(db, "board_key", k)
    db.commit()
    return {"board_key": k}


@router.get("/weekly-items")
def weekly_items(week: date | None = None, db: Session = Depends(get_db), _: User = Depends(current_user)):
    ws = bsvc.week_start(week or date.today())
    return [serial.pick(x, "id", "week_start", "department", "content", "owner", "due_date", "status", "order_id")
            for x in db.scalars(select(WeeklyPlanItem).where(WeeklyPlanItem.week_start == ws)
                                .order_by(WeeklyPlanItem.department, WeeklyPlanItem.id)).all()]


@router.post("/weekly-items")
def add_weekly(body: S.WeeklyItemIn, db: Session = Depends(get_db), u: User = Depends(current_user)):
    d = body.model_dump()
    d["week_start"] = bsvc.week_start(d["week_start"] or date.today())
    x = WeeklyPlanItem(**d)
    db.add(x)
    db.commit()
    return {"id": x.id}


@router.put("/weekly-items/{iid}")
def edit_weekly(iid: int, body: S.WeeklyItemIn, db: Session = Depends(get_db), u: User = Depends(current_user)):
    x = get_or_404(db, WeeklyPlanItem, iid, "周计划")
    d = body.model_dump()
    d["week_start"] = bsvc.week_start(d["week_start"] or x.week_start)
    for k, v in d.items():
        setattr(x, k, v)
    db.commit()
    return {"id": x.id}


@router.delete("/weekly-items/{iid}")
def del_weekly(iid: int, db: Session = Depends(get_db), u: User = Depends(current_user)):
    db.delete(get_or_404(db, WeeklyPlanItem, iid, "周计划"))
    db.commit()
    return {"ok": True}


@router.get("/assembly-notes")
def notes(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [{**serial.pick(n, "id", "content", "level", "order_id", "is_active", "created_at"),
             "order_no": db.get(SalesOrder, n.order_id).order_no if n.order_id else ""}
            for n in db.scalars(select(AssemblyNote).order_by(AssemblyNote.id.desc())).all()]


@router.post("/assembly-notes")
def add_note(body: S.NoteIn, db: Session = Depends(get_db), u: User = Depends(require(Role.WORKSHOP, Role.TECH, Role.PLANNER))):
    n = AssemblyNote(**body.model_dump())
    db.add(n)
    db.commit()
    return {"id": n.id}


@router.put("/assembly-notes/{nid}")
def edit_note(nid: int, body: S.NoteIn, db: Session = Depends(get_db), u: User = Depends(require(Role.WORKSHOP, Role.TECH, Role.PLANNER))):
    n = get_or_404(db, AssemblyNote, nid, "注意事项")
    for k, v in body.model_dump().items():
        setattr(n, k, v)
    db.commit()
    return {"id": n.id}


# ---------------------------------------------------------------- 系统：开放接口密钥 / AI / 审计
@router.get("/api-keys")
def api_keys(db: Session = Depends(get_db), _: User = Depends(require(Role.ADMIN))):
    return [serial.pick(k, "id", "name", "key_prefix", "scopes", "is_active", "last_used_at", "created_at")
            for k in db.scalars(select(ApiKey).order_by(ApiKey.id)).all()]


@router.post("/api-keys")
def create_api_key(body: S.ApiKeyIn, db: Session = Depends(get_db), u: User = Depends(require(Role.ADMIN))):
    raw, prefix, h = new_api_key()
    scopes = [s for s in body.scopes if s in ("read", "write")] or ["read"]
    k = ApiKey(name=body.name, key_prefix=prefix, key_hash=h, scopes=",".join(scopes))
    db.add(k)
    audit(db, u.id, "CREATE", "ApiKey", None, body.name)
    db.commit()
    return {"id": k.id, "key": raw, "tip": "密钥只显示这一次，请妥善保存"}


@router.delete("/api-keys/{kid}")
def revoke_api_key(kid: int, db: Session = Depends(get_db), u: User = Depends(require(Role.ADMIN))):
    k = get_or_404(db, ApiKey, kid, "密钥")
    k.is_active = False
    db.commit()
    return {"ok": True}


@router.get("/ai/config")
def ai_cfg(db: Session = Depends(get_db), _: User = Depends(require(Role.ADMIN))):
    return aisvc.public_config(db)


@router.put("/ai/config")
def ai_cfg_save(body: S.AiConfigIn, db: Session = Depends(get_db), u: User = Depends(require(Role.ADMIN))):
    aisvc.save_config(db, body.model_dump(exclude_none=True))
    audit(db, u.id, "AI_CONFIG", "Setting", None, {k: v for k, v in body.model_dump(exclude_none=True).items()
                                                   if k != "ai_api_key"})
    db.commit()
    return aisvc.public_config(db)


@router.post("/ai/test")
def ai_test(db: Session = Depends(get_db), u: User = Depends(require(Role.ADMIN))):
    r = aisvc.test_connection(db, u.id)
    db.commit()
    return r


@router.get("/ai/status")
def ai_status(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return {"enabled": aisvc.ai_config(db)["enabled"]}


@router.post("/ai/brief")
def ai_brief(db: Session = Depends(get_db), u: User = Depends(current_user)):
    r = aisvc.shortage_brief(db, u)
    db.commit()
    return r


@router.post("/ai/ask")
def ai_ask(body: S.AskIn, db: Session = Depends(get_db), u: User = Depends(current_user)):
    r = aisvc.ask(db, u, body.question)
    db.commit()
    return r


@router.get("/ai/logs")
def ai_logs(db: Session = Depends(get_db), _: User = Depends(require(Role.ADMIN))):
    return [serial.pick(x, "id", "scene", "prompt_chars", "ok", "fallback", "error", "created_at")
            for x in db.scalars(select(AiCallLog).order_by(AiCallLog.id.desc()).limit(200)).all()]


@router.get("/audit")
def audit_logs(entity: str = "", entity_id: int | None = None, page_no: int = 1, size: int = 100,
               db: Session = Depends(get_db), _: User = Depends(require(Role.ADMIN, Role.MANAGER))):
    q = select(AuditLog)
    if entity:
        q = q.where(AuditLog.entity == entity)
    if entity_id:
        q = q.where(AuditLog.entity_id == entity_id)
    return page(db, q.order_by(AuditLog.id.desc()), page_no, size,
                lambda a: {**serial.pick(a, "id", "action", "entity", "entity_id", "detail", "created_at"),
                           "user": serial.user_name(db, a.user_id)})


# ---------------------------------------------------------------- Excel 导入
@router.get("/import/{kind}/template")
def import_template(kind: str, _: User = Depends(current_user)):
    names = {"materials": "物料导入模板", "boms": "BOM导入模板", "stock": "期初库存导入模板"}
    from urllib.parse import quote
    return Response(importer.template(kind),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(names.get(kind, kind))}.xlsx"})


@router.post("/import/{kind}")
def import_run(kind: str, dry_run: bool = True, file: UploadFile = File(...), db: Session = Depends(get_db),
               u: User = Depends(require(Role.ADMIN, Role.TECH, Role.WAREHOUSE))):
    if kind == "stock" and not ({Role.ADMIN, Role.WAREHOUSE} & set(u.role_list)):
        raise BizError("期初库存只能由仓库或管理员导入", 403)
    r = importer.run(db, u, kind, file.file.read(), dry_run)
    if r["committed"]:
        # 导入 + 审计 + 齐套重算同一个事务：重算失败整批导入回滚，修好后重新导入不会重复入库
        try:
            db.flush()
            kitting.recalc(db, commit=False)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return r


# ---------------------------------------------------------------- 开放接口 v1（X-API-Key）
open_router = APIRouter(prefix="/open/v1", tags=["开放接口"])


@open_router.get("/stock")
def open_stock(purpose: str = "", keyword: str = "", db: Session = Depends(get_db), _=Depends(api_key_auth("read"))):
    """现存量（按物料 + 仓库）。"""
    return inv.onhand_summary(db, purpose or None, keyword)


@open_router.get("/orders")
def open_orders(status: str = "", db: Session = Depends(get_db), _=Depends(api_key_auth("read"))):
    """订单与齐套状态。"""
    q = select(SalesOrder)
    if status:
        q = q.where(SalesOrder.status.in_(status.split(",")))
    return [serial.order(o) for o in db.scalars(q.order_by(SalesOrder.due_date).limit(500)).all()]


@open_router.get("/shortages")
def open_shortages(db: Session = Depends(get_db), _=Depends(api_key_auth("read"))):
    return kitting.shortage_summary(db)


@open_router.get("/po-lines")
def open_po_lines(db: Session = Depends(get_db), _=Depends(api_key_auth("read"))):
    rows = db.scalars(select(PurchaseOrderLine).join(PurchaseOrder).where(
        PurchaseOrder.status == "APPROVED", PurchaseOrderLine.status.in_(["OPEN", "PARTIAL"]))).all()
    return [{**serial.po_line(x), "po_no": x.po.po_no, "supplier": x.po.supplier.name} for x in rows]


@open_router.post("/orders")
def open_create_order(body: S.OrderIn, db: Session = Depends(get_db), key: ApiKey = Depends(api_key_auth("write"))):
    """外部系统（如财务/CRM）推送销售订单。"""
    sysuser = db.scalar(select(User).where(User.username == "admin"))
    o = osvc.create_order(db, sysuser, body.model_dump())
    audit(db, None, "OPEN_API_CREATE", "SalesOrder", o.id, {"api_key": key.name})
    commit_recalc(db)
    return serial.order(o)


@open_router.get("/warehouses")
def open_warehouses(db: Session = Depends(get_db), _=Depends(api_key_auth("read"))):
    return [serial.pick(w, "id", "code", "name", "purpose") for w in db.scalars(select(Warehouse)).all()]
