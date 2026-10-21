"""U9 四项集中补齐的回归：备料台、订单贯通、急单任务排序、迁移/自动预警。"""
from datetime import date, timedelta
from decimal import Decimal as D
import io

from openpyxl import Workbook
import pytest
from sqlalchemy import select

from app.services import importer
from app.services.kitting import ProdDemand, Snapshot, compute, load_snapshot
from app.services.production import _validate_task_source
from app.main import run_scheduled_alert_scan
from app.db import SessionLocal
from app.core.errors import BizError
from app.models import Material, MaterialType, OrderStatus, SalesOrder


def test_material_plan_workbench_has_stock_occupation_history(client, H):
    client.post("/api/kitting/recalc", headers=H["jihua"])
    orders = client.get("/api/kitting/overview", headers=H["jihua"]).json()["items"]
    assert orders
    r = client.get(f"/api/orders/{orders[0]['id']}/material-plan", headers=H["jihua"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["order"]["id"] == orders[0]["id"]
    assert body["lines"]
    assert {"stock_total", "occupied_by_others", "stock_free", "purchase_history"} <= set(body["lines"][0])


def test_material_plan_uses_same_task_occupation_as_issue_guard(client, H):
    client.post("/api/kitting/recalc", headers=H["jihua"])
    with SessionLocal() as db:
        grants = compute(load_snapshot(db)).production_stock
        expected = {}
        for (_kind, _ref, mid), qty in grants.items():
            expected[mid] = expected.get(mid, D(0)) + qty
    assert expected, "演示数据应存在被任务保护的原料"
    checked = False
    orders = client.get("/api/kitting/overview", headers=H["jihua"]).json()["items"]
    for order in orders:
        lines = client.get(f"/api/orders/{order['id']}/material-plan", headers=H["jihua"]).json()["lines"]
        for line in lines:
            if line["material_id"] not in expected:
                continue
            checked = True
            assert D(str(line["task_occupied"])) == expected[line["material_id"]]
            occupied = D(str(line["occupied_by_others"])) + D(str(line["own_reserved"]))
            assert D(str(line["stock_free"])) == max(D(str(line["stock_total"])) - occupied, D(0))
    assert checked, "至少一个订单备料行应展示任务占用"


def test_order_options_and_linked_work_order_api(client, H):
    opts = client.get("/api/orders/options", headers=H["jihua"])
    assert opts.status_code == 200 and opts.json()
    order_id = opts.json()[0]["id"]
    client.post("/api/kitting/recalc", headers=H["jihua"])
    lines = client.get(f"/api/orders/{order_id}/material-plan", headers=H["jihua"]).json()["lines"]
    make = next((x for x in lines if x["type"] == "MAKE"), None)
    assert make, "演示订单应包含一个自制件"
    r = client.post("/api/work-orders", headers=H["jihua"], json={
        "source_mode": "ORDER", "order_id": order_id, "material_id": make["material_id"], "qty": 1,
        "planned_start": "2026-10-22", "planned_finish": "2026-10-25", "owner": "U9回归",
    })
    assert r.status_code == 200, r.text
    assert r.json()["source_mode"] == "ORDER" and r.json()["order_id"] == order_id and r.json()["order_no"]


def test_task_source_cannot_be_implicit_or_mixed(client, H):
    make = client.get("/api/materials", params={"type": "MAKE", "size": 100}, headers=H["jihua"]).json()["items"][0]
    base = {"material_id": make["id"], "qty": 1}
    assert client.post("/api/work-orders", headers=H["jihua"], json=base).status_code == 422
    assert client.post("/api/work-orders", headers=H["jihua"],
                       json={**base, "source_mode": "ORDER"}).status_code == 400
    order_id = client.get("/api/orders/options", headers=H["jihua"]).json()[0]["id"]
    assert client.post("/api/work-orders", headers=H["jihua"],
                       json={**base, "source_mode": "STOCK", "order_id": order_id}).status_code == 400
    ok = client.post("/api/work-orders", headers=H["jihua"], json={**base, "source_mode": "STOCK"})
    assert ok.status_code == 200 and ok.json()["source_mode"] == "STOCK" and ok.json()["order_id"] is None


def test_order_task_rejects_wrong_bom_material_and_ended_order(client):
    with SessionLocal() as db:
        order = db.scalar(select(SalesOrder).where(SalesOrder.status.in_(OrderStatus.ACTIVE_FOR_KIT)))
        rogue = Material(code=f"U9-WRONG-{order.id}", name="不属于该订单的自制件", unit="件",
                         type=MaterialType.MAKE)
        db.add(rogue)
        db.flush()
        with pytest.raises(BizError, match="不在订单.*批准 BOM"):
            _validate_task_source(db, {"source_mode": "ORDER", "order_id": order.id}, rogue.id)
        order.status = OrderStatus.CANCELLED
        db.flush()
        with pytest.raises(BizError, match="已结束或取消"):
            _validate_task_source(db, {"source_mode": "ORDER", "order_id": order.id}, rogue.id)
        db.rollback()


def test_urgent_order_priority_reaches_task_raw_material_allocation():
    today = date(2026, 10, 21)
    raw = 99
    low = ProdDemand("WO", 1, "普通单任务", 10, [(raw, D(1))], False, today + timedelta(days=1), 1,
                     (0, 9999, 4, today + timedelta(days=1), "SO-LOW"))
    urgent = ProdDemand("WO", 2, "急单任务", 11, [(raw, D(1))], False, today + timedelta(days=2), 2,
                        (0, 9999, 1, today + timedelta(days=2), "SO-URGENT"))
    snap = Snapshot(today, 2, [], {}, {}, {raw: D(1)}, {}, {}, [], prod_demands=[low, urgent])
    result = compute(snap)
    assert result.production_stock[("WO", 2, raw)] == D(1)
    assert result.production_stock.get(("WO", 1, raw), D(0)) == D(0)


def test_u9_cutover_templates_cover_required_balances():
    assert {"suppliers", "materials", "boms", "stock", "orders", "open_pos", "po_history"} <= set(importer.TEMPLATES)
    assert {"检验转运天数", "免BOM", "免图纸管控"} <= set(importer.TEMPLATES["materials"])
    for kind in ("suppliers", "orders", "open_pos", "po_history"):
        assert importer.template(kind)[:2] == b"PK"


def _xlsx(rows):
    wb = Workbook(); ws = wb.active
    for row in rows:
        ws.append(row)
    out = io.BytesIO(); wb.save(out)
    return out.getvalue()


def test_u9_cutover_imports_supplier_open_po_and_price_history(client, H):
    supplier = _xlsx([["供应商编码", "供应商名称", "联系人", "电话", "是否外协厂"],
                      ["U9-GYS-001", "U9迁移供应商", "迁移员", "13800000000", "否"]])
    r = client.post("/api/import/suppliers", params={"dry_run": False}, headers=H["admin"],
                    files={"file": ("supplier.xlsx", supplier)}).json()
    assert r["committed"] and r["created"] == 1

    buy = client.get("/api/materials", params={"type": "BUY", "size": 100}, headers=H["admin"]).json()["items"][0]
    open_po = _xlsx([["采购单号", "供应商编码", "物料编码", "未到数量", "单价", "需求日期", "承诺日期", "关联订单号", "采购员用户名"],
                     ["U9-OPEN-001", "U9-GYS-001", buy["code"], 2, 88.5, "2026-11-01", "2026-10-30", "", "caigou"]])
    r = client.post("/api/import/open_pos", params={"dry_run": False}, headers=H["admin"],
                    files={"file": ("open.xlsx", open_po)}).json()
    assert r["committed"] and r["created"] == 1

    history = _xlsx([["采购日期", "采购单号", "供应商编码", "物料编码", "数量", "单价"],
                     ["2026-01-08", "U9-HIS-001", "U9-GYS-001", buy["code"], 5, 77.5]])
    r = client.post("/api/import/po_history", params={"dry_run": False}, headers=H["admin"],
                    files={"file": ("history.xlsx", history)}).json()
    assert r["committed"] and r["created"] == 1
    ph = client.get(f"/api/materials/{buy['id']}/price-history", headers=H["admin"]).json()
    assert any(x["price"] == 77.5 for x in ph)


def test_alert_scan_endpoint_keeps_prestart_type_visible(client, H):
    r = client.post("/api/alerts-scan", headers=H["jihua"])
    assert r.status_code == 200, r.text
    alerts = client.get("/api/alerts", headers=H["jihua"]).json()["items"]
    assert isinstance(alerts, list)


def test_background_alert_scan_runs_against_real_session():
    assert run_scheduled_alert_scan(force=True) is True
    assert run_scheduled_alert_scan() is False  # 同一租约周期内第二个进程跳过
