"""For listeners who haven't played anything yet: what they said they like (the styles from the library, their own
words), asked once on «Для вас»; and what the world listens to most (ListenBrainz), by artist and album.

Revision ID: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('start_styles', postgresql.JSONB(astext_type=sa.Text()),
                                     server_default='[]', nullable=False))
    op.add_column('users', sa.Column('start_text', sa.Text(), server_default='', nullable=False))
    op.add_column('users', sa.Column('start_asked_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'popularity',
        sa.Column('mbid', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('listens', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('mbid'),
    )


def downgrade() -> None:
    op.drop_table('popularity')
    op.drop_column('users', 'start_asked_at')
    op.drop_column('users', 'start_text')
    op.drop_column('users', 'start_styles')
