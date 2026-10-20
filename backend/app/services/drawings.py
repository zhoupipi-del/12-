"""图纸版本受控：订单绑定批准版本。

规则：
  · 加工/装配前，单据确认【当前生效版本】→ 该版本即为单据绑定的批准版本（DrawingBinding）。
  · 图纸升版时旧版变为 SUPERSEDED（不是作废）：已绑定的在制单据继续按批准版本做，确认【不会】自动失效。
  · 升版后由技术先看影响清单，再逐单指定哪些单据切换到新版（switch_binding，切换后需重新确认新版）。
  · 重大安全/质量变更（change_level=MAJOR）：发布时对所有仍绑定旧版的在制单据强制暂停（hold），
    暂停期间不能开工/发出/领料；由技术逐单裁定“切换新版”或“按原版继续”，留痕后解除。
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..core.errors import BizError
from ..models import (Bom, Drawing, DrawingBinding, DrawingConfirmation, DrawingVersion, Material, OrderStatus,
                      OutsourceOrder, PurchaseOrderLine, RequisitionLine, Role, SalesOrder, User,
                      WorkOrder, now)
from .alerts import raise_alert
from .common import audit, get_or_404

ALLOWED = {".pdf", ".dwg", ".dxf", ".png", ".jpg", ".jpeg", ".step", ".stp", ".igs", ".zip"}


def save_upload(f: UploadFile, sub: str) -> tuple[str, str]:
    ext = Path(f.filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise BizError(f"不支持的文件类型 {ext}，允许：{' '.join(sorted(ALLOWED))}")
    d = settings.upload_dir / sub
    d.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    with (d / name).open("wb") as out:
        shutil.copyfileobj(f.file, out)
    return f.filename or name, f"{sub}/{name}"


def current_version(db: Session, drawing_no: str) -> DrawingVersion | None:
    return db.scalar(select(DrawingVersion).join(Drawing).where(Drawing.drawing_no == drawing_no,
                                                                DrawingVersion.status == "RELEASED"))


def upload_version(db: Session, user: User, drawing_id: int, version: str, change_note: str,
                   file: UploadFile | None, pdf: UploadFile | None) -> DrawingVersion:
    dw = get_or_404(db, Drawing, drawing_id, "图纸")
    version = version.strip().upper()
    if not version:
        raise BizError("请填写版本号")
    if any(v.version == version for v in dw.versions):
        raise BizError(f"版本 {version} 已存在")
    v = DrawingVersion(drawing_id=dw.id, version=version, change_note=change_note, uploaded_by=user.id)
    if file:
        v.file_name, v.file_path = save_upload(file, "drawings")
    if pdf:
        _, v.pdf_path = save_upload(pdf, "drawings")
    elif v.file_path.endswith(".pdf"):
        v.pdf_path = v.file_path
    db.add(v)
    db.flush()
    audit(db, user.id, "UPLOAD", "DrawingVersion", v.id, {"drawing_no": dw.drawing_no, "version": version})
    return v


def release(db: Session, user: User, version_id: int, change_level: str = "MINOR", hold_reason: str = "") -> dict:
    v = get_or_404(db, DrawingVersion, version_id, "图纸版本")
    if v.status != "DRAFT":
        raise BizError("只有草稿版本可以发布")
    if not v.pdf_path and not v.file_path:
        raise BizError("请先上传图纸文件")
    if change_level not in ("MINOR", "MAJOR"):
        raise BizError("变更级别必须是 MINOR 或 MAJOR")
    if change_level == "MAJOR" and not (hold_reason or v.change_note):
        raise BizError("重大变更请填写暂停原因/变更说明")
    old = [x for x in v.drawing.versions if x.status == "RELEASED"]
    for x in old:
        x.status = "SUPERSEDED"  # 不是作废：已绑定该版本的在制单据继续有效
    v.status, v.released_by, v.released_at, v.change_level = "RELEASED", user.id, now(), change_level
    db.flush()
    no = v.drawing.drawing_no
    affected = affected_docs(db, no)
    held = 0
    if old and change_level == "MAJOR":
        for b in db.scalars(select(DrawingBinding).where(DrawingBinding.drawing_id == v.drawing_id,
                                                         DrawingBinding.version_id != v.id)).all():
            if _ref_active(db, b.ref_type, b.ref_id):
                b.hold, b.hold_reason, b.updated_by = True, (hold_reason or v.change_note)[:300], user.id
                held += 1
    if old and (any(affected.values()) or held):
        lvl = "CRITICAL"
        title = (f"图纸重大变更：{no} {old[0].version} → {v.version}，{held} 张在制单据已强制暂停"
                 if held else f"图纸升版：{no} {old[0].version} → {v.version}，请评估影响并指定切换的单据")
        raise_alert(db, "DRAWING_CHANGED", f"DWG:{v.id}", title,
                    f"变更说明：{v.change_note}；在制单据仍按各自批准版本执行，不会自动切版。"
                    f"影响：订单 {len(affected['orders'])}、自制 {len(affected['work_orders'])}、"
                    f"外协 {len(affected['outsource'])}、采购行 {len(affected['po_lines'])}",
                    lvl, Role.TECH, "DRAWING", v.drawing_id)
    audit(db, user.id, "RELEASE", "DrawingVersion", v.id,
          {"drawing_no": no, "version": v.version, "level": change_level, "held": held})
    return {"version": v, "affected": affected, "held": held}


def _ref_active(db: Session, ref_type: str, ref_id: int) -> bool:
    if ref_type == "SALES_ORDER":
        o = db.get(SalesOrder, ref_id)
        return bool(o and o.status in OrderStatus.ACTIVE_FOR_KIT)
    if ref_type == "WORK_ORDER":
        w = db.get(WorkOrder, ref_id)
        return bool(w and w.status in ("PLANNED", "STARTED"))
    if ref_type == "OUTSOURCE":
        x = db.get(OutsourceOrder, ref_id)
        return bool(x and x.status in ("PLANNED", "SENT", "PARTIAL"))
    return False


def affected_docs(db: Session, drawing_no: str) -> dict:
    mats = [m.id for m in db.scalars(select(Material).where(Material.drawing_no == drawing_no)).all()]
    orders = []
    for o in db.scalars(select(SalesOrder).where(SalesOrder.status.in_(OrderStatus.ACTIVE_FOR_KIT))).all():
        if set(mats) & set(order_material_ids(db, o)):
            orders.append(o.order_no)
    wos = [w.wo_no for w in db.scalars(select(WorkOrder).where(WorkOrder.material_id.in_(mats),
                                                               WorkOrder.status.in_(["PLANNED", "STARTED"]))).all()] if mats else []
    oss = [x.os_no for x in db.scalars(select(OutsourceOrder).where(OutsourceOrder.drawing_no == drawing_no,
                                                                    OutsourceOrder.status.in_(["PLANNED", "SENT", "PARTIAL"]))).all()]
    pls = [f"{x.po.po_no}-{x.line_no}" for x in db.scalars(select(PurchaseOrderLine).where(
        PurchaseOrderLine.drawing_no == drawing_no, PurchaseOrderLine.status.in_(["OPEN", "PARTIAL"]))).all()]
    return {"orders": orders, "work_orders": wos, "outsource": oss, "po_lines": pls}


def bindings_of(db: Session, drawing_id: int) -> list[dict]:
    """某图纸当前被哪些在制单据绑定（含绑定版本、是否有新版可切换、是否暂停）— 升版影响评估用。"""
    cur = db.scalar(select(DrawingVersion).where(DrawingVersion.drawing_id == drawing_id, DrawingVersion.status == "RELEASED"))
    out = []
    for b in db.scalars(select(DrawingBinding).where(DrawingBinding.drawing_id == drawing_id)).all():
        if not _ref_active(db, b.ref_type, b.ref_id):
            continue
        ver = db.get(DrawingVersion, b.version_id)
        if b.ref_type == "SALES_ORDER":
            no = db.get(SalesOrder, b.ref_id).order_no
        elif b.ref_type == "WORK_ORDER":
            no = db.get(WorkOrder, b.ref_id).wo_no
        else:
            no = db.get(OutsourceOrder, b.ref_id).os_no
        out.append({"ref_type": b.ref_type, "ref_id": b.ref_id, "no": no, "bound_version": ver.version,
                    "bound_version_id": ver.id, "current_version": cur.version if cur else "",
                    "current_version_id": cur.id if cur else None,
                    "upgrade_available": bool(cur and cur.id != ver.id), "hold": b.hold, "hold_reason": b.hold_reason})
    out.sort(key=lambda x: (x["ref_type"], x["no"]))
    return out


def order_material_ids(db: Session, order: SalesOrder, max_depth: int = 8) -> list[int]:
    """订单 BOM 全展开的物料清单（去重）。"""
    if order.bom_snapshot:  # 下达时冻结的整棵 BOM 树
        import json
        tree = json.loads(order.bom_snapshot)
        seen: list[int] = []
        stack = [str(order.product_id)]
        while stack:
            for cid, _per in tree.get(stack.pop(), []):
                if cid not in seen:
                    seen.append(cid)
                    stack.append(str(cid))
        return seen
    boms = {b.material_id: b for b in db.scalars(select(Bom).where(Bom.status == "ACTIVE")).all()}
    top = db.get(Bom, order.bom_id) if order.bom_id else boms.get(order.product_id)
    seen: list[int] = []

    def walk(b: Bom | None, depth: int) -> None:
        if not b or depth > max_depth:
            return
        for ln in b.lines:
            if ln.child_id not in seen:
                seen.append(ln.child_id)
                walk(boms.get(ln.child_id), depth + 1)
    walk(top, 1)
    return seen


def _binding(db: Session, ref_type: str, ref_id: int, drawing_id: int) -> DrawingBinding | None:
    return db.scalar(select(DrawingBinding).where(DrawingBinding.ref_type == ref_type, DrawingBinding.ref_id == ref_id,
                                                  DrawingBinding.drawing_id == drawing_id))


def _bound_version(db: Session, ref_type: str, ref_id: int, drawing_id: int) -> tuple[DrawingVersion | None, DrawingBinding | None]:
    """单据的批准版本：有绑定用绑定；老数据没有绑定则取该单据最近一次确认过的版本。"""
    b = _binding(db, ref_type, ref_id, drawing_id)
    if b:
        return db.get(DrawingVersion, b.version_id), b
    c = db.scalar(select(DrawingConfirmation).join(DrawingVersion, DrawingVersion.id == DrawingConfirmation.drawing_version_id)
                  .where(DrawingConfirmation.ref_type == ref_type, DrawingConfirmation.ref_id == ref_id,
                         DrawingVersion.drawing_id == drawing_id).order_by(DrawingConfirmation.id.desc()))
    return (db.get(DrawingVersion, c.drawing_version_id) if c else None), None


def _ensure_binding(db: Session, user: User | None, ref_type: str, ref_id: int, drawing_id: int,
                    version_id: int) -> DrawingBinding:
    b = _binding(db, ref_type, ref_id, drawing_id)
    if b is None:
        b = DrawingBinding(ref_type=ref_type, ref_id=ref_id, drawing_id=drawing_id, version_id=version_id,
                           updated_by=user.id if user else None)
        db.add(b)
        db.flush()
    return b


def _ref_materials(db: Session, ref_type: str, ref_id: int) -> tuple[list[int], str]:
    if ref_type == "SALES_ORDER":
        o = get_or_404(db, SalesOrder, ref_id, "订单")
        return [o.product_id] + order_material_ids(db, o), "ASSEMBLY"
    if ref_type == "WORK_ORDER":
        return [get_or_404(db, WorkOrder, ref_id, "自制任务").material_id], "MACHINING"
    if ref_type == "OUTSOURCE":
        return [get_or_404(db, OutsourceOrder, ref_id, "外协单").material_id], "MACHINING"
    raise BizError("类型错误")


def required_drawings(db: Session, ref_type: str, ref_id: int) -> list[dict]:
    """某单据需要确认的图纸、批准版本及状态。已绑定批准版本的单据不受后续升版影响。"""
    mids, stage = _ref_materials(db, ref_type, ref_id)
    out, seen = [], set()
    for mid in mids:
        m = db.get(Material, mid)
        if not m.drawing_no or m.drawing_no in seen or m.drawing_exempt:
            continue
        seen.add(m.drawing_no)
        cur = current_version(db, m.drawing_no)
        if not cur:
            # 填了图号却没有已发布的受控版本：不能当作“无需图纸”放行。确无图纸的标准件请在物料上勾选“免图纸管控”
            out.append({"drawing_no": m.drawing_no, "drawing_id": None, "material": f"{m.code} {m.name}",
                        "version_id": None, "version": "（未归档）", "pdf_path": "", "stage": stage,
                        "current_version_id": None, "current_version": "", "upgrade_available": False,
                        "confirmed": False, "confirmed_at": None, "hold": False, "hold_reason": "",
                        "outdated_confirm": False, "missing": True})
            continue
        bound, b = _bound_version(db, ref_type, ref_id, cur.drawing_id)
        ver = bound or cur  # 还没批准过任何版本 → 需要确认当前生效版
        conf = db.scalar(select(DrawingConfirmation).where(
            DrawingConfirmation.ref_type == ref_type, DrawingConfirmation.ref_id == ref_id,
            DrawingConfirmation.drawing_version_id == ver.id))
        out.append({"drawing_no": m.drawing_no, "drawing_id": cur.drawing_id, "material": f"{m.code} {m.name}",
                    "version_id": ver.id, "version": ver.version, "pdf_path": ver.pdf_path, "stage": stage,
                    "current_version_id": cur.id, "current_version": cur.version,
                    "upgrade_available": ver.id != cur.id,
                    "confirmed": conf is not None, "confirmed_at": conf.confirmed_at if conf else None,
                    "hold": bool(b and b.hold), "hold_reason": b.hold_reason if b else "",
                    "outdated_confirm": False})
    return out


def confirm(db: Session, user: User, ref_type: str, ref_id: int, version_ids: list[int]) -> int:
    req = {r["version_id"]: r for r in required_drawings(db, ref_type, ref_id)}
    n = 0
    for vid in version_ids:
        r = req.get(vid)
        if not r:
            raise BizError("该图纸版本不是该单据的批准版本，请刷新；要换版请由技术走“切换版本”")
        if r["confirmed"]:
            continue
        db.add(DrawingConfirmation(drawing_version_id=vid, ref_type=ref_type, ref_id=ref_id, stage=r["stage"],
                                   confirmed_by=user.id, confirmed_at=now()))
        _ensure_binding(db, user, ref_type, ref_id, r["drawing_id"], vid)  # 确认即绑定批准版本
        n += 1
    db.flush()
    return n


def assert_confirmed(db: Session, ref_type: str, ref_id: int) -> None:
    reqs = required_drawings(db, ref_type, ref_id)
    held = [r for r in reqs if r["hold"]]
    if held:
        lst = "、".join(f"{r['drawing_no']}({r['version']})：{r['hold_reason']}" for r in held[:5])
        raise BizError(f"图纸重大变更，该单据已被强制暂停，需技术裁定后才能继续：{lst}", data={"held": held})
    missing = [r for r in reqs if r.get("missing")]
    if missing:
        lst = "、".join(f"{r['drawing_no']}（{r['material']}）" for r in missing[:8])
        raise BizError(f"以下物料填了图号但没有已发布的受控图纸，不能开工：{lst}。请技术先上传并发布，"
                       "确无图纸的标准件请在物料上勾选“免图纸管控”", data={"missing": missing})
    pending = [r for r in reqs if not r["confirmed"]]
    if pending:
        lst = "、".join(f"{r['drawing_no']}({r['version']})" for r in pending[:8])
        raise BizError(f"以下图纸未确认批准版本，不能开工：{lst}", data={"pending": pending})


def assert_not_held(db: Session, ref_type: str, ref_id: int) -> None:
    """领料/过账等动作也不得绕过强制暂停。"""
    held = [r for r in required_drawings(db, ref_type, ref_id) if r["hold"]]
    if held:
        raise BizError("图纸重大变更，该单据已被强制暂停：" + "、".join(r["drawing_no"] for r in held[:5]),
                       data={"held": held})


def switch_binding(db: Session, user: User, ref_type: str, ref_id: int, drawing_id: int, new_version_id: int,
                   reason: str) -> dict:
    """技术指定某单据切换到新版本。切换后需重新确认新版；同时解除该图纸的强制暂停。"""
    if not reason.strip():
        raise BizError("切换版本必须填写原因/评估结论")
    ver = get_or_404(db, DrawingVersion, new_version_id, "图纸版本")
    if ver.drawing_id != drawing_id or ver.status not in ("RELEASED", "SUPERSEDED"):
        raise BizError("目标版本无效")
    if not _ref_active(db, ref_type, ref_id):
        raise BizError("单据已结束，无需切换")
    old, b = _bound_version(db, ref_type, ref_id, drawing_id)
    b = b or _ensure_binding(db, user, ref_type, ref_id, drawing_id, old.id if old else ver.id)
    b.previous_version_id, b.version_id = (old.id if old else None), ver.id
    b.hold, b.hold_reason, b.updated_by = False, "", user.id
    audit(db, user.id, "DRAWING_SWITCH", ref_type, ref_id,
          {"drawing_id": drawing_id, "from": old.version if old else None, "to": ver.version, "reason": reason})
    return {"ref_type": ref_type, "ref_id": ref_id, "from": old.version if old else None, "to": ver.version,
            "need_reconfirm": True}


def resolve_hold(db: Session, user: User, ref_type: str, ref_id: int, drawing_id: int, reason: str) -> dict:
    """技术裁定：该单据按原批准版本继续（解除强制暂停）。要换新版请用 switch_binding。"""
    if not reason.strip():
        raise BizError("解除暂停必须填写裁定理由")
    b = _binding(db, ref_type, ref_id, drawing_id)
    if not b or not b.hold:
        raise BizError("该单据没有处于暂停的图纸")
    b.hold, b.hold_reason, b.updated_by = False, "", user.id
    audit(db, user.id, "DRAWING_HOLD_RELEASE", ref_type, ref_id, {"drawing_id": drawing_id, "reason": reason})
    return {"ok": True}


def change_drawing_no(db: Session, user: User, material_id: int, new_no: str, reason: str) -> dict:
    """技改换图号：物料图号更新 → BOM 中引用该物料处自动同步；在途采购行/请购行/外协单同步更新。"""
    m = get_or_404(db, Material, material_id, "物料")
    new_no = new_no.strip()
    if not new_no or not reason:
        raise BizError("请填写新图号和技改原因")
    old = m.drawing_no
    m.drawing_no = new_no
    n_po = n_req = n_os = 0
    for ln in db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.material_id == m.id,
                                                         PurchaseOrderLine.status == "OPEN")).all():
        if not ln.locked:
            ln.drawing_no = new_no
            n_po += 1
    for ln in db.scalars(select(RequisitionLine).where(RequisitionLine.material_id == m.id)).all():
        if ln.req.status in ("DRAFT", "CONFIRMED"):
            ln.drawing_no = new_no
            n_req += 1
    for x in db.scalars(select(OutsourceOrder).where(OutsourceOrder.material_id == m.id,
                                                     OutsourceOrder.status == "PLANNED")).all():
        x.drawing_no = new_no
        n_os += 1
    boms = db.scalars(select(Bom).join(Bom.lines).where(Bom.status == "ACTIVE")).unique().all()
    n_bom = sum(1 for b in boms if any(ln.child_id == m.id for ln in b.lines))
    if not db.scalar(select(Drawing).where(Drawing.drawing_no == new_no)):
        db.add(Drawing(drawing_no=new_no, name=m.name, material_id=m.id))
    audit(db, user.id, "CHANGE_DRAWING_NO", "Material", m.id, {"old": old, "new": new_no, "reason": reason})
    raise_alert(db, "DRAWING_CHANGED", f"DWGNO:{m.id}:{new_no}", f"技改换图号：{m.name} {old} → {new_no}",
                f"原因：{reason}；同步 BOM {n_bom} 处、采购行 {n_po}、请购行 {n_req}、外协单 {n_os}",
                "WARN", Role.BUYER, "MATERIAL", m.id)
    return {"old": old, "new": new_no, "boms": n_bom, "po_lines": n_po, "req_lines": n_req, "outsource": n_os}
