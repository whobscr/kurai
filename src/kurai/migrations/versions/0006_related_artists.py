"""«Слушают вместе»: for each user, the library's artists that the world's listeners play with the user's favourites
(ListenBrainz's similar artists, by who listens to them), found at night; the wave and the playlists mix them in.

Revision ID: 0006
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tastes', sa.Column('related_artists', postgresql.JSONB(astext_type=sa.Text()),
                                      server_default='[]', nullable=False))
    op.add_column('tastes', sa.Column('related_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('tastes', 'related_at')
    op.drop_column('tastes', 'related_artists')
