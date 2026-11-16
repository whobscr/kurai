"""«Любимое»: how many tracks the user's playlist of their most liked and most played tracks holds, and whether it's
made anew every night.

Revision ID: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('favorites_size', sa.Integer(), server_default='50', nullable=False))
    op.add_column('users', sa.Column('favorites_nightly', sa.Boolean(), server_default=sa.true(), nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'favorites_nightly')
    op.drop_column('users', 'favorites_size')
