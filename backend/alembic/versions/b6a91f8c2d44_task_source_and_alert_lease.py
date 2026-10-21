"""task source mode and alert scan lease

Revision ID: b6a91f8c2d44
Revises: 8d4b2e5f7a11
Create Date: 2026-10-21 20:00:00

旧任务按是否已关联整机订单回填来源：有 order_id 为 ORDER，否则为 STOCK。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b6a91f8c2d44"
down_revision: Union[str, Sequence[str], None] = "8d4b2e5f7a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("mf_work_order") as b:
        b.add_column(sa.Column("source_mode", sa.String(length=10), nullable=False, server_default="STOCK"))
    with op.batch_alter_table("mf_outsource") as b:
        b.add_column(sa.Column("source_mode", sa.String(length=10), nullable=False, server_default="STOCK"))
    op.execute("UPDATE mf_work_order SET source_mode='ORDER' WHERE order_id IS NOT NULL")
    op.execute("UPDATE mf_outsource SET source_mode='ORDER' WHERE order_id IS NOT NULL")
    op.execute("INSERT INTO sys_mutex (name, touched_at) VALUES ('alert_scan', NULL)")


def downgrade() -> None:
    op.execute("DELETE FROM sys_mutex WHERE name='alert_scan'")
    with op.batch_alter_table("mf_outsource") as b:
        b.drop_column("source_mode")
    with op.batch_alter_table("mf_work_order") as b:
        b.drop_column("source_mode")
