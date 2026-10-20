"""评审要求的 7 个关键场景（服务层，真线程并发）。

同一套测试既跑 SQLite（BEGIN IMMEDIATE 串行写），也要在 MySQL 8（行锁 + FOR UPDATE）上跑：
    DATABASE_URL=mysql+pymysql://... python -m pytest tests/test_scenarios.py

1 同一物料被两人同时预占，不超占          5 半成品到货/自制完成后，不重复计算下级原料
2 到货重复提交，不重复增加库存            6 图纸升版后旧在制订单仍用批准版本（见 test_api_flows）
3 已收货物料被领用后，冲销被阻断          7 部分完工 / 部分领料 / 部分发货，数量闭合
4 急单调整保护已领料，并记录受影响订单
"""
from __future__ import annotations

import threading
import uuid
from datetime import date, timedelta
from decimal import Decimal as Dc

import pytest
from sqlalchemy import func, select

from app.core.errors import BizError, Conflict
from app.db import SessionLocal
from app.models import (Arrival, Bom, BomLine, Material, OrderIssue, OrderStatus, Reservation, SalesOrder,
                        StockLot, Supplier, TxnType, User)
from app.services import inventory as inv
from app.services import kitting
from app.services import orders as osvc
from app.services import production as psvc
from app.services import purchase as pu
from app.services import reallocation as ra
from app.services import receiving as rc

TODAY = date.today()


# ------------------------------------------------------------------ 工具


@pytest.fixture(scope="module", autouse=True)
def _seeded(client):  # 确保演示库已建好、应用已启动（互斥行已预建）
    return client


def tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def u(db, name):
    return db.scalar(select(User).where(User.username == name))


def warm_pool(n):
    """先把 n 条数据库连接建好放进连接池：否则线程们会卡在握手上被自然错开，测不出竞态。"""
    from app.db import engine
    n = min(n, 5)                                                         # 连接池默认保留 5 条
    conns = [engine.connect() for _ in range(n)]
    [c.close() for c in conns]


def run_parallel(fns):
    """同时放行 N 个函数（各自独立会话），返回 [("ok", 值) | ("err", 异常)]。"""
    warm_pool(len(fns))
    barrier = threading.Barrier(len(fns))
    out = [None] * len(fns)

    def wrap(i, fn):
        barrier.wait()
        try:
            out[i] = ("ok", fn())
        except Exception as e:  # noqa: BLE001
            out[i] = ("err", e)
    ts = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(fns)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    return out


def make_material(db, code, mtype, **kw):
    m = Material(code=code, name=code, type=mtype, unit="件", lead_time_days=0, inbound_days=1, **kw)
    db.add(m)
    db.flush()
    return m


def make_bom(db, parent, *children):
    b = Bom(material_id=parent.id, version="V1", status="ACTIVE")
    for cm, q in children:
        b.lines.append(BomLine(child_id=cm.id, qty_per=Dc(q)))
    db.add(b)
    db.flush()
    return b


def make_order(db, product, qty, priority=3, start_in=10):
    admin = u(db, "admin")
    o = osvc.create_order(db, admin, {"product_id": product.id, "customer": f"客户{tag()}", "qty": qty,
                                     "due_date": TODAY + timedelta(days=30),
                                     "planned_start": TODAY + timedelta(days=start_in), "priority": priority})
    osvc.release(db, admin, o.id)
    return o


def put_stock(db, mat, qty, lot_no=None):
    wh = inv.default_warehouse(db)
    return inv.stock_in(db, material_id=mat.id, warehouse_id=wh.id, qty=Dc(qty), txn_type=TxnType.PURCHASE_IN,
                        lot_no=lot_no or f"L-{tag()}", user_id=u(db, "admin").id)


def onhand(db, mat):
    return Dc(db.scalar(select(func.coalesce(func.sum(StockLot.qty), 0)).where(StockLot.material_id == mat.id)))


def reserved(db, order_id, mat_id, locked=None):
    q = select(func.coalesce(func.sum(Reservation.qty), 0)).where(
        Reservation.order_id == order_id, Reservation.material_id == mat_id, Reservation.source_type == "STOCK")
    if locked is not None:
        q = q.where(Reservation.locked.is_(locked))
    return Dc(db.scalar(q))


def retire(*order_ids):
    """用例结束：取消本用例的订单，别影响后面的用例。"""
    with SessionLocal() as db:
        for oid in order_ids:
            o = db.get(SalesOrder, oid)
            if o:
                o.status = OrderStatus.CANCELLED
            for r in db.scalars(select(Reservation).where(Reservation.order_id == oid)).all():
                db.delete(r)
        db.commit()
        kitting.recalc(db)


def pick_all(db, order_id, qty_by_mat=None):
    admin = u(db, "cangku")
    lines = [{"material_id": m, "qty": q} for m, q in (qty_by_mat or {}).items()] or None
    pk = osvc.create_pick(db, admin, order_id, lines)
    osvc.post_pick(db, admin, pk.id)
    return pk


# ------------------------------------------------------------------ 场景 1：并发预占不超占


def test_1_concurrent_lock_never_overclaims(monkeypatch):
    # 在“检查可锁数量”和“提交”之间人为拉长 50ms 窗口，让竞态必然出现；有锁则后到者被挡在窗口外
    import time
    real_audit = osvc.audit

    def slow_audit(*a, **kw):
        time.sleep(0.05)
        return real_audit(*a, **kw)
    monkeypatch.setattr(osvc, "audit", slow_audit)
    t = tag()
    N = 5                                                                 # 5 个人同时各锁 8，库存只有 10
    with SessionLocal() as db:
        P, R = make_material(db, f"S1P-{t}", "PRODUCT"), make_material(db, f"S1R-{t}", "BUY")
        make_bom(db, P, (R, 1))
        put_stock(db, R, 10)
        orders = [make_order(db, P, 8) for _ in range(N)]
        db.commit()
        o_ids, r_id = [o.id for o in orders], R.id

    def lock(oid):
        def f():
            with SessionLocal() as db:
                osvc.lock_reservation(db, u(db, "jihua"), oid, r_id, Dc(8), "并发测试")
                db.commit()
        return f
    res = run_parallel([lock(i) for i in o_ids])
    oks = [r for r in res if r[0] == "ok"]
    assert len(oks) == 1, res                                             # 只有一个成功
    assert all(isinstance(r[1], BizError) and "不足" in r[1].message for r in res if r[0] == "err"), res
    with SessionLocal() as db:
        total = Dc(db.scalar(select(func.coalesce(func.sum(Reservation.qty), 0)).where(
            Reservation.material_id == r_id, Reservation.source_type == "STOCK")))
        assert total <= onhand(db, db.get(Material, r_id)) == 10          # 绝不超过现存量
    retire(*o_ids)


def test_1b_priority_does_not_steal_frozen_stock_and_realloc_approval_is_single():
    """已分配的库存冻结；急单不能直接抢；调料申请并发审批只会生效一次。"""
    t = tag()
    with SessionLocal() as db:
        P, R = make_material(db, f"S1BP-{t}", "PRODUCT"), make_material(db, f"S1BR-{t}", "BUY")
        make_bom(db, P, (R, 1))
        put_stock(db, R, 10)
        a = make_order(db, P, 8, priority=3)
        db.commit()
        kitting.recalc(db)
        assert reserved(db, a.id, R.id) == 8
        urgent = make_order(db, P, 8, priority=1)                         # 后来的特急单
        db.commit()
        kitting.recalc(db)
        # 特急单只拿到空闲的 2，普通单已冻结的 8 不动 → 计划不抖动
        assert reserved(db, a.id, R.id) == 8 and reserved(db, urgent.id, R.id) == 2
        assert db.get(SalesOrder, urgent.id).kit_status != "READY"
        # 再重算一遍，结果稳定
        kitting.recalc(db)
        assert reserved(db, a.id, R.id) == 8 and reserved(db, urgent.id, R.id) == 2
        # 预演：调 6 给特急单，影响里要列出普通单
        pv = ra.preview(db, urgent.id, R.id, Dc(6))
        assert [p["qty"] for p in pv["plan"]] == [Dc(6)] and pv["plan"][0]["from_order_id"] == a.id
        affected = [i["order_no"] for i in pv["impacts"] if not i["is_target"]]
        assert db.get(SalesOrder, a.id).order_no in affected
        req = ra.create_request(db, u(db, "jihua"), urgent.id, R.id, Dc(6), "客户催货")
        db.commit()
        a_id, u_id, r_id, req_id = a.id, urgent.id, R.id, req.id

    def approve():
        with SessionLocal() as db:
            ra.approve(db, u(db, "zhuguan"), req_id, "同意")
            db.commit()
    res = run_parallel([approve, approve])
    assert sorted(r[0] for r in res) == ["err", "ok"], res
    with SessionLocal() as db:
        assert reserved(db, a_id, r_id) == 2 and reserved(db, u_id, r_id) == 8
        assert reserved(db, a_id, r_id) + reserved(db, u_id, r_id) <= onhand(db, db.get(Material, r_id))
        # 受影响订单已随申请留痕
        saved = ra.get_or_404(db, ra.ReallocationRequest, req_id, "申请")
        assert saved.status == "APPROVED" and "order_no" in saved.impacts
    retire(a_id, u_id)


# ------------------------------------------------------------------ 场景 2：重复到货不重复入库


def _po_line(db, R, qty, supplier):
    admin = u(db, "caigou")
    po = pu.create_po(db, admin, supplier.id, [{"material_id": R.id, "qty": qty, "price": 1}])
    pu.approve_po(db, admin, po.id)
    return po.lines[0]


def test_2_duplicate_arrival_does_not_double_stock():
    t = tag()
    with SessionLocal() as db:
        R = make_material(db, f"S2R-{t}", "BUY")
        sup = Supplier(code=f"S2-{t}", name=f"测试供应商{t}")
        db.add(sup)
        db.flush()
        ln = _po_line(db, R, 10, sup)
        db.commit()
        ln_id, r_id = ln.id, R.id

    def arrive():
        with SessionLocal() as db:
            a, _ = rc.register_arrival(db, u(db, "cangku"), "PO", ln_id, Dc(10), client_token=f"tok-{t}")
            db.commit()
            return a.id
    res = run_parallel([arrive, arrive, arrive])
    assert all(r[0] == "ok" for r in res), res                         # 重复提交都“成功”，但返回同一张到货单
    assert len({r[1] for r in res}) == 1
    with SessionLocal() as db:
        arrivals = db.scalars(select(Arrival).where(Arrival.source_line_id == ln_id)).all()
        assert len(arrivals) == 1
        line = db.get(pu.PurchaseOrderLine, ln_id)
        assert Dc(line.arrived_qty) == 10                                # 不是 20/30
        # 没带幂等键的第二次登记：超过未到量，被拒
        with pytest.raises(BizError):
            rc.register_arrival(db, u(db, "cangku"), "PO", ln_id, Dc(10))
        db.rollback()
        a_id = arrivals[0].id

    def inspect():
        with SessionLocal() as db:
            rc.inspect(db, u(db, "zhijian"), a_id, Dc(10), Dc(0))
            db.commit()
    res = run_parallel([inspect, inspect])
    assert sorted(r[0] for r in res) == ["err", "ok"], res              # 并发检验只入库一次
    with SessionLocal() as db:
        assert onhand(db, db.get(Material, r_id)) == 10
        assert db.scalar(select(func.count()).select_from(StockLot).where(StockLot.material_id == r_id)) == 1


# ------------------------------------------------------------------ 场景 3：已领用后冲销被阻断


def test_3_reversal_blocked_after_consumption_then_allowed_after_return():
    t = tag()
    with SessionLocal() as db:
        P, R = make_material(db, f"S3P-{t}", "PRODUCT"), make_material(db, f"S3R-{t}", "BUY")
        make_bom(db, P, (R, 1))
        sup = Supplier(code=f"S3-{t}", name=f"测试供应商{t}")
        db.add(sup)
        db.flush()
        ln = _po_line(db, R, 10, sup)
        a, _ = rc.register_arrival(db, u(db, "cangku"), "PO", ln.id, Dc(10))
        rc.inspect(db, u(db, "zhijian"), a.id, Dc(10), Dc(0))
        order = make_order(db, P, 4)
        db.commit()
        kitting.recalc(db)
        pk = pick_all(db, order.id)
        db.commit()
        ln_id, r_id, o_id, pk_id = ln.id, R.id, order.id, pk.id

    with SessionLocal() as db:
        imp = pu.line_impact(db, ln_id)
        assert not imp["can_reverse"] and any("已被领用" in b for b in imp["blockers"])
        assert imp["next_steps"]                                          # 给出可走的更正路径
        with pytest.raises(Conflict):
            pu.reverse_line(db, u(db, "caigou"), ln_id, "录错了")
        db.rollback()
        # 退料（退回原批次）后，才允许冲销；原始记录都在
        osvc.return_pick(db, u(db, "cangku"), pk_id, [{"material_id": r_id, "qty": 4}], "订单取消部分")
        db.commit()
        imp = pu.line_impact(db, ln_id)
        assert imp["can_reverse"], imp["blockers"]
        pu.reverse_line(db, u(db, "caigou"), ln_id, "录错了")
        db.commit()
        assert onhand(db, db.get(Material, r_id)) == 0
        assert db.scalar(select(func.count()).select_from(Arrival).where(
            Arrival.source_line_id == ln_id, Arrival.status == "REVERSED")) == 1   # 原到货保留为“已冲销”
    retire(o_id)


# ------------------------------------------------------------------ 场景 4：急单调整保护已领料


def test_4_urgent_adjustment_protects_issued_material_and_records_affected():
    t = tag()
    with SessionLocal() as db:
        P, R = make_material(db, f"S4P-{t}", "PRODUCT"), make_material(db, f"S4R-{t}", "BUY")
        make_bom(db, P, (R, 1))
        put_stock(db, R, 10)
        a = make_order(db, P, 8, priority=3)
        db.commit()
        kitting.recalc(db)
        pick_all(db, a.id, {R.id: 5})                                     # A 已领走 5
        db.commit()
        kitting.recalc(db)
        assert Dc(db.scalar(select(OrderIssue.qty).where(OrderIssue.order_id == a.id))) == 5
        assert reserved(db, a.id, R.id) == 3                              # 只剩 3 还在预占里
        urgent = make_order(db, P, 8, priority=1)
        db.commit()
        kitting.recalc(db)
        assert reserved(db, urgent.id, R.id) == 2                         # 5(现存) - 3(A 的冻结)
        # 想调 6：A 只剩 3 可调，已领的 5 不在可调范围内
        with pytest.raises(BizError) as ei:
            ra.preview(db, urgent.id, R.id, Dc(6))
        assert "可调数量不足" in ei.value.message
        req = ra.create_request(db, u(db, "jihua"), urgent.id, R.id, Dc(3), "客户催货")
        ra.approve(db, u(db, "zhuguan"), req.id, "同意")
        db.commit()
        # 已领料原样保留；A 的预占被调走并留痕；总预占不超现存
        assert Dc(db.scalar(select(OrderIssue.qty).where(OrderIssue.order_id == a.id))) == 5
        assert reserved(db, a.id, R.id) == 0 and reserved(db, urgent.id, R.id) == 5
        assert onhand(db, R) == 5
        assert "order_no" in req.impacts and db.get(SalesOrder, a.id).order_no in req.impacts
        # 不能自己审批自己
        req2 = ra.create_request(db, u(db, "zhuguan"), a.id, R.id, Dc(1), "反调", [urgent.id])
        with pytest.raises(BizError):
            ra.approve(db, u(db, "zhuguan"), req2.id)
        a_id, u_id = a.id, urgent.id
        db.rollback()
    retire(a_id, u_id)


# ------------------------------------------------------------------ 场景 5：不重复计算下级原料


def test_5_semi_finished_and_work_order_do_not_double_count_raw():
    t = tag()
    with SessionLocal() as db:
        P1, S = make_material(db, f"S5P1-{t}", "PRODUCT"), make_material(db, f"S5S-{t}", "MAKE")
        P2, RAW = make_material(db, f"S5P2-{t}", "PRODUCT"), make_material(db, f"S5RAW-{t}", "BUY")
        make_bom(db, P1, (S, 1))
        make_bom(db, S, (RAW, 2))
        make_bom(db, P2, (RAW, 1))
        put_stock(db, RAW, 20)
        o1 = make_order(db, P1, 5, priority=2)                           # 要 5 个半成品 = 10 个原料
        db.commit()
        kitting.recalc(db)
        # 还没有加工任务：半成品由原料展开满足，占用 10 个原料
        assert reserved(db, o1.id, RAW.id) == 10
        # 建加工任务 5 个 S（计划 3 天后完工）
        wo = psvc.create_wo(db, u(db, "jihua"), {"material_id": S.id, "qty": 5, "planned_finish": TODAY + timedelta(days=3)})
        db.commit()
        kitting.recalc(db)
        # 订单改由加工任务供应，不再展开原料；原料只被加工任务“认领”一次（10），不是 20
        assert reserved(db, o1.id, RAW.id) == 0
        snap = kitting.load_snapshot(db)
        assert any(d.kind == "WO" and d.id == wo.id and dict(d.items)[RAW.id] == 10 for d in snap.prod_demands)
        # 另一订单要 11 个原料：空闲只剩 20-10=10 → 缺 1（若加工任务的原料没被认领，会误判为齐套）
        o2 = make_order(db, P2, 11, priority=3)
        db.commit()
        res = kitting.recalc(db)
        assert res.orders[o2.id].lines[0].short_qty == 1 and db.get(SalesOrder, o2.id).kit_status != "READY"
        # 加工任务开工、领料、完工入库：原料 20 → 10，半成品 +5，订单由库存满足，原料不再被重复扣
        psvc.start_wo(db, u(db, "chejian"), wo.id)
        pk = psvc.create_wo_pick(db, u(db, "cangku"), wo.id)
        osvc.post_pick(db, u(db, "cangku"), pk.id)
        psvc.report_wo(db, u(db, "chejian"), wo.id, Dc(5))
        db.commit()
        res = kitting.recalc(db)
        assert onhand(db, RAW) == 10 and onhand(db, S) == 5
        assert res.orders[o1.id].status == "READY"
        assert reserved(db, o1.id, S.id) == 5 and reserved(db, o1.id, RAW.id) == 0
        assert res.orders[o2.id].lines[0].short_qty == 1                 # 仍然只缺 1，没有被重复扣减
        # 本厂供料外协认领原料，包工包料不认领
        sup = Supplier(code=f"S5O-{t}", name=f"外协{t}", is_outsourcer=True)
        OUT = make_material(db, f"S5OUT-{t}", "OUTSOURCE")
        make_bom(db, OUT, (RAW, 3))
        db.add(sup)
        db.flush()
        cons = psvc.create_outsource(db, u(db, "caigou"), {"supplier_id": sup.id, "material_id": OUT.id, "qty": 2,
                                                          "supply_mode": "CONSIGNED"})
        turn = psvc.create_outsource(db, u(db, "caigou"), {"supplier_id": sup.id, "material_id": OUT.id, "qty": 2,
                                                          "supply_mode": "TURNKEY"})
        db.commit()
        snap = kitting.load_snapshot(db)
        kinds = {(d.kind, d.id): dict(d.items) for d in snap.prod_demands}
        assert kinds[("OUTSOURCE", cons.id)][RAW.id] == 6 and ("OUTSOURCE", turn.id) not in kinds
        with pytest.raises(BizError):                                     # 本厂供料外协必须先发料才能发出
            psvc.send_outsource(db, u(db, "caigou"), cons.id)
        db.rollback()
        o1_id, o2_id, cons_id, turn_id = o1.id, o2.id, cons.id, turn.id
    with SessionLocal() as db:
        for oid in (cons_id, turn_id):
            psvc.cancel_outsource(db, u(db, "caigou"), oid)
        db.commit()
    retire(o1_id, o2_id)


# ------------------------------------------------------------------ 场景 7：数量闭合


def test_7_partial_work_issue_return_and_shipment_quantities_close():
    t = tag()
    with SessionLocal() as db:
        P, R = make_material(db, f"S7P-{t}", "PRODUCT"), make_material(db, f"S7R-{t}", "BUY")
        make_bom(db, P, (R, 1))
        put_stock(db, R, 10)
        o = make_order(db, P, 10)
        db.commit()
        kitting.recalc(db)
        o_id, p_id, r_id = o.id, P.id, R.id
        # 分两次领料 6 + 4，再退 1
        pick_all(db, o_id, {r_id: 6})
        pk2 = pick_all(db, o_id, {r_id: 4})
        osvc.return_pick(db, u(db, "cangku"), pk2.id, [{"material_id": r_id, "qty": 1}], "多领")
        db.commit()
        issued = Dc(db.scalar(select(OrderIssue.qty).where(OrderIssue.order_id == o_id)))
        assert issued == 9 and onhand(db, R) == 1                         # 领 10 - 退 1 = 9；库存 10 - 9 = 1
        plan = {x["material_id"]: x for x in osvc.pick_plan(db, db.get(SalesOrder, o_id))}
        assert plan[r_id]["plan_qty"] == 1                                # 还差 1 未领 = 需求 10 - 已领 9
        with pytest.raises(BizError):                                     # 退料不能超过实领
            osvc.return_pick(db, u(db, "cangku"), pk2.id, [{"material_id": r_id, "qty": 4}], "超退")
        db.rollback()
        pick_all(db, o_id, {r_id: 1})
        db.commit()
        # 装配 → 完工入成品库
        osvc.start_assembly(db, u(db, "chejian"), o_id, "测试强制开工")
        osvc.complete(db, u(db, "chejian"), o_id)
        db.commit()
        fin = inv.default_warehouse(db, "FINISHED")
        assert inv.available(db, p_id, fin.id) == 10

    # 并发建两张各 6 的发货计划（订单共 10）：只能成功一张
    def plan_ship():
        with SessionLocal() as db:
            osvc.create_shipment(db, u(db, "chejian"), {"order_id": o_id, "qty": Dc(6), "plan_date": TODAY})
            db.commit()
    res = run_parallel([plan_ship, plan_ship])
    assert sorted(r[0] for r in res) == ["err", "ok"], res
    with SessionLocal() as db:
        # 剩 4 再排一张，合计刚好 10；再多排 1 被拒
        s2 = osvc.create_shipment(db, u(db, "chejian"), {"order_id": o_id, "qty": Dc(4), "plan_date": TODAY})
        with pytest.raises(BizError):
            osvc.create_shipment(db, u(db, "chejian"), {"order_id": o_id, "qty": Dc(1), "plan_date": TODAY})
        db.commit()
        ships = db.scalars(select(osvc.ShipmentPlan).where(osvc.ShipmentPlan.order_id == o_id)).all()
        assert sum(Dc(s.qty) for s in ships) == 10
        for s in ships:
            for act in ("submit", "approve", "ship"):
                osvc.shipment_action(db, u(db, "zhuguan"), s.id, act)
            with pytest.raises(BizError):                                 # 同一张不能重复发货出库
                osvc.shipment_action(db, u(db, "zhuguan"), s.id, "ship")
        db.commit()
        assert db.get(SalesOrder, o_id).status == OrderStatus.SHIPPED
        fin = inv.default_warehouse(db, "FINISHED")
        assert inv.available(db, p_id, fin.id) == 0                       # 成品入 10 出 10，闭合
        _ = s2

    # 自制任务部分完工：3 + 2 = 5，超报被拒，入库量闭合
    with SessionLocal() as db:
        S = make_material(db, f"S7S-{t}", "MAKE")
        wo = psvc.create_wo(db, u(db, "jihua"), {"material_id": S.id, "qty": 5})
        psvc.start_wo(db, u(db, "chejian"), wo.id)
        psvc.report_wo(db, u(db, "chejian"), wo.id, Dc(3))
        db.commit()
        with pytest.raises(BizError):
            psvc.report_wo(db, u(db, "chejian"), wo.id, Dc(3))
        db.rollback()
        psvc.report_wo(db, u(db, "chejian"), wo.id, Dc(2))
        db.commit()
        w = db.get(psvc.WorkOrder, wo.id)
        assert Dc(w.done_qty) == 5 and w.status == "DONE" and onhand(db, S) == 5
    retire(o_id)


# ------------------------------------------------------------------ 事务回滚（MySQL 上必须实测）


def test_8_failed_posting_rolls_back_everything():
    """领料单两行，第二行库存不足 → 整单回滚：第一行已扣的库存、领料记录、订单已领量都不留痕。"""
    t = tag()
    with SessionLocal() as db:
        P = make_material(db, f"S8P-{t}", "PRODUCT")
        R1, R2 = make_material(db, f"S8A-{t}", "BUY"), make_material(db, f"S8B-{t}", "BUY")
        make_bom(db, P, (R1, 1), (R2, 1))
        put_stock(db, R1, 10)
        put_stock(db, R2, 2)
        o = make_order(db, P, 5)
        db.commit()
        kitting.recalc(db)
        pk = osvc.create_pick(db, u(db, "cangku"), o.id, [{"material_id": R1.id, "qty": 5}, {"material_id": R2.id, "qty": 5}])
        db.commit()
        pk_id, o_id, r1_id, r2_id = pk.id, o.id, R1.id, R2.id
    with SessionLocal() as db:
        with pytest.raises(BizError):
            osvc.post_pick(db, u(db, "cangku"), pk_id)
        db.rollback()                                                    # 接口层异常时 get_db 关闭会话 = 回滚
    with SessionLocal() as db:
        assert onhand(db, db.get(Material, r1_id)) == 10 and onhand(db, db.get(Material, r2_id)) == 2
        assert db.get(osvc.PickList, pk_id).status == "DRAFT"
        assert db.scalar(select(func.count()).select_from(OrderIssue).where(OrderIssue.order_id == o_id)) == 0
    retire(o_id)
