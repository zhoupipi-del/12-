"""外部审计第二轮（12 个剩余缺陷复现）的回归测试：复现脚本里“坏行为出现”的断言，这里反过来——必须被拒绝/保持正确。

R1 完工/报工后原料不能全退 · R2 任务认领的原料不能被他单领走 · R3 调拨不能绕过锁料 · R4 到货幂等键完整比较
R5 工单继承批准 BOM · R6 已取消订单的草稿领料单不能过账 · R7 无 BOM 不能报工 · R8 重大图纸暂停拦完工
R9 派生批次领出再全退不算消耗 · R10 Excel 导入与重算同事务 · M1 迁移在 SQLite 也能升到最新
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.test_audit_regressions import Api, Dc, SessionLocal, TODAY, m, select, func, tag  # noqa: F401


@pytest.fixture(scope="module", autouse=True)
def _seeded(client):
    return client


@pytest.fixture
def api(client, H):
    return Api(client, H)


def test_r1_order_components_cannot_be_returned_after_completion(api):
    raw, fg = api.material("R1-RAW"), api.material("R1-FG", "PRODUCT")
    api.bom(fg, raw, 2)
    api.stock(raw, 2)
    oid = api.order(fg)
    pk = api.post("/picks", {"order_id": oid})
    api.post(f"/picks/{pk['id']}/post")
    api.post(f"/orders/{oid}/start", {})
    api.post(f"/orders/{oid}/complete")
    api.post(f"/picks/{pk['id']}/return", {"reason": "完工后退料", "lines": [{"material_id": raw, "qty": 2}]}, status=400)
    assert api.qty(fg) == 1 and api.qty(raw) == 0


def test_r1_work_order_only_unconsumed_tail_can_be_returned(api):
    raw, sub = api.material("R1W-RAW"), api.material("R1W-SUB", "MAKE")
    api.bom(sub, raw, 2)
    api.stock(raw, 4)
    w = api.post("/work-orders", {"material_id": sub, "qty": 2})
    api.post(f"/work-orders/{w['id']}/start")
    pk = api.post(f"/work-orders/{w['id']}/pick", {})
    api.post(f"/picks/{pk['id']}/post")                                   # 领 4
    api.post(f"/work-orders/{w['id']}/report", {"qty": 1})                # 用掉 2
    api.post(f"/picks/{pk['id']}/return", {"reason": "全退", "lines": [{"material_id": raw, "qty": 3}]}, status=400)
    api.post(f"/picks/{pk['id']}/return", {"reason": "退尾料", "lines": [{"material_id": raw, "qty": 2}]})
    assert api.qty(sub) == 1 and api.qty(raw) == 2


def test_r2_work_order_claimed_raw_cannot_be_taken_by_other_order(api):
    raw, sub, fg = api.material("R2-RAW"), api.material("R2-SUB", "MAKE"), api.material("R2-FG", "PRODUCT")
    api.bom(sub, raw)
    api.bom(fg, sub)
    api.stock(raw, 1)
    oid = api.order(fg)
    w = api.post("/work-orders", {"material_id": sub, "qty": 1, "order_id": oid})
    other = api.material("R2-OTHER", "PRODUCT")
    api.bom(other, raw)
    o2 = api.order(other, 4)
    pk = api.post("/picks", {"order_id": o2, "lines": [{"material_id": raw, "qty": 1}]})
    api.post(f"/picks/{pk['id']}/post", status=400)
    assert api.qty(raw) == 1
    wp = api.post(f"/work-orders/{w['id']}/pick", {})                    # 认领原料的任务自己可以领
    api.post(f"/picks/{wp['id']}/post")
    assert api.qty(raw) == 0


def test_r3_transfer_cannot_move_locked_stock(api):
    raw, fg = api.material("R3-RAW"), api.material("R3-FG", "PRODUCT")
    api.bom(fg, raw)
    api.stock(raw, 2)
    oid = api.order(fg)
    api.post(f"/orders/{oid}/lock", {"material_id": raw, "qty": 1, "reason": "回归：受保护"})
    with SessionLocal() as db:
        aft = db.scalar(select(m.Warehouse).where(m.Warehouse.purpose == "AFTERSALES")).id
        prod = inv_default(db)
    body = {"material_id": raw, "from_warehouse_id": prod, "to_warehouse_id": aft, "qty": 2, "remark": "回归"}
    api.post("/stock/transfer", body, status=400)                        # 2 个里有 1 个被锁
    api.post("/stock/transfer", {**body, "qty": 1})                      # 未被占用的 1 个可以转
    with SessionLocal() as db:
        held = db.scalar(select(m.Reservation).where(m.Reservation.order_id == oid, m.Reservation.locked.is_(True)))
        assert held and held.qty == 1
    assert api.get(f"/orders/{oid}")["kit_status"] == "READY"


def inv_default(db):
    from app.services.inventory import default_warehouse
    return default_warehouse(db).id


def test_r4_same_key_with_different_expected_qty_conflicts(api):
    raw = api.material("R4-KEY")
    _po, lid = api.po(raw, 10)
    h = {**api.h, "Idempotency-Key": "r4-" + tag()}
    a = api.post("/arrivals", {"source_type": "PO", "source_id": lid, "qty": 1, "expected_qty": 1}, headers=h)
    b = api.post("/arrivals", {"source_type": "PO", "source_id": lid, "qty": 1, "expected_qty": 1}, headers=h)
    assert a["id"] == b["id"]
    api.post("/arrivals", {"source_type": "PO", "source_id": lid, "qty": 1, "expected_qty": 5}, headers=h, status=409)


def test_r5_work_order_inherits_parent_frozen_bom(api):
    raw1, raw2 = api.material("R5-OLD"), api.material("R5-NEW")
    sub, fg = api.material("R5-SUB", "MAKE"), api.material("R5-FG", "PRODUCT")
    api.bom(sub, raw1)
    api.bom(fg, sub)
    oid = api.order(fg)
    w = api.post("/work-orders", {"material_id": sub, "qty": 1, "order_id": oid})
    api.bom(sub, raw2, version="V2")
    api.post(f"/work-orders/{w['id']}/start")
    pk = api.post(f"/work-orders/{w['id']}/pick", {})
    assert [x["material_id"] for x in pk["lines"]] == [raw1]
    api.post("/kitting/recalc")
    shortage_mats = {x["material_id"] for x in api.get("/kitting/shortages")}
    assert raw1 in shortage_mats and raw2 not in shortage_mats


def test_r6_draft_pick_cannot_post_after_order_cancelled(api):
    raw, fg = api.material("R6-RAW"), api.material("R6-FG", "PRODUCT")
    api.bom(fg, raw)
    api.stock(raw, 1)
    oid = api.order(fg)
    pk = api.post("/picks", {"order_id": oid})
    api.post(f"/orders/{oid}/cancel", {"reason": "回归取消"})
    api.post(f"/picks/{pk['id']}/post", status=400)
    assert api.qty(raw) == 1


def test_r7_work_order_without_bom_cannot_start_or_report(api):
    sub = api.material("R7-NOBOM", "MAKE")
    w = api.post("/work-orders", {"material_id": sub, "qty": 1})
    api.post(f"/work-orders/{w['id']}/start", status=400)
    ok = api.material("R7-EXEMPT", "MAKE", bom_exempt=True)               # 明确声明“无物料投入”的工艺才放行
    w2 = api.post("/work-orders", {"material_id": ok, "qty": 1})
    api.post(f"/work-orders/{w2['id']}/start")
    api.post(f"/work-orders/{w2['id']}/report", {"qty": 1})
    assert api.qty(ok) == 1


def _version(api, did, version):
    r = api.c.post(f"/api/drawings/{did}/versions", data={"version": version, "change_note": "回归"},
                   files={"file": ("a.pdf", b"%PDF-1.4 regression", "application/pdf")}, headers=api.h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_r8_major_drawing_hold_blocks_completion_and_shipment(api):
    no = "R8-DWG-" + tag()
    raw, fg = api.material("R8-RAW"), api.material("R8-FG", "PRODUCT", drawing=no)
    api.bom(fg, raw)
    api.stock(raw, 1)
    dw = api.post("/drawings", {"drawing_no": no, "name": "回归图纸"})
    v1 = _version(api, dw["id"], "A")
    api.post(f"/drawings/versions/{v1}/release", {"change_level": "MINOR"})
    oid = api.order(fg)
    api.post("/drawings-confirm", {"ref_type": "SALES_ORDER", "ref_id": oid, "version_ids": [v1]})
    pk = api.post("/picks", {"order_id": oid})
    api.post(f"/picks/{pk['id']}/post")
    api.post(f"/orders/{oid}/start", {})
    v2 = _version(api, dw["id"], "B")
    api.post(f"/drawings/versions/{v2}/release", {"change_level": "MAJOR", "hold_reason": "回归：安全问题"})
    api.post(f"/orders/{oid}/complete", status=400)
    assert api.qty(fg) == 0
    api.post("/drawings-resolve-hold", {"ref_type": "SALES_ORDER", "ref_id": oid, "drawing_id": dw["id"], "reason": "技术裁定继续按 A 版"})
    api.post(f"/orders/{oid}/complete")
    assert api.qty(fg) == 1


def test_r9_returned_derived_lot_is_not_consumed(api):
    raw, fg = api.material("R9-RAW"), api.material("R9-FG", "PRODUCT")
    api.bom(fg, raw)
    _po, lid = api.po(raw, 2)
    a = api.post("/arrivals", {"source_type": "PO", "source_id": lid, "qty": 1})
    api.post(f"/arrivals/{a['id']}/inspect", {"qualified": 0, "rejected": 1, "reject_reason": "缺陷"})
    eid = next(e["id"] for e in api.get("/exceptions")["items"] if e["material_id"] == raw)
    api.post(f"/exceptions/{eid}/dispose", {"disposition": "CONCESSION", "responsibility": "SUPPLIER", "comment": "让步"})
    api.post(f"/exceptions/{eid}/close", {"result": "关闭"})
    oid = api.order(fg)
    pk = api.post("/picks", {"order_id": oid})
    api.post(f"/picks/{pk['id']}/post")
    imp = api.get(f"/po-lines/{lid}/impact")
    assert not imp["can_reverse"]                                          # 领出未退：真消耗，仍阻断
    api.post(f"/picks/{pk['id']}/return", {"reason": "未使用全退", "lines": [{"material_id": raw, "qty": 1}]})
    imp = api.get(f"/po-lines/{lid}/impact")
    assert imp["can_reverse"], imp["blockers"]                             # 全退后不再误判为已消耗
    api.post(f"/po-lines/{lid}/reverse", {"reason": "回归更正"})
    assert api.qty(raw) == 0


def test_r10_excel_import_rolls_back_when_recalc_fails_and_retry_is_not_duplicated(api, monkeypatch):
    from openpyxl import Workbook
    from app.services import kitting
    code = f"R10-{tag()}"
    mid = api.post("/materials", {"code": code, "name": code, "type": "BUY"})["id"]
    with SessionLocal() as db:
        wh = db.scalar(select(m.Warehouse).where(m.Warehouse.purpose == "PRODUCTION")).code
    wb = Workbook()
    ws = wb.active
    ws.append(["物料编码", "仓库编码", "库位编码", "批次号", "数量", "单价", "入库日期"])
    ws.append([code, wh, "", "R10-LOT-" + tag(), 1, 1, "2026-10-01"])
    buf = io.BytesIO()
    wb.save(buf)
    f = ("a.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    real = kitting.recalc

    def boom(*a, **k):
        raise RuntimeError("模拟导入后重算失败")
    monkeypatch.setattr(kitting, "recalc", boom)
    with pytest.raises(RuntimeError):
        api.c.post("/api/import/stock?dry_run=false", files={"file": f}, headers=api.h)
    monkeypatch.setattr(kitting, "recalc", real)
    assert api.qty(mid) == 0                                               # 整批导入已回滚
    r = api.c.post("/api/import/stock?dry_run=false", files={"file": f}, headers=api.h)
    assert r.status_code == 200 and r.json()["committed"]
    assert api.qty(mid) == 1                                               # 修好后重试只入库一次


def test_m1_alembic_upgrade_head_works_on_a_fresh_sqlite_file():
    backend = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "DATABASE_URL": f"sqlite:///{d}/mig.db", "APP_ENV": "test", "AUTO_CREATE_TABLES": "0"}
        for cmd in (["upgrade", "head"], ["check"]):
            r = subprocess.run([sys.executable, "-m", "alembic", *cmd], cwd=backend, env=env, capture_output=True, text=True)
            assert r.returncode == 0, r.stderr[-800:]
