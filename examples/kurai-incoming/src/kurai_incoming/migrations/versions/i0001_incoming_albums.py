"""incoming albums

Revision ID: i0001
Revises: (none)
"""

import sqlalchemy as sa
from alembic import op

revision = 'i0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('incoming_albums',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('folder', sa.Text(), nullable=False),
    sa.Column('artist', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('files', sa.Integer(), nullable=False),
    sa.Column('size', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('found_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('folder')
    )


def downgrade() -> None:
    op.drop_table('incoming_albums')
