"""The library search: each track's place on its album (disc, number), its MusicBrainz recording and how much the world
listens to it (ListenBrainz). The artists' popularity is asked again at once, to bring the tracks' along.

Revision ID: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tracks', sa.Column('recording_mbid', sa.String(length=36), nullable=True))
    op.add_column('tracks', sa.Column('disc_number', sa.Integer(), nullable=True))
    op.add_column('tracks', sa.Column('track_number', sa.Integer(), nullable=True))
    op.add_column('tracks', sa.Column('listens', sa.BigInteger(), nullable=True))
    op.execute("UPDATE popularity SET updated_at = '1970-01-01' WHERE kind = 'artist'")


def downgrade() -> None:
    op.drop_column('tracks', 'listens')
    op.drop_column('tracks', 'track_number')
    op.drop_column('tracks', 'disc_number')
    op.drop_column('tracks', 'recording_mbid')
