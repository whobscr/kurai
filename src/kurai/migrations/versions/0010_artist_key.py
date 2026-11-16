"""Tracks get the key of their first credited artist (kurai/artists.py): artists are kept apart by it, not by the
whole line Navidrome joins a track's artists into. Filled here once (the same rule in SQL, as it stood at this
version); the library's sync keeps it after.

Revision ID: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tracks', sa.Column('artist_key', sa.Text(), server_default='', nullable=False))
    op.execute("UPDATE tracks SET artist_key = lower(trim(regexp_replace(split_part(artist, ' • ', 1), "
               "'\\s+(feat\\.?|ft\\.|featuring)\\s.*$', '', 'i')))")
    op.create_index('ix_tracks_artist_key', 'tracks', ['artist_key'])


def downgrade() -> None:
    op.drop_index('ix_tracks_artist_key', table_name='tracks')
    op.drop_column('tracks', 'artist_key')
