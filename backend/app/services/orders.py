"""销售订单全生命周期：录入 → 下达（锁定 BOM 版本）→ 领料（按 BOM 带出、先进先出）→ 开工装配
（图纸确认 + 齐套校验准入）→ 完工入库 → 发货计划审批 → 发货出库 → 关闭。"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import BizError
from ..models import (Bom, KitStatus, Material, MaterialType, OrderIssue, OrderStatus, PickLine, PickList,
                      OutsourceOrder, Reservation, SalesOrder, ShipmentPlan, StockLot, StockTxn, TxnType, User, Warehouse,
                      WarehousePurpose, WorkOrder, ZERO, now)
from .common import D, audit, get_or_404, next_no
from .drawings import assert_confirmed, assert_not_held
from . import protection as prot
from .protection import reserved_by_others
from .kitting import acquire_mutex
from .inventory import available, default_warehouse, fifo_out, stock_in


def active_bom(db: Session, material_id: int) -> Bom | None:
    return db.scalar(select(Bom).where(Bom.material_id == material_id, Bom.status == "ACTIVE"))


def create_order(db: Session, user: User, data: dict) -> SalesOrder:
    p = get_or_404(db, Material, data["product_id"], "机型")
    if p.type != MaterialType.PRODUCT:
        raise BizError("请选择整机机型")
    if D(data["qty"]) <= 0:
        raise BizError("数量必须大于 0")
    o = SalesOrder(order_no=data.get("order_no") or next_no(db, SalesOrder, "order_no", "SO"),
                   customer=data["customer"], product_id=p.id, qty=D(data["qty"]), due_date=data["due_date"],
                   planned_start=data.get("planned_start"), priority=int(data.get("priority", 3)),
                   is_nonstandard=bool(data.get("is_nonstandard")), remark=data.get("remark", ""),
                   station=data.get("station", ""))
    db.add(o)
    db.flush()
    audit(db, user.id, "CREATE", "SalesOrder", o.id, data)
    return o


EDITABLE = ("customer", "qty", "due_date", "planned_start", "priority", "priority_pin", "remark", "station",
            "is_nonstandard", "progress")


def update_order(db: Session, user: User, order_id: int, data: dict) -> SalesOrder:
    o = get_or_404(db, SalesOrder, order_id, "订单")
    if o.status in (OrderStatus.SHIPPED, OrderStatus.CLOSED, OrderStatus.CANCELLED):
        raise BizError("订单已结束，不能修改")
    before = {}
    for k, v in data.items():
        if k not in EDITABLE:
            raise BizError(f"字段 {k} 不可修改")
        if k == "qty" and o.status not in (OrderStatus.PLANNED, OrderStatus.RELEASED):
            raise BizError("装配开始后不能改数量")
        before[k] = getattr(o, k)
        setattr(o, k, D(v) if k == "qty" else v)
    audit(db, user.id, "EDIT", "SalesOrder", o.id, {"before": before, "after": data})
    return o


def bom_snapshot(db: Session, top: Bom) -> dict:
    """下达时把整棵多级 BOM（顶层用锁定版本，下层取当时生效版）冻结成 {物料: [[子件, 含损耗用量], ...]}。
    此后子 BOM 升版不会改变这张订单的需求，要采用新版须走变更。"""
    tree: dict[str, list] = {str(top.material_id): [[ln.child_id, str(D(ln.qty_per) * (1 + D(ln.scrap_rate)))] for ln in top.lines]}
    stack = [ln.child_id for ln in top.lines]
    while stack:
        mid = stack.pop()
        if str(mid) in tree:
            continue
        b = active_bom(db, mid)
        tree[str(mid)] = [[ln.child_id, str(D(ln.qty_per) * (1 + D(ln.scrap_rate)))] for ln in b.lines] if b else []
        stack.extend(ln.child_id for ln in b.lines) if b else None
    return tree


def release(db: Session, user: User, order_id: int) -> SalesOrder:
    """计划下达：锁定当前生效 BOM 版本，订单进入“已下达”，冲填时优先于未下达订单。"""
    o = get_or_404(db, SalesOrder, order_id, "订单")
    if o.status != OrderStatus.PLANNED:
        raise BizError("只有“已录入”的订单可以下达")
    b = active_bom(db, o.product_id)
    if not b or not b.lines:
        raise BizError(f"机型 {o.product.code} 没有生效的 BOM，请技术先维护 BOM")
    o.bom_id, o.status = b.id, OrderStatus.RELEASED
    o.bom_snapshot = json.dumps(bom_snapshot(db, b), ensure_ascii=False)
    audit(db, user.id, "RELEASE", "SalesOrder", o.id, {"bom": f"{b.id}/{b.version}"})
    return o


def cancel(db: Session, user: User, order_id: int, reason: str) -> SalesOrder:
    o = get_or_404(db, SalesOrder, order_id, "订单")
    if o.status not in (OrderStatus.PLANNED, OrderStatus.RELEASED):
        raise BizError("装配开始后不能取消，请走退料流程")
    issued = db.scalar(select(func.coalesce(func.sum(OrderIssue.qty), 0)).where(OrderIssue.order_id == o.id))
    if D(issued) > 0:
        raise BizError("订单已领料，请先退料再取消")
    o.status = OrderStatus.CANCELLED
    for r in db.scalars(select(Reservation).where(Reservation.order_id == o.id)).all():
        db.delete(r)
    for pk in db.scalars(select(PickList).where(PickList.order_id == o.id, PickList.status == "DRAFT")).all():
        pk.status = "CANCELLED"  # 未过账的领料单随订单作废，不能事后再扣库存
    audit(db, user.id, "CANCEL", "SalesOrder", o.id, reason)
    return o


def lock_reservation(db: Session, user: User, order_id: int, material_id: int, qty: Decimal, reason: str) -> Reservation:
    """人工锁定预占：急单协调结果固化，重算时不会被其他订单抢走。"""
    o = get_or_404(db, SalesOrder, order_id, "订单")
    if o.status not in OrderStatus.ACTIVE_FOR_KIT:
        raise BizError("订单不在生产中")
    if not reason:
        raise BizError("请填写锁定原因")
    qty = D(qty)
    from .kitting import acquire_mutex
    acquire_mutex(db)  # 与重算/调料串行，避免两人同时锁同一批库存而超占
    onhand = db.scalar(select(func.coalesce(func.sum(StockLot.qty), 0)).join(Warehouse).where(
        StockLot.material_id == material_id, Warehouse.purpose == WarehousePurpose.PRODUCTION))
    held = db.scalars(select(Reservation).where(Reservation.material_id == material_id,
                                                Reservation.source_type == "STOCK").with_for_update()).all()
    total_held = sum((D(r.qty) for r in held), ZERO)
    own = [r for r in held if r.order_id == o.id and not r.locked]
    own_qty = sum((D(r.qty) for r in own), ZERO)
    free = D(onhand) - total_held
    if qty <= 0 or qty > free + own_qty:
        raise BizError(f"可锁定数量不足：现存 {D(onhand):g}，已被预占/锁定 {total_held:g}，本订单已冻结 {own_qty:g}")
    to_move = max(qty - max(free, ZERO), ZERO)  # 先用空闲量，不够再把本订单已冻结的转成人工锁定
    for r in own:
        if to_move <= 0:
            break
        t = min(D(r.qty), to_move)
        r.qty = D(r.qty) - t
        to_move -= t
        if r.qty <= 0:
            db.delete(r)
    r = Reservation(order_id=o.id, material_id=material_id, qty=qty, source_type="STOCK", locked=True,
                    locked_by=user.id, lock_reason=reason)
    db.add(r)
    audit(db, user.id, "LOCK", "Reservation", None, {"order": o.order_no, "material": material_id, "qty": qty})
    return r


def unlock_reservation(db: Session, user: User, res_id: int) -> None:
    r = get_or_404(db, Reservation, res_id, "预占")
    if not r.locked:
        raise BizError("该预占不是人工锁定")
    audit(db, user.id, "UNLOCK", "Reservation", r.id, {"order": r.order_id, "material": r.material_id})
    db.delete(r)


# ------------------------------------------------------------------ 领料


def pick_plan(db: Session, order: SalesOrder) -> list[dict]:
    b = db.get(Bom, order.bom_id) if order.bom_id else active_bom(db, order.product_id)
    if not b:
        raise BizError("订单无 BOM")
    wh = default_warehouse(db)
    out = []
    for ln in b.lines:
        gross = D(ln.qty_per) * (1 + D(ln.scrap_rate)) * D(order.qty)
        iss = db.scalar(select(OrderIssue.qty).where(OrderIssue.order_id == order.id,
                                                     OrderIssue.material_id == ln.child_id))
        remain = max(gross - D(iss), ZERO)
        out.append({"material_id": ln.child_id, "code": ln.child.code, "name": ln.child.name,
                    "unit": ln.child.unit, "drawing_no": ln.child.drawing_no, "required": gross,
                    "issued": D(iss), "plan_qty": remain, "available": available(db, ln.child_id, wh.id)})
    return out


def create_pick(db: Session, user: User, order_id: int, lines: list[dict] | None = None,
                warehouse_id: int | None = None, picker: str = "") -> PickList:
    """参照 BOM 自动带出物料；lines 可只传需要调整的数量（不传则全部按计划量）。"""
    o = get_or_404(db, SalesOrder, order_id, "订单")
    if o.status not in (OrderStatus.RELEASED, OrderStatus.ASSEMBLING):
        raise BizError("订单需先下达才能领料")
    assert_not_held(db, "SALES_ORDER", o.id)
    plan = {p["material_id"]: p for p in pick_plan(db, o)}
    override = {ln["material_id"]: D(ln["qty"]) for ln in (lines or [])}
    wh = warehouse_id or default_warehouse(db).id
    pk = PickList(pick_no=next_no(db, PickList, "pick_no", "PK"), order_id=o.id, warehouse_id=wh,
                  picker=picker, created_by=user.id)
    for mid, p in plan.items():
        if lines is not None:  # 指定了明细：只领指定的物料
            if mid not in override:
                continue
            q = override[mid]
        else:  # 未指定：按 BOM 全部带出，默认领“计划量与可用量的较小值”
            q = min(p["plan_qty"], max(p["available"] - reserved_by_others(db, mid, o.id), ZERO))
        if q > 0 or (lines is None and p["plan_qty"] > 0):
            pk.lines.append(PickLine(material_id=mid, plan_qty=p["plan_qty"], qty=max(q, ZERO)))
    if not pk.lines:
        raise BizError("没有需要领用的物料")
    db.add(pk)
    db.flush()
    return pk


def post_pick(db: Session, user: User, pick_id: int) -> PickList:
    from .production import require_bom_confirmed
    db.flush()  # 保留同一事务前一步服务调用的合法变更，再刷新锁定状态
    acquire_mutex(db)  # 先互斥再锁单据，与报工/退料/完工保持相同顺序
    # 行锁：同一张领料单被重复提交/并发过账时，后到者会看到 POSTED 而被拒绝，不会重复扣库存
    pk = db.scalar(select(PickList).where(PickList.id == pick_id).with_for_update().execution_options(populate_existing=True))
    if pk is None:
        raise BizError("领料单不存在", 404)
    if pk.status != "DRAFT":
        raise BizError("领料单已过账")
    # 过账时重新核对来源单据：创建领料单之后订单可能已取消、任务已结束，旧草稿不能再扣库存
    if pk.order_id:
        so = db.get(SalesOrder, pk.order_id)
        if so is None or so.status not in (OrderStatus.RELEASED, OrderStatus.ASSEMBLING):
            raise BizError("来源订单已不在“已下达/装配中”，该领料单不能过账（请作废）")
        assert_not_held(db, "SALES_ORDER", pk.order_id)
    elif pk.work_order_id:
        wo = db.get(WorkOrder, pk.work_order_id)
        if wo is None or wo.status not in ("PLANNED", "STARTED"):
            raise BizError("来源自制任务已结束或取消，该领料单不能过账（请作废）")
        assert_not_held(db, "WORK_ORDER", pk.work_order_id)
        require_bom_confirmed(wo, "自制任务")  # 旧领料草稿也不能绕过技术确认
    elif pk.outsource_id:
        osd = db.get(OutsourceOrder, pk.outsource_id)
        if osd is None or osd.status not in ("PLANNED", "SENT", "PARTIAL"):
            raise BizError("来源外协单已结束或取消，该领料单不能过账（请作废）")
        assert_not_held(db, "OUTSOURCE", pk.outsource_id)
        require_bom_confirmed(osd, "外协单")
    pk.status = "POSTING"
    for ln in pk.lines:
        if D(ln.qty) <= 0:
            continue
        # 出库口也要尊重预占：不得领走其他订单已锁定/冻结的料（要抢料请走调料申请，审批后才能腾出）
        free = min(available(db, ln.material_id, pk.warehouse_id),
                   prot.free_to_issue(db, ln.material_id, pk.warehouse_id, order_id=pk.order_id,
                                      wo_id=pk.work_order_id, os_id=pk.outsource_id))
        if D(ln.qty) > free:
            raise BizError(f"{ln.material.code if ln.material else ln.material_id} 可领数量不足："
                           f"库存中有 {max(free, ZERO):g} 未被其他订单锁定/冻结或被任务占用，本次要领 {D(ln.qty):g}。"
                           "如需使用其他订单已占用的料，请走【调料申请】", data={"free": float(max(free, ZERO))})
        used = fifo_out(db, material_id=ln.material_id, warehouse_id=pk.warehouse_id, qty=ln.qty,
                        txn_type=TxnType.PICK_OUT, ref_type="PICK", ref_id=pk.id, order_id=pk.order_id,
                        user_id=user.id)
        ln.lots = json.dumps(used, ensure_ascii=False)
        if pk.order_id:
            oi = db.scalar(select(OrderIssue).where(OrderIssue.order_id == pk.order_id,
                                                    OrderIssue.material_id == ln.material_id).with_for_update())
            if not oi:
                oi = OrderIssue(order_id=pk.order_id, material_id=ln.material_id, qty=ZERO)
                db.add(oi)
            oi.qty = D(oi.qty) + D(ln.qty)
            # 领出的料从该订单的预占（人工锁定优先，其次冻结）里扣掉
            remain = D(ln.qty)
            for r in db.scalars(select(Reservation).where(Reservation.order_id == pk.order_id,
                                                          Reservation.material_id == ln.material_id,
                                                          Reservation.source_type == "STOCK")
                                .order_by(Reservation.locked.desc(), Reservation.id).with_for_update()).all():
                t = min(D(r.qty), remain)
                r.qty = D(r.qty) - t
                remain -= t
                if r.qty <= 0:
                    db.delete(r)
                if remain <= 0:
                    break
    pk.status, pk.posted_at = "POSTED", now()
    audit(db, user.id, "POST", "PickList", pk.id)
    return pk


def _consumed_qty(db: Session, pk: PickList, material_id: int) -> Decimal:
    """该领料单来源已经【用掉】的该物料数量（不能再退）：
    订单 → 完工后按 BOM 需求量；自制任务 → BOM 用量 × 已报工量；外协单 → 发出后按 BOM 用量 × 外协数量。"""
    if pk.order_id:
        o = db.get(SalesOrder, pk.order_id)
        if o and o.status in (OrderStatus.COMPLETED, OrderStatus.SHIPPED):
            return next((p["required"] for p in pick_plan(db, o) if p["material_id"] == material_id), ZERO)
    elif pk.work_order_id:
        w = db.get(WorkOrder, pk.work_order_id)
        if w:
            per = next((q for c, q in prot.bom_lines(db, w) if c == material_id), ZERO)
            return per * D(w.done_qty)
    elif pk.outsource_id:
        x = db.get(OutsourceOrder, pk.outsource_id)
        if x and x.status in ("SENT", "PARTIAL", "RETURNED"):  # 计划/已取消的外协单没有把料发出去
            per = next((q for c, q in prot.bom_lines(db, x) if c == material_id), ZERO)
            return per * D(x.qty)
    return ZERO


def _issued_of(db: Session, pk: PickList, material_id: int) -> Decimal:
    if pk.order_id:
        return D(db.scalar(select(OrderIssue.qty).where(OrderIssue.order_id == pk.order_id,
                                                        OrderIssue.material_id == material_id)))
    col, ref = (PickList.work_order_id, pk.work_order_id) if pk.work_order_id else (PickList.outsource_id, pk.outsource_id)
    return prot.issued_net(db, col, ref, material_id)


def return_pick(db: Session, user: User, pick_id: int, lines: list[dict], reason: str) -> PickList:
    from .production import require_bom_confirmed
    """退料：已过账领料单的部分/全部退回生产库（新批次入库），订单已领量同步减少。原领料记录保留。"""
    if not reason:
        raise BizError("退料必须填写原因")
    # 先拒绝重复行，整笔请求尚未发生库存/台账变更；不得把每行各自的可退额重复使用。
    mids = [item["material_id"] for item in lines]
    if len(mids) != len(set(mids)):
        raise BizError("退料物料不能重复，请合并同一物料的数量后提交")
    db.flush()  # 保留同一事务前一步服务调用的合法变更，再刷新锁定状态
    acquire_mutex(db)
    pk = db.scalar(select(PickList).where(PickList.id == pick_id).with_for_update().execution_options(populate_existing=True))
    if pk is None or pk.status != "POSTED":
        raise BizError("只有已过账的领料单才能退料")
    # 消耗量与已领量共用来源锁，防止另一张领料单退料或报工同时改变可退总额。
    source, ref = ((SalesOrder, pk.order_id) if pk.order_id else
                   (WorkOrder, pk.work_order_id) if pk.work_order_id else (OutsourceOrder, pk.outsource_id))
    db.scalar(select(source).where(source.id == ref).with_for_update().execution_options(populate_existing=True))
    # 可退数量要按该任务的批准 BOM 算“已消耗”：BOM 未经技术确认的旧单据先确认再退
    src_doc = db.get(source, ref)
    if pk.work_order_id:
        require_bom_confirmed(src_doc, "自制任务")
    elif pk.outsource_id:
        require_bom_confirmed(src_doc, "外协单")
    by_mat = {ln.material_id: ln for ln in pk.lines}
    for item in lines:
        ln = by_mat.get(item["material_id"])
        q = D(item["qty"])
        if ln is None:
            raise BizError("退料物料不在该领料单内")
        if q <= 0 or q > D(ln.qty) - D(ln.returned_qty):
            raise BizError(f"退料数量无效：可退 {D(ln.qty) - D(ln.returned_qty):g}")
        # 已经用于完工/报工/发外协的部分不能退：可退 = 已领未退 − 已消耗。完工后的整体撤销要走有权限的冲回流程，不能靠退料
        spare = max(_issued_of(db, pk, ln.material_id) - _consumed_qty(db, pk, ln.material_id), ZERO)
        if q > spare:
            raise BizError(f"该物料已有 {_consumed_qty(db, pk, ln.material_id):g} 用于完工/加工，只能退尾料 {spare:g}"
                           "；如需撤销已完成的产出，请走完工冲回/返工流程")
        # 退回原批次（可追溯，也让“来料批次是否被领用”的判断随退料恢复）
        lots = json.loads(ln.lots or "[]")
        left = q
        for ent in lots:
            can = D(ent["qty"]) - D(ent.get("returned", 0))
            t = min(can, left)
            if t <= 0:
                continue
            lot = db.scalar(select(StockLot).where(StockLot.id == ent["lot_id"]).with_for_update())
            if lot is None:
                continue
            lot.qty = D(lot.qty) + t
            lot.last_move_at = now()
            db.add(StockTxn(txn_type=TxnType.PICK_RETURN, material_id=ln.material_id, warehouse_id=lot.warehouse_id,
                            lot_id=lot.id, qty=t, ref_type="PICK", ref_id=pk.id, order_id=pk.order_id,
                            user_id=user.id, remark=f"退料：{reason}", created_at=now()))
            ent["returned"] = float(D(ent.get("returned", 0)) + t)
            left -= t
            if left <= 0:
                break
        if left > 0:
            raise BizError("原批次记录不足，无法退回（请联系管理员盘点调整）")
        ln.lots = json.dumps(lots, ensure_ascii=False)
        ln.returned_qty = D(ln.returned_qty) + q
        if pk.order_id:
            oi = db.scalar(select(OrderIssue).where(OrderIssue.order_id == pk.order_id,
                                                    OrderIssue.material_id == ln.material_id).with_for_update())
            if oi:
                oi.qty = max(D(oi.qty) - q, ZERO)
    audit(db, user.id, "PICK_RETURN", "PickList", pk.id, {"lines": lines, "reason": reason})
    return pk


# ------------------------------------------------------------------ 装配 / 完工


def start_assembly(db: Session, user: User, order_id: int, override_reason: str = "") -> SalesOrder:
    """开工准入：①图纸当前版本已确认（硬性）②齐套（未齐套需主管填写原因强制开工）。"""
    o = get_or_404(db, SalesOrder, order_id, "订单")
    if o.status != OrderStatus.RELEASED:
        raise BizError("订单需为“已下达”")
    assert_confirmed(db, "SALES_ORDER", o.id)
    if o.kit_status != KitStatus.READY and not override_reason:
        raise BizError(f"订单未齐套（{KitStatus.LABELS.get(o.kit_status, o.kit_status or '未计算')}），"
                       "如需强制开工请填写原因", data={"need_override": True})
    o.status = OrderStatus.ASSEMBLING
    audit(db, user.id, "START", "SalesOrder", o.id, {"kit": o.kit_status, "override": override_reason})
    return o


def complete(db: Session, user: User, order_id: int, warehouse_id: int | None = None) -> SalesOrder:
    db.flush()  # 保留同一事务前一步服务调用的合法变更，再刷新锁定状态
    acquire_mutex(db)  # 必须在读状态/核对领料之前持有，直至库存、台账、重算一起提交
    o = db.scalar(select(SalesOrder).where(SalesOrder.id == order_id).with_for_update()
                  .execution_options(populate_existing=True))  # 已预读的 ORM 对象也必须刷新为锁定后的状态
    if o is None:
        raise BizError("订单不存在", 404)
    if o.status != OrderStatus.ASSEMBLING:
        raise BizError("订单需为“装配中”")
    # 成品入库前必须已领齐 BOM 物料（已领未退 ≥ 需求），否则账面凭空多出一台整机、料还留在库里被别的订单再领
    db.flush()
    assert_not_held(db, "SALES_ORDER", o.id)  # 图纸重大变更暂停期间，成品不能转为可用库存
    short = [f"{p['code']} 需 {p['required']:g}、已领 {p['issued']:g}" for p in pick_plan(db, o) if p["plan_qty"] > 0]
    if short:
        raise BizError("BOM 物料未领齐，不能完工入库（请先领料并过账）：" + "；".join(short[:8]), data={"short": short})
    wh = warehouse_id or default_warehouse(db, WarehousePurpose.FINISHED).id
    stock_in(db, material_id=o.product_id, warehouse_id=wh, qty=o.qty, txn_type=TxnType.FINISHED_IN,
             lot_no=o.order_no, ref_type="ORDER", ref_id=o.id, order_id=o.id, user_id=user.id)
    o.status, o.progress = OrderStatus.COMPLETED, 100
    for r in db.scalars(select(Reservation).where(Reservation.order_id == o.id)).all():
        db.delete(r)
    audit(db, user.id, "COMPLETE", "SalesOrder", o.id)
    return o


# ------------------------------------------------------------------ 发货计划审批


def shipped_or_planned(db: Session, order_id: int) -> Decimal:
    v = db.scalar(select(func.coalesce(func.sum(ShipmentPlan.qty), 0)).where(
        ShipmentPlan.order_id == order_id, ShipmentPlan.status.in_(["DRAFT", "SUBMITTED", "APPROVED", "SHIPPED"])))
    return D(v)


def create_shipment(db: Session, user: User, data: dict) -> ShipmentPlan:
    # 锁订单行：并发建发货计划时，“已排发货量”的校验串行执行，不会合计超过订单量
    o = db.scalar(select(SalesOrder).where(SalesOrder.id == data["order_id"]).with_for_update())
    if o is None:
        raise BizError("订单不存在", 404)
    if o.status not in (OrderStatus.ASSEMBLING, OrderStatus.COMPLETED):
        raise BizError("订单需在装配中或已完工才能做发货计划")
    qty = D(data.get("qty") or o.qty)
    if qty <= 0 or shipped_or_planned(db, o.id) + qty > D(o.qty):
        raise BizError("发货数量超过订单剩余可发数量")
    s = ShipmentPlan(ship_no=next_no(db, ShipmentPlan, "ship_no", "SH"), order_id=o.id, qty=qty,
                     plan_date=data["plan_date"], address=data.get("address", ""), carrier=data.get("carrier", ""),
                     created_by=user.id)
    db.add(s)
    db.flush()
    return s


def shipment_action(db: Session, user: User, ship_id: int, action: str, comment: str = "") -> ShipmentPlan:
    s = db.scalar(select(ShipmentPlan).where(ShipmentPlan.id == ship_id).with_for_update())  # 防重复出库
    if s is None:
        raise BizError("发货计划不存在", 404)
    flow = {"submit": (("DRAFT", "REJECTED"), "SUBMITTED"), "approve": (("SUBMITTED",), "APPROVED"),
            "reject": (("SUBMITTED",), "REJECTED"), "ship": (("APPROVED",), "SHIPPED"),
            "cancel": (("DRAFT", "REJECTED", "SUBMITTED"), "CANCELLED")}
    if action not in flow:
        raise BizError("未知操作")
    frm, to = flow[action]
    if s.status not in frm:
        raise BizError("当前状态不能执行该操作")
    if action == "reject" and not comment:
        raise BizError("驳回请填写原因")
    if action in ("approve", "reject"):
        s.approved_by, s.approved_at, s.approve_comment = user.id, now(), comment
    if action == "ship":
        o = s.order
        if o.status not in (OrderStatus.COMPLETED, OrderStatus.SHIPPED):
            raise BizError("订单尚未完工入库，不能发货出库")
        assert_not_held(db, "SALES_ORDER", o.id)  # 图纸重大变更暂停未裁定前不得发货
        wh = default_warehouse(db, WarehousePurpose.FINISHED)
        fifo_out(db, material_id=o.product_id, warehouse_id=wh.id, qty=s.qty, txn_type=TxnType.SHIP_OUT,
                 ref_type="SHIPMENT", ref_id=s.id, order_id=o.id, customer=o.customer, user_id=user.id)
        s.shipped_at = now()
        s.status = to
        db.flush()
        shipped = db.scalar(select(func.coalesce(func.sum(ShipmentPlan.qty), 0)).where(
            ShipmentPlan.order_id == o.id, ShipmentPlan.status == "SHIPPED"))
        if D(shipped) >= D(o.qty):
            o.status = OrderStatus.SHIPPED
    s.status = to
    audit(db, user.id, action.upper(), "ShipmentPlan", s.id, comment)
    return s


def suggest_planned_start(db: Session, order: SalesOrder) -> date:
    from datetime import timedelta
    return order.due_date - timedelta(days=order.product.lead_time_days or 0)


__all__ = ["create_order", "update_order", "release", "cancel", "lock_reservation", "unlock_reservation",
           "pick_plan", "create_pick", "post_pick", "start_assembly", "complete", "create_shipment",
           "shipment_action", "WorkOrder"]
