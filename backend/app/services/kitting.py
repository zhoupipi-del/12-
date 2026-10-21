"""齐套引擎 —— 系统核心。

算法（每次全量重算，几百张订单规模毫秒级）：

1. 订单排序（冲填优先级）：
     已下达/装配中 优先于 仅录入 → 人工置顶 → 优先级(特急>急>普通>低) → 需求日 → 单号
   已下达订单的料不会被“还没下达”的订单抢走。
2. 供应池：
     现存量  = 生产物料库（WarehousePurpose.PRODUCTION）批次合计。售后备件库独立分账，不参与。
     在途    = 采购未到量（按承诺交期）+ 外协未回量（按预计回厂）+ 自制任务未完工量（按计划完工）
               + 已到货待检量（按明天可用）
   人工锁定的预占先从现存量中扣出，专属该订单。
3. 按顺序为每张订单展开 BOM（多级）：
     需求 = 单位用量 ×(1+损耗) × 订单数量 − 已领料
     先用库存（= 冲填预占）→ 再用在途（按到货日先后）→ 仍不足：
       有下级 BOM 的自制/外协件 → 缺口向下级展开（MRP 逐层净算）
       否则 → 缺料
4. 行状态：
     READY         全部由库存满足
     INCOMING_OK   用到在途，且 可用日 ≤ 需求日
                   （可用日 = 预计到货日 + 物料的检验/转运天数 inbound_days；自制件为计划完工日）
     INCOMING_LATE 用到在途，但可用日晚于需求日 → 交期风险
     SHORT         没有任何供应 → 需补单
   订单状态取最差行；订单预计齐套日 = 各行最晚可用日。
   “缓冲天数”（默认 2）不是到货要求，而是【开工前检查节点】：距需求日 ≤ 缓冲天数
   且仍未齐套的订单，打上 check_due 标记并出预警，提醒计划员去冲填/催料。
5. 冻结与调料：
   已分配给订单的库存预占会被“冻结”：重算只把【空闲量】按优先级分给还有缺口的订单，
   不会把已承诺给他单的料抢走，计划不抖动。要动他单的冻结量，走调料申请（reallocation）：先预演影响、再审批。
6. 供需依赖：
   在制/待开工的自制任务、本厂供料的外协单，其【未领原料】先从供给里扣出（原料不再当作空闲库存），
   原料不够则该任务的产出可用日顺延；订单只按“成品件”引用其产出，不重复展开下级原料。
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (Arrival, Bom, KitLine, KitStatus, Material, OrderIssue, OrderStatus,
                      OutsourceOrder, PickLine, PickList, PurchaseOrderLine, Reservation, SalesOrder,
                      StockLot, SysMutex, Warehouse, WarehousePurpose, WorkOrder, ZERO, now)
from .common import D

FAR_FUTURE = date(2099, 12, 31)


# ------------------------------------------------------------------ 数据结构


@dataclass
class Supply:
    type: str          # PO / OUTSOURCE / WO / ARRIVAL
    id: int
    qty: Decimal
    date: date
    confirmed: bool = True  # 交期是否已确认（供应商回复/计划完工）
    order_id: int | None = None  # 专属某订单（按单采购/按单加工），优先给该订单
    arrive: date | None = None   # 预计到货/完工日（date 为可用日 = arrive + 检验转运天数）


@dataclass
class OrderIn:
    id: int
    order_no: str
    product_id: int
    bom_material_id: int
    bom_override: list[tuple[int, Decimal]] | None  # 订单锁定的 BOM 版本
    qty: Decimal
    need_date: date
    sort_key: tuple
    bom_tree: dict[int, list[tuple[int, Decimal]]] | None = None  # 下达时冻结的整棵 BOM；None = 老订单，按当前生效 BOM


@dataclass
class ProdDemand:
    """在制自制任务 / 本厂供料外协单 尚未领用的原料需求。"""
    kind: str                       # WO / OUTSOURCE
    id: int
    label: str
    output_id: int                  # 产出物料
    items: list[tuple[int, Decimal]]
    started: bool
    planned: date
    order_id: int | None = None
    # 关联整机订单的冲填顺序。让急单优先级贯通到自制/本厂供料外协的原料竞争。
    order_sort: tuple = field(default_factory=tuple)


@dataclass
class Snapshot:
    today: date
    buffer_days: int
    orders: list[OrderIn]
    boms: dict[int, list[tuple[int, Decimal]]]            # 物料 → [(子件, 单位用量含损耗)]
    lead_time: dict[int, int]
    on_hand: dict[int, Decimal]
    incoming: dict[int, list[Supply]]
    issued: dict[tuple[int, int], Decimal]                # (订单, 物料) → 已领
    locked: list[tuple[int, int, Decimal, int]]           # (订单, 物料, 数量, 预占id)
    frozen: list[tuple[int, int, Decimal]] = field(default_factory=list)  # (订单, 物料, 数量) 已分配、冻结的库存预占
    prod_demands: list[ProdDemand] = field(default_factory=list)


@dataclass
class LineOut:
    order_id: int
    material_id: int
    parent_id: int | None
    level: int
    required: Decimal
    issued: Decimal = ZERO
    from_stock: Decimal = ZERO
    from_incoming: Decimal = ZERO
    from_children: Decimal = ZERO
    short_qty: Decimal = ZERO
    need_date: date | None = None
    expected_date: date | None = None
    status: str = KitStatus.READY
    supplies: list[dict] = field(default_factory=list)
    exploded: bool = False  # 缺口已向下级展开（汇总齐套率时不重复计算）


@dataclass
class OrderOut:
    order_id: int
    status: str
    color: str
    kit_rate: Decimal
    ready_date: date | None
    ready_within_buffer: bool
    lines: list[LineOut]
    check_due: bool = False  # 已到开工前检查节点（距需求日 ≤ 缓冲天数）仍未齐套


@dataclass
class KitResult:
    orders: dict[int, OrderOut]
    reservations: list[dict]  # 新的非锁定预占
    shortages: dict[int, dict]  # 物料 → {qty, need_date, orders:[...]}
    production_stock: dict[tuple[str, int, int], Decimal] = field(default_factory=dict)  # 实际分配的现存量，不能用全部需求代替


# ------------------------------------------------------------------ 纯计算（无数据库，便于单测）


def compute(snap: Snapshot) -> KitResult:
    """两遍计算：第一遍用现有冻结预占；若有订单实际用不完冻结量（需求减少/已领料），
    释放多余部分后再算一遍，让空闲量能分给还有缺口的订单。"""
    res, used = _compute_once(snap)
    before = sorted((o, m, q) for o, m, q in snap.frozen)
    after = sorted((o, m, q) for (o, m), q in used.items() if q > 0)
    if before != after:
        from dataclasses import replace
        res, _ = _compute_once(replace(snap, frozen=[(o, m, q) for o, m, q in after]))
    return res


def _compute_once(snap: Snapshot) -> tuple[KitResult, dict[tuple[int, int], Decimal]]:
    pool = dict(snap.on_hand)
    incoming = {m: sorted((Supply(s.type, s.id, s.qty, s.date, s.confirmed, s.order_id, s.arrive) for s in lst),
                          key=lambda s: (s.date, s.type, s.id))
                for m, lst in snap.incoming.items()}
    active_orders = {o.id for o in snap.orders}
    sort_of = {o.id: o.sort_key for o in snap.orders}
    reservations: list[dict] = []
    shortages: dict[int, dict] = {}
    results: dict[int, OrderOut] = {}
    production_stock: dict[tuple[str, int, int], Decimal] = defaultdict(lambda: ZERO)

    # (A) 人工锁定预占：专属订单，最先扣出
    locked_pool: dict[tuple[int, int], Decimal] = defaultdict(lambda: ZERO)
    for oid, mid, qty, _rid in snap.locked:
        take = min(qty, max(pool.get(mid, ZERO), ZERO))
        pool[mid] = pool.get(mid, ZERO) - take
        locked_pool[(oid, mid)] += take

    def add_shortage(mid: int, qty: Decimal, need: date, oid: int | None, label: str) -> None:
        sh = shortages.setdefault(mid, {"qty": ZERO, "need_date": need, "orders": []})
        sh["qty"] += qty
        sh["need_date"] = min(sh["need_date"], need)
        sh["orders"].append({"order_id": oid, "order_no": label, "qty": qty})

    # (B) 在制任务的未领原料：先扣出，不再当作空闲库存
    def claim_prod(d: ProdDemand) -> None:
        ready, short = snap.today, False
        for mid, qty in d.items:
            remain = qty
            t = min(max(pool.get(mid, ZERO), ZERO), remain)
            if t > 0:
                pool[mid] = pool.get(mid, ZERO) - t
                production_stock[(d.kind, d.id, mid)] += t
                remain -= t
            for s in incoming.get(mid, []):
                if remain <= 0:
                    break
                if s.qty <= 0 or (s.order_id is not None and s.order_id != d.order_id):
                    continue
                t = min(s.qty, remain)
                s.qty -= t
                remain -= t
                ready = max(ready, s.date)
            if remain > 0:
                short = True
                add_shortage(mid, remain, d.planned, None, d.label)
        for s in incoming.get(d.output_id, []):
            if s.type == d.kind and s.id == d.id:
                s.date = FAR_FUTURE if short else max(s.date, ready)
                s.confirmed = s.confirmed and not short

    ordered = sorted(snap.prod_demands, key=lambda d: (
        not d.started,
        d.order_sort or (1, 9999, 9, d.planned, d.label),
        d.planned,
        d.kind,
        d.id,
    ))
    for d in ordered:
        if d.started:
            claim_prod(d)

    # (C) 冻结的库存预占：按订单优先级，库存不够时低优先级先让出
    frozen_pool: dict[tuple[int, int], Decimal] = defaultdict(lambda: ZERO)
    for oid, mid, qty in sorted(snap.frozen, key=lambda f: sort_of.get(f[0], (9, 9, 9, FAR_FUTURE, ""))):
        if oid not in active_orders:
            continue
        take = min(qty, max(pool.get(mid, ZERO), ZERO))
        pool[mid] = pool.get(mid, ZERO) - take
        frozen_pool[(oid, mid)] += take
    frozen_used: dict[tuple[int, int], Decimal] = defaultdict(lambda: ZERO)

    for d in ordered:
        if not d.started:
            claim_prod(d)

    def satisfy(order: OrderIn, mid: int, qty: Decimal, need: date, level: int,
                parent: int | None, lines: list[LineOut], path: tuple = ()) -> LineOut:
        ln = LineOut(order.id, mid, parent, level, qty, need_date=need)
        lines.append(ln)
        remain = qty
        latest: date | None = None
        # (1) 人工锁定预占
        lk = locked_pool.get((order.id, mid), ZERO)
        if lk > 0 and remain > 0:
            t = min(lk, remain)
            locked_pool[(order.id, mid)] = lk - t
            ln.from_stock += t
            remain -= t
            ln.supplies.append({"type": "LOCKED", "qty": t})
        # (2a) 已冻结给本订单的库存
        fz = frozen_pool.get((order.id, mid), ZERO)
        if fz > 0 and remain > 0:
            t = min(fz, remain)
            frozen_pool[(order.id, mid)] = fz - t
            frozen_used[(order.id, mid)] += t
            ln.from_stock += t
            remain -= t
            reservations.append({"order_id": order.id, "material_id": mid, "qty": t,
                                 "source_type": "STOCK", "source_id": None, "expected_date": None})
            ln.supplies.append({"type": "STOCK", "qty": t})
        # (2b) 空闲库存（冲填）：只分配没被冻结的部分
        avail = pool.get(mid, ZERO)
        if avail > 0 and remain > 0:
            t = min(avail, remain)
            pool[mid] = avail - t
            frozen_used[(order.id, mid)] += t
            ln.from_stock += t
            remain -= t
            reservations.append({"order_id": order.id, "material_id": mid, "qty": t,
                                 "source_type": "STOCK", "source_id": None, "expected_date": None})
            ln.supplies.append({"type": "STOCK", "qty": t})
        # (3) 在途：先用专属本订单的，再用公共的（按可用日先后）；专属其他在产订单的不动
        mine = [s for s in incoming.get(mid, []) if s.order_id == order.id]
        common = [s for s in incoming.get(mid, []) if s.order_id is None or s.order_id not in active_orders]
        for s in mine + common:
            if remain <= 0:
                break
            if s.qty <= 0:
                continue
            t = min(s.qty, remain)
            s.qty -= t
            remain -= t
            ln.from_incoming += t
            latest = s.date if latest is None or s.date > latest else latest
            reservations.append({"order_id": order.id, "material_id": mid, "qty": t,
                                 "source_type": s.type, "source_id": s.id, "expected_date": s.date})
            ln.supplies.append({"type": s.type, "id": s.id, "qty": t, "date": s.date.isoformat(),
                                "arrive": (s.arrive or s.date).isoformat(), "confirmed": s.confirmed})
        # (4) 有下级 BOM → 缺口向下展开
        worst_child = KitStatus.READY
        sub = order.bom_tree.get(mid, []) if order.bom_tree is not None else snap.boms.get(mid, [])
        if remain > 0 and sub and mid not in path and level < 10:
            lt = snap.lead_time.get(mid, 0)
            child_need = need - timedelta(days=lt)
            child_latest: date | None = None
            child_short = False
            for cid, per in sub:
                c = satisfy(order, cid, per * remain, child_need, level + 1, mid, lines, path + (mid,))
                if KitStatus.RANK[c.status] > KitStatus.RANK[worst_child]:
                    worst_child = c.status
                if c.status == KitStatus.SHORT:
                    child_short = True
                cd = c.expected_date or (FAR_FUTURE if c.status == KitStatus.INCOMING_LATE else snap.today)
                child_latest = cd if child_latest is None or cd > child_latest else child_latest
            ln.from_children = remain
            ln.exploded = True
            remain = ZERO
            if not child_short:
                made = (child_latest or snap.today) + timedelta(days=lt)
                latest = made if latest is None or made > latest else latest
        ln.short_qty = remain
        # 状态：可用日 ≤ 需求日 才算按时（缓冲天数不再是到货要求）
        if ln.short_qty > 0 or worst_child == KitStatus.SHORT:
            ln.status = KitStatus.SHORT
            ln.expected_date = None
        elif ln.from_incoming > 0 or ln.from_children > 0:
            exp = latest or snap.today
            if exp >= FAR_FUTURE:  # 上游原料不足，无法确定可用日
                ln.expected_date, ln.status = None, KitStatus.INCOMING_LATE
            else:
                ln.expected_date = exp
                late = exp > need or worst_child == KitStatus.INCOMING_LATE
                ln.status = KitStatus.INCOMING_LATE if late else KitStatus.INCOMING_OK
        else:
            ln.status = KitStatus.READY
            ln.expected_date = snap.today
        if ln.short_qty > 0:
            add_shortage(mid, ln.short_qty, need, order.id, order.order_no)
        return ln

    for order in sorted(snap.orders, key=lambda o: o.sort_key):
        lines: list[LineOut] = []
        comps = order.bom_override if order.bom_override is not None else snap.boms.get(order.bom_material_id, [])
        if order.bom_tree is not None:
            comps = order.bom_tree.get(order.product_id, comps)
        for cid, per in comps:
            gross = per * order.qty
            issued = snap.issued.get((order.id, cid), ZERO)
            net = max(gross - issued, ZERO)
            ln = satisfy(order, cid, net, order.need_date, 1, order.product_id, lines, (order.product_id,))
            ln.issued = min(issued, gross)
        # 本订单冻结量用不完的部分还回空闲池（后面的订单可用；下一遍计算会让所有订单都能用到）
        for (oid, mid), left in list(frozen_pool.items()):
            if oid == order.id and left > 0:
                pool[mid] = pool.get(mid, ZERO) + left
                frozen_pool[(oid, mid)] = ZERO
        results[order.id] = _summarize(order, lines, snap)

    return KitResult(results, reservations, shortages, dict(production_stock)), dict(frozen_used)


def _summarize(order: OrderIn, lines: list[LineOut], snap: Snapshot) -> OrderOut:
    if not lines:
        return OrderOut(order.id, KitStatus.SHORT, "RED", ZERO, None, False, lines)
    top = [ln for ln in lines if ln.level == 1]
    worst = max(top, key=lambda ln: KitStatus.RANK[ln.status]).status
    # 齐套率 = 已齐品项数 / 总品项数（按品项计，避免螺栓等大数量物料扭曲比例）
    ok = sum(1 for ln in top if ln.status == KitStatus.READY)
    rate = (Decimal(ok) / Decimal(len(top))).quantize(Decimal("0.0001")) if top else Decimal("1")
    if worst == KitStatus.SHORT or any(ln.expected_date is None for ln in top):
        ready = None
    else:
        ready = max(ln.expected_date for ln in top)
    within = ready is not None and ready <= snap.today + timedelta(days=snap.buffer_days)
    check_due = worst != KitStatus.READY and order.need_date - timedelta(days=snap.buffer_days) <= snap.today
    return OrderOut(order.id, worst, KitStatus.COLOR[worst], rate, ready, within, lines, check_due)


# ------------------------------------------------------------------ 从数据库构建快照


def _bom_lines(bom: Bom) -> list[tuple[int, Decimal]]:
    return [(ln.child_id, D(ln.qty_per) * (1 + D(ln.scrap_rate))) for ln in bom.lines]


def order_sort_key(o: SalesOrder, need: date) -> tuple:
    committed = 0 if o.status in (OrderStatus.RELEASED, OrderStatus.ASSEMBLING) else 1
    pin = o.priority_pin if o.priority_pin and o.priority_pin > 0 else 9999
    return (committed, pin, o.priority, need, o.order_no)


def order_need_date(o: SalesOrder, lead_time: dict[int, int]) -> date:
    if o.planned_start:
        return o.planned_start
    return o.due_date - timedelta(days=lead_time.get(o.product_id, 0))


def _doc_bom(db: Session, doc, boms: dict[int, list[tuple[int, Decimal]]]) -> list[tuple[int, Decimal]]:
    """自制任务/外协单用建单时冻结的批准 BOM；老单据没有快照才用当前生效 BOM。"""
    if doc.bom_snapshot:
        return [(int(c), Decimal(p)) for c, p in json.loads(doc.bom_snapshot)]
    return boms.get(doc.material_id, [])


def _load_prod_demands(db: Session, boms: dict[int, list[tuple[int, Decimal]]], today: date,
                       order_keys: dict[int, tuple]) -> list[ProdDemand]:
    """在制自制任务 / 本厂供料外协单：BOM 原料需求 − 已过账领料。"""
    iss_wo: dict[tuple[int, int], Decimal] = defaultdict(lambda: ZERO)
    iss_os: dict[tuple[int, int], Decimal] = defaultdict(lambda: ZERO)
    rows = db.execute(select(PickList.work_order_id, PickList.outsource_id, PickLine.material_id, func.sum(PickLine.qty - PickLine.returned_qty))
                      .join(PickLine, PickLine.pick_id == PickList.id)
                      .where(PickList.status == "POSTED")
                      .where((PickList.work_order_id.is_not(None)) | (PickList.outsource_id.is_not(None)))
                      .group_by(PickList.work_order_id, PickList.outsource_id, PickLine.material_id)).all()
    for wid, oid, mid, q in rows:
        if wid:
            iss_wo[(wid, mid)] += D(q)
        if oid:
            iss_os[(oid, mid)] += D(q)
    out: list[ProdDemand] = []
    for wo in db.scalars(select(WorkOrder).where(WorkOrder.status.in_(["PLANNED", "STARTED"]))).all():
        if D(wo.qty) - D(wo.done_qty) <= 0:
            continue
        items = [(cid, max(per * D(wo.qty) - iss_wo.get((wo.id, cid), ZERO), ZERO)) for cid, per in _doc_bom(db, wo, boms)]
        items = [(c, q) for c, q in items if q > 0]
        if items:
            out.append(ProdDemand("WO", wo.id, wo.wo_no, wo.material_id, items, wo.status == "STARTED",
                                  wo.planned_finish or today, wo.order_id, order_keys.get(wo.order_id, ())))
    for o in db.scalars(select(OutsourceOrder).where(OutsourceOrder.status.in_(["PLANNED", "SENT", "PARTIAL"]),
                                                      OutsourceOrder.supply_mode == "CONSIGNED")).all():
        items = [(cid, max(per * D(o.qty) - iss_os.get((o.id, cid), ZERO), ZERO)) for cid, per in _doc_bom(db, o, boms)]
        items = [(c, q) for c, q in items if q > 0]
        if items:
            out.append(ProdDemand("OUTSOURCE", o.id, o.os_no, o.material_id, items, o.status != "PLANNED",
                                  o.expected_return or today, o.order_id, order_keys.get(o.order_id, ())))
    return out


def acquire_mutex(db: Session, name: str = "kitting") -> None:
    """串行化全局写（重算/调料审批）：锁住互斥行直到事务结束。SQLite 本身串行写，天然满足。"""
    row = db.scalar(select(SysMutex).where(SysMutex.name == name).with_for_update())
    if row is None:
        try:
            with db.begin_nested():
                db.add(SysMutex(name=name, touched_at=now()))
                db.flush()
        except Exception:  # 并发创建：另一个事务已建好
            pass
        row = db.scalar(select(SysMutex).where(SysMutex.name == name).with_for_update())
    if row is not None:
        row.touched_at = now()


def load_snapshot(db: Session, today: date | None = None, pin_override: dict[int, int] | None = None) -> Snapshot:
    today = today or date.today()
    mats = db.scalars(select(Material)).all()
    lead = {m.id: m.lead_time_days or 0 for m in mats}

    boms: dict[int, list[tuple[int, Decimal]]] = {}
    for b in db.scalars(select(Bom).where(Bom.status == "ACTIVE")).all():
        boms[b.material_id] = _bom_lines(b)

    orders: list[OrderIn] = []
    for o in db.scalars(select(SalesOrder).where(SalesOrder.status.in_(OrderStatus.ACTIVE_FOR_KIT))).all():
        override = None
        if o.bom_id:
            b = db.get(Bom, o.bom_id)
            if b:
                override = _bom_lines(b)
        need = order_need_date(o, lead)
        if pin_override and o.id in pin_override:
            o_pin = pin_override[o.id]
            key = list(order_sort_key(o, need))
            key[0], key[1] = 0, o_pin
            key = tuple(key)
        else:
            key = order_sort_key(o, need)
        tree = None
        if o.bom_snapshot:
            tree = {int(k): [(int(c), Decimal(p)) for c, p in v] for k, v in json.loads(o.bom_snapshot).items()}
        orders.append(OrderIn(o.id, o.order_no, o.product_id, o.product_id, override, D(o.qty), need, key, tree))

    on_hand: dict[int, Decimal] = defaultdict(lambda: ZERO)
    rows = db.execute(select(StockLot.material_id, StockLot.qty).join(Warehouse)
                      .where(Warehouse.purpose == WarehousePurpose.PRODUCTION, StockLot.qty > 0)).all()
    for mid, q in rows:
        on_hand[mid] += D(q)

    # 在途供应的“可用日” = 预计到货日 + 物料的检验/转运天数；自制件完工直接入库，不加
    ib = {m.id: max(m.inbound_days or 0, 0) for m in mats}
    incoming: dict[int, list[Supply]] = defaultdict(list)
    for ln in db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.status.in_(["OPEN", "PARTIAL"]))).all():
        if ln.po.status != "APPROVED":
            continue
        q = ln.open_qty
        if q > 0:
            d = max(ln.promised_date or ln.need_date or today + timedelta(days=lead.get(ln.material_id, 7)), today)
            incoming[ln.material_id].append(Supply("PO", ln.id, q, d + timedelta(days=ib[ln.material_id]),
                                                   ln.promised_date is not None, ln.order_id, d))
    for os_ in db.scalars(select(OutsourceOrder).where(OutsourceOrder.status.in_(["PLANNED", "SENT", "PARTIAL"]))).all():
        q = os_.open_qty
        if q > 0:
            d = max(os_.expected_return or today + timedelta(days=lead.get(os_.material_id, 7)), today)
            incoming[os_.material_id].append(Supply("OUTSOURCE", os_.id, q, d + timedelta(days=ib[os_.material_id]),
                                                    os_.status != "PLANNED", os_.order_id, d))
    for wo in db.scalars(select(WorkOrder).where(WorkOrder.status.in_(["PLANNED", "STARTED"]))).all():
        q = max(D(wo.qty) - D(wo.done_qty), ZERO)
        if q > 0:
            d = max(wo.planned_finish or today + timedelta(days=lead.get(wo.material_id, 7)), today)
            incoming[wo.material_id].append(Supply("WO", wo.id, q, d, wo.planned_finish is not None, wo.order_id, d))
    # 已到货待检：检验、转运完成后才可用
    for ar in db.scalars(select(Arrival).where(Arrival.status.in_(["PENDING", "INSPECTING"]))).all():
        d = today + timedelta(days=ib.get(ar.material_id, 1))
        incoming[ar.material_id].append(Supply("ARRIVAL", ar.id, D(ar.qty), d, True, None, today))

    issued = {(r.order_id, r.material_id): D(r.qty) for r in db.scalars(select(OrderIssue)).all()}
    locked = [(r.order_id, r.material_id, D(r.qty), r.id)
              for r in db.scalars(select(Reservation).where(Reservation.locked.is_(True))).all()]
    active_ids = {o.id for o in orders}
    frozen = [(r.order_id, r.material_id, D(r.qty))
              for r in db.scalars(select(Reservation).where(Reservation.locked.is_(False),
                                                            Reservation.source_type == "STOCK")).all()
              if r.order_id in active_ids]
    prod = _load_prod_demands(db, boms, today, {o.id: o.sort_key for o in orders})
    return Snapshot(today, settings.kit_buffer_days, orders, boms, lead, dict(on_hand), dict(incoming), issued, locked, frozen, prod)


# ------------------------------------------------------------------ 持久化


def recalc(db: Session, today: date | None = None, commit: bool = True) -> KitResult:
    """重算并写回：KitLine 全量替换、非锁定预占全量替换、订单冗余字段更新、生成缺料预警。"""
    db.flush()  # 会话不自动 flush：先刷出未落库的写入，快照才是最新状态
    acquire_mutex(db)
    snap = load_snapshot(db, today)
    res = compute(snap)
    ts = now()
    db.execute(delete(KitLine))
    db.execute(delete(Reservation).where(Reservation.locked.is_(False)))
    for r in res.reservations:
        db.add(Reservation(**r))
    active_ids = set()
    for oid, out in res.orders.items():
        active_ids.add(oid)
        so = db.get(SalesOrder, oid)
        so.kit_status, so.kit_color, so.kit_rate = out.status, out.color, out.kit_rate
        so.kit_ready_date, so.kit_calc_at = out.ready_date, ts
        for ln in out.lines:
            db.add(KitLine(order_id=oid, material_id=ln.material_id, parent_id=ln.parent_id, level=ln.level,
                           required=ln.required, issued=ln.issued, from_stock=ln.from_stock,
                           from_incoming=ln.from_incoming, from_children=ln.from_children,
                           short_qty=ln.short_qty, need_date=ln.need_date, expected_date=ln.expected_date,
                           status=ln.status, supply_detail=json.dumps(ln.supplies, default=str)))
    db.flush()
    from .alerts import sync_kit_alerts  # 避免循环导入
    sync_kit_alerts(db, res)
    if commit:
        db.commit()
    return res


def simulate_pin(db: Session, order_id: int) -> list[dict]:
    """模拟：把某订单提到最高优先级，看【纯优先级】会影响哪些订单（不落库）。
    两边都忽略现有冻结，只比较优先级变化本身；真正动他单已冻结的料要走调料申请（reallocation.preview）。"""
    from dataclasses import replace
    base = compute(replace(load_snapshot(db), frozen=[]))
    sim = compute(replace(load_snapshot(db, pin_override={order_id: 0}), frozen=[]))
    out = []
    for oid, b in base.orders.items():
        s = sim.orders.get(oid)
        if s and (oid == order_id or s.status != b.status or s.ready_date != b.ready_date or s.kit_rate != b.kit_rate):
            so = db.get(SalesOrder, oid)
            out.append({"order_id": oid, "order_no": so.order_no, "customer": so.customer,
                        "before": b.status, "after": s.status,
                        "before_ready": b.ready_date, "after_ready": s.ready_date,
                        "before_rate": b.kit_rate, "after_rate": s.kit_rate, "is_target": oid == order_id})
    out.sort(key=lambda x: (not x["is_target"], x["order_no"]))
    return out


def shortage_summary(db: Session, res: KitResult | None = None) -> list[dict]:
    """按物料汇总缺口（供生成请购/自制/外协建议）。"""
    res = res or compute(load_snapshot(db))
    out = []
    for mid, s in res.shortages.items():
        m = db.get(Material, mid)
        out.append({"material_id": mid, "code": m.code, "name": m.name, "type": m.type,
                    "drawing_no": m.drawing_no, "short_qty": s["qty"], "need_date": s["need_date"],
                    "orders": s["orders"]})
    out.sort(key=lambda x: x["need_date"])
    return out
