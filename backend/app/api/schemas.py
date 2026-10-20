"""请求体校验模型（输出直接返回 dict，由 FastAPI 统一编码 Decimal/日期）。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6)


class UserIn(BaseModel):
    username: str
    name: str
    password: Optional[str] = None
    roles: list[str] = []
    department: str = ""
    is_active: bool = True


class MaterialIn(BaseModel):
    code: str
    name: str
    spec: str = ""
    unit: str = "件"
    type: str
    drawing_no: str = ""
    drawing_exempt: bool = False
    is_common: bool = False
    is_spare: bool = False
    lead_time_days: int = 7
    inbound_days: int = Field(default=1, ge=0, le=60)  # 到货后检验+转运天数
    safety_min: Decimal = Decimal(0)
    safety_max: Decimal = Decimal(0)
    spare_min: Decimal = Decimal(0)
    spare_max: Decimal = Decimal(0)
    default_buyer_id: Optional[int] = None
    default_supplier_id: Optional[int] = None
    is_active: bool = True


class SupplierIn(BaseModel):
    code: str
    name: str
    contact: str = ""
    phone: str = ""
    is_outsourcer: bool = False
    is_active: bool = True


class WarehouseIn(BaseModel):
    code: str
    name: str
    purpose: str
    is_active: bool = True


class LocationIn(BaseModel):
    code: str
    name: str = ""


class BomLineIn(BaseModel):
    child_id: int
    qty_per: Decimal = Field(gt=0)
    scrap_rate: Decimal = Decimal(0)
    seq: int = 10
    remark: str = ""


class BomIn(BaseModel):
    material_id: int
    version: str = "V1"
    is_mature: bool = False
    remark: str = ""
    lines: list[BomLineIn] = []


class DrawingIn(BaseModel):
    drawing_no: str
    name: str
    material_id: Optional[int] = None


class ConfirmIn(BaseModel):
    ref_type: str
    ref_id: int
    version_ids: list[int]


class DrawingNoChangeIn(BaseModel):
    material_id: int
    new_drawing_no: str
    reason: str


class OrderIn(BaseModel):
    order_no: Optional[str] = None
    customer: str
    product_id: int
    qty: Decimal = Field(gt=0)
    due_date: date
    planned_start: Optional[date] = None
    priority: int = 3
    is_nonstandard: bool = False
    station: str = ""
    remark: str = ""


class ReasonIn(BaseModel):
    reason: str = ""


class LockIn(BaseModel):
    material_id: int
    qty: Decimal
    reason: str


class StartIn(BaseModel):
    override_reason: str = ""


class ReqLineIn(BaseModel):
    material_id: int
    qty: Decimal = Field(gt=0)
    need_date: Optional[date] = None
    supplier_id: Optional[int] = None
    price: Optional[Decimal] = None
    buyer_id: Optional[int] = None
    drawing_no: Optional[str] = None
    order_id: Optional[int] = None
    remark: str = ""


class ReqIn(BaseModel):
    remark: str = ""
    lines: list[ReqLineIn]


class PoLineIn(BaseModel):
    material_id: int
    qty: Decimal = Field(gt=0)
    price: Optional[Decimal] = None
    need_date: Optional[date] = None
    promised_date: Optional[date] = None
    drawing_no: Optional[str] = None
    order_id: Optional[int] = None


class PoIn(BaseModel):
    supplier_id: int
    buyer_id: Optional[int] = None
    remark: str = ""
    lines: list[PoLineIn]


class PoLineEditIn(BaseModel):
    changes: dict
    reason: str = ""
    version: Optional[int] = None


class VersionReasonIn(BaseModel):
    reason: str = ""
    version: Optional[int] = None


class PortalConfirmIn(BaseModel):
    promised_date: date
    note: str = ""


class WorkOrderIn(BaseModel):
    material_id: int
    qty: Decimal = Field(gt=0)
    planned_start: Optional[date] = None
    planned_finish: Optional[date] = None
    order_id: Optional[int] = None
    owner: str = ""
    remark: str = ""


class QtyIn(BaseModel):
    qty: Decimal = Field(gt=0)
    warehouse_id: Optional[int] = None


class OutsourceIn(BaseModel):
    supplier_id: int
    material_id: int
    qty: Decimal = Field(gt=0)
    price: Optional[Decimal] = None
    expected_return: Optional[date] = None
    drawing_no: Optional[str] = None
    order_id: Optional[int] = None
    supply_mode: str = Field(default="TURNKEY", pattern="^(TURNKEY|CONSIGNED)$")  # 包工包料 / 本厂供料
    remark: str = ""


class ArrivalIn(BaseModel):
    source_type: str  # PO / OUTSOURCE
    source_id: int
    qty: Decimal = Field(gt=0)
    expected_qty: Optional[Decimal] = None
    note: str = ""
    client_token: Optional[str] = Field(default=None, max_length=64)  # 幂等键：重复提交只登记一次


class ReallocPreviewIn(BaseModel):
    to_order_id: int
    material_id: int
    qty: Decimal = Field(gt=0)
    from_order_ids: Optional[list[int]] = None


class ReallocRequestIn(ReallocPreviewIn):
    reason: str = Field(min_length=1, max_length=300)


class DecisionIn(BaseModel):
    comment: str = ""


class InspectIn(BaseModel):
    qualified: Decimal
    rejected: Decimal = Decimal(0)
    warehouse_id: Optional[int] = None
    location_id: Optional[int] = None
    lot_no: str = ""
    note: str = ""
    reject_reason: str = ""


class ExceptionIn(BaseModel):
    category: str
    material_id: int
    qty: Decimal = Field(gt=0)
    description: str
    arrival_id: Optional[int] = None
    supplier_id: Optional[int] = None
    deadline_days: int = 3


class CommentIn(BaseModel):
    comment: str = ""


class DisposeIn(BaseModel):
    disposition: str
    responsibility: str
    comment: str = ""
    owner_id: Optional[int] = None
    deadline: Optional[date] = None


class ReinspectIn(BaseModel):
    qualified: Decimal
    warehouse_id: Optional[int] = None
    comment: str = ""


class CloseIn(BaseModel):
    result: str


class TransferIn(BaseModel):
    material_id: int
    from_warehouse_id: int
    to_warehouse_id: int
    qty: Decimal = Field(gt=0)
    to_location_id: Optional[int] = None
    remark: str = ""


class SpareOutIn(BaseModel):
    material_id: int
    warehouse_id: int
    qty: Decimal = Field(gt=0)
    kind: str  # SALE / USE
    customer: str = ""
    remark: str = ""


class AdjustIn(BaseModel):
    lot_id: int
    new_qty: Decimal = Field(ge=0)
    remark: str


class PickLineIn(BaseModel):
    material_id: int
    qty: Decimal = Field(ge=0)


class PickIn(BaseModel):
    order_id: int
    warehouse_id: Optional[int] = None
    picker: str = ""
    lines: Optional[list[PickLineIn]] = None


class ShipmentIn(BaseModel):
    order_id: int
    qty: Optional[Decimal] = None
    plan_date: date
    address: str = ""
    carrier: str = ""


class WeeklyItemIn(BaseModel):
    week_start: Optional[date] = None
    department: str
    content: str
    owner: str = ""
    due_date: Optional[date] = None
    status: str = "TODO"
    order_id: Optional[int] = None


class NoteIn(BaseModel):
    content: str
    level: str = "INFO"
    order_id: Optional[int] = None
    is_active: bool = True


class BoardSettingIn(BaseModel):
    board_mode: Optional[str] = None
    showcase_title: Optional[str] = None
    showcase_slogan: Optional[str] = None
    showcase_intro: Optional[str] = None
    board_refresh_seconds: Optional[str] = None


class ApiKeyIn(BaseModel):
    name: str
    scopes: list[str] = ["read"]


class AiConfigIn(BaseModel):
    ai_enabled: Optional[str] = None
    ai_base_url: Optional[str] = None
    ai_model: Optional[str] = None
    ai_api_key: Optional[str] = None


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ReturnPickIn(BaseModel):
    reason: str = Field(min_length=1, max_length=200)
    lines: list[PickLineIn]


class ReleaseIn(BaseModel):
    change_level: str = Field(default="MINOR", pattern="^(MINOR|MAJOR)$")  # 一般变更 / 重大(安全、质量)
    hold_reason: str = ""


class SwitchIn(BaseModel):
    ref_type: str
    ref_id: int
    drawing_id: int
    new_version_id: int
    reason: str = Field(min_length=1, max_length=300)


class ResolveHoldIn(BaseModel):
    ref_type: str
    ref_id: int
    drawing_id: int
    reason: str = Field(min_length=1, max_length=300)
