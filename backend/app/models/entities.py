"""全部业务表。

分区：
  1. 系统：用户、审计、预警、开放接口密钥、AI 调用日志、看板设置
  2. 主数据：物料、仓库、库位、供应商、BOM、图纸版本
  3. 销售与计划：销售订单、齐套结果、预占
  4. 采购：请购（计划单）、采购订单（行级锁定）、行修订记录、供应商门户令牌
  5. 生产：自制任务、外协单
  6. 库存：批次、流水、到货检验、领料
  7. 质量：来料异常单
  8. 交付：发货计划、周计划、装配注意事项
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint, Index)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from .common import MONEY, QTY, ZERO, TimestampMixin, now

# ---------------------------------------------------------------- 1. 系统


class User(Base, TimestampMixin):
    __tablename__ = "sys_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(50))
    password_hash: Mapped[str] = mapped_column(String(200))
    roles: Mapped[str] = mapped_column(String(200), default="")  # 逗号分隔，一个账号可兼多个角色
    department: Mapped[str] = mapped_column(String(50), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def role_list(self) -> list[str]:
        return [r for r in self.roles.split(",") if r]


class AuditLog(Base):
    __tablename__ = "sys_audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    action: Mapped[str] = mapped_column(String(50))
    entity: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[Optional[int]] = mapped_column(Integer)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (Index("ix_audit_entity", "entity", "entity_id"),)


class Alert(Base):
    __tablename__ = "sys_alert"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(30))  # SHORTAGE / LATE_SUPPLY / STOCK_LOW / ...
    level: Mapped[str] = mapped_column(String(10), default="WARN")  # INFO / WARN / CRITICAL
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text, default="")
    dedupe_key: Mapped[str] = mapped_column(String(200), index=True)
    target_role: Mapped[str] = mapped_column(String(30), default="")
    ref_type: Mapped[str] = mapped_column(String(30), default="")
    ref_id: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), default="OPEN")  # OPEN / ACK / RESOLVED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ApiKey(Base, TimestampMixin):
    """开放接口密钥（解决“系统封闭、无法二次开发”）。只存哈希。"""
    __tablename__ = "sys_api_key"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    key_prefix: Mapped[str] = mapped_column(String(12))
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    scopes: Mapped[str] = mapped_column(String(200), default="read")  # read,write
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class AiCallLog(Base):
    __tablename__ = "sys_ai_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    scene: Mapped[str] = mapped_column(String(50))
    prompt_chars: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Setting(Base):
    """键值配置：看板模式、展示模式文案等。"""
    __tablename__ = "sys_setting"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------- 2. 主数据


class Supplier(Base, TimestampMixin):
    __tablename__ = "md_supplier"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    contact: Mapped[str] = mapped_column(String(50), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    is_outsourcer: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否外协厂
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    portal_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True)  # 供应商门户链接令牌


class Material(Base, TimestampMixin):
    __tablename__ = "md_material"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    spec: Mapped[str] = mapped_column(String(200), default="")
    unit: Mapped[str] = mapped_column(String(10), default="件")
    type: Mapped[str] = mapped_column(String(12))  # MaterialType
    drawing_no: Mapped[str] = mapped_column(String(80), default="", index=True)
    drawing_exempt: Mapped[bool] = mapped_column(Boolean, default=False)  # 免图纸管控（确无受控图纸的标准件）
    bom_exempt: Mapped[bool] = mapped_column(Boolean, default=False)  # 免 BOM：工艺确无物料投入（需技术明确勾选，缺资料不等于免投料）
    is_common: Mapped[bool] = mapped_column(Boolean, default=False)  # 通用件（多机型共用，参与冲填抢料）
    is_spare: Mapped[bool] = mapped_column(Boolean, default=False)   # 同时作为售后备件
    lead_time_days: Mapped[int] = mapped_column(Integer, default=7)
    # 到货后检验 + 转运到线边所需天数：在途可用日 = 预计到货日 + inbound_days
    inbound_days: Mapped[int] = mapped_column(Integer, default=1)
    # 生产库安全库存上下限（低于下限=断料预警，高于上限=积压预警）
    safety_min: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    safety_max: Mapped[Decimal] = mapped_column(QTY, default=ZERO)  # 0 = 不检查上限
    # 售后备件库安全库存
    spare_min: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    spare_max: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    default_buyer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    default_supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("md_supplier.id"))
    batch_managed: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Warehouse(Base, TimestampMixin):
    __tablename__ = "md_warehouse"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(50))
    purpose: Mapped[str] = mapped_column(String(12))  # WarehousePurpose —— 分账依据
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    locations: Mapped[list["Location"]] = relationship(back_populates="warehouse")


class Location(Base):
    __tablename__ = "md_location"
    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("md_warehouse.id"))
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(50), default="")
    warehouse: Mapped[Warehouse] = relationship(back_populates="locations")
    __table_args__ = (UniqueConstraint("warehouse_id", "code"),)


class Bom(Base, TimestampMixin):
    """BOM 表头。一个物料可有多个版本，同一时刻只有一个 ACTIVE。"""
    __tablename__ = "md_bom"
    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"), index=True)
    version: Mapped[str] = mapped_column(String(20), default="V1")
    status: Mapped[str] = mapped_column(String(10), default="DRAFT")  # DRAFT / ACTIVE / OBSOLETE
    is_mature: Mapped[bool] = mapped_column(Boolean, default=False)  # 成熟机型（技术统一维护）
    remark: Mapped[str] = mapped_column(String(200), default="")
    maintained_by: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    material: Mapped[Material] = relationship()
    lines: Mapped[list["BomLine"]] = relationship(back_populates="bom", cascade="all, delete-orphan",
                                                  order_by="BomLine.seq")


class BomLine(Base):
    __tablename__ = "md_bom_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    bom_id: Mapped[int] = mapped_column(ForeignKey("md_bom.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=10)
    child_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"))
    qty_per: Mapped[Decimal] = mapped_column(QTY)
    scrap_rate: Mapped[Decimal] = mapped_column(QTY, default=ZERO)  # 损耗率 0.05 = 5%
    remark: Mapped[str] = mapped_column(String(200), default="")
    bom: Mapped[Bom] = relationship(back_populates="lines")
    child: Mapped[Material] = relationship()


class Drawing(Base, TimestampMixin):
    __tablename__ = "md_drawing"
    id: Mapped[int] = mapped_column(primary_key=True)
    drawing_no: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    material_id: Mapped[Optional[int]] = mapped_column(ForeignKey("md_material.id"))
    versions: Mapped[list["DrawingVersion"]] = relationship(back_populates="drawing",
                                                            order_by="DrawingVersion.id")


class DrawingVersion(Base, TimestampMixin):
    __tablename__ = "md_drawing_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    drawing_id: Mapped[int] = mapped_column(ForeignKey("md_drawing.id"), index=True)
    version: Mapped[str] = mapped_column(String(10))  # A / B / C ...
    # DRAFT / RELEASED(当前生效，新单据绑定此版) / SUPERSEDED(已被新版替代，已绑定的在制单据可继续使用) / OBSOLETE(作废)
    status: Mapped[str] = mapped_column(String(12), default="DRAFT")
    change_level: Mapped[str] = mapped_column(String(8), default="MINOR")  # MINOR 一般变更 / MAJOR 重大(安全/质量)
    change_note: Mapped[str] = mapped_column(Text, default="")
    file_name: Mapped[str] = mapped_column(String(200), default="")  # 原始文件（DWG 等）
    file_path: Mapped[str] = mapped_column(String(300), default="")
    pdf_path: Mapped[str] = mapped_column(String(300), default="")   # 受控 PDF，用于在线查阅
    uploaded_by: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    released_by: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    drawing: Mapped[Drawing] = relationship(back_populates="versions")
    __table_args__ = (UniqueConstraint("drawing_id", "version"),)


class DrawingBinding(Base):
    """单据绑定的批准图纸版本。升版不自动改变绑定；由技术评估后指定切换或暂停。"""
    __tablename__ = "md_drawing_binding"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref_type: Mapped[str] = mapped_column(String(20))  # SALES_ORDER / WORK_ORDER / OUTSOURCE
    ref_id: Mapped[int] = mapped_column(Integer)
    drawing_id: Mapped[int] = mapped_column(ForeignKey("md_drawing.id"))
    version_id: Mapped[int] = mapped_column(ForeignKey("md_drawing_version.id"))
    hold: Mapped[bool] = mapped_column(Boolean, default=False)  # 重大变更暂停：解除前不能开工/发出/继续
    hold_reason: Mapped[str] = mapped_column(String(300), default="")
    previous_version_id: Mapped[Optional[int]] = mapped_column(Integer)
    updated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (UniqueConstraint("ref_type", "ref_id", "drawing_id"),)


class DrawingConfirmation(Base):
    """加工/装配前的图纸版本确认记录（准入条件）。"""
    __tablename__ = "md_drawing_confirm"
    id: Mapped[int] = mapped_column(primary_key=True)
    drawing_version_id: Mapped[int] = mapped_column(ForeignKey("md_drawing_version.id"))
    ref_type: Mapped[str] = mapped_column(String(20))  # SALES_ORDER / WORK_ORDER / OUTSOURCE
    ref_id: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(20))  # MACHINING / ASSEMBLY
    confirmed_by: Mapped[int] = mapped_column(ForeignKey("sys_user.id"))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (Index("ix_dwg_confirm_ref", "ref_type", "ref_id"),)


# ---------------------------------------------------------------- 3. 销售与计划


class SalesOrder(Base, TimestampMixin):
    __tablename__ = "so_order"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_no: Mapped[str] = mapped_column(String(30), unique=True)
    customer: Mapped[str] = mapped_column(String(100))
    product_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"))  # 机型
    bom_id: Mapped[Optional[int]] = mapped_column(ForeignKey("md_bom.id"))  # 下达时锁定的 BOM 版本
    bom_snapshot: Mapped[Optional[str]] = mapped_column(Text)  # 下达时冻结的整棵多级 BOM（JSON）：子 BOM 升版不影响已下达订单
    qty: Mapped[Decimal] = mapped_column(QTY)
    due_date: Mapped[date] = mapped_column(Date)           # 客户交期
    planned_start: Mapped[Optional[date]] = mapped_column(Date)  # 计划装配开始（=物料需求日）
    priority: Mapped[int] = mapped_column(Integer, default=3)    # Priority
    priority_pin: Mapped[int] = mapped_column(Integer, default=0)  # 人工置顶顺序（>0 优先，越小越前）
    status: Mapped[str] = mapped_column(String(12), default="PLANNED")
    is_nonstandard: Mapped[bool] = mapped_column(Boolean, default=False)
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 装配进度 %
    station: Mapped[str] = mapped_column(String(50), default="")  # 装配工位
    remark: Mapped[str] = mapped_column(Text, default="")
    # 齐套结果冗余（便于列表/看板查询）
    kit_status: Mapped[str] = mapped_column(String(15), default="")
    kit_color: Mapped[str] = mapped_column(String(8), default="")
    kit_rate: Mapped[Decimal] = mapped_column(QTY, default=ZERO)  # 齐套率 0~1
    kit_ready_date: Mapped[Optional[date]] = mapped_column(Date)  # 预计齐套日
    kit_calc_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    product: Mapped[Material] = relationship(foreign_keys=[product_id])


class KitLine(Base):
    """齐套计算结果（每次重算整体替换）。"""
    __tablename__ = "pl_kit_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("so_order.id", ondelete="CASCADE"), index=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"), index=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("md_material.id"))
    level: Mapped[int] = mapped_column(Integer, default=1)
    required: Mapped[Decimal] = mapped_column(QTY)      # 毛需求（扣已领）
    issued: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    from_stock: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    from_incoming: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    from_children: Mapped[Decimal] = mapped_column(QTY, default=ZERO)  # 自制件：由下级物料齐套可做出的量
    short_qty: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    need_date: Mapped[Optional[date]] = mapped_column(Date)
    expected_date: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(15))
    supply_detail: Mapped[str] = mapped_column(Text, default="")  # JSON：分配到的供应来源
    material: Mapped[Material] = relationship(foreign_keys=[material_id])


class Reservation(Base, TimestampMixin):
    """预占（冲填）。locked=True 为人工锁定，重算时优先保留。"""
    __tablename__ = "pl_reservation"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("so_order.id", ondelete="CASCADE"), index=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"), index=True)
    qty: Mapped[Decimal] = mapped_column(QTY)
    source_type: Mapped[str] = mapped_column(String(12))  # STOCK / PO / WO / OUTSOURCE / ARRIVAL
    source_id: Mapped[Optional[int]] = mapped_column(Integer)
    expected_date: Mapped[Optional[date]] = mapped_column(Date)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_by: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    lock_reason: Mapped[str] = mapped_column(String(200), default="")


class SysMutex(Base):
    """应用级互斥行：用 SELECT ... FOR UPDATE 串行化齐套重算 / 调料审批等全局写操作。"""
    __tablename__ = "sys_mutex"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    touched_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ReallocationRequest(Base, TimestampMixin):
    """调料申请：把已冻结给其他订单的库存调给急单。先预演影响，审批通过后才执行。"""
    __tablename__ = "pl_realloc"
    id: Mapped[int] = mapped_column(primary_key=True)
    req_no: Mapped[str] = mapped_column(String(30), unique=True)
    to_order_id: Mapped[int] = mapped_column(ForeignKey("so_order.id"))
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"))
    qty: Mapped[Decimal] = mapped_column(QTY)
    reason: Mapped[str] = mapped_column(String(300))
    plan: Mapped[str] = mapped_column(Text, default="[]")     # JSON [{from_order_id, order_no, qty}]
    impacts: Mapped[str] = mapped_column(Text, default="[]")  # JSON 预演结果
    status: Mapped[str] = mapped_column(String(10), default="PENDING")  # PENDING / APPROVED / REJECTED / EXPIRED
    requested_by: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    decided_by: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    decision_comment: Mapped[str] = mapped_column(String(300), default="")


class OrderIssue(Base):
    """订单已领料汇总（领料单过账后累加），齐套从需求中扣除。"""
    __tablename__ = "pl_order_issue"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("so_order.id", ondelete="CASCADE"))
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"))
    qty: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    __table_args__ = (UniqueConstraint("order_id", "material_id"),)


# ---------------------------------------------------------------- 4. 采购


class Requisition(Base, TimestampMixin):
    """请购计划单：在这里改图号、供应商、数量，确认后才转采购订单。"""
    __tablename__ = "pu_requisition"
    id: Mapped[int] = mapped_column(primary_key=True)
    req_no: Mapped[str] = mapped_column(String(30), unique=True)
    status: Mapped[str] = mapped_column(String(12), default="DRAFT")  # DRAFT / CONFIRMED / CONVERTED / CANCELLED
    source: Mapped[str] = mapped_column(String(20), default="MANUAL")  # MANUAL / KITTING
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    remark: Mapped[str] = mapped_column(Text, default="")
    lines: Mapped[list["RequisitionLine"]] = relationship(back_populates="req", cascade="all, delete-orphan")


class RequisitionLine(Base):
    __tablename__ = "pu_requisition_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    req_id: Mapped[int] = mapped_column(ForeignKey("pu_requisition.id", ondelete="CASCADE"))
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"))
    drawing_no: Mapped[str] = mapped_column(String(80), default="")
    qty: Mapped[Decimal] = mapped_column(QTY)
    need_date: Mapped[Optional[date]] = mapped_column(Date)
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("md_supplier.id"))
    price: Mapped[Optional[Decimal]] = mapped_column(MONEY)
    buyer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("so_order.id"))  # 关联销售订单（可空）
    po_line_id: Mapped[Optional[int]] = mapped_column(Integer)
    remark: Mapped[str] = mapped_column(String(200), default="")
    req: Mapped[Requisition] = relationship(back_populates="lines")
    material: Mapped[Material] = relationship()


class PurchaseOrder(Base, TimestampMixin):
    __tablename__ = "pu_order"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_no: Mapped[str] = mapped_column(String(30), unique=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("md_supplier.id"), index=True)
    buyer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    status: Mapped[str] = mapped_column(String(12), default="DRAFT")  # DRAFT / APPROVED / CLOSED / CANCELLED
    order_date: Mapped[date] = mapped_column(Date, default=date.today)
    remark: Mapped[str] = mapped_column(Text, default="")
    # 编辑锁（防止多人同时改同一张单）
    edit_lock_user: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    edit_lock_until: Mapped[Optional[datetime]] = mapped_column(DateTime)
    version: Mapped[int] = mapped_column(Integer, default=1)  # 乐观锁版本号
    supplier: Mapped[Supplier] = relationship()
    lines: Mapped[list["PurchaseOrderLine"]] = relationship(back_populates="po", cascade="all, delete-orphan",
                                                            order_by="PurchaseOrderLine.line_no")


class PurchaseOrderLine(Base):
    """采购订单行 —— 锁定粒度是“行”，不是“整单”。"""
    __tablename__ = "pu_order_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("pu_order.id", ondelete="CASCADE"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"), index=True)
    drawing_no: Mapped[str] = mapped_column(String(80), default="")
    qty: Mapped[Decimal] = mapped_column(QTY)
    price: Mapped[Decimal] = mapped_column(MONEY, default=ZERO)
    need_date: Mapped[Optional[date]] = mapped_column(Date)
    promised_date: Mapped[Optional[date]] = mapped_column(Date)  # 供应商承诺交期
    arrived_qty: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    received_qty: Mapped[Decimal] = mapped_column(QTY, default=ZERO)  # 检验合格入库数
    status: Mapped[str] = mapped_column(String(12), default="OPEN")  # OPEN / PARTIAL / RECEIVED / CLOSED / CANCELLED
    supplier_note: Mapped[str] = mapped_column(String(300), default="")
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("so_order.id"))
    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")
    material: Mapped[Material] = relationship()

    @property
    def locked(self) -> bool:
        """有到货/入库的行被锁定；其他行不受影响。"""
        return self.arrived_qty > 0 or self.received_qty > 0

    @property
    def open_qty(self) -> Decimal:
        if self.status in ("CLOSED", "CANCELLED"):
            return ZERO
        return max(self.qty - self.arrived_qty, ZERO)


class PoLineRevision(Base):
    """采购行修订/冲销记录，追溯谁在什么时候改了什么。"""
    __tablename__ = "pu_line_revision"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_line_id: Mapped[int] = mapped_column(ForeignKey("pu_order_line.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(20))  # EDIT / REVERSE / CLOSE_REMAIN / CANCEL / SUPPLIER_DATE
    before: Mapped[str] = mapped_column(Text, default="")
    after: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(String(300), default="")
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------------------------------------------------------------- 5. 生产


class WorkOrder(Base, TimestampMixin):
    """自制件加工任务。"""
    __tablename__ = "mf_work_order"
    id: Mapped[int] = mapped_column(primary_key=True)
    wo_no: Mapped[str] = mapped_column(String(30), unique=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"), index=True)
    qty: Mapped[Decimal] = mapped_column(QTY)
    done_qty: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    planned_start: Mapped[Optional[date]] = mapped_column(Date)
    planned_finish: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(12), default="PLANNED")  # PLANNED / STARTED / DONE / CANCELLED
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("so_order.id"))
    owner: Mapped[str] = mapped_column(String(50), default="")
    remark: Mapped[str] = mapped_column(Text, default="")
    bom_snapshot: Mapped[Optional[str]] = mapped_column(Text)  # 建单时冻结的批准 BOM（JSON [[子件, 含损耗用量]]）：领料/报工/需求都读它
    material: Mapped[Material] = relationship()


class OutsourceOrder(Base, TimestampMixin):
    """外协单：发出 → 外协厂加工 → 回收（走到货检验）。"""
    __tablename__ = "mf_outsource"
    id: Mapped[int] = mapped_column(primary_key=True)
    os_no: Mapped[str] = mapped_column(String(30), unique=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("md_supplier.id"))
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"), index=True)
    drawing_no: Mapped[str] = mapped_column(String(80), default="")
    qty: Mapped[Decimal] = mapped_column(QTY)
    price: Mapped[Decimal] = mapped_column(MONEY, default=ZERO)
    sent_date: Mapped[Optional[date]] = mapped_column(Date)
    expected_return: Mapped[Optional[date]] = mapped_column(Date)
    arrived_qty: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    received_qty: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    status: Mapped[str] = mapped_column(String(12), default="PLANNED")  # PLANNED / SENT / PARTIAL / RETURNED / CANCELLED
    # TURNKEY 包工包料（外协厂自备料）/ CONSIGNED 本厂供料（发出时按 BOM 发料，发出前计入原料需求）
    supply_mode: Mapped[str] = mapped_column(String(10), default="TURNKEY")
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("so_order.id"))
    remark: Mapped[str] = mapped_column(Text, default="")
    bom_snapshot: Mapped[Optional[str]] = mapped_column(Text)  # 同自制任务：建单时冻结的批准 BOM
    supplier: Mapped[Supplier] = relationship()
    material: Mapped[Material] = relationship()

    @property
    def open_qty(self) -> Decimal:
        if self.status in ("CANCELLED", "RETURNED"):
            return ZERO
        return max(self.qty - self.arrived_qty, ZERO)


# ---------------------------------------------------------------- 6. 库存


class StockLot(Base, TimestampMixin):
    """批次库存（先进先出依据 received_at）。"""
    __tablename__ = "inv_lot"
    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("md_warehouse.id"), index=True)
    location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("md_location.id"))
    lot_no: Mapped[str] = mapped_column(String(50))
    qty: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    unit_cost: Mapped[Decimal] = mapped_column(MONEY, default=ZERO)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    source_type: Mapped[str] = mapped_column(String(20), default="")
    source_id: Mapped[Optional[int]] = mapped_column(Integer)
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("md_supplier.id"))
    last_move_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    material: Mapped[Material] = relationship()
    warehouse: Mapped[Warehouse] = relationship()
    location: Mapped[Optional[Location]] = relationship()


class StockTxn(Base):
    """库存流水（出入库明细），只追加不修改。"""
    __tablename__ = "inv_txn"
    id: Mapped[int] = mapped_column(primary_key=True)
    txn_type: Mapped[str] = mapped_column(String(20), index=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("md_warehouse.id"))
    lot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("inv_lot.id"))
    qty: Mapped[Decimal] = mapped_column(QTY)  # 正数入，负数出
    ref_type: Mapped[str] = mapped_column(String(20), default="")
    ref_id: Mapped[Optional[int]] = mapped_column(Integer)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("so_order.id"))
    customer: Mapped[str] = mapped_column(String(100), default="")  # 备件销售客户
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    remark: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    material: Mapped[Material] = relationship()
    warehouse: Mapped[Warehouse] = relationship()


class Arrival(Base, TimestampMixin):
    """到货登记 → 待检 → 检验（合格入库 / 不合格转来料异常）。"""
    __tablename__ = "inv_arrival"
    id: Mapped[int] = mapped_column(primary_key=True)
    arrival_no: Mapped[str] = mapped_column(String(30), unique=True)
    client_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True)  # 幂等键：重复提交只登记一次
    source_type: Mapped[str] = mapped_column(String(12))  # PO / OUTSOURCE
    source_line_id: Mapped[int] = mapped_column(Integer)  # PO 行 id 或外协单 id
    supplier_id: Mapped[int] = mapped_column(ForeignKey("md_supplier.id"))
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"), index=True)
    qty: Mapped[Decimal] = mapped_column(QTY)  # 实到数量
    expected_qty: Mapped[Decimal] = mapped_column(QTY, default=ZERO)  # 本次应到（用于数量差异）
    arrived_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    status: Mapped[str] = mapped_column(String(12), default="PENDING")  # PENDING / INSPECTING / INSPECTED / REVERSED
    qualified_qty: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    rejected_qty: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    inspector_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    inspected_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    inspect_note: Mapped[str] = mapped_column(Text, default="")
    warehouse_id: Mapped[Optional[int]] = mapped_column(ForeignKey("md_warehouse.id"))
    location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("md_location.id"))
    lot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("inv_lot.id"))
    unit_cost: Mapped[Decimal] = mapped_column(MONEY, default=ZERO)
    material: Mapped[Material] = relationship()
    supplier: Mapped[Supplier] = relationship()


class PickList(Base, TimestampMixin):
    """领料单：参照订单 BOM 自动带出物料，先进先出扣批次。"""
    __tablename__ = "inv_pick"
    id: Mapped[int] = mapped_column(primary_key=True)
    pick_no: Mapped[str] = mapped_column(String(30), unique=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("so_order.id"))
    work_order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("mf_work_order.id"))
    outsource_id: Mapped[Optional[int]] = mapped_column(ForeignKey("mf_outsource.id"))
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("md_warehouse.id"))
    status: Mapped[str] = mapped_column(String(10), default="DRAFT")  # DRAFT / POSTING / POSTED / CANCELLED
    picker: Mapped[str] = mapped_column(String(50), default="")
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    lines: Mapped[list["PickLine"]] = relationship(back_populates="pick", cascade="all, delete-orphan")


class PickLine(Base):
    __tablename__ = "inv_pick_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    pick_id: Mapped[int] = mapped_column(ForeignKey("inv_pick.id", ondelete="CASCADE"))
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"))
    plan_qty: Mapped[Decimal] = mapped_column(QTY)
    qty: Mapped[Decimal] = mapped_column(QTY)  # 实领
    returned_qty: Mapped[Decimal] = mapped_column(QTY, default=ZERO)  # 已退料
    lots: Mapped[str] = mapped_column(Text, default="")  # 过账后：JSON [{lot_id, lot_no, qty}]
    pick: Mapped[PickList] = relationship(back_populates="lines")
    material: Mapped[Material] = relationship()


# ---------------------------------------------------------------- 7. 质量


class IncomingException(Base, TimestampMixin):
    """来料异常申请单。

    流程：DRAFT → SUBMITTED → (审核) DISPOSED → [退换/返修: AWAIT_REINSPECT → REINSPECTED] → CLOSED
          SUBMITTED → REJECTED(驳回) → 修改后重新提交
    """
    __tablename__ = "qc_exception"
    id: Mapped[int] = mapped_column(primary_key=True)
    exc_no: Mapped[str] = mapped_column(String(30), unique=True)
    category: Mapped[str] = mapped_column(String(10))  # QTY 数量 / QUALITY 质量
    arrival_id: Mapped[Optional[int]] = mapped_column(ForeignKey("inv_arrival.id"))
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("md_supplier.id"))
    material_id: Mapped[int] = mapped_column(ForeignKey("md_material.id"))
    qty: Mapped[Decimal] = mapped_column(QTY)
    description: Mapped[str] = mapped_column(Text)
    responsibility: Mapped[str] = mapped_column(String(12), default="")  # SUPPLIER / INTERNAL / LOGISTICS
    disposition: Mapped[str] = mapped_column(String(12), default="")  # RETURN / EXCHANGE / CONCESSION / REWORK
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    deadline: Mapped[Optional[date]] = mapped_column(Date)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))  # 当前处理责任人
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    reinspect_qualified: Mapped[Decimal] = mapped_column(QTY, default=ZERO)
    result: Mapped[str] = mapped_column(Text, default="")
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    material: Mapped[Material] = relationship()
    supplier: Mapped[Optional[Supplier]] = relationship()
    logs: Mapped[list["ExceptionLog"]] = relationship(back_populates="exc", cascade="all, delete-orphan",
                                                      order_by="ExceptionLog.id")


class ExceptionLog(Base):
    __tablename__ = "qc_exception_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    exc_id: Mapped[int] = mapped_column(ForeignKey("qc_exception.id", ondelete="CASCADE"))
    from_status: Mapped[str] = mapped_column(String(16), default="")
    to_status: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    exc: Mapped[IncomingException] = relationship(back_populates="logs")


# ---------------------------------------------------------------- 8. 交付 / 看板


class ShipmentPlan(Base, TimestampMixin):
    """发货计划：DRAFT → SUBMITTED → APPROVED → SHIPPED（REJECTED 可退回修改）。"""
    __tablename__ = "dl_shipment"
    id: Mapped[int] = mapped_column(primary_key=True)
    ship_no: Mapped[str] = mapped_column(String(30), unique=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("so_order.id"))
    qty: Mapped[Decimal] = mapped_column(QTY)
    plan_date: Mapped[date] = mapped_column(Date)
    address: Mapped[str] = mapped_column(String(300), default="")
    carrier: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(12), default="DRAFT")
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    approved_by: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    shipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    approve_comment: Mapped[str] = mapped_column(String(300), default="")
    order: Mapped[SalesOrder] = relationship()


class WeeklyPlanItem(Base, TimestampMixin):
    """部门周计划（小屏）。"""
    __tablename__ = "bd_weekly_plan"
    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, index=True)  # 周一
    department: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(String(300))
    owner: Mapped[str] = mapped_column(String(50), default="")
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="TODO")  # TODO / DOING / DONE
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("so_order.id"))


class AssemblyNote(Base, TimestampMixin):
    """装配注意事项（大屏展示）。"""
    __tablename__ = "bd_assembly_note"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("so_order.id"))
    content: Mapped[str] = mapped_column(String(300))
    level: Mapped[str] = mapped_column(String(10), default="INFO")  # INFO / IMPORTANT
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
