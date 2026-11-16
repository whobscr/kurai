"""The library search («Музыка → В библиотеке»): the artists, albums and tracks of what's in storage whose names hold
every word of the query, and the sets the player plays from it: an artist's whole discography, its most played tracks
first (ListenBrainz), an album in its own order, one track."""

import re
from dataclasses import dataclass, field

from sqlalchemy import func, nulls_last, select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai.db.models import Track
from kurai.recommend import popularity

KINDS = ("artist", "album", "track")
ARTISTS = 5
ALBUMS = 12
TRACKS = 30


@dataclass
class Album:
    id: str
    title: str
    artist: str
    year: int | None
    tracks: int
    cover: str  # one of its tracks: the cover is the track's


@dataclass
class Artist:
    name: str
    albums: list[Album] = field(default_factory=list)


@dataclass
class Found:
    artists: list[Artist] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.artists or self.albums or self.tracks)


def _matches(column, query: str) -> list:
    """Every word of the query somewhere in the column, case aside."""
    escaped = (re.sub(r"([\\%_])", r"\\\1", word) for word in query.split())
    return [column.ilike(f"%{word}%", escape="\\") for word in escaped]


def _popular_first(stmt):
    """The statement with the tracks' popularity at hand; its order: the track's listens, then its album's."""
    score, joins = popularity.track_score()
    for table, on in joins:
        stmt = stmt.outerjoin(table, on)
    return stmt.order_by(nulls_last(Track.listens.desc()), score.desc())


def _albums(where, limit: int):
    """The albums of the tracks matching `where`: one row per album, newest first; its artist the one most of its
    tracks have."""
    return (select(Track.album_id, func.min(Track.album), func.mode().within_group(Track.artist),
                   func.max(Track.year), func.count(), func.min(Track.id))
            .where(*where, Track.album_id != "").group_by(Track.album_id)
            .order_by(nulls_last(func.max(Track.year).desc()), func.min(Track.album)).limit(limit))


async def find(db: AsyncSession, query: str, kinds: set[str] | None = None) -> Found:
    """What the library has for the query; `kinds` (artist, album, track) narrows it, none: everything."""
    kinds = kinds or set(KINDS)
    found = Found()
    if not query.split():
        return found
    if "artist" in kinds:
        names = (await db.execute(select(Track.artist).where(*_matches(Track.artist, query))
                                  .group_by(Track.artist).order_by(func.length(Track.artist), Track.artist)
                                  .limit(ARTISTS))).scalars().all()
        for name in names:
            rows = (await db.execute(_albums([Track.artist == name], 100))).all()
            found.artists.append(Artist(name, [Album(*row) for row in rows]))
    if "album" in kinds:
        found.albums = [Album(*row) for row in (await db.execute(
            _albums(_matches(Track.album, query), ALBUMS))).all()]
    if "track" in kinds:
        found.tracks = list((await db.execute(
            _popular_first(select(Track).where(*_matches(Track.title, query))).order_by(Track.title)
            .limit(TRACKS))).scalars())
    return found


async def tracks_of(db: AsyncSession, which: str) -> tuple[str, list[str]]:
    """A set to play: «artist:<name>» (the discography, the most played first: the track's listens, then its album's),
    «album:<id>» (in its order) or «track:<id>». Its name and its tracks' ids; unknown: ("", [])."""
    kind, _sep, key = which.partition(":")
    if kind == "track":
        track = await db.get(Track, key)
        return (track.title, [track.id]) if track else ("", [])
    if kind == "album":
        rows = (await db.execute(select(Track.id, Track.album).where(Track.album_id == key)
                                 .order_by(nulls_last(Track.disc_number.asc()), nulls_last(Track.track_number.asc()),
                                           Track.title))).all()
        return (rows[0][1], [r[0] for r in rows]) if rows else ("", [])
    if kind == "artist":
        ids = list((await db.execute(
            _popular_first(select(Track.id).where(Track.artist == key))
            .order_by(nulls_last(Track.disc_number.asc()), nulls_last(Track.track_number.asc()), Track.title)
            )).scalars())
        return (key, ids) if ids else ("", [])
    return "", []
