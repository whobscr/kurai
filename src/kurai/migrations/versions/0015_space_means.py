"""Cached per-space library mean, so retrieval.joint() doesn't recompute it on every wave rebuild.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("space_means",
                    sa.Column("space", sa.String(16), primary_key=True),
                    sa.Column("model", sa.String(128), primary_key=True),
                    sa.Column("vector", Vector(), nullable=False),
                    sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now()))


def downgrade() -> None:
    op.drop_table("space_means")
