"""齐套引擎纯函数测试（不依赖数据库）。"""
from datetime import date, timedelta
from decimal import Decimal as Dc

from app.models import KitStatus
from app.services.kitting import OrderIn, Snapshot, Supply, compute

TODAY = date(2026, 10, 20)
PROD, BOLT, MOTOR, FRAME, STEEL = 1, 2, 3, 4, 5


def order(oid, qty, need_days, pri=3, committed=0, pin=9999):
    need = TODAY + timedelta(days=need_days)
    return OrderIn(oid, f"SO{oid}", PROD, PROD, None, Dc(qty), need, (committed, pin, pri, need, f"SO{oid}"))


def snap(orders, on_hand=None, incoming=None, locked=None, boms=None, issued=None):
    return Snapshot(TODAY, 2, orders,
                    boms or {PROD: [(BOLT, Dc(4)), (MOTOR, Dc(1))]},
                    {PROD: 3, FRAME: 3},
                    on_hand or {}, incoming or {}, issued or {}, locked or [])


def test_all_in_stock_is_ready():
    r = compute(snap([order(1, 2, 10)], on_hand={BOLT: Dc(8), MOTOR: Dc(2)}))
    o = r.orders[1]
    assert o.status == KitStatus.READY and o.color == "GREEN"
    assert o.kit_rate == Dc(1)
    assert o.ready_within_buffer


def test_priority_urgent_order_gets_common_part_first():
    # 两台整机抢 1 个电机：特急单拿到，普通单缺料
    r = compute(snap([order(1, 1, 10, pri=3), order(2, 1, 12, pri=1)],
                     on_hand={BOLT: Dc(8), MOTOR: Dc(1)}))
    assert r.orders[2].status == KitStatus.READY
    assert r.orders[1].status == KitStatus.SHORT
    assert r.shortages[MOTOR]["qty"] == Dc(1)


def test_committed_order_not_robbed_by_unreleased():
    r = compute(snap([order(1, 1, 10, pri=3, committed=0), order(2, 1, 5, pri=1, committed=1)],
                     on_hand={BOLT: Dc(8), MOTOR: Dc(1)}))
    assert r.orders[1].status == KitStatus.READY
    assert r.orders[2].status == KitStatus.SHORT


def test_incoming_ok_vs_late_uses_available_date_not_buffer():
    # 需求日 = 今天+10。可用日（到货+检验转运）≤ 需求日即按时；缓冲天数不再是到货要求
    inc_ok = {MOTOR: [Supply("PO", 1, Dc(1), TODAY + timedelta(days=10))]}
    r = compute(snap([order(1, 1, 10)], on_hand={BOLT: Dc(4)}, incoming=inc_ok))
    assert r.orders[1].status == KitStatus.INCOMING_OK
    assert r.orders[1].ready_date == TODAY + timedelta(days=10)

    inc_late = {MOTOR: [Supply("PO", 1, Dc(1), TODAY + timedelta(days=11))]}
    r = compute(snap([order(1, 1, 10)], on_hand={BOLT: Dc(4)}, incoming=inc_late))
    assert r.orders[1].status == KitStatus.INCOMING_LATE


def test_check_due_is_a_precheck_node_not_an_arrival_rule():
    # 需求日 = 今天+2：已进入开工前检查节点；在途按时到也要提示检查
    inc = {MOTOR: [Supply("PO", 1, Dc(1), TODAY + timedelta(days=2))]}
    r = compute(snap([order(1, 1, 2)], on_hand={BOLT: Dc(4)}, incoming=inc))
    assert r.orders[1].status == KitStatus.INCOMING_OK
    assert r.orders[1].check_due is True
    # 需求日还远：不触发
    r = compute(snap([order(1, 1, 20)], on_hand={BOLT: Dc(4)}, incoming=inc))
    assert r.orders[1].check_due is False


def test_locked_reservation_beats_priority():
    # 普通单人工锁定了电机，特急单也抢不走
    r = compute(snap([order(1, 1, 10, pri=3), order(2, 1, 10, pri=1)],
                     on_hand={BOLT: Dc(8), MOTOR: Dc(1)}, locked=[(1, MOTOR, Dc(1), 99)]))
    assert r.orders[1].status == KitStatus.READY
    assert r.orders[2].status == KitStatus.SHORT


def test_issued_qty_reduces_requirement():
    r = compute(snap([order(1, 2, 10)], on_hand={BOLT: Dc(0), MOTOR: Dc(2)},
                     issued={(1, BOLT): Dc(8)}))
    assert r.orders[1].status == KitStatus.READY


def test_multilevel_make_part_explodes_to_children():
    # 整机需要 1 个机架(自制, BOM: 2 钢板, 加工 3 天)；机架无库存，钢板有库存
    boms = {PROD: [(FRAME, Dc(1))], FRAME: [(STEEL, Dc(2))]}
    r = compute(snap([order(1, 1, 10)], on_hand={STEEL: Dc(2)}, boms=boms))
    o = r.orders[1]
    frame = next(ln for ln in o.lines if ln.material_id == FRAME)
    steel = next(ln for ln in o.lines if ln.material_id == STEEL)
    assert frame.exploded and frame.from_children == Dc(1)
    assert steel.level == 2 and steel.status == KitStatus.READY
    assert o.status == KitStatus.INCOMING_OK  # 需要加工 3 天，今天+3 ≤ 今天+8
    assert o.ready_date == TODAY + timedelta(days=3)

    r = compute(snap([order(1, 1, 10)], on_hand={STEEL: Dc(1)}, boms=boms))
    assert r.orders[1].status == KitStatus.SHORT
    assert r.shortages[STEEL]["qty"] == Dc(1)


def test_partial_stock_plus_incoming_and_reservations_written():
    inc = {BOLT: [Supply("PO", 7, Dc(10), TODAY + timedelta(days=3))]}
    r = compute(snap([order(1, 2, 10)], on_hand={BOLT: Dc(5), MOTOR: Dc(2)}, incoming=inc))
    bolt = next(ln for ln in r.orders[1].lines if ln.material_id == BOLT)
    assert bolt.from_stock == Dc(5) and bolt.from_incoming == Dc(3)
    srcs = {(x["material_id"], x["source_type"]): x["qty"] for x in r.reservations}
    assert srcs[(BOLT, "STOCK")] == Dc(5) and srcs[(BOLT, "PO")] == Dc(3)
    assert r.orders[1].kit_rate == Dc("0.5")  # 2 个品项中 1 个已齐


def test_pegged_supply_reserved_for_its_order():
    # 按单采购的电机（专属订单 1）不会被优先级更高的订单 2 抢走
    inc = {MOTOR: [Supply("PO", 1, Dc(1), TODAY + timedelta(days=3), True, 1)]}
    r = compute(snap([order(1, 1, 10, pri=3), order(2, 1, 10, pri=1)], on_hand={BOLT: Dc(8)}, incoming=inc))
    assert r.orders[1].status == KitStatus.INCOMING_OK
    assert r.orders[2].status == KitStatus.SHORT


def test_bom_cycle_does_not_recurse_forever():
    boms = {PROD: [(FRAME, Dc(1))], FRAME: [(STEEL, Dc(1))], STEEL: [(FRAME, Dc(1))]}
    r = compute(snap([order(1, 1, 10)], boms=boms))
    assert r.orders[1].status == KitStatus.SHORT
