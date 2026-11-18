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


def downgrade() -> None:
    op.drop_column("wave_events", "chosen")
    op.drop_column("wave_events", "disliked")