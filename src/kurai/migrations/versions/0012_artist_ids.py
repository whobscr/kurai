"""Artists' MusicBrainz ids found by name, for the tracks whose tags have none (recommend/artist_ids.py).

Revision ID: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('artist_ids',
                    sa.Column('artist_key', sa.Text(), nullable=False),
                    sa.Column('mbid', sa.String(length=36), nullable=True),
                    sa.Column('looked_up_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                              nullable=False),
                    sa.PrimaryKeyConstraint('artist_key'))


def downgrade() -> None:
    op.drop_table('artist_ids')
