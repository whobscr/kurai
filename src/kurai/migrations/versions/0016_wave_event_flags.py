"""WaveEvent: `chosen` (user-initiated play) and `disliked` (explicit 👎) so the wave can tell deliberate choices from
passive completions and contextual skips from permanent dislikes.

Revision ID: 0016
Revises: 0015
"""

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("wave_events", sa.Column("chosen", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("wave_events", sa.Column("disliked", sa.Boolean(), server_default="false", nullable=False))
    op.drop_index("ix_wave_events_user_id", table_name="wave_events")
    op.create_index("ix_wave_events_user_at", "wave_events", ["user_id", "at"])


def downgrade() -> None:
    op.drop_index("ix_wave_events_user_at", table_name="wave_events")
    op.create_index("ix_wave_events_user_id", "wave_events", ["user_id"])
    op.drop_column("wave_events", "chosen")
    op.drop_column("wave_events", "disliked")
