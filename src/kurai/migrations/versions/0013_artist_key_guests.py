"""Artist keys after the guest rule widened (kurai/artists.py): «Artist (feat. Guest)», «[ft. …]» and «feat.Guest»
were whole artists of their own, so the artist's pause, the limit per artist and the spacing missed them.

Filled here in Python, not SQL: the rule must be exactly the runtime one (0010's SQL used lower(), the code casefold()).
artist_ids is keyed by the artist's key, so its rows are moved too — two old keys can become one; the id already found
is kept, and the later lookup's time.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

from kurai.artists import artist_key

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

BATCH = 500


def upgrade() -> None:
    conn = op.get_bind()

    rows = conn.execute(sa.text("select id, artist, artist_key from tracks")).fetchall()
    changed = [{"k": key, "i": tid} for tid, artist, old in rows
               if (key := artist_key(artist)) != (old or "")]
    for start in range(0, len(changed), BATCH):
        conn.execute(sa.text("update tracks set artist_key = :k where id = :i"), changed[start:start + BATCH])

    for old, mbid, looked_up in conn.execute(
            sa.text("select artist_key, mbid, looked_up_at from artist_ids")).fetchall():
        new = artist_key(old)
        if new == old or not new:
            continue
        conn.execute(sa.text("delete from artist_ids where artist_key = :k"), {"k": old})
        conn.execute(sa.text(
            "insert into artist_ids (artist_key, mbid, looked_up_at) values (:k, :m, :t) "
            "on conflict (artist_key) do update set "
            "mbid = coalesce(nullif(artist_ids.mbid, ''), excluded.mbid), "
            "looked_up_at = greatest(artist_ids.looked_up_at, excluded.looked_up_at)"),
            {"k": new, "m": mbid, "t": looked_up})


def downgrade() -> None:
    """The old keys aren't recoverable (the guest text is gone from them); the sync refills from the rule in force."""
