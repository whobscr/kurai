"""«Не сейчас» on WaveEvent; repair 0014's onboarding reset (restore start_asked_at for users whose answers survive,
so their stored prior is used again — start.first_taste now persists it); re-look-up artist ids lost to 0013's merge
(the '' sentinel kept over a real mbid).

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wave_events", sa.Column("not_now", sa.Boolean(), server_default="false", nullable=False))
    op.execute("update users set start_asked_at = now() where start_asked_at is null "
               "and (coalesce(jsonb_array_length(start_styles), 0) > 0 or coalesce(start_text, '') <> '')")
    # '' means «looked up, not found»; deleting these lets the nightly artist_ids job ask again (heals 0013's loss)
    op.execute("delete from artist_ids where mbid = ''")


def downgrade() -> None:
    op.drop_column("wave_events", "not_now")
