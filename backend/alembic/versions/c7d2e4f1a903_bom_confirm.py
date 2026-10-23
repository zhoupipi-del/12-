"""legacy task BOM technician confirmation

Revision ID: c7d2e4f1a903
Revises: b6a91f8c2d44
Create Date: 2026-10-23 09:00:00

自制任务 / 外协单增加 BOM 确认留痕字段（确认人、确认时间、依据）。
没有 bom_snapshot 的旧单据即“待技术确认”，迁移不替技术选版本，也不改写任何已有数据。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c7d2e4f1a903"
down_revision: Union[str, Sequence[str], None] = "b6a91f8c2d44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("mf_work_order", "mf_outsource")


def upgrade() -> None:
    for t in TABLES:
        with op.batch_alter_table(t) as b:
            b.add_column(sa.Column("bom_confirmed_by", sa.Integer(), nullable=True))
            b.add_column(sa.Column("bom_confirmed_at", sa.DateTime(), nullable=True))
            b.add_column(sa.Column("bom_confirm_note", sa.String(length=300), nullable=False, server_default=""))


def downgrade() -> None:
    for t in TABLES:
        with op.batch_alter_table(t) as b:
            b.drop_column("bom_confirm_note")
            b.drop_column("bom_confirmed_at")
            b.drop_column("bom_confirmed_by")
