"""登录、用户、主数据（物料 / 供应商 / 仓库库位 / BOM）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..core.deps import current_user, require
from ..core.errors import BizError
from ..core.security import create_token, hash_password, new_portal_token, verify_password
from ..db import get_db
from ..models import (Bom, BomLine, Location, Material, MaterialType, OrderStatus, Priority, Role, Supplier,
                      TxnType, User, Warehouse, WarehousePurpose, KitStatus)
from ..services.common import audit, get_or_404
from ..services.purchase import price_history, suggest
from . import schemas as S
from . import serial
from .util import commit_recalc, page

router = APIRouter()


# ---------------------------------------------------------------- auth
_FAILS: dict[str, list[float]] = {}
_MAX_FAILS, _WINDOW = 8, 900  # 15 分钟内同一账号连续失败 8 次 → 暂时锁定


def _throttled(name: str) -> bool:
    import time
    now_ = time.time()
    hits = [t for t in _FAILS.get(name, []) if now_ - t < _WINDOW]
    _FAILS[name] = hits
    return len(hits) >= _MAX_FAILS


@router.post("/auth/login")
def login(body: S.LoginIn, db: Session = Depends(get_db)):
    import time
    if _throttled(body.username):
        raise HTTPException(429, "登录失败次数过多，请 15 分钟后再试")
    u = db.scalar(select(User).where(User.username == body.username))
    if not u or not u.is_active or not verify_password(body.password, u.password_hash):
        _FAILS.setdefault(body.username, []).append(time.time())
        raise HTTPException(401, "用户名或密码错误")
    _FAILS.pop(body.username, None)
    return {"token": create_token(u.id), "user": serial.user(u)}


@router.get("/auth/me")
def me(u: User = Depends(current_user)):
    return serial.user(u)


@router.post("/auth/password")
def change_password(body: S.PasswordIn, u: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(body.old_password, u.password_hash):
        raise BizError("原密码错误")
    u.password_hash = hash_password(body.new_password)
    db.commit()
    return {"ok": True}


@router.get("/meta")
def meta(_: User = Depends(current_user)):
    return {"roles": Role.LABELS, "material_types": MaterialType.LABELS, "purposes": WarehousePurpose.LABELS,
            "order_status": OrderStatus.LABELS, "priority": Priority.LABELS, "kit_status": KitStatus.LABELS,
            "txn_types": TxnType.LABELS}


# ---------------------------------------------------------------- users
@router.get("/users")
def users(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [serial.user(u) for u in db.scalars(select(User).order_by(User.id)).all()]


@router.post("/users")
def create_user(body: S.UserIn, db: Session = Depends(get_db), me_: User = Depends(require(Role.ADMIN))):
    if db.scalar(select(User).where(User.username == body.username)):
        raise BizError("用户名已存在")
    if not body.password or len(body.password) < 6:
        raise BizError("初始密码至少 6 位")
    bad = [r for r in body.roles if r not in Role.ALL]
    if bad:
        raise BizError(f"无效角色：{bad}")
    u = User(username=body.username, name=body.name, password_hash=hash_password(body.password),
             roles=",".join(body.roles), department=body.department, is_active=body.is_active)
    db.add(u)
    db.flush()
    audit(db, me_.id, "CREATE", "User", u.id, body.username)
    db.commit()
    return serial.user(u)


@router.put("/users/{uid}")
def update_user(uid: int, body: S.UserIn, db: Session = Depends(get_db), me_: User = Depends(require(Role.ADMIN))):
    u = get_or_404(db, User, uid, "用户")
    u.name, u.roles, u.department, u.is_active = body.name, ",".join(body.roles), body.department, body.is_active
    if body.password:
        u.password_hash = hash_password(body.password)
    audit(db, me_.id, "EDIT", "User", u.id, {"roles": body.roles, "active": body.is_active})
    db.commit()
    return serial.user(u)


# ---------------------------------------------------------------- materials
@router.get("/materials")
def materials(keyword: str = "", type: str = "", is_common: bool | None = None, page_no: int = 1, size: int = 50,
              db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = select(Material)
    if keyword:
        like = f"%{keyword}%"
        q = q.where(or_(Material.code.like(like), Material.name.like(like), Material.drawing_no.like(like),
                        Material.spec.like(like)))
    if type:
        q = q.where(Material.type.in_(type.split(",")))
    if is_common is not None:
        q = q.where(Material.is_common.is_(is_common))
    return page(db, q.order_by(Material.code), page_no, size, serial.material)


@router.post("/materials")
def create_material(body: S.MaterialIn, db: Session = Depends(get_db), u: User = Depends(require(Role.TECH, Role.PLANNER))):
    if body.type not in MaterialType.ALL:
        raise BizError("物料类型无效")
    if db.scalar(select(Material).where(Material.code == body.code)):
        raise BizError("物料编码已存在")
    m = Material(**body.model_dump())
    db.add(m)
    db.flush()
    audit(db, u.id, "CREATE", "Material", m.id, body.code)
    db.commit()
    return serial.material(m)


@router.put("/materials/{mid}")
def update_material(mid: int, body: S.MaterialIn, db: Session = Depends(get_db),
                    u: User = Depends(require(Role.TECH, Role.PLANNER, Role.WAREHOUSE))):
    m = get_or_404(db, Material, mid, "物料")
    data = body.model_dump()
    if data["drawing_no"] != m.drawing_no:
        raise BizError("修改图号请使用【图纸 → 技改换图号】，以便同步 BOM 与在途单据")
    for k, v in data.items():
        setattr(m, k, v)
    audit(db, u.id, "EDIT", "Material", m.id, body.code)
    commit_recalc(db)
    return serial.material(m)


@router.get("/materials/{mid}/suggest")
def material_suggest(mid: int, supplier_id: int | None = None, db: Session = Depends(get_db),
                     _: User = Depends(current_user)):
    return suggest(db, mid, supplier_id)


@router.get("/materials/{mid}/price-history")
def material_price_history(mid: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return price_history(db, mid, 30)


# ---------------------------------------------------------------- suppliers
@router.get("/suppliers")
def suppliers(keyword: str = "", outsourcer: bool | None = None, db: Session = Depends(get_db),
              _: User = Depends(current_user)):
    q = select(Supplier)
    if keyword:
        q = q.where(or_(Supplier.code.like(f"%{keyword}%"), Supplier.name.like(f"%{keyword}%")))
    if outsourcer is not None:
        q = q.where(Supplier.is_outsourcer.is_(outsourcer))
    return [serial.supplier(s) for s in db.scalars(q.order_by(Supplier.code)).all()]


@router.post("/suppliers")
def create_supplier(body: S.SupplierIn, db: Session = Depends(get_db), u: User = Depends(require(Role.BUYER))):
    if db.scalar(select(Supplier).where(Supplier.code == body.code)):
        raise BizError("供应商编码已存在")
    s = Supplier(**body.model_dump())
    db.add(s)
    db.commit()
    return serial.supplier(s)


@router.put("/suppliers/{sid}")
def update_supplier(sid: int, body: S.SupplierIn, db: Session = Depends(get_db), u: User = Depends(require(Role.BUYER))):
    s = get_or_404(db, Supplier, sid, "供应商")
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    db.commit()
    return serial.supplier(s)


@router.post("/suppliers/{sid}/portal-token")
def supplier_portal_token(sid: int, db: Session = Depends(get_db), u: User = Depends(require(Role.BUYER))):
    """生成/重置供应商门户链接（旧链接立即失效）。"""
    s = get_or_404(db, Supplier, sid, "供应商")
    s.portal_token = new_portal_token()
    audit(db, u.id, "PORTAL_TOKEN", "Supplier", s.id)
    db.commit()
    return {"token": s.portal_token, "path": f"/portal/{s.portal_token}"}


@router.delete("/suppliers/{sid}/portal-token")
def supplier_portal_revoke(sid: int, db: Session = Depends(get_db), u: User = Depends(require(Role.BUYER))):
    s = get_or_404(db, Supplier, sid, "供应商")
    s.portal_token = None
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- warehouses
@router.get("/warehouses")
def warehouses(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [{**serial.pick(w, "id", "code", "name", "purpose", "is_active"),
             "purpose_label": WarehousePurpose.LABELS.get(w.purpose),
             "locations": [serial.pick(x, "id", "code", "name") for x in w.locations]}
            for w in db.scalars(select(Warehouse).order_by(Warehouse.id)).all()]


@router.post("/warehouses")
def create_warehouse(body: S.WarehouseIn, db: Session = Depends(get_db), u: User = Depends(require(Role.WAREHOUSE))):
    if body.purpose not in WarehousePurpose.ALL:
        raise BizError("仓库用途无效")
    w = Warehouse(**body.model_dump())
    db.add(w)
    db.commit()
    return {"id": w.id}


@router.put("/warehouses/{wid}")
def update_warehouse(wid: int, body: S.WarehouseIn, db: Session = Depends(get_db),
                     u: User = Depends(require(Role.WAREHOUSE))):
    w = get_or_404(db, Warehouse, wid, "仓库")
    for k, v in body.model_dump().items():
        setattr(w, k, v)
    commit_recalc(db)
    return {"id": w.id}


@router.post("/warehouses/{wid}/locations")
def add_location(wid: int, body: S.LocationIn, db: Session = Depends(get_db), u: User = Depends(require(Role.WAREHOUSE))):
    get_or_404(db, Warehouse, wid, "仓库")
    loc = Location(warehouse_id=wid, code=body.code, name=body.name)
    db.add(loc)
    db.commit()
    return {"id": loc.id}


# ---------------------------------------------------------------- BOM
@router.get("/boms")
def boms(material_id: int | None = None, keyword: str = "", db: Session = Depends(get_db),
         _: User = Depends(current_user)):
    q = select(Bom).join(Material, Material.id == Bom.material_id)
    if material_id:
        q = q.where(Bom.material_id == material_id)
    if keyword:
        q = q.where(or_(Material.code.like(f"%{keyword}%"), Material.name.like(f"%{keyword}%")))
    return [{**serial.pick(b, "id", "material_id", "version", "status", "is_mature", "remark", "updated_at"),
             "material": f"{b.material.code} {b.material.name}", "material_type": b.material.type,
             "line_count": len(b.lines)} for b in db.scalars(q.order_by(Material.code, Bom.id)).all()]


@router.get("/boms/{bid}")
def get_bom(bid: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return serial.bom(get_or_404(db, Bom, bid, "BOM"))


@router.get("/boms/{bid}/tree")
def bom_tree(bid: int, qty: float = 1, db: Session = Depends(get_db), _: User = Depends(current_user)):
    """多级展开（选机型和数量后展开全部物料）。"""
    from decimal import Decimal
    active = {b.material_id: b for b in db.scalars(select(Bom).where(Bom.status == "ACTIVE")).all()}

    def walk(b: Bom, mult: Decimal, depth: int, path: tuple) -> list[dict]:
        out = []
        for ln in b.lines:
            need = Decimal(str(ln.qty_per)) * (1 + Decimal(str(ln.scrap_rate))) * mult
            node = {"id": f"{'-'.join(map(str, path))}-{ln.id}", "material_id": ln.child_id, "code": ln.child.code,
                    "name": ln.child.name, "type": ln.child.type, "drawing_no": ln.child.drawing_no,
                    "qty_per": ln.qty_per, "total": need, "unit": ln.child.unit, "level": depth}
            sub = active.get(ln.child_id)
            if sub and depth < 8 and ln.child_id not in path:
                node["children"] = walk(sub, need, depth + 1, path + (ln.child_id,))
            out.append(node)
        return out
    b = get_or_404(db, Bom, bid, "BOM")
    return walk(b, Decimal(str(qty)), 1, (b.material_id,))


def _check_cycle(db: Session, b: Bom) -> None:
    """生效前检查循环引用（A 含 B、B 又含 A）。"""
    active = {x.material_id: x for x in db.scalars(select(Bom).where(Bom.status == "ACTIVE")).all()}
    active[b.material_id] = b

    def walk(mid: int, path: tuple) -> None:
        bb = active.get(mid)
        if not bb:
            return
        for ln in bb.lines:
            if ln.child_id in path:
                raise BizError(f"BOM 循环引用：{ln.child.code} 出现在自己的下级中")
            walk(ln.child_id, path + (ln.child_id,))
    walk(b.material_id, (b.material_id,))


def _apply_lines(db: Session, b: Bom, lines: list[S.BomLineIn]) -> None:
    seen = set()
    for ln in lines:
        if ln.child_id == b.material_id:
            raise BizError("子件不能是自身")
        if ln.child_id in seen:
            raise BizError("同一 BOM 中子件重复，请合并用量")
        seen.add(ln.child_id)
        get_or_404(db, Material, ln.child_id, "子件物料")
    b.lines.clear()
    db.flush()
    for ln in lines:
        b.lines.append(BomLine(**ln.model_dump()))


@router.post("/boms")
def create_bom(body: S.BomIn, db: Session = Depends(get_db), u: User = Depends(require(Role.TECH))):
    get_or_404(db, Material, body.material_id, "物料")
    if db.scalar(select(Bom).where(Bom.material_id == body.material_id, Bom.version == body.version)):
        raise BizError("该版本已存在")
    b = Bom(material_id=body.material_id, version=body.version, is_mature=body.is_mature, remark=body.remark,
            maintained_by=u.id)
    db.add(b)
    db.flush()
    _apply_lines(db, b, body.lines)
    audit(db, u.id, "CREATE", "Bom", b.id, body.version)
    db.commit()
    return serial.bom(b)


@router.put("/boms/{bid}")
def update_bom(bid: int, body: S.BomIn, db: Session = Depends(get_db), u: User = Depends(require(Role.TECH))):
    b = get_or_404(db, Bom, bid, "BOM")
    if b.status != "DRAFT":
        raise BizError("生效/作废的 BOM 不能直接修改，请“复制为新版本”后修改再生效")
    b.is_mature, b.remark = body.is_mature, body.remark
    _apply_lines(db, b, body.lines)
    audit(db, u.id, "EDIT", "Bom", b.id)
    db.commit()
    return serial.bom(b)


@router.post("/boms/{bid}/copy")
def copy_bom(bid: int, version: str, db: Session = Depends(get_db), u: User = Depends(require(Role.TECH))):
    src = get_or_404(db, Bom, bid, "BOM")
    if db.scalar(select(Bom).where(Bom.material_id == src.material_id, Bom.version == version)):
        raise BizError("该版本已存在")
    b = Bom(material_id=src.material_id, version=version, is_mature=src.is_mature, remark=f"复制自 {src.version}",
            maintained_by=u.id)
    for ln in src.lines:
        b.lines.append(BomLine(child_id=ln.child_id, qty_per=ln.qty_per, scrap_rate=ln.scrap_rate, seq=ln.seq,
                               remark=ln.remark))
    db.add(b)
    db.commit()
    return serial.bom(b)


@router.post("/boms/{bid}/activate")
def activate_bom(bid: int, db: Session = Depends(get_db), u: User = Depends(require(Role.TECH))):
    """生效：同物料其他生效版本自动作废。已下达订单仍按下达时锁定的版本执行。"""
    b = get_or_404(db, Bom, bid, "BOM")
    if not b.lines:
        raise BizError("BOM 没有子件")
    _check_cycle(db, b)
    for x in db.scalars(select(Bom).where(Bom.material_id == b.material_id, Bom.status == "ACTIVE")).all():
        x.status = "OBSOLETE"
    b.status = "ACTIVE"
    audit(db, u.id, "ACTIVATE", "Bom", b.id, b.version)
    commit_recalc(db)
    return serial.bom(b)
