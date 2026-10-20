"""通用字段与枚举常量。

枚举统一存为字符串（而非数据库 ENUM），新增取值不需要改表结构。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

QTY = Numeric(16, 4)
MONEY = Numeric(16, 4)
ZERO = Decimal("0")


def now() -> datetime:
    """本地时间（单厂部署，统一用服务器本地时区，避免时区换算错误）。"""
    return datetime.now().replace(microsecond=0)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now, server_default=func.now())


class Role:
    ADMIN = "ADMIN"          # 系统管理员
    MANAGER = "MANAGER"      # 生产主管 / 审批人
    PLANNER = "PLANNER"      # 计划员
    BUYER = "BUYER"          # 采购员
    WAREHOUSE = "WAREHOUSE"  # 仓库
    QUALITY = "QUALITY"      # 质检
    WORKSHOP = "WORKSHOP"    # 车间
    TECH = "TECH"            # 技术（BOM / 图纸）
    AFTERSALES = "AFTERSALES"  # 售后
    SALES = "SALES"          # 销售
    ALL = [ADMIN, MANAGER, PLANNER, BUYER, WAREHOUSE, QUALITY, WORKSHOP, TECH, AFTERSALES, SALES]
    LABELS = {
        ADMIN: "管理员", MANAGER: "生产主管", PLANNER: "计划员", BUYER: "采购员", WAREHOUSE: "仓库",
        QUALITY: "质检", WORKSHOP: "车间", TECH: "技术", AFTERSALES: "售后", SALES: "销售",
    }


class MaterialType:
    PRODUCT = "PRODUCT"      # 整机 / 成品
    MAKE = "MAKE"            # 自制件
    BUY = "BUY"              # 外购件
    OUTSOURCE = "OUTSOURCE"  # 外协件
    ALL = [PRODUCT, MAKE, BUY, OUTSOURCE]
    LABELS = {PRODUCT: "整机", MAKE: "自制", BUY: "外购", OUTSOURCE: "外协"}


class WarehousePurpose:
    PRODUCTION = "PRODUCTION"  # 生产物料库（参与齐套）
    AFTERSALES = "AFTERSALES"  # 售后备件库（独立分账，不参与生产齐套）
    FINISHED = "FINISHED"      # 成品库
    QUARANTINE = "QUARANTINE"  # 待检/隔离库
    ALL = [PRODUCTION, AFTERSALES, FINISHED, QUARANTINE]
    LABELS = {PRODUCTION: "生产物料", AFTERSALES: "售后备件", FINISHED: "成品", QUARANTINE: "隔离"}


class TxnType:
    PURCHASE_IN = "PURCHASE_IN"        # 采购入库
    OUTSOURCE_IN = "OUTSOURCE_IN"      # 外协回收入库
    PRODUCE_IN = "PRODUCE_IN"          # 自制件完工入库
    FINISHED_IN = "FINISHED_IN"        # 整机完工入库
    CONCESSION_IN = "CONCESSION_IN"    # 让步接收入库
    PICK_OUT = "PICK_OUT"              # 生产领料
    SPARE_SALE_OUT = "SPARE_SALE_OUT"  # 售后备件销售出库
    SPARE_USE_OUT = "SPARE_USE_OUT"    # 售后维修领用
    SHIP_OUT = "SHIP_OUT"              # 成品发货出库
    RETURN_OUT = "RETURN_OUT"          # 退供应商
    REVERSAL = "REVERSAL"              # 红字冲销
    TRANSFER_OUT = "TRANSFER_OUT"      # 调拨出
    TRANSFER_IN = "TRANSFER_IN"        # 调拨入
    ADJUST = "ADJUST"                  # 盘点调整
    PICK_RETURN = "PICK_RETURN"        # 退料入库（已领料退回）
    LABELS = {
        PURCHASE_IN: "采购入库", OUTSOURCE_IN: "外协入库", PRODUCE_IN: "自制入库", FINISHED_IN: "完工入库",
        CONCESSION_IN: "让步接收", PICK_OUT: "生产领料", SPARE_SALE_OUT: "备件销售", SPARE_USE_OUT: "售后领用",
        SHIP_OUT: "发货出库", RETURN_OUT: "退供应商", REVERSAL: "红字冲销", TRANSFER_OUT: "调拨出",
        TRANSFER_IN: "调拨入", ADJUST: "盘点调整", PICK_RETURN: "退料入库",
    }


class Priority:
    URGENT = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4
    LABELS = {1: "特急", 2: "急", 3: "普通", 4: "低"}


class OrderStatus:
    PLANNED = "PLANNED"        # 已录入
    RELEASED = "RELEASED"      # 计划已下达（参与齐套预占）
    ASSEMBLING = "ASSEMBLING"  # 装配中
    COMPLETED = "COMPLETED"    # 完工入库
    SHIPPED = "SHIPPED"        # 已发货
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    ACTIVE_FOR_KIT = [PLANNED, RELEASED, ASSEMBLING]
    LABELS = {PLANNED: "已录入", RELEASED: "已下达", ASSEMBLING: "装配中", COMPLETED: "已完工",
              SHIPPED: "已发货", CLOSED: "已关闭", CANCELLED: "已取消"}


class KitStatus:
    READY = "READY"                  # 库存已预占
    INCOMING_OK = "INCOMING_OK"      # 在途，预计在需求日-缓冲天数前到
    INCOMING_LATE = "INCOMING_LATE"  # 在途但晚于需求，交期风险
    SHORT = "SHORT"                  # 无供应，需补单
    COLOR = {READY: "GREEN", INCOMING_OK: "YELLOW", INCOMING_LATE: "RED", SHORT: "RED"}
    RANK = {READY: 0, INCOMING_OK: 1, INCOMING_LATE: 2, SHORT: 3}
    LABELS = {READY: "已齐", INCOMING_OK: "在途可齐", INCOMING_LATE: "在途延误", SHORT: "缺料"}
