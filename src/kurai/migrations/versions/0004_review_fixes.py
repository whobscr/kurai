"""A TOTP code works once; a Telegram link code expires.

Revision ID: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('totp_last_step', sa.BigInteger(), nullable=True))
    op.add_column('users', sa.Column('telegram_link_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'telegram_link_at')
    op.drop_column('users', 'totp_last_step')
