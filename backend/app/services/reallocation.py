"""调料申请：把已冻结给其他订单的库存调给急单。

重算只分配空闲量、不抢别人已冻结的料（计划不抖动）。急单确实要动他单的冻结量时：
  ① 预演（preview）：算出从哪些订单调、调多少，以及每张受影响订单的状态/齐套日变化 —— 不落库
  ② 申请（create_request）：保存预演结果，等审批
  ③ 审批（approve）：加互斥锁 → 重新校验冻结量没变 → 转移预占 → 重算；已领用的料不在预占里，天然受保护
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import replace
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import BizError
from ..models import (Material, OrderStatus, ReallocationRequest, Reservation, Role, SalesOrder, User, now)
from . import kitting
from .common import D, audit, dumps, get_or_404, next_no


def _holdings(res: kitting.KitResult, material_id: int) -> dict[int, Decimal]:
    h: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for r in res.reservations:
        if r["material_id"] == material_id and r["source_type"] == "STOCK":
            h[r["order_id"]] += D(r["qty"])
    return h


def _make_plan(snap: kitting.Snapshot, base: kitting.KitResult, to_order_id: int, material_id: int,
               qty: Decimal, from_order_ids: list[int] | None) -> list[dict]:
    hold = _holdings(base, material_id)
    key = {o.id: o.sort_key for o in snap.orders}
    names = {o.id: o.order_no for o in snap.orders}
    if to_order_id not in key:
        raise BizError("目标订单不在生产中")
    # 默认从“优先级比目标低”的订单里取，最低优先级的先让；也可以人工指定来源订单
    if from_order_ids:
        cands = [oid for oid in from_order_ids if oid != to_order_id and hold.get(oid, 0) > 0]
    else:
        cands = [oid for oid in hold if oid != to_order_id and key.get(oid, (9,)) > key[to_order_id]]
    cands.sort(key=lambda oid: key.get(oid, (9,)), reverse=True)
    plan, remain = [], D(qty)
    for oid in cands:
        if remain <= 0:
            break
        t = min(hold[oid], remain)
        plan.append({"from_order_id": oid, "order_no": names.get(oid, str(oid)), "qty": t})
        remain -= t
    if remain > 0:
        raise BizError(f"可调数量不足：还差 {remain:g}（其他订单冻结的该物料不够，或优先级不低于目标订单）")
    return plan


def _impacts(db: Session, snap: kitting.Snapshot, base: kitting.KitResult, to_order_id: int,
             material_id: int, plan: list[dict], qty: Decimal) -> list[dict]:
    frozen: dict[tuple[int, int], Decimal] = defaultdict(lambda: Decimal("0"))
    for r in base.reservations:  # 以基线实际占用为准
        if r["source_type"] == "STOCK":
            frozen[(r["order_id"], r["material_id"])] += D(r["qty"])
    for p in plan:
        frozen[(p["from_order_id"], material_id)] -= D(p["qty"])
    frozen[(to_order_id, material_id)] += D(qty)
    sim = kitting.compute(replace(snap, frozen=[(o, m, q) for (o, m), q in frozen.items() if q > 0]))
    out = []
    for oid, b in base.orders.items():
        s = sim.orders.get(oid)
        if s and (oid == to_order_id or s.status != b.status or s.ready_date != b.ready_date or s.kit_rate != b.kit_rate):
            so = db.get(SalesOrder, oid)
            out.append({"order_id": oid, "order_no": so.order_no, "customer": so.customer,
                        "before": b.status, "after": s.status,
                        "before_ready": b.ready_date, "after_ready": s.ready_date,
                        "before_rate": b.kit_rate, "after_rate": s.kit_rate,
                        "is_target": oid == to_order_id, "is_donor": any(p["from_order_id"] == oid for p in plan)})
    out.sort(key=lambda x: (not x["is_target"], x["order_no"]))
    return out


def preview(db: Session, to_order_id: int, material_id: int, qty: Decimal,
            from_order_ids: list[int] | None = None) -> dict:
    qty = D(qty)
    if qty <= 0:
        raise BizError("调料数量必须大于 0")
    snap = kitting.load_snapshot(db)
    base = kitting.compute(snap)
    plan = _make_plan(snap, base, to_order_id, material_id, qty, from_order_ids)
    imp = _impacts(db, snap, base, to_order_id, material_id, plan, qty)
    m = db.get(Material, material_id)
    return {"to_order_id": to_order_id, "material_id": material_id, "material": f"{m.code} {m.name}",
            "qty": qty, "plan": plan, "impacts": imp,
            "affected_orders": [i["order_no"] for i in imp if not i["is_target"]]}


def create_request(db: Session, user: User, to_order_id: int, material_id: int, qty: Decimal, reason: str,
                   from_order_ids: list[int] | None = None) -> ReallocationRequest:
    if not reason.strip():
        raise BizError("请填写调料原因")
    pv = preview(db, to_order_id, material_id, qty, from_order_ids)
    r = ReallocationRequest(req_no=next_no(db, ReallocationRequest, "req_no", "RA"), to_order_id=to_order_id,
                            material_id=material_id, qty=D(qty), reason=reason.strip()[:300],
                            plan=dumps(pv["plan"]), impacts=dumps(pv["impacts"]), requested_by=user.id)
    db.add(r)
    db.flush()
    audit(db, user.id, "REALLOC_REQUEST", "ReallocationRequest", r.id,
          {"to": to_order_id, "material": material_id, "qty": qty, "affected": pv["affected_orders"]})
    return r


def _shift(db: Session, order_id: int, material_id: int, qty: Decimal) -> None:
    """从某订单的（非人工锁定）STOCK 预占里扣掉 qty。"""
    remain = D(qty)
    rows = db.scalars(select(Reservation).where(Reservation.order_id == order_id, Reservation.material_id == material_id,
                                                Reservation.source_type == "STOCK", Reservation.locked.is_(False))
                      .order_by(Reservation.id).with_for_update()).all()
    for r in rows:
        t = min(D(r.qty), remain)
        r.qty = D(r.qty) - t
        remain -= t
        if r.qty <= 0:
            db.delete(r)
        if remain <= 0:
            break
    if remain > 0:
        raise BizError("来源订单的冻结量已变化，请重新预演后再申请")


def approve(db: Session, user: User, req_id: int, comment: str = "") -> ReallocationRequest:
    kitting.acquire_mutex(db)
    r = db.scalar(select(ReallocationRequest).where(ReallocationRequest.id == req_id).with_for_update())
    if r is None:
        raise BizError("调料申请不存在", 404)
    if r.status != "PENDING":
        raise BizError(f"申请已处理（{r.status}）")
    if r.requested_by == user.id and Role.ADMIN not in (user.roles or ""):
        raise BizError("申请人不能审批自己的调料申请")
    # 先把当前分配结果落库（保证预占行 = 基线），再校验：冻结量变了就作废，要求重新预演
    kitting.recalc(db, commit=False)
    snap = kitting.load_snapshot(db)
    base = kitting.compute(snap)
    hold = _holdings(base, r.material_id)
    plan = json.loads(r.plan)
    for p in plan:
        if hold.get(p["from_order_id"], 0) < D(p["qty"]):
            r.status = "EXPIRED"
            r.decided_by, r.decided_at, r.decision_comment = user.id, now(), "冻结量已变化，申请作废"
            db.flush()
            raise BizError("来源订单的冻结量已变化，申请已作废，请重新预演", data={"expired": True})
    # 先把基线实际占用落成预占行（重算前的冻结可能含空闲分配），再转移
    for p in plan:
        _shift(db, p["from_order_id"], r.material_id, D(p["qty"]))
    db.add(Reservation(order_id=r.to_order_id, material_id=r.material_id, qty=D(r.qty), source_type="STOCK",
                       locked=False))
    r.status, r.decided_by, r.decided_at, r.decision_comment = "APPROVED", user.id, now(), comment[:300]
    audit(db, user.id, "REALLOC_APPROVE", "ReallocationRequest", r.id,
          {"plan": plan, "affected": [i["order_no"] for i in json.loads(r.impacts) if not i.get("is_target")]})
    db.flush()
    kitting.recalc(db, commit=False)
    return r


def reject(db: Session, user: User, req_id: int, comment: str) -> ReallocationRequest:
    r = db.scalar(select(ReallocationRequest).where(ReallocationRequest.id == req_id).with_for_update())
    if r is None:
        raise BizError("调料申请不存在", 404)
    if r.status != "PENDING":
        raise BizError(f"申请已处理（{r.status}）")
    r.status, r.decided_by, r.decided_at, r.decision_comment = "REJECTED", user.id, now(), comment[:300]
    audit(db, user.id, "REALLOC_REJECT", "ReallocationRequest", r.id, comment)
    return r
