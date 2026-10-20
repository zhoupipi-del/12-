"""演示数据：python -m app.seed  （会清空并重建 SQLite 演示库）

场景设计（覆盖录音中的每个痛点）：
  * 三个机型共用 电机/减速机/轴承/螺栓 等通用件，多台整机并行 → 冲填抢料
  * 特急单 vs 普通单争抢同一台电机 → 优先级分配
  * 机架是自制件（下级钢板），主轴是外协件 → 自制/外协也参与齐套
  * 有到货待检、有不合格转来料异常、有供应商未回复交期
  * 售后备件库独立分账，安全库存高低预警
  * 图纸 B 版已发布，A 版作废
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal as Dc

from sqlalchemy import select

from .config import settings
from .core.security import hash_password
from .db import Base, SessionLocal, engine
from .models import (AssemblyNote, Bom, BomLine, Drawing, DrawingVersion, Location, Material, Role, SalesOrder, Supplier, TxnType, User, Warehouse, WeeklyPlanItem, now)
from .services import inventory as inv
from .services import kitting, orders as osvc, production as psvc, purchase as pu, receiving as rc
from .services.alerts import scan_all
from .services.board import set_setting

T = date.today()


def d(n: int) -> date:
    return T + timedelta(days=n)


def run() -> None:
    if not settings.allows_demo:
        raise SystemExit("拒绝执行：演示数据只能在 APP_ENV=demo/development 下灌入（会清空数据库）。"
                         "正式环境请用 python -m app.cli create-admin。")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    db = SessionLocal()

    # ---------------------------------------------------------- 用户（一个账号可兼多角色）
    users = {}
    for un, name, roles, dept in [
        ("admin", "系统管理员", [Role.ADMIN], "信息部"),
        ("zhuguan", "刘主管", [Role.MANAGER, Role.PLANNER], "生产部"),
        ("jihua", "王计划", [Role.PLANNER], "生产部"),
        ("caigou", "李采购", [Role.BUYER], "采购部"),
        ("cangku", "赵仓管", [Role.WAREHOUSE], "仓库"),
        ("zhijian", "陈质检", [Role.QUALITY], "质量部"),
        ("chejian", "张班长", [Role.WORKSHOP], "装配车间"),
        ("jishu", "周工", [Role.TECH], "技术部"),
        ("shouhou", "孙售后", [Role.AFTERSALES], "售后部"),
        ("xiaoshou", "吴销售", [Role.SALES], "销售部"),
    ]:
        u = User(username=un, name=name, password_hash=hash_password("admin123" if un == "admin" else "123456"),
                 roles=",".join(roles), department=dept)
        db.add(u)
        users[un] = u
    db.flush()
    _admin, buyer, wh_user, qc, tech = users["admin"], users["caigou"], users["cangku"], users["zhijian"], users["jishu"]

    # ---------------------------------------------------------- 仓库（分账）
    wh_prod = Warehouse(code="WH01", name="生产物料库", purpose="PRODUCTION")
    wh_spare = Warehouse(code="WH02", name="售后备件库", purpose="AFTERSALES")
    wh_fin = Warehouse(code="WH03", name="成品库", purpose="FINISHED")
    db.add_all([wh_prod, wh_spare, wh_fin])
    db.flush()
    for i in range(1, 7):
        db.add(Location(warehouse_id=wh_prod.id, code=f"A-0{i}", name=f"A 区 {i} 号架"))
    for i in range(1, 4):
        db.add(Location(warehouse_id=wh_spare.id, code=f"S-0{i}", name=f"备件 {i} 号架"))

    # ---------------------------------------------------------- 供应商
    sup = {}
    for code, name, os_ in [("S001", "湘中电机有限公司", False), ("S002", "星沙恒力传动", False),
                            ("S003", "株洲宏精轴承", False), ("S004", "长沙通用标准件", False),
                            ("S005", "楚天钢材贸易", False), ("S006", "株洲恒温热处理厂", True),
                            ("S007", "衡阳精工机加工", True), ("S008", "江南输送带厂", False)]:
        s = Supplier(code=code, name=name, contact="联系人", phone="0731-88888888", is_outsourcer=os_)
        db.add(s)
        sup[code] = s
    db.flush()
    sup["S001"].portal_token = "demo-supplier-s001"

    # ---------------------------------------------------------- 物料
    def M(code, name, type_, spec="", unit="件", dwg="", common=False, spare=False, lt=7, smin=0, smax=0,
          pmin=0, pmax=0, s=None):
        m = Material(code=code, name=name, type=type_, spec=spec, unit=unit, drawing_no=dwg, is_common=common,
                     is_spare=spare, lead_time_days=lt, safety_min=Dc(smin), safety_max=Dc(smax), spare_min=Dc(pmin),
                     spare_max=Dc(pmax), default_buyer_id=buyer.id, default_supplier_id=s.id if s else None)
        db.add(m)
        return m

    P1 = M("CP-PYB900", "PYB900 圆锥破碎机", "PRODUCT", "整机", "台", "YE-PYB900-000", lt=10)
    P2 = M("CP-YK1236", "YK1236 圆振动筛", "PRODUCT", "整机", "台", "YE-YK1236-000", lt=7)
    P3 = M("CP-DTL80", "DTL80 矿用带式输送机", "PRODUCT", "整机", "台", "YE-DTL80-000", lt=8)
    motor = M("WG-DJ-75", "三相异步电机", "BUY", "YE3-280S-4 75kW", "台", common=True, spare=True, lt=15,
              smin=1, smax=6, pmin=1, pmax=3, s=sup["S001"])
    reducer = M("WG-JSJ-01", "硬齿面减速机", "BUY", "ZLY250", "台", common=True, lt=20, smin=0, s=sup["S002"])
    bearing = M("WG-ZC-22320", "调心滚子轴承", "BUY", "22320CA/W33", "套", common=True, spare=True, lt=10,
                smin=4, smax=40, pmin=4, pmax=20, s=sup["S003"])
    bolt = M("WG-LS-M20", "高强度螺栓", "BUY", "M20×80 10.9级", "套", common=True, lt=5, smin=200, smax=2000,
             s=sup["S004"])
    steel = M("YC-GB-Q345-20", "钢板 Q345B", "BUY", "δ20mm", "吨", lt=7, smin=2, s=sup["S005"])
    liner = M("WG-CB-900", "高锰钢衬板", "BUY", "ZGMn13 PYB900", "套", spare=True, lt=25, pmin=2, pmax=6,
              s=sup["S005"])
    screen = M("WG-SW-1236", "聚氨酯筛网", "BUY", "1200×3600 孔15", "片", spare=True, lt=12, pmin=4, pmax=12,
               s=sup["S004"])
    belt = M("WG-SDD-800", "钢丝绳芯输送带", "BUY", "ST1000 B800", "米", lt=18, s=sup["S008"])
    roller = M("WG-TG-133", "托辊组", "BUY", "Φ133×465", "组", common=True, spare=True, lt=10, smin=20, s=sup["S008"])
    frame9 = M("ZZ-JJ-900", "破碎机机架", "MAKE", "焊接件", "件", "YE-PYB900-100", lt=6)
    frame12 = M("ZZ-JJ-1236", "振动筛筛箱", "MAKE", "焊接件", "件", "YE-YK1236-100", lt=5)
    frame80 = M("ZZ-JJ-80", "输送机机架段", "MAKE", "焊接件", "段", "YE-DTL80-100", lt=4)
    shaft = M("WX-ZZ-900", "破碎机主轴", "OUTSOURCE", "42CrMo 调质", "根", "YE-PYB900-201", lt=12)
    sleeve = M("WX-PXT-900", "偏心套", "OUTSOURCE", "ZQSn10-1", "件", "YE-PYB900-202", lt=14)
    exciter = M("WX-JZQ-1236", "激振器", "OUTSOURCE", "箱式", "台", "YE-YK1236-300", lt=12)
    db.flush()

    def BOM(mat, lines, mature=True):
        b = Bom(material_id=mat.id, version="V1", status="ACTIVE", is_mature=mature, maintained_by=tech.id)
        for i, (child, q) in enumerate(lines, 1):
            b.lines.append(BomLine(seq=i * 10, child_id=child.id, qty_per=Dc(str(q))))
        db.add(b)
        return b

    BOM(P1, [(frame9, 1), (shaft, 1), (sleeve, 1), (motor, 1), (bearing, 2), (liner, 1), (bolt, 48)])
    BOM(P2, [(frame12, 1), (exciter, 2), (motor, 1), (bearing, 4), (screen, 6), (bolt, 120)])
    BOM(P3, [(frame80, 10), (reducer, 1), (motor, 1), (roller, 60), (belt, 220), (bolt, 200)])
    BOM(frame9, [(steel, Dc("1.8"))])
    BOM(frame12, [(steel, Dc("1.2"))])
    BOM(frame80, [(steel, Dc("0.35"))])
    db.flush()

    # ---------------------------------------------------------- 图纸（主轴已升版 B，A 作废）
    for no, name, mat in [("YE-PYB900-201", "破碎机主轴", shaft), ("YE-PYB900-100", "破碎机机架", frame9),
                          ("YE-PYB900-202", "偏心套", sleeve)]:
        dw = Drawing(drawing_no=no, name=name, material_id=mat.id)
        db.add(dw)
        db.flush()
        if no == "YE-PYB900-201":
            db.add(DrawingVersion(drawing_id=dw.id, version="A", status="SUPERSEDED", change_note="首版",
                                  uploaded_by=tech.id, released_by=tech.id, released_at=now() - timedelta(days=60)))
            db.add(DrawingVersion(drawing_id=dw.id, version="B", status="RELEASED",
                                  change_note="轴颈 Φ180→Φ185，配合新偏心套", uploaded_by=tech.id,
                                  released_by=tech.id, released_at=now() - timedelta(days=3)))
        else:
            db.add(DrawingVersion(drawing_id=dw.id, version="A", status="RELEASED", change_note="首版",
                                  uploaded_by=tech.id, released_by=tech.id, released_at=now() - timedelta(days=90)))
    db.flush()
    # 演示数据里没建受控图纸的物料标记“免图纸管控”（真实上线时：填了图号就必须有已发布图纸，确无图纸的标准件才勾选免管控）
    have = {d.drawing_no for d in db.scalars(select(Drawing)).all()}
    for mm in db.scalars(select(Material).where(Material.drawing_no != "")).all():
        if mm.drawing_no not in have:
            mm.drawing_exempt = True
    db.commit()

    # ---------------------------------------------------------- 历史采购（形成历史价格 / 供应商）
    hist = [("S001", motor, 2, "28500"), ("S002", reducer, 1, "16800"), ("S003", bearing, 20, "2350"),
            ("S004", bolt, 1500, "6.8"), ("S005", steel, 12, "4650"), ("S005", liner, 4, "8900"),
            ("S004", screen, 12, "780"), ("S008", belt, 300, "420"), ("S008", roller, 120, "185")]
    for code, m, q, price in hist:
        po = pu.create_po(db, buyer, sup[code].id, [{"material_id": m.id, "qty": q, "price": Dc(price),
                                                     "need_date": d(-40)}], "历史订单")
        po.order_date = d(-45)
        pu.approve_po(db, buyer, po.id)
        ln = po.lines[0]
        a, _ = rc.register_arrival(db, wh_user, "PO", ln.id, Dc(q))
        rc.inspect(db, qc, a.id, Dc(q), Dc(0), wh_prod.id, lot_no=f"H{po.po_no[-6:]}")
    db.commit()
    # 历史入库批次改到 40 天前（看先进先出效果）；把一部分调去售后库
    from sqlalchemy import update
    from .models import StockLot
    db.execute(update(StockLot).values(received_at=datetime.now() - timedelta(days=40),
                                       last_move_at=datetime.now() - timedelta(days=40)))
    db.commit()
    # 消耗掉一些历史库存，留下需要的初始现存量
    target = {motor.id: 3, reducer.id: 1, bearing.id: 12, bolt.id: 380, steel.id: 3.2, liner.id: 1,
              screen.id: 12, belt.id: 260, roller.id: 70}
    for mid, keep in target.items():
        have = inv.available(db, mid, wh_prod.id)
        if have > Dc(str(keep)):
            inv.fifo_out(db, material_id=mid, warehouse_id=wh_prod.id, qty=have - Dc(str(keep)),
                         txn_type=TxnType.PICK_OUT, ref_type="HISTORY", remark="历史生产领用")
    db.commit()
    # 售后备件库：独立分账
    inv.stock_in(db, material_id=bearing.id, warehouse_id=wh_spare.id, qty=Dc(6), txn_type=TxnType.PURCHASE_IN,
                 lot_no="SP-BRG-01", unit_cost=Dc("2350"), ref_type="INIT", remark="售后备件期初")
    inv.stock_in(db, material_id=liner.id, warehouse_id=wh_spare.id, qty=Dc(1), txn_type=TxnType.PURCHASE_IN,
                 lot_no="SP-LIN-01", unit_cost=Dc("8900"), ref_type="INIT", remark="售后备件期初")
    inv.stock_in(db, material_id=screen.id, warehouse_id=wh_spare.id, qty=Dc(14), txn_type=TxnType.PURCHASE_IN,
                 lot_no="SP-SCR-01", unit_cost=Dc("780"), ref_type="INIT", remark="售后备件期初")
    inv.spare_out(db, material_id=screen.id, warehouse_id=wh_spare.id, qty=Dc(2), kind="SALE",
                  customer="郴州宏达矿业", remark="客户备件采购")
    inv.spare_out(db, material_id=bearing.id, warehouse_id=wh_spare.id, qty=Dc(2), kind="USE",
                  remark="售后上门维修更换")
    db.commit()

    # ---------------------------------------------------------- 在途采购（部分已回复交期）
    po1 = pu.create_po(db, buyer, sup["S001"].id, [
        {"material_id": motor.id, "qty": 2, "need_date": d(6), "promised_date": d(4)},
        {"material_id": motor.id, "qty": 1, "need_date": d(14)}], "电机补货")
    pu.approve_po(db, buyer, po1.id)
    po2 = pu.create_po(db, buyer, sup["S003"].id, [{"material_id": bearing.id, "qty": 10, "need_date": d(5),
                                                    "promised_date": d(7)}], "轴承")
    pu.approve_po(db, buyer, po2.id)
    po3 = pu.create_po(db, buyer, sup["S004"].id, [
        {"material_id": bolt.id, "qty": 600, "need_date": d(4), "promised_date": d(2)},
        {"material_id": screen.id, "qty": 12, "need_date": d(9), "promised_date": d(8)}], "标准件+筛网")
    pu.approve_po(db, buyer, po3.id)
    po4 = pu.create_po(db, buyer, sup["S008"].id, [{"material_id": roller.id, "qty": 80, "need_date": d(10)}])
    pu.approve_po(db, buyer, po4.id)
    po5 = pu.create_po(db, buyer, sup["S002"].id, [{"material_id": reducer.id, "qty": 1, "need_date": d(12),
                                                    "promised_date": d(16)}], "减速机，供应商说要排产")
    pu.approve_po(db, buyer, po5.id)
    db.commit()
    # 一行部分到货 → 该行锁定（同单另一行仍可改）
    a, _ = rc.register_arrival(db, wh_user, "PO", po3.lines[0].id, Dc(300))
    rc.inspect(db, qc, a.id, Dc(300), Dc(0), wh_prod.id)
    # 一笔到货待检
    rc.register_arrival(db, wh_user, "PO", po2.lines[0].id, Dc(4))
    # 一笔检验不合格 → 自动生成来料异常
    po6 = pu.create_po(db, buyer, sup["S005"].id, [{"material_id": steel.id, "qty": 4, "need_date": d(3),
                                                    "promised_date": d(1)}], "钢板")
    pu.approve_po(db, buyer, po6.id)
    a, _ = rc.register_arrival(db, wh_user, "PO", po6.lines[0].id, Dc(4))
    rc.inspect(db, qc, a.id, Dc(3), Dc(1), wh_prod.id, reject_reason="1 吨钢板表面严重锈蚀、局部起皮")
    db.commit()

    # ---------------------------------------------------------- 销售订单
    o1 = osvc.create_order(db, users["xiaoshou"], {"customer": "郴州宏达矿业", "product_id": P1.id, "qty": 1,
                                                   "due_date": d(18), "planned_start": d(8), "priority": 3,
                                                   "station": "1 号工位"})
    o2 = osvc.create_order(db, users["xiaoshou"], {"customer": "湘南恒盛矿业", "product_id": P1.id, "qty": 1,
                                                   "due_date": d(12), "planned_start": d(5), "priority": 1,
                                                   "is_nonstandard": True, "remark": "非标：加长主轴",
                                                   "station": "2 号工位"})
    o3 = osvc.create_order(db, users["xiaoshou"], {"customer": "湘西鑫源矿业", "product_id": P2.id, "qty": 1,
                                                   "due_date": d(10), "planned_start": d(4), "priority": 2,
                                                   "station": "3 号工位"})
    o4 = osvc.create_order(db, users["xiaoshou"], {"customer": "娄底华锑矿业", "product_id": P3.id, "qty": 1,
                                                   "due_date": d(22), "planned_start": d(12), "priority": 3})
    o5 = osvc.create_order(db, users["xiaoshou"], {"customer": "宁乡长兴煤业", "product_id": P2.id, "qty": 1,
                                                   "due_date": d(3), "planned_start": d(-4), "priority": 2,
                                                   "station": "4 号工位"})
    osvc.create_order(db, users["xiaoshou"], {"customer": "衡阳金瑞矿业", "product_id": P3.id, "qty": 2,
                                                   "due_date": d(40), "priority": 4})
    for o in (o1, o2, o3, o4, o5):
        osvc.release(db, users["jihua"], o.id)
    db.commit()

    # 自制 / 外协在途
    psvc.create_wo(db, users["jihua"], {"material_id": frame9.id, "qty": 1, "planned_start": d(-1),
                                        "planned_finish": d(3), "order_id": o2.id, "owner": "焊接班"})
    psvc.create_wo(db, users["jihua"], {"material_id": frame12.id, "qty": 2, "planned_start": d(0),
                                        "planned_finish": d(2), "owner": "焊接班"})
    psvc.create_wo(db, users["jihua"], {"material_id": frame80.id, "qty": 10, "planned_start": d(3),
                                        "planned_finish": d(9), "order_id": o4.id, "owner": "焊接班"})
    so1 = psvc.create_outsource(db, buyer, {"supplier_id": sup["S006"].id, "material_id": shaft.id, "qty": 2,
                                            "price": Dc("6800"), "expected_return": d(2)})
    so2 = psvc.create_outsource(db, buyer, {"supplier_id": sup["S007"].id, "material_id": sleeve.id, "qty": 1,
                                            "price": Dc("5200"), "expected_return": d(3)})
    so3 = psvc.create_outsource(db, buyer, {"supplier_id": sup["S007"].id, "material_id": exciter.id, "qty": 4,
                                            "price": Dc("9600"), "expected_return": d(1)})
    db.commit()
    # 外协发出前确认图纸（主轴 B 版）
    from .services import drawings as dsvc
    for x in (so1, so2):
        req = dsvc.required_drawings(db, "OUTSOURCE", x.id)
        dsvc.confirm(db, users["jishu"], "OUTSOURCE", x.id, [r["version_id"] for r in req])
        psvc.send_outsource(db, buyer, x.id, d(-5))
    psvc.send_outsource(db, buyer, so3.id, d(-6))
    db.commit()

    # 订单 5 已装配中（已领部分料）
    kitting.recalc(db)
    pk = osvc.create_pick(db, wh_user, o5.id, [{"material_id": bolt.id, "qty": 120},
                                               {"material_id": screen.id, "qty": 4}])
    osvc.post_pick(db, wh_user, pk.id)
    db.commit()
    db.get(SalesOrder, o5.id).status = "ASSEMBLING"
    db.get(SalesOrder, o5.id).progress = 35
    db.commit()

    # ---------------------------------------------------------- 看板内容
    db.add_all([
        AssemblyNote(content="PYB900 主轴已升版 B（Φ185），装配前务必核对图纸版本", level="IMPORTANT"),
        AssemblyNote(content="YK1236 激振器安装后需空载试车 2 小时并记录振幅", level="INFO", order_id=o5.id),
        AssemblyNote(content="高强螺栓按 10.9 级力矩表拧紧，力矩扳手每日校验", level="INFO"),
    ])
    ws = T - timedelta(days=T.weekday())
    for dept, content, owner, due, st in [
        ("生产部", f"{o5.order_no} 振动筛完成装配并试车", "张班长", d(2), "DOING"),
        ("生产部", f"{o3.order_no} 振动筛备料齐套确认", "王计划", d(3), "TODO"),
        ("采购部", "跟催恒力减速机交期（影响 DTL80）", "李采购", d(1), "TODO"),
        ("采购部", "轴承到货 4 套待检，催剩余 6 套", "李采购", d(2), "DOING"),
        ("仓库", "售后备件库盘点", "赵仓管", d(4), "TODO"),
        ("质量部", "钢板锈蚀来料异常处理", "陈质检", d(1), "DOING"),
        ("技术部", "PYB900 非标加长主轴图纸发布", "周工", d(2), "TODO"),
    ]:
        db.add(WeeklyPlanItem(week_start=ws, department=dept, content=content, owner=owner, due_date=due, status=st))
    set_setting(db, "board_key", "board-demo")
    db.commit()

    kitting.recalc(db)
    scan_all(db)
    db.close()
    print("演示数据已生成。登录：admin / admin123；业务账号 jihua、caigou、cangku、zhijian、chejian、jishu、"
          "zhuguan、shouhou、xiaoshou 密码均为 123456")
    print("看板：/board/workshop?key=board-demo  周计划：/board/weekly?key=board-demo  "
          "供应商门户：/portal/demo-supplier-s001")


if __name__ == "__main__":
    run()
