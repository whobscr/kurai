"""Who a track is by, where artists are kept apart (the pulse's and the mixes' limits per artist, the artists' pause,
the spacing, «Любимое»): its first credited artist. Navidrome joins a track's artists into one line («Charli XCX •
Charlotte Aitchison • Finn Keane»), and a guest in a single tag («Artist feat. Guest», «Artist (feat. Guest)») is common
too: taken whole, each guest made another artist of the same one, so one artist could fill a whole pulse.

The same rule lives in web/static/js/player.js (the player's own spacing) and was written in SQL by migration 0010;
migration 0013 refills the keys after this rule changed. Change all three together."""

import re

SEPARATOR = " • "  # Navidrome's join of a track's several artists
# A guest credit, from its separator to the end of the line. Before the marker: whitespace, or an opening bracket
# («Artist (feat. Guest)», «Artist [ft. Guest]»). After it: a dot («feat.Guest»), or whitespace/end — never a letter,
# so «The Features» stays whole. «with» and «vs» are deliberately not here: «Dance with the Dead» is one artist.
_GUESTS = re.compile(r"(?:\s+|\s*[\(\[]\s*)(?:feat\.|ft\.|(?:feat|ft|featuring)(?=\s|$))\s*.*$", re.IGNORECASE)


def main_artist(artist: str | None) -> str:
    """The first credited artist, as written."""
    return _GUESTS.sub("", (artist or "").split(SEPARATOR)[0]).strip()


def artist_key(artist: str | None) -> str:
    """The same, for comparing (stored as tracks.artist_key for the database's own filters)."""
    return main_artist(artist).casefold()


def top_artists(weights: list[tuple[str | None, float]], count: int = 20) -> list[dict]:
    """The strongest artists of (a track's artist line, weight) pairs: [{"name", "weight"}], each artist once, by its
    key (one spelled two ways is one), under the name it came first with."""
    totals: dict[str, list] = {}
    for artist, weight in weights:
        if key := artist_key(artist):
            totals.setdefault(key, [main_artist(artist), 0.0])[1] += weight
    ranked = sorted(totals.values(), key=lambda nw: -nw[1])[:count]
    return [{"name": name, "weight": round(weight, 2)} for name, weight in ranked]
