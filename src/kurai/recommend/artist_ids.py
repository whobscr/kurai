"""MusicBrainz ids for the library's artists whose tags have none. ListenBrainz knows artists by these ids only, so
without them an artist gets no popularity, no «слушают вместе» and no new music found from them. Each artist is looked
up on MusicBrainz once by name and one of their titles (public_data.artist_id), PER_RUN artists a run, the ones with
the most tracks first; one not found is asked again after RETRY. The ids found go into the tracks without one, here
and after each library sync (Navidrome's own id, from the tags, always wins); the files aren't touched."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kurai.artists import main_artist
from kurai.db.models import ArtistId, Track
from kurai.integrations import public_data

PER_RUN = 100
RETRY = timedelta(days=30)
TITLES = 2  # the titles tried per artist before the search by name alone


async def _due(db: AsyncSession, now: datetime) -> list[tuple[str, str, list[str]]]:
    """(key, an artist line, titles) of the artists none of whose tracks has an id, not looked up yet or not found
    before RETRY."""
    rows = await db.execute(
        select(Track.artist_key, func.min(Track.artist), func.array_agg(func.distinct(Track.title)))
        .outerjoin(ArtistId, ArtistId.artist_key == Track.artist_key)
        .where(Track.artist_key != "",
               or_(ArtistId.artist_key.is_(None), and_(ArtistId.mbid.is_(None), ArtistId.looked_up_at < now - RETRY)))
        .group_by(Track.artist_key)
        .having(func.max(func.coalesce(Track.artist_mbid, "")) == "")
        .order_by(func.count().desc(), Track.artist_key).limit(PER_RUN))
    return [(key, line, sorted(titles)[:TITLES]) for key, line, titles in rows.all()]


async def apply(db: AsyncSession) -> int:
    """The ids found go into the tracks that have none; how many tracks got one. Commit is the caller's."""
    result = await db.execute(
        update(Track).where(Track.artist_key == ArtistId.artist_key, ArtistId.mbid.is_not(None),
                            func.coalesce(Track.artist_mbid, "") == "")
        .values(artist_mbid=ArtistId.mbid).execution_options(synchronize_session=False))
    return result.rowcount


async def look_up(db: AsyncSession) -> dict:
    """A run: the due artists asked about, each answer kept at once (MusicBrainz unreachable: the job fails, what was
    found stays and goes into the tracks)."""
    now = datetime.now(UTC)
    due = await _due(db, now)
    if not due:
        return {"looked_up": 0}
    client = await public_data.client(db)
    found = 0
    try:
        for key, line, titles in due:
            mbid = await client.artist_id(main_artist(line), titles)
            stmt = insert(ArtistId).values(artist_key=key, mbid=mbid, looked_up_at=now)
            await db.execute(stmt.on_conflict_do_update(index_elements=[ArtistId.artist_key],
                                                        set_={"mbid": mbid, "looked_up_at": now}))
            await db.commit()
            found += mbid is not None
    finally:
        tracks = await apply(db)
        await db.commit()
    return {"looked_up": len(due), "found": found, "tracks": tracks}
