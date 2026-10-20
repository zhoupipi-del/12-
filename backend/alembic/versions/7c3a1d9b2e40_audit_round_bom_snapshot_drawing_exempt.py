"""audit round: order BOM snapshot, material drawing_exempt

Revision ID: 7c3a1d9b2e40
Revises: 49f0559f51e5
Create Date: 2026-10-20 14:00:00

已下达的老订单 bom_snapshot 保持 NULL（沿用当前生效 BOM 的旧行为）；新下达的订单在下达时冻结整棵 BOM。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '7c3a1d9b2e40'
down_revision: Union[str, Sequence[str], None] = '49f0559f51e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('so_order', sa.Column('bom_snapshot', sa.Text(), nullable=True))
    op.add_column('md_material', sa.Column('drawing_exempt', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('md_material', 'drawing_exempt')
    op.drop_column('so_order', 'bom_snapshot')
