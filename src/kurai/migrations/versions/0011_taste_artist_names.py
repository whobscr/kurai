"""The tastes' artists by their first credited artist (kurai/artists.py), as 1.8.2 began to count them: favourites
saved before it are whole lines Navidrome joins a track's artists into («Charli XCX • Charlotte Aitchison • Finn
Keane»), and one artist spelled two ways («Florence And The Machine», «Florence and the Machine») was two. The
favourites of one artist become one, their weights added up, strongest first; «слушают вместе» keeps each artist
once, under their own name. The rule is written here as it stood at this version; the night makes tastes after it.

Revision ID: 0011
"""

import json
import re

import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None

SEPARATOR = " • "
GUESTS = re.compile(r"\s+(?:feat\.?|ft\.|featuring)\s.*$", re.IGNORECASE)


def main_artist(artist: str | None) -> str:
    return GUESTS.sub("", (artist or "").split(SEPARATOR)[0]).strip()


def cleaned(top: list[dict], related: list[dict]) -> tuple[list[dict], list[dict]]:
    """A taste's favourites and «слушают вместе» with each artist once, by the first credited name."""
    merged: dict[str, dict] = {}
    for a in top or []:
        if name := main_artist(a.get("name")):
            entry = merged.setdefault(name.casefold(), {"name": name, "weight": 0.0})
            entry["weight"] = round(entry["weight"] + float(a.get("weight") or 0), 2)
    seen, related_out = set(), []
    for a in related or []:  # stored strongest first: the first of an artist stays
        name = main_artist(a.get("name"))
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            related_out.append({**a, "name": name})
    return sorted(merged.values(), key=lambda a: -a["weight"]), related_out


def upgrade() -> None:
    conn = op.get_bind()
    for user_id, top, related in conn.execute(sa.text("SELECT user_id, top_artists, related_artists FROM tastes")):
        new_top, new_related = cleaned(top, related)
        if (new_top, new_related) != (top, related):
            conn.execute(sa.text("UPDATE tastes SET top_artists = CAST(:top AS jsonb), "
                                 "related_artists = CAST(:related AS jsonb) WHERE user_id = :user_id"),
                         {"top": json.dumps(new_top, ensure_ascii=False),
                          "related": json.dumps(new_related, ensure_ascii=False), "user_id": user_id})


def downgrade() -> None:
    pass  # nothing to undo: the whole lines aren't wanted back
