"""U9 独立审计（2026-10-22）发现问题的修复回归：G1 / F-1 / F-2 / F-3 / F-4。
SQLite 与 MySQL 共用。每个用例断言“正确行为”。"""
from __future__ import annotations

import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal as Dc

import pytest
from sqlalchemy import func, select

from app import models as m
from app.db import SessionLocal
from tests.test_audit_regressions import Api, TODAY, tag


@pytest.fixture(scope="module", autouse=True)
def _seeded(client):
    return client


@pytest.fixture
def api(client, H):
    return Api(client, H)


def wh(purpose):
    with SessionLocal() as db:
        return db.scalar(select(m.Warehouse).where(m.Warehouse.purpose == purpose, m.Warehouse.is_active.is_(True))
                         .order_by(m.Warehouse.id)).id


def wo(api, sub, qty=1, order_id=None):
    return api.post("/work-orders", {"material_id": sub, "qty": qty, "source_mode": "ORDER" if order_id else "STOCK",
                                     **({"order_id": order_id} if order_id else {})})


def osd(api, sub, qty=1, mode="CONSIGNED"):
    with SessionLocal() as db:
        sid = db.scalar(select(m.Supplier).where(m.Supplier.is_outsourcer.is_(True))).id
    return api.post("/outsource", {"material_id": sub, "supplier_id": sid, "supply_mode": mode, "qty": qty,
                                   "source_mode": "STOCK"})


# ================================================================== G1 鉴权后再拿全局锁


def _write_routes():
    from app.main import app
    out = []
    for path, ops in app.openapi()["paths"].items():
        if not path.startswith("/api/") or path.startswith(("/api/auth/", "/api/ai/")):
            continue
        for method in ops:
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                out.append((method.upper(), path.replace("{token}", "bad-token").replace("{kind}", "stock")
                            .replace("{action}", "approve").replace("{", "").replace("}", "")))
    return out


def test_g1_invalid_or_missing_credentials_never_take_the_global_lock(client, monkeypatch):
    calls = []
    import app.services.kitting as kt
    import app.core.deps as deps
    import app.api.routes_supply as rs
    real = kt.acquire_mutex
    monkeypatch.setattr(kt, "acquire_mutex", lambda db, name="kitting": calls.append(name) or real(db, name))
    monkeypatch.setattr(rs, "acquire_mutex", lambda db, name="kitting": calls.append(name) or real(db, name))
    routes = _write_routes()
    assert len(routes) > 60
    for method, path in routes:
        for headers in ({}, {"Authorization": "Bearer not-a-token"}, {"X-API-Key": "psk_bad"}):
            client.request(method, path, headers=headers, json={})
    assert calls == [], f"{len(calls)} 次无效/匿名写请求拿了全局锁"


def test_g1_authenticated_write_still_takes_the_global_lock_and_queues(api, client):
    from app.services.kitting import acquire_mutex
    held, done = threading.Event(), threading.Event()

    def holder():
        with SessionLocal() as db:
            acquire_mutex(db)
            held.set()
            done.wait(10)
            db.rollback()

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    t0 = time.time()
    threading.Timer(1.5, done.set).start()
    r = client.post("/api/materials", json={"code": "G1-" + tag(), "name": "g1", "type": "BUY"}, headers=api.h)
    dt = time.time() - t0
    done.set()
    t.join()
    assert r.status_code == 200 and dt >= 1.2, f"合法写请求没有排队等锁：{dt:.2f}s"


def test_g1_anonymous_post_is_not_queued_behind_the_lock(client):
    from app.services.kitting import acquire_mutex
    held, done = threading.Event(), threading.Event()

    def holder():
        with SessionLocal() as db:
            acquire_mutex(db)
            held.set()
            done.wait(10)
            db.rollback()

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    t0 = time.time()
    r = client.post("/api/orders", json={})
    dt = time.time() - t0
    done.set()
    t.join()
    assert r.status_code in (401, 403) and dt < 1.5, f"匿名请求等待 {dt:.2f}s"


# ================================================================== F-3 外协：已发出不能直接取消，可撤回发出


def _sent_consigned(api, code, qty=1, per=1):
    raw, sub = api.material(code + "-RAW"), api.material(code + "-SUB", "OUTSOURCE")
    api.bom(sub, raw, per)
    api.stock(raw, per * qty)
    o = osd(api, sub, qty)
    pk = api.post(f"/outsource/{o['id']}/pick", {})
    api.post(f"/picks/{pk['id']}/post")
    api.post(f"/outsource/{o['id']}/send", {})
    return raw, sub, o, pk


def _return(api, pid, mid, q, status=200):
    return api.post(f"/picks/{pid}/return", {"reason": "撤回后退料", "lines": [{"material_id": mid, "qty": q}]},
                    status=status)


def test_f3_sent_outsource_cannot_be_cancelled_directly(api):
    raw, sub, o, pk = _sent_consigned(api, "F3A")
    r = api.c.post(f"/api/outsource/{o['id']}/cancel", headers=api.h)
    assert r.status_code == 400 and r.json()["data"].get("need_recall") is True, r.text
    assert api.qty(raw) == 0
    _return(api, pk["id"], raw, 1, status=400)          # 发出后料算在外协厂，不能直接退


def test_f3_recall_then_return_material_then_cancel(api):
    raw, sub, o, pk = _sent_consigned(api, "F3B", qty=2, per=3)
    api.post(f"/outsource/{o['id']}/recall", {"reason": ""}, status=400)       # 原因必填
    r = api.post(f"/outsource/{o['id']}/recall", {"reason": "外协厂未开工，撤回"})
    assert r["status"] == "PLANNED" and r["sent_date"] is None
    api.post(f"/outsource/{o['id']}/recall", {"reason": "重复"}, status=400)   # 已是计划状态
    _return(api, pk["id"], raw, 6)                                             # 料能退回了
    assert api.qty(raw) == 6
    c = api.post(f"/outsource/{o['id']}/cancel")
    assert c["status"] == "CANCELLED"
    api.post(f"/outsource/{o['id']}/cancel", status=400)                       # 已结束


def test_f3_recall_blocked_after_any_return_from_supplier(api):
    raw, sub, o, pk = _sent_consigned(api, "F3C", qty=2)
    api.post("/arrivals", {"source_type": "OUTSOURCE", "source_id": o["id"], "qty": 1})
    api.post(f"/outsource/{o['id']}/recall", {"reason": "已有回厂"}, status=400)
    api.post(f"/outsource/{o['id']}/cancel", status=400)


def test_f3_turnkey_sent_order_also_needs_recall_before_cancel(api):
    sub = api.material("F3D-SUB", "OUTSOURCE")
    o = osd(api, sub, 1, mode="TURNKEY")
    api.post(f"/outsource/{o['id']}/send", {})
    api.post(f"/outsource/{o['id']}/cancel", status=400)
    api.post(f"/outsource/{o['id']}/recall", {"reason": "供应商取消"})
    assert api.post(f"/outsource/{o['id']}/cancel")["status"] == "CANCELLED"


def test_f3_planned_order_with_posted_pick_can_cancel_and_return_material(api):
    """原缺陷：计划状态领了料再取消，退料口径把“非计划”都算已消耗，料被锁死。"""
    raw, sub = api.material("F3E-RAW"), api.material("F3E-SUB", "OUTSOURCE")
    api.bom(sub, raw, 2)
    api.stock(raw, 2)
    o = osd(api, sub, 1)
    pk = api.post(f"/outsource/{o['id']}/pick", {})
    api.post(f"/picks/{pk['id']}/post")
    api.post(f"/outsource/{o['id']}/cancel")
    _return(api, pk["id"], raw, 2)
    assert api.qty(raw) == 2


def test_f3_recalled_order_can_be_sent_again_without_losing_material(api):
    raw, sub, o, pk = _sent_consigned(api, "F3F")
    api.post(f"/outsource/{o['id']}/recall", {"reason": "改发货日期"})
    r = api.post(f"/outsource/{o['id']}/send", {})
    assert r["status"] == "SENT" and api.qty(raw) == 0


def test_f3_concurrent_recall_and_cancel_only_one_path_wins(api):
    raw, sub, o, pk = _sent_consigned(api, "F3G")
    gate = threading.Barrier(6, timeout=20)

    def go(i):
        gate.wait()
        if i % 2:
            return api.c.post(f"/api/outsource/{o['id']}/cancel", headers=api.h).status_code
        return api.c.post(f"/api/outsource/{o['id']}/recall", json={"reason": "并发"}, headers=api.h).status_code

    with ThreadPoolExecutor(6) as ex:
        codes = list(ex.map(go, range(6)))
    assert not [c for c in codes if c >= 500], codes
    with SessionLocal() as db:
        st = db.get(m.OutsourceOrder, o["id"]).status
    assert st in ("PLANNED", "CANCELLED") and codes.count(200) in (1, 2), (st, codes)


# ================================================================== F-4 已完工未发完的订单也受重大图纸变更约束


def _version(api, did, version):
    r = api.c.post(f"/api/drawings/{did}/versions", data={"version": version, "change_note": "回归"},
                   files={"file": ("a.pdf", b"%PDF-1.4 regression", "application/pdf")}, headers=api.h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _completed_order(api, code, qty=1):
    no = f"{code}-{tag()}"
    raw, fg = api.material(code + "-RAW"), api.material(code + "-FG", "PRODUCT", drawing=no)
    api.bom(fg, raw)
    api.stock(raw, qty)
    dw = api.post("/drawings", {"drawing_no": no, "name": "回归图纸"})
    v1 = _version(api, dw["id"], "A")
    api.post(f"/drawings/versions/{v1}/release", {"change_level": "MINOR"})
    oid = api.order(fg, qty=qty)
    api.post("/drawings-confirm", {"ref_type": "SALES_ORDER", "ref_id": oid, "version_ids": [v1]})
    pk = api.post("/picks", {"order_id": oid})
    api.post(f"/picks/{pk['id']}/post")
    api.post(f"/orders/{oid}/start", {})
    api.post(f"/orders/{oid}/complete")
    return oid, fg, dw["id"]


def _approved_shipment(api, oid, qty=None):
    sh = api.post("/shipments", {"order_id": oid, "plan_date": TODAY.isoformat(), **({"qty": qty} if qty else {})})
    for a in ("submit", "approve"):
        api.post(f"/shipments/{sh['id']}/{a}", {})
    return sh["id"]


def _hold_of(api, oid):
    req = api.c.get("/api/drawings-required", params={"ref_type": "SALES_ORDER", "ref_id": oid}, headers=api.h).json()
    return [r["hold"] for r in req]


def test_f4_major_change_after_completion_blocks_shipment_until_resolved(api):
    oid, fg, did = _completed_order(api, "F4A")
    sid = _approved_shipment(api, oid)
    v2 = _version(api, did, "B")
    rel = api.post(f"/drawings/versions/{v2}/release", {"change_level": "MAJOR", "hold_reason": "回归：安全问题"})
    assert rel["held"] == 1 and _hold_of(api, oid) == [True]
    api.post(f"/shipments/{sid}/ship", {}, status=400)
    assert api.qty(fg) == 1
    api.post("/drawings-resolve-hold", {"ref_type": "SALES_ORDER", "ref_id": oid, "drawing_id": did,
                                        "reason": "技术裁定：成品按 A 版放行"})
    api.post(f"/shipments/{sid}/ship", {})
    assert api.qty(fg) == 0


def test_f4_minor_change_does_not_hold_completed_order(api):
    oid, fg, did = _completed_order(api, "F4B")
    sid = _approved_shipment(api, oid)
    v2 = _version(api, did, "B")
    rel = api.post(f"/drawings/versions/{v2}/release", {"change_level": "MINOR"})
    assert rel["held"] == 0
    api.post(f"/shipments/{sid}/ship", {})


def test_f4_partially_shipped_order_is_held_for_the_rest(api):
    oid, fg, did = _completed_order(api, "F4C", qty=2)
    s1 = _approved_shipment(api, oid, 1)
    api.post(f"/shipments/{s1}/ship", {})                 # 先发 1 台，订单仍是已完工
    s2 = _approved_shipment(api, oid, 1)
    v2 = _version(api, did, "B")
    api.post(f"/drawings/versions/{v2}/release", {"change_level": "MAJOR", "hold_reason": "回归：质量问题"})
    api.post(f"/shipments/{s2}/ship", {}, status=400)
    assert api.qty(fg) == 1


def test_f4_fully_shipped_order_is_no_longer_held(api):
    oid, fg, did = _completed_order(api, "F4D")
    sid = _approved_shipment(api, oid)
    api.post(f"/shipments/{sid}/ship", {})
    v2 = _version(api, did, "B")
    rel = api.post(f"/drawings/versions/{v2}/release", {"change_level": "MAJOR", "hold_reason": "回归"})
    assert rel["held"] == 0 and _hold_of(api, oid) == [False]


def test_f4_impact_list_includes_completed_orders(api):
    oid, fg, did = _completed_order(api, "F4E")
    no = api.get(f"/orders/{oid}")["order_no"]
    v2 = _version(api, did, "B")
    rel = api.post(f"/drawings/versions/{v2}/release", {"change_level": "MINOR"})
    assert no in rel["affected"]["orders"]


# ================================================================== F-1 旧单据 BOM：不再静默冻结，须技术确认


def _legacy_wo(api, code, per=1, qty=1, issue=True, start=True):
    """造一张“迁移前的旧自制任务”：按 V1 开工并领料后，快照被清空（旧库没有这一列的数据）。"""
    raw, sub = api.material(code + "-RAW"), api.material(code + "-SUB", "MAKE")
    b1 = api.bom(sub, raw, per, version="V1")
    api.stock(raw, per * qty)
    w = wo(api, sub, qty)
    pk = None
    if start:
        api.post(f"/work-orders/{w['id']}/start")
    if issue:
        pk = api.post(f"/work-orders/{w['id']}/pick", {})
        api.post(f"/picks/{pk['id']}/post")
    with SessionLocal() as db:
        db.get(m.WorkOrder, w["id"]).bom_snapshot = None
        db.commit()
    return raw, sub, w, pk, b1


def _confirm(api, kind, doc_id, status=200, **body):
    path = "work-orders" if kind == "WORK_ORDER" else "outsource"
    return api.post(f"/{path}/{doc_id}/bom-confirm", {"reason": "按工艺卡核对", **body}, status=status)


def test_f1_task_without_bom_is_pending_and_never_auto_frozen(api):
    raw, sub = api.material("F1A-RAW"), api.material("F1A-SUB", "MAKE")
    w = wo(api, sub, 1)                                                       # 建单时没有 BOM
    assert w["bom_state"] == "PENDING_CONFIRM"
    api.bom(sub, raw, 1)                                                      # 技术之后补了 BOM
    r = api.c.post(f"/api/work-orders/{w['id']}/start", headers=api.h)
    assert r.status_code == 400 and r.json()["data"]["need_bom_confirm"] is True, r.text
    api.post(f"/work-orders/{w['id']}/pick", {}, status=400)
    with SessionLocal() as db:
        assert db.get(m.WorkOrder, w["id"]).bom_snapshot is None              # 没有被悄悄冻结


def test_f1_pending_blocks_every_stock_moving_action(api):
    raw, sub, w, pk, _ = _legacy_wo(api, "F1B", per=2, qty=2, issue=True)
    api.post(f"/work-orders/{w['id']}/report", {"qty": 1}, status=400)
    api.post(f"/picks/{pk['id']}/return", {"reason": "x", "lines": [{"material_id": raw, "qty": 1}]}, status=400)
    # 草稿领料单也不能过账：先造一张草稿（任务还有未领量时才能再领；这里直接取消再用新单据验证 post 路径）
    raw2, sub2 = api.material("F1B2-RAW"), api.material("F1B2-SUB", "MAKE")
    api.bom(sub2, raw2, 1)
    api.stock(raw2, 1)
    w2 = wo(api, sub2, 1)
    api.post(f"/work-orders/{w2['id']}/start")
    draft = api.post(f"/work-orders/{w2['id']}/pick", {})
    with SessionLocal() as db:
        db.get(m.WorkOrder, w2["id"]).bom_snapshot = None
        db.commit()
    api.post(f"/picks/{draft['id']}/post", status=400)
    assert api.qty(raw2) == 1


def test_f1_confirm_requires_tech_role_and_reason_and_valid_bom(api, H):
    raw, sub, w, pk, b1 = _legacy_wo(api, "F1C")
    other = api.material("F1C-OTHER", "MAKE")
    api.bom(other, raw, 1)
    with SessionLocal() as db:
        other_bom = db.scalar(select(m.Bom.id).where(m.Bom.material_id == other))
    path = f"/api/work-orders/{w['id']}/bom-confirm"
    r = api.c.post(path, json={"bom_id": b1, "reason": "x确认"}, headers=H["chejian"])
    assert r.status_code == 403
    assert api.c.post(path, json={"bom_id": b1, "reason": ""}, headers=H["jishu"]).status_code == 422
    _confirm(api, "WORK_ORDER", w["id"], status=400, bom_id=other_bom)           # 别的物料的 BOM
    _confirm(api, "WORK_ORDER", w["id"], status=400)                              # 两个都没给
    _confirm(api, "WORK_ORDER", w["id"], status=400, bom_id=b1, use_order_bom=True)  # 两个都给
    draft = api.post("/boms", {"material_id": sub, "version": "V9", "lines": [{"child_id": raw, "qty_per": 1}]})
    _confirm(api, "WORK_ORDER", w["id"], status=400, bom_id=draft["id"])           # 草稿版


def test_f1_technician_can_pick_the_old_version_the_task_was_really_opened_with(api, H):
    """旧单据是按 V1（用量 1）开的并已领料；技术之后升到 V2（用量 3）。确认时选 V1，报工按 V1 通过。"""
    raw, sub, w, pk, b1 = _legacy_wo(api, "F1D", per=1, qty=1)
    api.bom(sub, raw, 3, version="V2")                                            # 升版，V1 作废
    api.post(f"/work-orders/{w['id']}/report", {"qty": 1}, status=400)            # 未确认：不会按任何一版自动判
    r = api.c.post(f"/api/work-orders/{w['id']}/bom-confirm", json={"bom_id": b1, "reason": "工艺卡为 V1，当时按 V1 投料"},
                   headers=H["jishu"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bom_state"] == "CONFIRMED" and body["warnings"] == []
    assert api.post(f"/work-orders/{w['id']}/report", {"qty": 1})["status"] == "DONE"
    with SessionLocal() as db:
        x = db.get(m.WorkOrder, w["id"])
        assert x.bom_confirmed_by and x.bom_confirmed_at and "V1" in x.bom_confirm_note
        log = db.scalar(select(m.AuditLog).where(m.AuditLog.action == "BOM_CONFIRM", m.AuditLog.entity_id == w["id"]))
        assert log is not None and "V1" in log.detail


def test_f1_confirming_a_version_that_conflicts_with_issued_material_warns_but_does_not_decide(api, H):
    raw, sub, w, pk, b1 = _legacy_wo(api, "F1E", per=1, qty=1)
    api.post(f"/work-orders/{w['id']}/report", {"qty": 1}, status=400)
    b2 = api.bom(sub, raw, 3, version="V2")
    # 先把已完工量造出来再确认 V2：已完工 1 件需要 3，但只领了 1 → 提示
    with SessionLocal() as db:
        x = db.get(m.WorkOrder, w["id"])
        x.done_qty = Dc(1)
        db.commit()
    body = _confirm(api, "WORK_ORDER", w["id"], bom_id=b2)
    assert body["warnings"] and "不够" in body["warnings"][0]


def test_f1_confirm_twice_is_rejected_snapshot_not_rewritable(api):
    raw, sub, w, pk, b1 = _legacy_wo(api, "F1F")
    _confirm(api, "WORK_ORDER", w["id"], bom_id=b1)
    _confirm(api, "WORK_ORDER", w["id"], status=400, bom_id=b1)


def test_f1_use_order_bom_takes_the_orders_approved_lines(api):
    raw, sub, fg = api.material("F1G-RAW"), api.material("F1G-SUB", "MAKE"), api.material("F1G-FG", "PRODUCT")
    api.bom(sub, raw, 4)
    api.bom(fg, sub, 1)
    oid = api.order(fg)
    w = wo(api, sub, 1, order_id=oid)
    with SessionLocal() as db:
        order_lines = __import__("json").loads(db.get(m.SalesOrder, oid).bom_snapshot)[str(sub)]
        db.get(m.WorkOrder, w["id"]).bom_snapshot = None
        db.commit()
    _confirm(api, "WORK_ORDER", w["id"], use_order_bom=True)
    with SessionLocal() as db:
        got = __import__("json").loads(db.get(m.WorkOrder, w["id"]).bom_snapshot)
    assert got == order_lines
    stock_only = wo(api, sub, 1)
    with SessionLocal() as db:
        db.get(m.WorkOrder, stock_only["id"]).bom_snapshot = None
        db.commit()
    _confirm(api, "WORK_ORDER", stock_only["id"], status=400, use_order_bom=True)   # 无关联订单


def test_f1_cancelled_legacy_task_can_be_confirmed_then_tail_returned(api):
    raw, sub, w, pk, b1 = _legacy_wo(api, "F1H", per=2, qty=2)
    api.post(f"/work-orders/{w['id']}/cancel")
    api.post(f"/picks/{pk['id']}/return", {"reason": "x", "lines": [{"material_id": raw, "qty": 4}]}, status=400)
    _confirm(api, "WORK_ORDER", w["id"], bom_id=b1)
    api.post(f"/picks/{pk['id']}/return", {"reason": "取消退料", "lines": [{"material_id": raw, "qty": 4}]})
    assert api.qty(raw) == 4


def test_f1_consigned_outsource_same_rules_turnkey_unaffected(api):
    raw, sub = api.material("F1I-RAW"), api.material("F1I-SUB", "OUTSOURCE")
    o = osd(api, sub, 1)                                                           # 建单时没有 BOM
    assert o["bom_state"] == "PENDING_CONFIRM"
    b = api.bom(sub, raw, 1)
    api.stock(raw, 1)
    api.post(f"/outsource/{o['id']}/pick", {}, status=400)
    api.post(f"/outsource/{o['id']}/send", {}, status=400)
    _confirm(api, "OUTSOURCE", o["id"], bom_id=b)
    pk = api.post(f"/outsource/{o['id']}/pick", {})
    api.post(f"/picks/{pk['id']}/post")
    assert api.post(f"/outsource/{o['id']}/send", {})["status"] == "SENT"
    t = osd(api, api.material("F1I-TURNKEY", "OUTSOURCE"), 1, mode="TURNKEY")     # 包工包料：本厂不投料，不需要 BOM
    assert api.post(f"/outsource/{t['id']}/send", {})["status"] == "SENT"


def test_f1_pending_list_filter_and_alert_resolved_by_confirmation(api):
    raw, sub, w, pk, b1 = _legacy_wo(api, "F1J")
    ids = [x["id"] for x in api.get("/work-orders?bom_pending=true&size=500")["items"]]
    assert w["id"] in ids
    api.post("/alerts-scan", {})
    with SessionLocal() as db:
        a = db.scalar(select(m.Alert).where(m.Alert.dedupe_key == f"BOM_CONFIRM:WORK_ORDER:{w['id']}"))
        assert a is not None and a.status != "RESOLVED" and a.target_role == m.Role.TECH
    _confirm(api, "WORK_ORDER", w["id"], bom_id=b1)
    with SessionLocal() as db:
        a = db.scalar(select(m.Alert).where(m.Alert.dedupe_key == f"BOM_CONFIRM:WORK_ORDER:{w['id']}"))
        assert a.status == "RESOLVED"
    assert w["id"] not in [x["id"] for x in api.get("/work-orders?bom_pending=true&size=500")["items"]]


def test_f1_concurrent_confirmation_succeeds_exactly_once(api, H):
    raw, sub, w, pk, b1 = _legacy_wo(api, "F1K")
    gate = threading.Barrier(6, timeout=20)

    def go(_):
        gate.wait()
        return api.c.post(f"/api/work-orders/{w['id']}/bom-confirm", json={"bom_id": b1, "reason": "并发确认"},
                          headers=H["jishu"]).status_code

    with ThreadPoolExecutor(6) as ex:
        codes = list(ex.map(go, range(6)))
    assert codes.count(200) == 1 and not [c for c in codes if c >= 500], codes


# ================================================================== F-2 期初库存导入防重复


def _stock_file(rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["物料编码", "仓库编码", "库位编码", "批次号", "数量", "单价", "入库日期"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return ("a.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _imp(api, rows, dry=False):
    r = api.c.post(f"/api/import/stock?dry_run={'true' if dry else 'false'}", files={"file": _stock_file(rows)},
                   headers=api.h)
    assert r.status_code == 200, r.text
    return r.json()


def _mat_wh(api, label):
    code = f"{label}-{tag()}"
    mid = api.post("/materials", {"code": code, "name": code, "type": "BUY"})["id"]
    with SessionLocal() as db:
        whs = {w.purpose: w.code for w in db.scalars(select(m.Warehouse)).all()}
    return code, mid, whs["PRODUCTION"], whs.get("AFTERSALES")


def test_f2_same_file_twice_is_rejected_stock_not_doubled(api):
    code, mid, whc, _ = _mat_wh(api, "F2A")
    rows = [[code, whc, "", "LOT-" + tag(), 1, 1, "2026-10-01"]]
    assert _imp(api, rows)["committed"] is True
    r2 = _imp(api, rows)
    assert r2["committed"] is False and r2["error_count"] == 1 and "重复" in r2["errors"][0]["error"]
    assert api.qty(mid) == 1


def test_f2_duplicate_rejected_even_after_the_lot_was_fully_consumed(api):
    code, mid, whc, _ = _mat_wh(api, "F2B")
    rows = [[code, whc, "", "LOT-" + tag(), 1, 1, "2026-10-01"]]
    _imp(api, rows)
    fg = api.material("F2B-FG", "PRODUCT")
    api.bom(fg, mid)
    oid = api.order(fg)
    pk = api.post("/picks", {"order_id": oid})
    api.post(f"/picks/{pk['id']}/post")
    assert api.qty(mid) == 0
    assert _imp(api, rows)["committed"] is False
    assert api.qty(mid) == 0


def test_f2_blank_lot_number_reimport_is_also_caught(api):
    code, mid, whc, _ = _mat_wh(api, "F2C")
    rows = [[code, whc, "", "", 2, 1, "2026-10-01"]]
    assert _imp(api, rows)["committed"] is True
    assert _imp(api, rows)["committed"] is False
    assert api.qty(mid) == 2


def test_f2_duplicate_inside_one_file_is_rejected(api):
    code, mid, whc, _ = _mat_wh(api, "F2D")
    lot = "LOT-" + tag()
    r = _imp(api, [[code, whc, "", lot, 1, 1, "2026-10-01"], [code, whc, "", lot, 1, 1, "2026-10-01"]])
    assert r["committed"] is False and r["error_count"] == 1
    assert api.qty(mid) == 0


def test_f2_mixed_file_new_row_is_not_written_when_another_row_is_a_duplicate(api):
    c1, m1, whc, _ = _mat_wh(api, "F2E1")
    c2, m2, _, _ = _mat_wh(api, "F2E2")
    lot1 = "LOT-" + tag()
    _imp(api, [[c1, whc, "", lot1, 1, 1, "2026-10-01"]])
    r = _imp(api, [[c2, whc, "", "LOT-" + tag(), 1, 1, "2026-10-01"], [c1, whc, "", lot1, 1, 1, "2026-10-01"]])
    assert r["committed"] is False
    assert api.qty(m1) == 1 and api.qty(m2) == 0                         # 整批不入库


def test_f2_same_lot_number_in_another_warehouse_or_for_another_material_is_allowed(api):
    c1, m1, whc, whc2 = _mat_wh(api, "F2F1")
    c2, m2, _, _ = _mat_wh(api, "F2F2")
    lot = "LOT-" + tag()
    assert whc2
    r = _imp(api, [[c1, whc, "", lot, 1, 1, "2026-10-01"], [c1, whc2, "", lot, 1, 1, "2026-10-01"],
                   [c2, whc, "", lot, 1, 1, "2026-10-01"]])
    assert r["committed"] is True and api.qty(m1) == 2 and api.qty(m2) == 1


def test_f2_dry_run_reports_duplicates_and_writes_nothing(api):
    code, mid, whc, _ = _mat_wh(api, "F2G")
    rows = [[code, whc, "", "LOT-" + tag(), 1, 1, "2026-10-01"]]
    _imp(api, rows)
    pre = _imp(api, rows, dry=True)
    assert pre["committed"] is False and pre["error_count"] == 1
    assert api.qty(mid) == 1


def test_f2_concurrent_identical_imports_land_once(api):
    code, mid, whc, _ = _mat_wh(api, "F2H")
    f = _stock_file([[code, whc, "", "LOT-" + tag(), 1, 1, "2026-10-01"]])
    gate = threading.Barrier(4, timeout=20)

    def go(_):
        gate.wait()
        return api.c.post("/api/import/stock?dry_run=false", files={"file": f}, headers=api.h).json().get("committed")

    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(go, range(4)))
    assert res.count(True) == 1, res
    assert api.qty(mid) == 1
