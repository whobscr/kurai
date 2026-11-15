"""audio analysis: track_audio, tastes.audio_vector

Revision ID: 0002
Revises: 0001
"""

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('track_audio',
    sa.Column('track_id', sa.String(length=64), nullable=False),
    sa.Column('size', sa.BigInteger(), nullable=False),
    sa.Column('effnet', pgvector.sqlalchemy.vector.VECTOR(dim=1280), nullable=True),
    sa.Column('effnet_model', sa.String(length=64), nullable=True),
    sa.Column('clap', pgvector.sqlalchemy.vector.VECTOR(dim=512), nullable=True),
    sa.Column('clap_model', sa.String(length=128), nullable=True),
    sa.Column('bpm', sa.Float(), nullable=True),
    sa.Column('key', sa.String(length=16), nullable=True),
    sa.Column('danceability', sa.Float(), nullable=True),
    sa.Column('voice', sa.Float(), nullable=True),
    sa.Column('moods', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('styles', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('themes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('seconds', sa.Float(), nullable=True),
    sa.Column('analyzed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['track_id'], ['tracks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('track_id')
    )
    # nearest-neighbour search by sound and by a description of it
    op.execute('CREATE INDEX ix_track_audio_effnet ON track_audio USING hnsw (effnet vector_cosine_ops)')
    op.execute('CREATE INDEX ix_track_audio_clap ON track_audio USING hnsw (clap vector_cosine_ops)')
    op.add_column('tastes', sa.Column('audio_vector', pgvector.sqlalchemy.vector.VECTOR(dim=1280), nullable=True))


def downgrade() -> None:
    op.drop_column('tastes', 'audio_vector')
    op.drop_table('track_audio')
