"""«Пауза для исполнителя» leaves a resting artist out (while others can fill the mix) instead of lowering them: its
«Сила паузы исполнителя» is gone, and its saved values with it.

Revision ID: 0008
"""

from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM settings WHERE key IN "
               "('mix.wave.artist_rest_penalty', 'mix.playlists.artist_rest_penalty')")


def downgrade() -> None:
    pass  # the defaults come back by themselves
