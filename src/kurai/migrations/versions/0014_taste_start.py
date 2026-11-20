"""A taste's audio model, and the first taste stored once at start_asked_at (was re-drawn at random nightly).

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from kurai.audio import EFFNET_DIM

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tastes", sa.Column("audio_model", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("start_text_vector", Vector(), nullable=True))
    op.add_column("users", sa.Column("start_audio_vector", Vector(EFFNET_DIM), nullable=True))
    op.add_column("users", sa.Column("start_top_artists", sa.dialects.postgresql.JSONB(), server_default="[]"))
    op.add_column("users", sa.Column("start_text_model", sa.String(128), nullable=True))
    op.add_column("users", sa.Column("start_audio_model", sa.String(64), nullable=True))
    op.add_column("listens", sa.Column("updated_at", sa.DateTime(timezone=True),
                                       server_default=sa.func.now(), nullable=False))
    op.execute("update listens set updated_at = coalesce(last_played, 'epoch')")
    # existing start_asked_at rows had no stored taste: clear them so the onboarding page re-runs once
    op.execute("update users set start_asked_at = null where start_asked_at is not null")


def downgrade() -> None:
    op.drop_column("tastes", "audio_model")
    op.drop_column("users", "start_text_vector")
    op.drop_column("users", "start_audio_vector")
    op.drop_column("users", "start_top_artists")
    op.drop_column("users", "start_text_model")
    op.drop_column("users", "start_audio_model")
    op.drop_column("listens", "updated_at")