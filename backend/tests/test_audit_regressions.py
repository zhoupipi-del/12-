"""外部源码审计（12 项缺陷复现）的回归测试：审计脚本里“坏行为出现”的断言，这里全部反过来——坏行为必须被拒绝。

对应关系：01 静态路径越界 · 02 默认密钥(生产启动拒绝) · 03 展示密钥越权(见 test_api_flows) · 04 幂等键
05 计划工单掩盖缺料 · 06 报工不耗料 · 07 整机未领料完工 · 08 领走他单锁料 · 09 子 BOM 升版改已下达订单
10 让步接收批次未冲回 · 11 库存提交与重算原子性 · 12 有图号无受控图纸仍放行
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal as Dc

import pytest
from sqlalchemy import func, select

from app import models as m
from app.db import SessionLocal
from app.services import inventory as inv

TODAY = date.today()


@pytest.fixture(scope="module", autouse=True)
def _seeded(client):
    return client


def tag():
    return uuid.uuid4().hex[:6].upper()


class Api:
    def __init__(self, client, H):
        self.c, self.h = client, H["admin"]

    def call(self, method, path, data=None, status=200, headers=None):
        r = self.c.request(method, "/api" + path, json=data, headers=headers or self.h)
        assert r.status_code == status, (path, r.status_code, r.text)
        return r.json() if "application/json" in r.headers.get("content-type", "") else {}

    def post(self, path, data=None, **kw):
        return self.call("POST", path, data, **kw)

    def get(self, path):
        return self.call("GET", path)

    def material(self, code, kind="BUY", drawing="", **kw):
        return self.post("/materials", {"code": f"{code}-{tag()}", "name": code, "type": kind, "drawing_no": drawing,
                                        "lead_time_days": 0, **kw})["id"]

    def bom(self, parent, child, per=1, version="V1"):
        b = self.post("/boms", {"material_id": parent, "version": version, "lines": [{"child_id": child, "qty_per": per}]})
        self.post(f"/boms/{b['id']}/activate")
        return b["id"]

    def order(self, product, priority=3, qty=1):
        o = self.post("/orders", {"customer": "REG", "product_id": product, "qty": qty,
                                  "planned_start": (TODAY + timedelta(days=20)).isoformat(),
                                  "due_date": (TODAY + timedelta(days=25)).isoformat(), "priority": priority})
        self.post(f"/orders/{o['id']}/release")
        return o["id"]

    def stock(self, mid, qty):
        with SessionLocal() as db:
            lot = inv.stock_in(db, material_id=mid, warehouse_id=inv.default_warehouse(db).id, qty=Dc(qty),
                               txn_type=m.TxnType.ADJUST, ref_type="REG", user_id=1)
            db.commit()
            lid = lot.id
        self.post("/kitting/recalc")
        return lid

    def qty(self, mid):
        with SessionLocal() as db:
            return Dc(db.scalar(select(func.coalesce(func.sum(m.StockLot.qty), 0)).where(m.StockLot.material_id == mid)) or 0)

    def po(self, mid, qty=10):
        with SessionLocal() as db:
            sup = db.scalar(select(m.Supplier).where(m.Supplier.is_outsourcer.is_(False))).id
        po = self.post("/purchase-orders", {"supplier_id": sup, "lines": [
            {"material_id": mid, "qty": qty, "price": 1, "need_date": (TODAY + timedelta(days=10)).isoformat()}]})
        self.post(f"/purchase-orders/{po['id']}/approve")
        return po["id"], po["lines"][0]["id"]


@pytest.fixture
def api(client, H):
    return Api(client, H)


def test_01_static_handler_never_serves_files_outside_frontend(client, tmp_path):
    from app.main import DIST
    if not DIST.is_dir():
        pytest.skip("前端未构建")
    marker = tmp_path / "outside.txt"
    marker.write_text("OUTSIDE-MARKER")
    for p in ("/%2F" + str(marker).lstrip("/").replace("/", "%2F"), "/..%2F..%2F" + "x", "/%2e%2e/%2e%2e/etc/passwd"):
        r = client.get(p)
        assert "OUTSIDE-MARKER" not in r.text and "root:" not in r.text
        assert r.status_code in (200, 404)  # 回退到首页或不存在，绝不是目录外文件


def test_02_production_refuses_default_signing_key(monkeypatch):
    from app.config import DEFAULT_SECRET, settings
    from app.core import guard
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "secret_key", DEFAULT_SECRET)
    assert any("SECRET_KEY" in x for x in guard.problems())
    with pytest.raises(RuntimeError):
        guard.enforce()


def test_04_same_idempotency_key_registers_arrival_once(api):
    mid = api.material("REG-RETRY")
    po, lid = api.po(mid, 10)
    h = {**api.h, "Idempotency-Key": "k-" + tag()}
    data = {"source_type": "PO", "source_id": lid, "qty": 1}
    a = api.post("/arrivals", data, headers=h)
    b = api.post("/arrivals", data, headers=h)
    assert a["id"] == b["id"]
    assert Dc(str(api.get(f"/purchase-orders/{po}")["lines"][0]["arrived_qty"])) == 1
    api.post("/arrivals", {**data, "qty": 2}, headers=h, status=409)  # 同键不同内容 → 冲突


def test_05_planned_work_order_does_not_hide_raw_shortage(api):
    raw, sub, prod = api.material("REG-RAW"), api.material("REG-SUB", "MAKE"), api.material("REG-FG", "PRODUCT")
    api.bom(sub, raw)
    api.bom(prod, sub)
    oid = api.order(prod)
    api.post("/work-orders", {"material_id": sub, "qty": 1, "order_id": oid,
                              "planned_finish": (TODAY + timedelta(days=3)).isoformat()})
    assert api.get(f"/orders/{oid}")["kit_status"] != "INCOMING_OK"
    assert any(s["material_id"] == raw for s in api.get("/kitting/shortages"))


def test_06_work_order_cannot_report_output_without_issued_raw(api):
    raw, sub = api.material("REG-WO-RAW"), api.material("REG-WO-SUB", "MAKE")
    api.bom(sub, raw, 2)
    w = api.post("/work-orders", {"material_id": sub, "qty": 1})
    api.post(f"/work-orders/{w['id']}/start")
    api.post(f"/work-orders/{w['id']}/report", {"qty": 1}, status=400)
    assert api.qty(sub) == 0
    api.stock(raw, 2)
    pk = api.post(f"/work-orders/{w['id']}/pick", {})
    api.post(f"/picks/{pk['id']}/post")
    api.post(f"/work-orders/{w['id']}/report", {"qty": 1})
    assert api.qty(sub) == 1 and api.qty(raw) == 0


def test_07_order_cannot_complete_without_issuing_components(api):
    raw, prod = api.material("REG-FG-RAW"), api.material("REG-FG2", "PRODUCT")
    api.bom(prod, raw, 2)
    api.stock(raw, 2)
    oid = api.order(prod)
    api.post(f"/orders/{oid}/start", {})
    api.post(f"/orders/{oid}/complete", status=400)
    assert api.qty(prod) == 0 and api.qty(raw) == 2
    pk = api.post("/picks", {"order_id": oid})
    api.post(f"/picks/{pk['id']}/post")
    api.post(f"/orders/{oid}/complete")
    assert api.qty(prod) == 1 and api.qty(raw) == 0


def test_08_other_order_cannot_pick_locked_material(api):
    raw, prod = api.material("REG-LOCK-RAW"), api.material("REG-LOCK-FG", "PRODUCT")
    api.bom(prod, raw)
    api.stock(raw, 1)
    a, b = api.order(prod, 1), api.order(prod, 4)
    api.post(f"/orders/{a}/lock", {"material_id": raw, "qty": 1, "reason": "回归：受保护预占"})
    pk = api.post("/picks", {"order_id": b, "lines": [{"material_id": raw, "qty": 1}]})
    api.post(f"/picks/{pk['id']}/post", status=400)
    assert api.qty(raw) == 1
    # 被锁的订单自己可以领
    pa = api.post("/picks", {"order_id": a, "lines": [{"material_id": raw, "qty": 1}]})
    api.post(f"/picks/{pa['id']}/post")
    assert api.qty(raw) == 0


def test_09_child_bom_upgrade_does_not_change_released_order(api):
    raw, sub, prod = api.material("REG-BOM-RAW"), api.material("REG-BOM-SUB", "MAKE"), api.material("REG-BOM-FG", "PRODUCT")
    api.bom(sub, raw, 1)
    api.bom(prod, sub)
    oid = api.order(prod)
    need = lambda: next(x["required"] for x in api.get(f"/orders/{oid}")["kit_lines"] if x["material_id"] == raw)  # noqa: E731
    assert Dc(str(need())) == 1
    api.bom(sub, raw, 2, version="V2")
    api.post("/kitting/recalc")
    assert Dc(str(need())) == 1                                           # 已下达订单仍按下达时冻结的 BOM
    o2 = api.order(prod)                                                  # 新下达的订单采用新版
    n2 = next(x["required"] for x in api.get(f"/orders/{o2}")["kit_lines"] if x["material_id"] == raw)
    assert Dc(str(n2)) == 2


def test_10_concession_stock_is_reversed_with_purchase_line(api):
    raw = api.material("REG-CONC")
    po, lid = api.po(raw, 2)
    a = api.post("/arrivals", {"source_type": "PO", "source_id": lid, "qty": 1})
    api.post(f"/arrivals/{a['id']}/inspect", {"qualified": 0, "rejected": 1, "reject_reason": "缺陷"})
    eid = next(e["id"] for e in api.get("/exceptions")["items"] if e["material_id"] == raw)
    api.post(f"/exceptions/{eid}/dispose", {"disposition": "CONCESSION", "responsibility": "SUPPLIER", "comment": "让步"})
    api.post(f"/exceptions/{eid}/close", {"result": "关闭"})
    assert api.qty(raw) == 1
    imp = api.get(f"/po-lines/{lid}/impact")
    assert any(i["type"] == "异常处置入库批次" for i in imp["items"])
    api.post(f"/po-lines/{lid}/reverse", {"reason": "回归：更正"})
    assert api.qty(raw) == 0                                               # 派生批次一并冲回
    a2 = api.post("/arrivals", {"source_type": "PO", "source_id": lid, "qty": 2})
    api.post(f"/arrivals/{a2['id']}/inspect", {"qualified": 2})
    assert api.qty(raw) == 2                                               # 不再出现 3


def test_10b_consumed_concession_lot_blocks_reversal(api):
    raw = api.material("REG-CONC2")
    po, lid = api.po(raw, 2)
    a = api.post("/arrivals", {"source_type": "PO", "source_id": lid, "qty": 1})
    api.post(f"/arrivals/{a['id']}/inspect", {"qualified": 0, "rejected": 1, "reject_reason": "缺陷"})
    eid = next(e["id"] for e in api.get("/exceptions")["items"] if e["material_id"] == raw)
    api.post(f"/exceptions/{eid}/dispose", {"disposition": "CONCESSION", "responsibility": "SUPPLIER", "comment": "让步"})
    api.post(f"/exceptions/{eid}/close", {"result": "关闭"})
    with SessionLocal() as db:
        inv.fifo_out(db, material_id=raw, warehouse_id=inv.default_warehouse(db).id, qty=Dc(1),
                     txn_type=m.TxnType.PICK_OUT, ref_type="REG", user_id=1)
        db.commit()
    api.post(f"/po-lines/{lid}/reverse", {"reason": "回归"}, status=409)


def test_11_failed_recalc_rolls_back_inventory_change(api, monkeypatch):
    from app.api import util
    raw = api.material("REG-ATOMIC")
    lid = api.stock(raw, 3)

    def boom(_db, *a, **k):
        raise RuntimeError("模拟重算失败")
    monkeypatch.setattr(util, "recalc", boom)
    with pytest.raises(RuntimeError):                                      # 会话级 TestClient 会把服务端异常抛出来
        api.c.request("POST", "/api/stock/adjust", json={"lot_id": lid, "new_qty": 2, "remark": "回归"}, headers=api.h)
    monkeypatch.undo()
    assert api.qty(raw) == 3                                               # 整笔回滚，库存没有“先变了”


def test_12_drawing_no_without_controlled_drawing_blocks_start(api):
    sub = api.material("REG-DWG", "MAKE", drawing="NOT-ARCHIVED-" + tag())
    w = api.post("/work-orders", {"material_id": sub, "qty": 1})
    req = api.c.get("/api/drawings-required", params={"ref_type": "WORK_ORDER", "ref_id": w["id"]}, headers=api.h).json()
    assert req and req[0].get("missing")
    api.post(f"/work-orders/{w['id']}/start", status=400)
    # 确无图纸的标准件：明确勾选免管控后才放行
    ex = api.material("REG-DWG-EX", "MAKE", drawing="STD-" + tag(), drawing_exempt=True, bom_exempt=True)
    w2 = api.post("/work-orders", {"material_id": ex, "qty": 1})
    assert api.post(f"/work-orders/{w2['id']}/start")["status"] == "STARTED"
