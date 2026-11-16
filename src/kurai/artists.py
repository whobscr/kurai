"""Who a track is by, where artists are kept apart (the wave's and the mixes' limits per artist, the artists' pause,
the spacing, «Любимое»): its first credited artist. Navidrome joins a track's artists into one line («Charli XCX •
Charlotte Aitchison • Finn Keane»), and a guest in a single tag («Artist feat. Guest») is common too: taken whole,
each guest made another artist of the same one, so one artist could fill a whole wave."""

import re

SEPARATOR = " • "  # Navidrome's join of a track's several artists
_GUESTS = re.compile(r"\s+(?:feat\.?|ft\.|featuring)\s.*$", re.IGNORECASE)


def main_artist(artist: str | None) -> str:
    """The first credited artist, as written."""
    return _GUESTS.sub("", (artist or "").split(SEPARATOR)[0]).strip()


def artist_key(artist: str | None) -> str:
    """The same, for comparing (stored as tracks.artist_key for the database's own filters)."""
    return main_artist(artist).casefold()
