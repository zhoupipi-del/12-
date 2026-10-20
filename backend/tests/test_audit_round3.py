"""第三版四项修复的正确行为断言；SQLite / MySQL 共用。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app import models as m
from app.api.util import commit_recalc
from app.core.errors import BizError
from app.db import SessionLocal, engine
from app.services import orders
from tests.test_audit_regressions import Api, TODAY


@pytest.fixture
def api(client, H):
    return Api(client, H)


def task(api, mid, kind="MAKE", **kw):
    if kind == "MAKE":
        return api.post("/work-orders", {"material_id": mid, "qty": 1, **kw}), "/work-orders"
    with SessionLocal() as db:
        sid = db.scalar(select(m.Supplier).where(m.Supplier.is_outsourcer.is_(True))).id
    return api.post("/outsource", {"material_id": mid, "supplier_id": sid,
                                         "supply_mode": "CONSIGNED", "qty": 1, **kw}), "/outsource"


@pytest.mark.parametrize("kind", ["MAKE", "OUTSOURCE"])
def test_f1_linked_missing_bom_is_not_exemption(api, kind):
    sub, fg = api.material("F1-MISSING", kind), api.material("F1-FG", "PRODUCT")
    api.bom(fg, sub)
    oid = api.order(fg)
    doc, path = task(api, sub, kind, order_id=oid)
    with SessionLocal() as db:
        cls = m.WorkOrder if kind == "MAKE" else m.OutsourceOrder
        assert not db.get(m.Material, sub).bom_exempt
        assert db.get(cls, doc["id"]).bom_snapshot is None
    api.post(f"{path}/{doc['id']}/pick", {}, status=400)
    api.post(f"{path}/{doc['id']}/" + ("start" if kind == "MAKE" else "send"), {}, status=400)
    assert api.qty(sub) == 0


@pytest.mark.parametrize("kind", ["MAKE", "OUTSOURCE"])
def test_f1_legacy_empty_snapshot_also_requires_explicit_exemption(api, kind):
    mid = api.material("F1-LEGACY", kind)
    doc, path = task(api, mid, kind)
    with SessionLocal() as db:
        cls = m.WorkOrder if kind == "MAKE" else m.OutsourceOrder
        db.get(cls, doc["id"]).bom_snapshot = "[]"
        db.commit()
    api.post(f"{path}/{doc['id']}/" + ("start" if kind == "MAKE" else "send"), {}, status=400)
    assert api.qty(mid) == 0


@pytest.mark.parametrize("kind", ["MAKE", "OUTSOURCE"])
def test_f1_explicit_no_input_process_remains_available(api, kind):
    mid = api.material("F1-EXEMPT", kind, bom_exempt=True)
    fg = api.material("F1-EXEMPT-FG", "PRODUCT")
    api.bom(fg, mid)
    oid = api.order(fg)
    doc, path = task(api, mid, kind, order_id=oid)
    api.post(f"{path}/{doc['id']}/" + ("start" if kind == "MAKE" else "send"), {})
    if kind == "MAKE":
        api.post(f"{path}/{doc['id']}/report", {"qty": 1})
        assert api.qty(mid) == 1


@pytest.mark.parametrize("kind", ["MAKE", "OUTSOURCE"])
def test_f2_one_input_granted_once_not_blocked_by_all_demand(api, kind):
    raw, sub = api.material("F2-RAW"), api.material("F2-SUB", kind)
    api.bom(sub, raw)
    api.stock(raw, 1)
    date_field = "planned_finish" if kind == "MAKE" else "expected_return"
    early, path = task(api, sub, kind, **{date_field: (TODAY + timedelta(days=1)).isoformat()})
    late, _ = task(api, sub, kind, **{date_field: (TODAY + timedelta(days=10)).isoformat()})
    p1 = api.post(f"{path}/{early['id']}/pick", {})
    p2 = api.post(f"{path}/{late['id']}/pick", {})
    api.post(f"/picks/{p2['id']}/post", status=400)
    api.post(f"/picks/{p1['id']}/post")
    assert api.qty(raw) == 0
    api.post(f"/picks/{p2['id']}/post", status=400)
    api.stock(raw, 1)
    api.post(f"/picks/{p2['id']}/post")
    assert api.qty(raw) == 0


def partial_wo(api):
    raw, sub = api.material("F3-RAW"), api.material("F3-SUB", "MAKE")
    api.bom(sub, raw, 2)
    api.stock(raw, 4)
    w, path = task(api, sub, qty=2)
    api.post(f"{path}/{w['id']}/start")
    pk = api.post(f"{path}/{w['id']}/pick", {})
    api.post(f"/picks/{pk['id']}/post")
    api.post(f"{path}/{w['id']}/report", {"qty": 1})
    return raw, sub, pk["id"]


def test_f3_duplicate_return_rejected_atomically_tail_still_returns(api):
    raw, sub, pid = partial_wo(api)
    with SessionLocal() as db:
        before = db.scalar(select(func.count()).select_from(m.StockTxn).where(m.StockTxn.material_id == raw))
    api.post(f"/picks/{pid}/return", {"reason": "重复行", "lines": [
        {"material_id": raw, "qty": 2}, {"material_id": raw, "qty": 2}]}, status=400)
    assert api.qty(raw) == 0 and api.qty(sub) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(m.StockTxn).where(m.StockTxn.material_id == raw)) == before
        assert db.scalar(select(m.PickLine.returned_qty).where(m.PickLine.pick_id == pid)) == 0
    body = {"reason": "未耗尾料", "lines": [{"material_id": raw, "qty": 2}]}
    api.post(f"/picks/{pid}/return", body)
    api.post(f"/picks/{pid}/return", body, status=400)
    assert api.qty(raw) == 2 and api.qty(sub) == 1


def test_f4_concurrent_completion_refreshes_stale_state_posts_once(api):
    raw, fg = api.material("F4-RAW"), api.material("F4-FG", "PRODUCT")
    api.bom(fg, raw)
    api.stock(raw, 1)
    oid = api.order(fg)
    pk = api.post("/picks", {"order_id": oid})
    api.post(f"/picks/{pk['id']}/post")
    api.post(f"/orders/{oid}/start", {})
    gate = Barrier(2, timeout=15)

    def worker():
        with SessionLocal() as db:
            if engine.dialect.name == "mysql":
                cached = db.get(m.SalesOrder, oid)
                assert cached.status == m.OrderStatus.ASSEMBLING
                user = db.scalar(select(m.User).where(m.User.username == "cangku"))
                gate.wait()
            else:
                gate.wait()  # SQLite BEGIN IMMEDIATE 串行写，不在持锁后等另一写事务
                user = db.scalar(select(m.User).where(m.User.username == "cangku"))
            try:
                orders.complete(db, user, oid)
                commit_recalc(db)
                return "ok"
            except BizError:
                db.rollback()
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=45) for f in [pool.submit(worker) for _ in range(2)]]
    assert sorted(results) == ["ok", "rejected"], results
    assert api.qty(fg) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(m.StockTxn).where(
            m.StockTxn.ref_type == "ORDER", m.StockTxn.ref_id == oid,
            m.StockTxn.txn_type == m.TxnType.FINISHED_IN)) == 1
        assert db.get(m.SalesOrder, oid).status == m.OrderStatus.COMPLETED


def test_f4_completion_and_recalc_failure_roll_back_together(api, monkeypatch):
    from app.api import util
    raw, fg = api.material("F4-ATOMIC-RAW"), api.material("F4-ATOMIC-FG", "PRODUCT")
    api.bom(fg, raw)
    api.stock(raw, 1)
    oid = api.order(fg)
    pk = api.post("/picks", {"order_id": oid})
    api.post(f"/picks/{pk['id']}/post")
    api.post(f"/orders/{oid}/start", {})

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic recalc failure")

    with monkeypatch.context() as patch:
        patch.setattr(util, "recalc", fail)
        with SessionLocal() as db:
            user = db.scalar(select(m.User).where(m.User.username == "cangku"))
            orders.complete(db, user, oid)
            with pytest.raises(RuntimeError, match="synthetic"):
                util.commit_recalc(db)
    assert api.qty(fg) == 0
    with SessionLocal() as db:
        assert db.get(m.SalesOrder, oid).status == m.OrderStatus.ASSEMBLING
        assert db.scalar(select(func.count()).select_from(m.StockTxn).where(
            m.StockTxn.ref_type == "ORDER", m.StockTxn.ref_id == oid,
            m.StockTxn.txn_type == m.TxnType.FINISHED_IN)) == 0
    api.post(f"/orders/{oid}/complete")
    assert api.qty(fg) == 1


def assembly_order(api, code):
    raw, fg = api.material(code + "-RAW"), api.material(code + "-FG", "PRODUCT")
    api.bom(fg, raw)
    api.stock(raw, 1)
    oid = api.order(fg)
    pk = api.post("/picks", {"order_id": oid})
    api.post(f"/picks/{pk['id']}/post")
    api.post(f"/orders/{oid}/start", {})
    return oid, fg


def concurrent_requests(api, calls):
    gate = Barrier(len(calls), timeout=15)

    def call(path, body):
        gate.wait()
        return api.c.post("/api" + path, json=body, headers=api.h).status_code

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return [f.result(timeout=45) for f in [pool.submit(call, *x) for x in calls]]


def test_f4_http_concurrent_complete_posts_once(api):
    oid, fg = assembly_order(api, "F4-HTTP")
    results = concurrent_requests(api, [(f"/orders/{oid}/complete", None)] * 2)
    assert sorted(results) == [200, 400], results
    assert api.qty(fg) == 1


def test_f4_completion_retry_and_shipment_share_lock_order(api):
    oid, fg = assembly_order(api, "F4-SHIP")
    api.post(f"/orders/{oid}/complete")
    results = concurrent_requests(api, [(f"/orders/{oid}/complete", None),
        ("/shipments", {"order_id": oid, "qty": 1, "plan_date": TODAY.isoformat()})])
    assert results == [400, 200], results
    assert api.qty(fg) == 1
