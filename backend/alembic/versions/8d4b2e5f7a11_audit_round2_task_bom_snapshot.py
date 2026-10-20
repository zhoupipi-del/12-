"""audit round 2: work order / outsource frozen BOM snapshot

Revision ID: 8d4b2e5f7a11
Revises: 7c3a1d9b2e40
Create Date: 2026-10-20 15:00:00

已存在的自制任务/外协单 bom_snapshot 保持 NULL：首次开工/领料/报工时按当时生效 BOM 冻结一次。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '8d4b2e5f7a11'
down_revision: Union[str, Sequence[str], None] = '7c3a1d9b2e40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('mf_work_order', sa.Column('bom_snapshot', sa.Text(), nullable=True))
    op.add_column('mf_outsource', sa.Column('bom_snapshot', sa.Text(), nullable=True))
    op.add_column('md_material', sa.Column('bom_exempt', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('md_material', 'bom_exempt')
    op.drop_column('mf_outsource', 'bom_snapshot')
    op.drop_column('mf_work_order', 'bom_snapshot')
