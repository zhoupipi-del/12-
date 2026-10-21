"""输出序列化。"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ..models import (Arrival, Bom, IncomingException, KitLine, Material, OutsourceOrder, PickList,
                      PurchaseOrder, PurchaseOrderLine, Requisition, Reservation, SalesOrder,
                      ShipmentPlan, Supplier, User, WorkOrder, OrderStatus, KitStatus, Priority,
                      MaterialType)


def pick(obj, *fields) -> dict:
    return {f: getattr(obj, f) for f in fields}


def user_name(db: Session, uid: int | None) -> str:
    if not uid:
        return ""
    u = db.get(User, uid)
    return u.name if u else ""


def user(u: User) -> dict:
    return {**pick(u, "id", "username", "name", "department", "is_active"), "roles": u.role_list}


def material(m: Material) -> dict:
    return {**pick(m, "id", "code", "name", "spec", "unit", "type", "drawing_no", "drawing_exempt", "bom_exempt", "is_common", "is_spare",
                   "lead_time_days", "inbound_days", "safety_min", "safety_max", "spare_min", "spare_max", "default_buyer_id",
                   "default_supplier_id", "is_active"),
            "type_label": MaterialType.LABELS.get(m.type, m.type)}


def supplier(s: Supplier) -> dict:
    return {**pick(s, "id", "code", "name", "contact", "phone", "is_outsourcer", "is_active"),
            "has_portal": bool(s.portal_token)}


def bom(b: Bom) -> dict:
    return {**pick(b, "id", "material_id", "version", "status", "is_mature", "remark", "updated_at"),
            "material": f"{b.material.code} {b.material.name}",
            "lines": [{**pick(ln, "id", "seq", "child_id", "qty_per", "scrap_rate", "remark"),
                       "code": ln.child.code, "name": ln.child.name, "spec": ln.child.spec, "unit": ln.child.unit,
                       "type": ln.child.type, "drawing_no": ln.child.drawing_no, "is_common": ln.child.is_common}
                      for ln in b.lines]}


def order(o: SalesOrder) -> dict:
    return {**pick(o, "id", "order_no", "customer", "product_id", "bom_id", "qty", "due_date", "planned_start",
                   "priority", "priority_pin", "status", "is_nonstandard", "progress", "station", "remark",
                   "kit_status", "kit_color", "kit_rate", "kit_ready_date", "kit_calc_at", "created_at"),
            "product": f"{o.product.code} {o.product.name}", "product_name": o.product.name,
            "status_label": OrderStatus.LABELS.get(o.status, o.status),
            "priority_label": Priority.LABELS.get(o.priority, str(o.priority)),
            "kit_label": KitStatus.LABELS.get(o.kit_status, "未计算")}


def kit_line(k: KitLine) -> dict:
    return {**pick(k, "id", "material_id", "parent_id", "level", "required", "issued", "from_stock",
                   "from_incoming", "from_children", "short_qty", "need_date", "expected_date", "status"),
            "code": k.material.code, "name": k.material.name, "type": k.material.type, "unit": k.material.unit,
            "drawing_no": k.material.drawing_no, "is_common": k.material.is_common,
            "status_label": KitStatus.LABELS.get(k.status, k.status), "color": KitStatus.COLOR.get(k.status),
            "supplies": json.loads(k.supply_detail or "[]")}


def reservation(db: Session, r: Reservation) -> dict:
    m = db.get(Material, r.material_id)
    return {**pick(r, "id", "order_id", "material_id", "qty", "source_type", "source_id", "expected_date",
                   "locked", "lock_reason"),
            "code": m.code, "name": m.name, "locked_by": user_name(db, r.locked_by)}


def req(db: Session, r: Requisition) -> dict:
    return {**pick(r, "id", "req_no", "status", "source", "remark", "created_at"),
            "created_by": user_name(db, r.created_by),
            "lines": [{**pick(ln, "id", "material_id", "drawing_no", "qty", "need_date", "supplier_id", "price",
                              "buyer_id", "order_id", "po_line_id", "remark"),
                       "code": ln.material.code, "name": ln.material.name, "unit": ln.material.unit,
                       "supplier": db.get(Supplier, ln.supplier_id).name if ln.supplier_id else "",
                       "buyer": user_name(db, ln.buyer_id)} for ln in r.lines]}


def po_line(ln: PurchaseOrderLine) -> dict:
    return {**pick(ln, "id", "po_id", "line_no", "material_id", "drawing_no", "qty", "price", "need_date",
                   "promised_date", "arrived_qty", "received_qty", "status", "supplier_note", "order_id"),
            "code": ln.material.code, "name": ln.material.name, "spec": ln.material.spec, "unit": ln.material.unit,
            "locked": ln.locked, "open_qty": ln.open_qty, "amount": ln.qty * ln.price}


def po(db: Session, p: PurchaseOrder, with_lines: bool = True) -> dict:
    d = {**pick(p, "id", "po_no", "supplier_id", "buyer_id", "status", "order_date", "remark", "version",
                "edit_lock_until", "created_at"),
         "supplier": p.supplier.name, "buyer": user_name(db, p.buyer_id),
         "edit_lock_user": user_name(db, p.edit_lock_user),
         "line_count": len(p.lines), "locked_lines": sum(1 for x in p.lines if x.locked),
         "amount": sum((x.qty * x.price for x in p.lines if x.status != "CANCELLED"), 0)}
    if with_lines:
        d["lines"] = [po_line(x) for x in p.lines]
    return d


def wo(w: WorkOrder) -> dict:
    return {**pick(w, "id", "wo_no", "material_id", "qty", "done_qty", "planned_start", "planned_finish", "status",
                   "source_mode", "order_id", "owner", "remark"),
            "code": w.material.code, "name": w.material.name, "drawing_no": w.material.drawing_no,
            "order_no": w.order.order_no if getattr(w, "order", None) else ""}


def outsource(o: OutsourceOrder) -> dict:
    return {**pick(o, "id", "os_no", "supplier_id", "material_id", "drawing_no", "qty", "price", "sent_date",
                   "expected_return", "arrived_qty", "received_qty", "status", "source_mode", "order_id", "remark", "supply_mode"),
            "supplier": o.supplier.name, "code": o.material.code, "name": o.material.name, "open_qty": o.open_qty,
            "order_no": o.order.order_no if getattr(o, "order", None) else ""}


def arrival(db: Session, a: Arrival) -> dict:
    return {**pick(a, "id", "arrival_no", "source_type", "source_line_id", "supplier_id", "material_id", "qty",
                   "expected_qty", "arrived_at", "status", "qualified_qty", "rejected_qty", "inspected_at",
                   "inspect_note", "warehouse_id", "lot_id"),
            "supplier": a.supplier.name, "code": a.material.code, "name": a.material.name, "unit": a.material.unit,
            "inspector": user_name(db, a.inspector_id)}


def exception(db: Session, e: IncomingException, logs: bool = False) -> dict:
    from ..services.receiving import QTY_DISPOSITIONS, QUALITY_DISPOSITIONS, RESPONSIBILITY, STATUS_LABELS
    d = {**pick(e, "id", "exc_no", "category", "arrival_id", "supplier_id", "material_id", "qty", "description",
                "responsibility", "disposition", "status", "deadline", "owner_id", "reinspect_qualified", "result",
                "closed_at", "created_at"),
         "code": e.material.code, "name": e.material.name, "supplier": e.supplier.name if e.supplier else "",
         "status_label": STATUS_LABELS.get(e.status, e.status),
         "disposition_label": {**QUALITY_DISPOSITIONS, **QTY_DISPOSITIONS}.get(e.disposition, ""),
         "responsibility_label": RESPONSIBILITY.get(e.responsibility, ""),
         "owner": user_name(db, e.owner_id), "creator": user_name(db, e.created_by),
         "arrival_no": db.get(Arrival, e.arrival_id).arrival_no if e.arrival_id else ""}
    if logs:
        d["logs"] = [{**pick(x, "from_status", "to_status", "action", "comment", "created_at"),
                      "user": user_name(db, x.user_id)} for x in e.logs]
    return d


def pick_list(db: Session, p: PickList) -> dict:
    wo_ = db.get(WorkOrder, p.work_order_id) if p.work_order_id else None
    os_ = db.get(OutsourceOrder, p.outsource_id) if p.outsource_id else None
    source = ("订单 " + db.get(SalesOrder, p.order_id).order_no) if p.order_id else (
        ("自制 " + wo_.wo_no) if wo_ else (("外协 " + os_.os_no) if os_ else ""))
    return {**pick(p, "id", "pick_no", "order_id", "work_order_id", "outsource_id", "warehouse_id", "status", "picker",
                   "posted_at", "created_at"),
            "order_no": db.get(SalesOrder, p.order_id).order_no if p.order_id else "", "source": source,
            "lines": [{**pick(x, "id", "material_id", "plan_qty", "qty", "returned_qty"), "code": x.material.code,
                       "name": x.material.name, "unit": x.material.unit, "lots": json.loads(x.lots or "[]")}
                      for x in p.lines]}


def shipment(db: Session, s: ShipmentPlan) -> dict:
    return {**pick(s, "id", "ship_no", "order_id", "qty", "plan_date", "address", "carrier", "status",
                   "approved_at", "shipped_at", "approve_comment", "created_at"),
            "order_no": s.order.order_no, "customer": s.order.customer, "product": s.order.product.name,
            "creator": user_name(db, s.created_by), "approver": user_name(db, s.approved_by)}


def realloc(db, r) -> dict:
    import json as _j
    so = db.get(SalesOrder, r.to_order_id)
    m = db.get(Material, r.material_id)
    return {**pick(r, "id", "req_no", "to_order_id", "material_id", "qty", "reason", "status", "requested_by",
                   "decided_by", "decided_at", "decision_comment", "created_at"),
            "to_order_no": so.order_no if so else "", "material": f"{m.code} {m.name}" if m else "",
            "plan": _j.loads(r.plan or "[]"), "impacts": _j.loads(r.impacts or "[]")}
