"""initial schema

Revision ID: 0001
Revises: (none)
"""

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # vectors (pgvector); a no-op where the server's admin already made it (Kurai's user may not create it)
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.create_table('llm_calls',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task', sa.String(length=48), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('thinking', sa.Boolean(), nullable=False),
    sa.Column('seconds', sa.Float(), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('settings',
    sa.Column('key', sa.String(length=128), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('tracks',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('artist', sa.Text(), nullable=False),
    sa.Column('album', sa.Text(), nullable=False),
    sa.Column('album_id', sa.String(length=64), nullable=False),
    sa.Column('artist_mbid', sa.String(length=36), nullable=True),
    sa.Column('release_group_mbid', sa.String(length=36), nullable=True),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('duration', sa.Integer(), nullable=False),
    sa.Column('year', sa.Integer(), nullable=True),
    sa.Column('genres', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('size', sa.BigInteger(), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(), nullable=True),
    sa.Column('embedding_model', sa.String(length=128), nullable=True),
    sa.Column('seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('display_name', sa.String(length=128), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('must_change_password', sa.Boolean(), nullable=False),
    sa.Column('totp_secret', sa.Text(), nullable=True),
    sa.Column('totp_enabled', sa.Boolean(), nullable=False),
    sa.Column('recovery_codes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('accent_color', sa.String(length=7), nullable=True),
    sa.Column('theme_mode', sa.String(length=8), nullable=False),
    sa.Column('navidrome_id', sa.String(length=64), nullable=True),
    sa.Column('navidrome_password', sa.Text(), nullable=True),
    sa.Column('telegram_chat_id', sa.BigInteger(), nullable=True),
    sa.Column('telegram_link_code', sa.String(length=32), nullable=True),
    sa.Column('notify_push', sa.Boolean(), nullable=False),
    sa.Column('notify_telegram', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('telegram_chat_id'),
    sa.UniqueConstraint('telegram_link_code'),
    sa.UniqueConstraint('username')
    )
    op.create_table('jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=48), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('night_only', sa.Boolean(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('dedupe_key', sa.String(length=128), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('listens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('track_id', sa.String(length=64), nullable=False),
    sa.Column('play_count', sa.Integer(), nullable=False),
    sa.Column('last_played', sa.DateTime(timezone=True), nullable=True),
    sa.Column('starred', sa.Boolean(), nullable=False),
    sa.Column('rating', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['track_id'], ['tracks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'track_id')
    )
    op.create_table('notifications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('link', sa.Text(), nullable=False),
    sa.Column('read', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('playlists',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('track_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('navidrome_id', sa.String(length=64), nullable=True),
    sa.Column('feedback', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('push_subscriptions',
    sa.Column('endpoint', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('keys', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('endpoint')
    )
    op.create_table('sessions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('pending_totp', sa.Boolean(), nullable=False),
    sa.Column('csrf_token', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('tastes',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('vector', pgvector.sqlalchemy.vector.VECTOR(), nullable=True),
    sa.Column('embedding_model', sa.String(length=128), nullable=True),
    sa.Column('top_artists', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('tracks_used', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_index('ix_jobs_queue', 'jobs', ['status', 'priority', 'id'])
    op.create_index('ix_jobs_dedupe', 'jobs', ['dedupe_key', 'status'])
    op.create_index('ix_listens_user', 'listens', ['user_id'])
    op.create_index('ix_tracks_artist', 'tracks', ['artist'])
    op.create_index('ix_notifications_user', 'notifications', ['user_id', 'created_at'])


def downgrade() -> None:
    op.drop_table('tastes')
    op.drop_table('sessions')
    op.drop_table('push_subscriptions')
    op.drop_table('playlists')
    op.drop_table('notifications')
    op.drop_table('listens')
    op.drop_table('jobs')
    op.drop_table('users')
    op.drop_table('tracks')
    op.drop_table('settings')
    op.drop_table('llm_calls')
