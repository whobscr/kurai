"""What the world listens to most, for the library's artists, albums and tracks: ListenBrainz's listens of each artist's
albums (release groups) and tracks (recordings), by the MusicBrainz ids in the tracks' tags; a track without its
recording's id is matched by its title. Refreshed a part at a time (PER_RUN artists a run,
each again after REFRESH), so a big library fills in over a few runs without hammering ListenBrainz."""

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import settings
from kurai.db.models import Popularity, Track
from kurai.i18n import _
from kurai.integrations import public_data

PER_RUN = 150
REFRESH = timedelta(days=30)


async def _due_artists(db: AsyncSession, now: datetime) -> list[str]:
    """The library's artists never asked about or asked about before REFRESH."""
    known = Popularity.__table__.alias("known")
    rows = await db.execute(
        select(Track.artist_mbid).distinct()
        .outerjoin(known, and_(known.c.mbid == Track.artist_mbid, known.c.kind == "artist"))
        .where(Track.artist_mbid.is_not(None), Track.artist_mbid != "",
               or_(known.c.mbid.is_(None), known.c.updated_at < now - REFRESH)))
    return list(rows.scalars())


async def _put(db: AsyncSession, mbid: str, kind: str, listens: int, now: datetime) -> None:
    stmt = insert(Popularity).values(mbid=mbid, kind=kind, listens=listens, updated_at=now)
    await db.execute(stmt.on_conflict_do_update(index_elements=[Popularity.mbid],
                                                set_={"kind": kind, "listens": listens, "updated_at": now}))


def title_key(title: str) -> str:
    """A title as matched: no case, no brackets («(Remastered 2011)», «[Live]»), letters and digits only."""
    return re.sub(r"\W+", " ", re.sub(r"[(\[].*?[)\]]", " ", title.casefold())).strip()


async def _put_tracks(db: AsyncSession, artist_mbid: str, recordings: list[dict]) -> int:
    """The artist's library tracks get their recording's listens (0: ListenBrainz doesn't list it). The tracks
    matched."""
    by_mbid = {r["mbid"]: r["listens"] for r in recordings}
    by_title: dict[str, int] = {}
    for r in recordings:  # several recordings of one title (live, remaster): the most played
        key = title_key(r["title"])
        by_title[key] = max(by_title.get(key, 0), r["listens"])
    tracks = (await db.execute(select(Track.id, Track.recording_mbid, Track.title)
                               .where(Track.artist_mbid == artist_mbid))).all()
    matched = 0
    for track_id, recording, title in tracks:
        listens = by_mbid.get(recording) if recording else None
        if listens is None:
            listens = by_title.get(title_key(title))
        matched += listens is not None
        await db.execute(update(Track).where(Track.id == track_id).values(listens=listens or 0))
    return matched


async def refresh(db: AsyncSession) -> dict:
    """The next PER_RUN due artists: their albums' and tracks' listens, and the artist's as the albums' sum (an artist
    ListenBrainz doesn't know is kept with 0, so it isn't asked again before REFRESH)."""
    if not await settings.get(db, "listenbrainz.token"):
        return {"skipped": _("нет токена ListenBrainz («Системные настройки → Общие»)")}
    now = datetime.now(UTC)
    due = await _due_artists(db, now)
    client = await public_data.client(db)
    albums = tracks = 0
    for mbid in due[:PER_RUN]:
        groups = await client.top_release_groups(mbid)  # an error (no token, no network) ends the run: it's retried
        for group in groups:
            await _put(db, group["mbid"], "release_group", int(group["listens"] or 0), now)
        tracks += await _put_tracks(db, mbid, await client.top_recordings(mbid))
        await _put(db, mbid, "artist", sum(int(g["listens"] or 0) for g in groups), now)
        await db.commit()
        albums += len(groups)
    return {"artists": min(len(due), PER_RUN), "albums": albums, "tracks": tracks, "left": max(0, len(due) - PER_RUN)}


def track_score():
    """A track's popularity in the world: its album's listens, or a tenth of its artist's when the album is unknown
    (an artist's listens are the sum of all their albums')."""
    album = Popularity.__table__.alias("album")
    artist = Popularity.__table__.alias("artist")
    score = func.coalesce(album.c.listens, artist.c.listens / 10, 0)
    joins = [(album, album.c.mbid == Track.release_group_mbid), (artist, artist.c.mbid == Track.artist_mbid)]
    return score, joins
