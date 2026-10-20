"""端到端业务流程测试（真实 API + 演示数据）。"""
from datetime import date, timedelta


def mat_id(client, H, code):
    r = client.get("/api/materials", params={"keyword": code}, headers=H["admin"]).json()
    return next(m["id"] for m in r["items"] if m["code"] == code)


def wh_id(client, H, purpose):
    return next(w["id"] for w in client.get("/api/warehouses", headers=H["admin"]).json() if w["purpose"] == purpose)


def test_login_and_roles(client, H):
    assert client.get("/api/auth/me", headers=H["jihua"]).json()["roles"] == ["PLANNER"]
    assert client.post("/api/auth/login", json={"username": "jihua", "password": "x"}).status_code == 401
    # 仓库不能建采购单
    r = client.post("/api/purchase-orders", headers=H["cangku"], json={"supplier_id": 1, "lines": []})
    assert r.status_code == 403


def test_kitting_overview_priority_order(client, H):
    ov = client.get("/api/kitting/overview", headers=H["jihua"]).json()
    assert ov["buffer_days"] == 2
    items = ov["items"]
    # 已下达/装配中排在未下达前面；特急排第一
    assert items[0]["priority"] == 1
    assert items[-1]["status"] == "PLANNED"
    assert sum(ov["summary"].values()) == len(items)


def test_bom_tree_explodes_multilevel(client, H):
    boms = client.get("/api/boms", params={"keyword": "PYB900"}, headers=H["jishu"]).json()
    tree = client.get(f"/api/boms/{boms[0]['id']}/tree", params={"qty": 2}, headers=H["jishu"]).json()
    frame = next(n for n in tree if n["code"] == "ZZ-JJ-900")
    assert frame["children"][0]["code"] == "YC-GB-Q345-20"
    assert float(frame["children"][0]["total"]) == 3.6  # 1.8 吨 × 2 台


def test_po_line_level_lock_and_reverse(client, H):
    """录音痛点：一行入库整单锁死。现在：只锁该行，其他行可改；锁定行走行级冲销。"""
    bolt, screen = mat_id(client, H, "WG-LS-M20"), mat_id(client, H, "WG-SW-1236")
    po = client.post("/api/purchase-orders", headers=H["caigou"], json={
        "supplier_id": 4, "lines": [{"material_id": bolt, "qty": 100}, {"material_id": screen, "qty": 5}]}).json()
    assert po["lines"][0]["price"] is not None and float(po["lines"][0]["price"]) == 6.8  # 历史价格自动带出
    client.post(f"/api/purchase-orders/{po['id']}/approve", headers=H["caigou"]).raise_for_status()
    l1, l2 = po["lines"][0]["id"], po["lines"][1]["id"]
    a = client.post("/api/arrivals", headers=H["cangku"], json={"source_type": "PO", "source_id": l1, "qty": 100}).json()
    client.post(f"/api/arrivals/{a['id']}/inspect", headers=H["zhijian"], json={"qualified": 100}).raise_for_status()
    po = client.get(f"/api/purchase-orders/{po['id']}", headers=H["caigou"]).json()
    assert po["lines"][0]["locked"] and not po["lines"][1]["locked"]
    # 未锁定行照常改（需填原因，留修订记录）
    r = client.put(f"/api/po-lines/{l2}", headers=H["caigou"],
                   json={"changes": {"qty": 8}, "reason": "客户加单", "version": po["version"]})
    assert r.status_code == 200, r.text
    po = r.json()
    # 锁定行不能改物料/价格
    r = client.put(f"/api/po-lines/{l1}", headers=H["caigou"],
                   json={"changes": {"price": 7}, "reason": "调价", "version": po["version"]})
    assert r.status_code == 400 and "行级冲销" in r.json()["detail"]
    # 旧版本号 → 冲突
    r = client.put(f"/api/po-lines/{l2}", headers=H["caigou"], json={"changes": {"qty": 9}, "reason": "x", "version": 1})
    assert r.status_code == 409
    # 冲销前影响分析
    imp = client.get(f"/api/po-lines/{l1}/impact", headers=H["caigou"]).json()
    assert imp["can_reverse"] and any(i["type"] == "入库批次" for i in imp["items"])
    r = client.post(f"/api/po-lines/{l1}/reverse", headers=H["caigou"],
                    json={"reason": "单价录错", "version": po["version"]})
    assert r.status_code == 200, r.text
    po = r.json()
    assert not po["lines"][0]["locked"] and float(po["lines"][0]["arrived_qty"]) == 0
    assert float(po["lines"][1]["qty"]) == 8  # 另一行不受影响
    # 冲销后可以改价
    r = client.put(f"/api/po-lines/{l1}", headers=H["caigou"],
                   json={"changes": {"price": 7}, "reason": "更正单价", "version": po["version"]})
    assert r.status_code == 200
    revs = client.get(f"/api/purchase-orders/{po['id']}", headers=H["caigou"]).json()["revisions"]
    assert {"EDIT", "REVERSE"} <= {x["action"] for x in revs}


def test_edit_lock_between_users(client, H):
    pos = client.get("/api/purchase-orders", params={"status": "APPROVED"}, headers=H["caigou"]).json()["items"]
    pid = pos[0]["id"]
    assert client.post(f"/api/purchase-orders/{pid}/lock", headers=H["caigou"]).status_code == 200
    r = client.post(f"/api/purchase-orders/{pid}/lock", headers=H["jihua"])
    assert r.status_code == 409 and "正在编辑" in r.json()["detail"]
    client.delete(f"/api/purchase-orders/{pid}/lock", headers=H["caigou"])
    assert client.post(f"/api/purchase-orders/{pid}/lock", headers=H["jihua"]).status_code == 200
    client.delete(f"/api/purchase-orders/{pid}/lock", headers=H["jihua"])


def test_arrival_triggers_recalc_and_fill(client, H):
    """通用件入库 → 自动重算 → 按优先级冲填。"""
    before = {o["order_no"]: o for o in client.get("/api/kitting/overview", headers=H["jihua"]).json()["items"]}
    srcs = client.get("/api/arrival-sources", params={"keyword": "WG-DJ-75"}, headers=H["cangku"]).json()
    src = next(s for s in srcs if s["source_type"] == "PO")
    a = client.post("/api/arrivals", headers=H["cangku"],
                    json={"source_type": "PO", "source_id": src["source_id"], "qty": float(src["open_qty"])}).json()
    client.post(f"/api/arrivals/{a['id']}/inspect", headers=H["zhijian"],
                json={"qualified": float(src["open_qty"])}).raise_for_status()
    after = {o["order_no"]: o for o in client.get("/api/kitting/overview", headers=H["jihua"]).json()["items"]}
    improved = [n for n in after if after[n]["kit_rate"] > before[n]["kit_rate"]]
    assert improved, "电机入库后应有订单齐套率提升"


def test_quality_exception_full_workflow(client, H):
    exc = client.get("/api/exceptions", params={"status": "SUBMITTED", "category": "QUALITY"},
                     headers=H["zhijian"]).json()["items"][0]
    eid = exc["id"]
    # 仓库无权审核
    assert client.post(f"/api/exceptions/{eid}/dispose", headers=H["cangku"],
                       json={"disposition": "EXCHANGE", "responsibility": "SUPPLIER"}).status_code == 403
    e = client.post(f"/api/exceptions/{eid}/dispose", headers=H["zhijian"],
                    json={"disposition": "EXCHANGE", "responsibility": "SUPPLIER", "comment": "通知换货"}).json()
    assert e["status"] == "AWAIT_REINSPECT"
    e = client.post(f"/api/exceptions/{eid}/reinspect", headers=H["zhijian"], json={"qualified": 1}).json()
    assert e["status"] == "REINSPECTED"
    assert client.post(f"/api/exceptions/{eid}/close", headers=H["zhijian"], json={"result": ""}).status_code == 400
    e = client.post(f"/api/exceptions/{eid}/close", headers=H["zhijian"], json={"result": "换货合格入库"}).json()
    assert e["status"] == "CLOSED" and len(e["logs"]) >= 4


def test_qty_exception_auto_created_on_short_delivery(client, H):
    srcs = client.get("/api/arrival-sources", params={"keyword": "WG-TG-133"}, headers=H["cangku"]).json()
    src = srcs[0]
    r = client.post("/api/arrivals", headers=H["cangku"], json={
        "source_type": src["source_type"], "source_id": src["source_id"], "qty": 30, "expected_qty": 40}).json()
    assert r["exception_no"]
    exc = client.get("/api/exceptions", params={"keyword": r["exception_no"]}, headers=H["zhijian"]).json()["items"][0]
    assert exc["category"] == "QTY" and float(exc["qty"]) == 10


def test_classify_exception_rule_fallback(client, H):
    r = client.post("/api/exceptions/classify", headers=H["zhijian"], json={"text": "轴承外圈有锈蚀和划伤"}).json()
    # AI/规则只给“候选类别 + 可能原因 + 待核实证据”，不判定责任方
    assert r["source"] == "RULE" and r["category_candidate"] == "QUALITY"
    assert "responsibility" not in r and r["possible_causes"] and r["evidence_to_verify"] and r["disclaimer"]


def test_drawing_gate_blocks_assembly_until_confirmed(client, H):
    orders = client.get("/api/orders", params={"status": "RELEASED"}, headers=H["jihua"]).json()["items"]
    o = next(x for x in orders if "PYB900" in x["product"])
    r = client.post(f"/api/orders/{o['id']}/start", headers=H["zhuguan"], json={"override_reason": "客户催"})
    assert r.status_code == 400 and "图纸" in r.json()["detail"]
    req = client.get("/api/drawings-required", params={"ref_type": "SALES_ORDER", "ref_id": o["id"]},
                     headers=H["chejian"]).json()
    assert any(x["drawing_no"] == "YE-PYB900-201" and x["version"] == "B" for x in req)
    client.post("/api/drawings-confirm", headers=H["chejian"], json={
        "ref_type": "SALES_ORDER", "ref_id": o["id"], "version_ids": [x["version_id"] for x in req]}).raise_for_status()
    # 未齐套：车间不能强制开工，主管可以
    detail = client.get(f"/api/orders/{o['id']}", headers=H["chejian"]).json()
    if detail["kit_status"] != "READY":
        r = client.post(f"/api/orders/{o['id']}/start", headers=H["chejian"], json={})
        assert r.status_code == 400 and r.json()["data"].get("need_override")
        r = client.post(f"/api/orders/{o['id']}/start", headers=H["chejian"], json={"override_reason": "x"})
        assert r.status_code == 403
    r = client.post(f"/api/orders/{o['id']}/start", headers=H["zhuguan"], json={"override_reason": "缺件后补，先装主体"})
    assert r.status_code == 200 and r.json()["status"] == "ASSEMBLING"


def test_drawing_upgrade_keeps_bound_version_for_inflight_orders(client, H):
    """场景6：图纸升版后，旧在制订单仍使用批准版本；技术评估后才指定切换；重大变更才强制暂停。"""
    dws = client.get("/api/drawings", params={"keyword": "YE-PYB900-100"}, headers=H["jishu"]).json()
    did = dws[0]["id"]
    o = client.get("/api/orders", params={"status": "ASSEMBLING"}, headers=H["jihua"]).json()["items"]
    o = next(x for x in o if "PYB900" in x["product"])
    before = next(x for x in client.get("/api/drawings-required", params={"ref_type": "SALES_ORDER", "ref_id": o["id"]},
                                        headers=H["chejian"]).json() if x["drawing_no"] == "YE-PYB900-100")
    assert before["confirmed"]
    old_ver = before["version"]
    # 一般变更升版 B
    r = client.post(f"/api/drawings/{did}/versions", headers=H["jishu"], data={"version": "B", "change_note": "加强筋"},
                    files={"pdf": ("frame_b.pdf", b"%PDF-1.4 demo", "application/pdf")})
    assert r.status_code == 200, r.text
    vb = r.json()["id"]
    rel = client.post(f"/api/drawings/versions/{vb}/release", headers=H["jishu"], json={"change_level": "MINOR"}).json()
    assert rel["affected"]["orders"] and rel["held"] == 0
    after = next(x for x in client.get("/api/drawings-required", params={"ref_type": "SALES_ORDER", "ref_id": o["id"]},
                                       headers=H["chejian"]).json() if x["drawing_no"] == "YE-PYB900-100")
    # 在制订单仍按批准版本，确认没有失效，只提示有新版可切
    assert after["version"] == old_ver and after["confirmed"] and after["upgrade_available"]
    assert after["current_version"] == "B" and not after["hold"]
    binds = client.get(f"/api/drawings/{did}/bindings", headers=H["jishu"]).json()
    assert any(b["no"] == o["order_no"] and b["bound_version"] == old_ver and b["upgrade_available"] for b in binds)
    # 技术评估后指定该单切换 → 切到 B，需重新确认
    sw = client.post("/api/drawings-switch", headers=H["jishu"], json={
        "ref_type": "SALES_ORDER", "ref_id": o["id"], "drawing_id": did, "new_version_id": vb, "reason": "评估：未加工机架，可切"})
    assert sw.status_code == 200, sw.text
    sw_after = next(x for x in client.get("/api/drawings-required", params={"ref_type": "SALES_ORDER", "ref_id": o["id"]},
                                          headers=H["chejian"]).json() if x["drawing_no"] == "YE-PYB900-100")
    assert sw_after["version"] == "B" and not sw_after["confirmed"]
    # 重大变更 C：仍绑定旧版的单据强制暂停（已切到 B 的订单也算“非 C”→暂停）
    r = client.post(f"/api/drawings/{did}/versions", headers=H["jishu"], data={"version": "C", "change_note": "安全隐患整改"},
                    files={"pdf": ("frame_c.pdf", b"%PDF-1.4 demo", "application/pdf")})
    vc = r.json()["id"]
    rel = client.post(f"/api/drawings/versions/{vc}/release", headers=H["jishu"],
                      json={"change_level": "MAJOR", "hold_reason": "焊缝强度不足"}).json()
    assert rel["held"] >= 1
    held = next(x for x in client.get("/api/drawings-required", params={"ref_type": "SALES_ORDER", "ref_id": o["id"]},
                                      headers=H["chejian"]).json() if x["drawing_no"] == "YE-PYB900-100")
    assert held["hold"] and "焊缝" in held["hold_reason"]
    # 暂停期间领料被拦
    assert client.post("/api/picks", headers=H["cangku"], json={"order_id": o["id"]}).status_code == 400
    # 技术裁定后解除
    res = client.post("/api/drawings-resolve-hold", headers=H["jishu"], json={
        "ref_type": "SALES_ORDER", "ref_id": o["id"], "drawing_id": did, "reason": "评估后按原版继续"})
    assert res.status_code == 200, res.text


def test_lock_reservation_and_simulation(client, H):
    ov = client.get("/api/kitting/overview", headers=H["jihua"]).json()["items"]
    low = ov[-1]
    sim = client.get(f"/api/orders/{low['id']}/simulate-top", headers=H["jihua"]).json()
    assert any(x["is_target"] for x in sim) or sim == []
    bolt = mat_id(client, H, "WG-LS-M20")
    r = client.post(f"/api/orders/{low['id']}/lock", headers=H["jihua"],
                    json={"material_id": bolt, "qty": 10, "reason": "老客户承诺"})
    assert r.status_code == 200, r.text
    d = client.get(f"/api/orders/{low['id']}", headers=H["jihua"]).json()
    locked = [x for x in d["reservations"] if x["locked"]]
    assert locked and float(locked[0]["qty"]) == 10
    assert client.delete(f"/api/reservations/{locked[0]['id']}", headers=H["jihua"]).status_code == 200


def test_shortage_to_requisition_and_convert(client, H):
    sh = client.get("/api/kitting/shortages", headers=H["caigou"]).json()
    assert sh
    r = client.post("/api/kitting/shortages/to-requisition", headers=H["caigou"]).json()
    assert r["requisition_id"] or r["outsource_suggestions"] or r["make_suggestions"]
    if r["requisition_id"]:
        req = client.get(f"/api/requisitions/{r['requisition_id']}", headers=H["caigou"]).json()
        ln = req["lines"][0]
        assert ln["supplier_id"] and ln["buyer"]  # 自动带出供应商和采购员
        client.put(f"/api/requisition-lines/{ln['id']}", headers=H["caigou"],
                   json={"drawing_no": "YE-NEW-001"}).raise_for_status()
        pos = client.post(f"/api/requisitions/{r['requisition_id']}/convert", headers=H["caigou"]).json()
        assert pos and pos[0]["po_no"].startswith("PO")


def test_supplier_portal_confirm(client, H):
    p = client.get("/api/portal/demo-supplier-s001").json()
    assert p["supplier"] == "湘中电机有限公司"
    if p["lines"]:
        lid = p["lines"][0]["id"]
        r = client.post(f"/api/portal/demo-supplier-s001/lines/{lid}/confirm",
                        json={"promised_date": (date.today() + timedelta(days=30)).isoformat(), "note": "排产中"})
        assert r.status_code == 200
        alerts = client.get("/api/alerts", params={"type": "SUPPLIER_LATE"}, headers=H["caigou"]).json()["items"]
        assert alerts
    assert client.get("/api/portal/wrong-token").status_code == 404


def test_stock_separate_ledgers_and_spare_out(client, H):
    prod = client.get("/api/stock/onhand", params={"purpose": "PRODUCTION"}, headers=H["cangku"]).json()
    spare = client.get("/api/stock/onhand", params={"purpose": "AFTERSALES"}, headers=H["cangku"]).json()
    assert {r["purpose"] for r in prod} == {"PRODUCTION"} and {r["purpose"] for r in spare} == {"AFTERSALES"}
    spare_wh = wh_id(client, H, "AFTERSALES")
    screen = mat_id(client, H, "WG-SW-1236")
    assert client.post("/api/stock/spare-out", headers=H["shouhou"], json={
        "material_id": screen, "warehouse_id": spare_wh, "qty": 1, "kind": "SALE"}).status_code == 400  # 销售需客户
    assert client.post("/api/stock/spare-out", headers=H["shouhou"], json={
        "material_id": screen, "warehouse_id": spare_wh, "qty": 1, "kind": "SALE", "customer": "某矿"}).status_code == 200
    # 生产库不能走售后出库
    assert client.post("/api/stock/spare-out", headers=H["shouhou"], json={
        "material_id": screen, "warehouse_id": wh_id(client, H, "PRODUCTION"), "qty": 1, "kind": "USE"}).status_code == 400
    rep = client.get("/api/stock/spare-report", headers=H["shouhou"]).json()
    row = next(x for x in rep if x["material_id"] == screen)
    assert row["sale"] >= 3  # 种子数据 2 + 本次 1


def test_fifo_pick_by_bom(client, H):
    o = client.get("/api/orders", params={"status": "RELEASED"}, headers=H["cangku"]).json()["items"][0]
    plan = client.get(f"/api/orders/{o['id']}/pick-plan", headers=H["cangku"]).json()
    assert plan and all("plan_qty" in p for p in plan)
    pk = client.post("/api/picks", headers=H["cangku"], json={"order_id": o["id"]}).json()
    assert pk["lines"]  # 参照 BOM 自动带出
    r = client.post(f"/api/picks/{pk['id']}/post", headers=H["cangku"])
    assert r.status_code == 200, r.text
    lots = [lot for ln in r.json()["lines"] for lot in ln["lots"]]
    assert lots  # 记录了扣减的批次（先进先出）


def test_shipment_approval_flow(client, H):
    o = client.get("/api/orders", params={"status": "ASSEMBLING"}, headers=H["chejian"]).json()["items"][0]
    # 演示订单只领了部分料：BOM 物料未领齐时不能完工入库（否则账上凭空多出整机、料还能被别的订单再领）
    r = client.post(f"/api/orders/{o['id']}/complete", headers=H["chejian"])
    assert r.status_code == 400 and "未领齐" in r.json()["detail"]
    from app.db import SessionLocal
    from app.models import OrderIssue, SalesOrder
    from app.services.orders import pick_plan
    with SessionLocal() as db:  # 模拟余下的料也已领用过账
        for p in pick_plan(db, db.get(SalesOrder, o["id"])):
            row = db.query(OrderIssue).filter_by(order_id=o["id"], material_id=p["material_id"]).first()
            if row:
                row.qty = p["required"]
            else:
                db.add(OrderIssue(order_id=o["id"], material_id=p["material_id"], qty=p["required"]))
        db.commit()
    client.post(f"/api/orders/{o['id']}/complete", headers=H["chejian"]).raise_for_status()
    s = client.post("/api/shipments", headers=H["xiaoshou"], json={
        "order_id": o["id"], "plan_date": date.today().isoformat(), "address": "郴州"}).json()
    assert client.post(f"/api/shipments/{s['id']}/approve", headers=H["xiaoshou"]).status_code == 403
    client.post(f"/api/shipments/{s['id']}/submit", headers=H["xiaoshou"]).raise_for_status()
    assert client.post(f"/api/shipments/{s['id']}/reject", headers=H["zhuguan"], json={"comment": ""}).status_code == 400
    client.post(f"/api/shipments/{s['id']}/approve", headers=H["zhuguan"], json={"comment": "同意"}).raise_for_status()
    s = client.post(f"/api/shipments/{s['id']}/ship", headers=H["cangku"]).json()
    assert s["status"] == "SHIPPED"
    assert client.get(f"/api/orders/{o['id']}", headers=H["chejian"]).json()["status"] == "SHIPPED"


def test_board_modes_and_key(client, H):
    assert client.get("/api/board/workshop").status_code == 401
    b = client.get("/api/board/workshop", params={"key": "board-demo"}).json()
    assert b["mode"] == "WORKSHOP" and "shortages" in b and "incoming" in b
    client.put("/api/board/settings", headers=H["zhuguan"], json={"board_mode": "SHOWCASE"}).raise_for_status()
    s = client.get("/api/board/workshop", params={"key": "board-demo"}).json()
    assert s["mode"] == "SHOWCASE" and "shortages" not in s  # 展示模式隐藏内部信息
    assert all("*" in x["customer"] for x in s["assembling"])  # 客户名脱敏
    client.put("/api/board/settings", headers=H["zhuguan"], json={"board_mode": "WORKSHOP"})
    w = client.get("/api/board/weekly", params={"key": "board-demo"}).json()
    assert "生产部" in w["groups"]


def test_open_api_with_key(client, H):
    k = client.post("/api/api-keys", headers=H["admin"], json={"name": "财务系统", "scopes": ["read"]}).json()["key"]
    assert client.get("/api/open/v1/stock").status_code == 401
    assert client.get("/api/open/v1/stock", headers={"X-API-Key": k}).status_code == 200
    assert client.get("/api/open/v1/orders", headers={"X-API-Key": k}).status_code == 200
    r = client.post("/api/open/v1/orders", headers={"X-API-Key": k}, json={
        "customer": "x", "product_id": 1, "qty": 1, "due_date": "2027-01-01"})
    assert r.status_code == 403  # 只读密钥不能写


def test_ai_disabled_falls_back(client, H):
    assert client.get("/api/ai/status", headers=H["jihua"]).json()["enabled"] is False
    r = client.post("/api/ai/brief", headers=H["jihua"]).json()
    assert r["source"] == "RULE" and "缺料简报" in r["text"]
    r = client.post("/api/ai/ask", headers=H["jihua"], json={"question": "哪些订单缺料？"}).json()
    assert r["source"] == "RULE"


def test_material_drawing_change_must_use_tech_change(client, H):
    m = client.get("/api/materials", params={"keyword": "WX-ZZ-900"}, headers=H["jishu"]).json()["items"][0]
    body = {k: m[k] for k in ("code", "name", "spec", "unit", "type", "drawing_no", "is_common", "is_spare",
                               "lead_time_days", "is_active")}
    body["drawing_no"] = "CHANGED"
    assert client.put(f"/api/materials/{m['id']}", headers=H["jishu"], json=body).status_code == 400
    r = client.post("/api/drawings-change-no", headers=H["jishu"], json={
        "material_id": m["id"], "new_drawing_no": "YE-PYB900-201-1", "reason": "非标加长主轴"}).json()
    assert r["new"] == "YE-PYB900-201-1" and r["boms"] >= 1


def test_dashboard_and_alerts(client, H):
    d = client.get("/api/dashboard", headers=H["zhuguan"]).json()
    assert d["orders"]["active"] >= 1 and "todo" in d
    client.post("/api/alerts-scan", headers=H["zhuguan"]).raise_for_status()
    al = client.get("/api/alerts", headers=H["zhuguan"]).json()["items"]
    assert al and al[0]["level"] == "CRITICAL"


def test_excel_import_materials_bom_stock(client, H):
    import io
    from openpyxl import Workbook

    def xlsx(rows):
        wb = Workbook()
        ws = wb.active
        for r in rows:
            ws.append(r)
        b = io.BytesIO()
        wb.save(b)
        return b.getvalue()

    r = client.get("/api/import/materials/template", headers=H["admin"])
    assert r.status_code == 200 and r.content[:2] == b"PK"
    mats = xlsx([["编码", "名称", "规格", "单位", "类型", "图号", "通用件"],
                 ["CP-NEW-1", "新机型", "", "台", "整机", "", "否"],
                 ["WG-NEW-2", "新外购件", "M8", "件", "外购", "", "是"],
                 ["BAD", "", "", "", "外购", "", ""]])
    r = client.post("/api/import/materials", params={"dry_run": True}, headers=H["admin"],
                    files={"file": ("m.xlsx", mats)}).json()
    assert r["error_count"] == 1 and r["errors"][0]["row"] == 4 and not r["committed"]
    mats = xlsx([["编码", "名称", "类型"], ["CP-NEW-1", "新机型", "整机"], ["WG-NEW-2", "新外购件", "外购"]])
    r = client.post("/api/import/materials", params={"dry_run": False}, headers=H["admin"],
                    files={"file": ("m.xlsx", mats)}).json()
    assert r["committed"] and r["created"] == 2
    boms = xlsx([["父件编码", "版本", "子件编码", "单位用量", "损耗率", "生效"],
                 ["CP-NEW-1", "V1", "WG-NEW-2", 4, 0.02, "是"]])
    r = client.post("/api/import/boms", params={"dry_run": False}, headers=H["admin"],
                    files={"file": ("b.xlsx", boms)}).json()
    assert r["committed"], r
    st = xlsx([["物料编码", "仓库编码", "库位编码", "批次号", "数量", "单价", "入库日期"],
               ["WG-NEW-2", "WH01", "A-01", "QC-1", 50, 3.5, "2026-09-01"]])
    assert client.post("/api/import/stock", params={"dry_run": False}, headers=H["jishu"],
                       files={"file": ("s.xlsx", st)}).status_code == 403
    r = client.post("/api/import/stock", params={"dry_run": False}, headers=H["cangku"],
                    files={"file": ("s.xlsx", st)}).json()
    assert r["committed"] and r["created"] == 1


def test_board_mode_param_cannot_bypass_showcase(client, H):
    """客户展示：凭看板密钥访问时，mode 参数无效；周计划在展示模式下对密钥访问关闭。"""
    key = client.get("/api/board/settings", headers=H["admin"]).json()["board_key"]
    client.put("/api/board/settings", headers=H["admin"], json={"board_mode": "SHOWCASE"}).raise_for_status()
    try:
        r = client.get("/api/board/workshop", params={"key": key, "mode": "WORKSHOP"}).json()
        assert r["mode"] == "SHOWCASE" and "plan" not in r and "shortages" not in r
        assert "assembling" in r and all("*" in x["customer"] or len(x["customer"]) <= 2 for x in r["assembling"])
        assert client.get("/api/board/weekly", params={"key": key}).status_code == 403
        # 登录用户可以预览工作模式
        r = client.get("/api/board/workshop", params={"mode": "WORKSHOP"}, headers=H["admin"]).json()
        assert r["mode"] == "WORKSHOP"
    finally:
        client.put("/api/board/settings", headers=H["admin"], json={"board_mode": "WORKSHOP"}).raise_for_status()


def test_production_guard_rejects_defaults(monkeypatch):
    from app.config import settings
    from app.core import guard
    from app.db import SessionLocal
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "secret_key", "change-me-in-production")
    monkeypatch.setattr(settings, "cors_origins", ["*"])
    monkeypatch.setattr(settings, "auto_create_tables", True)
    with SessionLocal() as db:
        errs = guard.problems(db)
    text = "\n".join(errs)
    assert "SECRET_KEY" in text and "CORS" in text and "AUTO_CREATE_TABLES" in text
    assert "admin" in text and "演示默认密码" in text and "board-demo" in text
    import pytest
    with pytest.raises(RuntimeError):
        guard.enforce(None)
    # 演示灌库在生产模式下直接拒绝
    from app import seed
    with pytest.raises(SystemExit):
        seed.run()


def test_ai_ask_is_bound_to_user_permissions(client, H, monkeypatch):
    from app.db import SessionLocal
    from app.models import User
    from app.services import ai
    with SessionLocal() as db:
        worker = db.query(User).filter(User.username == "chejian").one()
        boss = db.query(User).filter(User.username == "zhuguan").one()
        f_worker, f_boss = ai._facts(db, worker), ai._facts(db, boss)
    assert "orders" not in f_worker and "shortages" not in f_worker     # 车间账号看不到客户/订单/缺料明细
    assert f_boss["orders"] and "shortages" in f_boss


def test_login_throttle(client):
    for _ in range(8):
        assert client.post("/api/auth/login", json={"username": "nobody-x", "password": "bad"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "nobody-x", "password": "bad"}).status_code == 429
