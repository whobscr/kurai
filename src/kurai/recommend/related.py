"""«Слушают вместе»: the artists the world's listeners play together with a user's favourites. ListenBrainz's similar
artists are counted from listening sessions (who is played in the same sessions as whom), not from the sound, so they
lead to the neighbouring genres people actually combine: from rock to the alternative and electronic its listeners
also play. Depth 2 follows the neighbours' neighbours too: wider, further from the habitual.

`neighbours` is the walk itself (the plugins use it too); `refresh` keeps each user's list of such artists that the
library has, for the pulse and the playlists (mixing.Param «related_share»)."""

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import settings
from kurai.artists import artist_key, main_artist
from kurai.db.models import Taste, Track, User
from kurai.integrations import public_data
from kurai.integrations.public_data import PublicData, PublicDataError
from kurai.recommend import mixing

log = logging.getLogger(__name__)

SEEDS = 10  # the user's favourite artists asked about
PER_ARTIST = 30  # neighbours taken of each
SECOND_STEP = 10  # the strongest neighbours whose own neighbours are asked about at depth 2
SECOND_STEP_WEIGHT = 0.5  # what a neighbour's neighbour counts, against a direct one
KEEP = 60  # artists kept per user


async def neighbours(client: PublicData, seeds: dict[str, tuple[str, float]], depth: int = 1,
                     skip: set[str] | None = None) -> dict[str, dict]:
    """The artists listened together with the seeds ({mbid: (name, weight)}), seeds and `skip` left out:
    {mbid: {"name", "score", "via" (the seed or neighbour it came from most), "depth"}}. Each seed's neighbours'
    scores are scaled to its strongest (1) and by the seed's weight; an artist reached from several seeds adds up."""
    skip = (skip or set()) | set(seeds)
    found: dict[str, dict] = {}
    step = [(mbid, name, weight) for mbid, (name, weight) in seeds.items()]
    for level in range(1, depth + 1):
        reached: dict[str, float] = {}
        for mbid, name, weight in step:
            similar = await client.similar_artists(mbid, PER_ARTIST)
            top = max((s["score"] for s in similar), default=0) or 1
            for s in similar:
                if s["mbid"] in skip:
                    continue
                score = s["score"] / top * weight
                entry = found.setdefault(s["mbid"], {"name": s["name"], "score": 0.0, "via": name, "depth": level,
                                                     "_best": 0.0})
                entry["score"] += score
                if score > entry["_best"] and entry["depth"] == level:
                    entry["_best"], entry["via"] = score, name
                reached[s["mbid"]] = reached.get(s["mbid"], 0) + score
        strongest = sorted(reached.items(), key=lambda kv: -kv[1])[:SECOND_STEP]
        step = [(m, found[m]["name"], score * SECOND_STEP_WEIGHT) for m, score in strongest]
    for entry in found.values():
        entry.pop("_best")
        entry["score"] = round(entry["score"], 4)
    return found


async def seeds_of(db: AsyncSession, taste: Taste, count: int = SEEDS) -> dict[str, tuple[str, float]]:
    """A taste's favourite artists that have a MusicBrainz id in the library: {mbid: (name, weight)}."""
    out: dict[str, tuple[str, float]] = {}
    for a in (taste.top_artists or [])[:count]:
        mbid = (await db.execute(select(Track.artist_mbid).where(Track.artist_key == artist_key(a["name"]),
                                                                  Track.artist_mbid.is_not(None),
                                                                  Track.artist_mbid != "").limit(1))
                ).scalar_one_or_none()
        if mbid:
            out[mbid] = (a["name"], float(a.get("weight") or 1))
    return out


async def refresh(db: AsyncSession, user: User) -> dict:
    """The user's «слушают вместе» among the library's artists (their own favourites left out), as deep as the
    deepest profile asks. ListenBrainz unreachable: the last list stays."""
    taste = await db.get(Taste, user.id)
    if taste is None or not taste.top_artists:
        return {"skipped": "вкус ещё не известен"}
    values = await settings.get_all(db)
    depth = max(int(values[mixing.BY_KEY["related_depth"].setting(p)]) for p in mixing.PROFILES)
    if not any(float(values[mixing.BY_KEY["related_share"].setting(p)]) for p in mixing.PROFILES):
        return {"skipped": "«Доля «слушают вместе»» — 0"}
    seeds = await seeds_of(db, taste)
    if not seeds:
        return {"skipped": "у любимых исполнителей нет идентификаторов MusicBrainz"}
    favourites = {artist_key(a["name"]) for a in taste.top_artists}
    try:
        found = await neighbours(await public_data.client(db), seeds, depth)
    except PublicDataError as e:
        log.warning("«слушают вместе» for %s not refreshed: %s", user.username, e)
        return {"error": str(e)}
    library = dict((await db.execute(select(Track.artist_mbid, func.min(Track.artist))
                                     .where(Track.artist_mbid.in_(list(found))).group_by(Track.artist_mbid))).all())
    related = [{"name": main_artist(library[m]), "mbid": m, "score": e["score"], "via": e["via"], "depth": e["depth"]}
               for m, e in sorted(found.items(), key=lambda kv: -kv[1]["score"])
               if m in library and artist_key(library[m]) not in favourites][:KEEP]
    taste.related_artists, taste.related_at = related, datetime.now(UTC)
    await db.commit()
    return {"related": len(related), "asked": len(seeds)}


def artists_for(taste: Taste | None, depth: int) -> dict[str, dict]:
    """The user's related artists down to `depth`, by their key (kurai/artists.py)."""
    if taste is None:
        return {}
    return {artist_key(a["name"]): a for a in taste.related_artists or [] if a.get("depth", 1) <= depth}
