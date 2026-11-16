"""Public listening data: ListenBrainz (what is popular, which artists are similar, an artist's most played albums)
and MusicBrainz (an artist's tags), each through its own route of the outbound gateway («public_data» and
«musicbrainz»: MusicBrainz is unreachable through some proxies and some providers, so it has its own switch).
MusicBrainz asks for at most one request a second and a User-Agent with a contact. A busy or rate-limited answer (429,
5xx) is tried again a few times, slower each time (as long as Retry-After says, when it says); so is a connection that
fails or a proxy that can't reach the site."""

import asyncio
import re
import time
from typing import Any

import aiohttp
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import __version__, outbound, settings
from kurai.i18n import _

LISTENBRAINZ = "https://api.listenbrainz.org/1"
LISTENBRAINZ_LABS = "https://labs.api.listenbrainz.org"
MUSICBRAINZ = "https://musicbrainz.org/ws/2"
# ListenBrainz's own similar-artists dataset (the one its artist pages use)
# the waits before the tries after a busy answer, in seconds
RETRY_WAITS = (2, 5, 15, 30)
BUSY = {429, 500, 502, 503, 504}
# a connection that didn't happen (refused, timed out, the proxy couldn't reach the site)
UNREACHABLE = (aiohttp.ClientError, asyncio.TimeoutError, ProxyError, ProxyConnectionError, ProxyTimeoutError)
# an artist every source knows (Radiohead), for the «Проверить» buttons
KNOWN_ARTIST = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
SIMILAR_ALGORITHM = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"


# ListenBrainz's popularity data needs a user's token (since 2026: «bad actors and AI scrapers»)
TOKEN_NEEDED = _("ListenBrainz требует токен: впишите его в «Системные настройки → Общие → Токен ListenBrainz» "
                 "(listenbrainz.org → Settings → User token)")
TOKEN_REFUSED = _("ListenBrainz не принял токен: проверьте «Токен ListenBrainz» на вкладке «Общие»")


class PublicDataError(Exception):
    pass


def _retry_after(value: str | None, default: float) -> float:
    """Retry-After in seconds (a date isn't worth parsing: the default then), at most a minute."""
    try:
        return min(max(float(value), default), 60.0) if value else default
    except ValueError:
        return default


async def client(db: AsyncSession) -> "PublicData":
    """The client with the settings of «Общие» (the token, MusicBrainz's contact) and the gateway's two routes."""
    values = await settings.get_all(db)
    return PublicData(outbound.route_from(values, "public_data"), values["listenbrainz.token"],
                      values["musicbrainz.contact"], outbound.route_from(values, "musicbrainz"))


class PublicData:
    def __init__(self, route: outbound.Route, listenbrainz_token: str = "", contact: str = "",
                 musicbrainz_route: outbound.Route | None = None):
        self.route = route
        self.musicbrainz_route = musicbrainz_route or route
        self.token = listenbrainz_token
        contact = contact or "https://github.com/whobscr/kurai"
        self.user_agent = f"Kurai/{__version__} ( {contact} )"
        self._last_mb = 0.0

    async def _get(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                   route: outbound.Route | None = None) -> Any:
        headers = {"User-Agent": self.user_agent, **(headers or {})}
        route = route or self.route
        for attempt in range(len(RETRY_WAITS) + 1):
            try:
                async with outbound.session_for_route(route, 60) as s, \
                        s.get(url, params=params, headers=headers) as resp:
                    if resp.status == 204:
                        return None
                    if resp.status in BUSY and attempt < len(RETRY_WAITS):
                        await asyncio.sleep(_retry_after(resp.headers.get("Retry-After"), RETRY_WAITS[attempt]))
                        continue
                    if resp.status == 401 and url.startswith(LISTENBRAINZ):
                        raise PublicDataError(TOKEN_NEEDED if not self.token else TOKEN_REFUSED)
                    if resp.status >= 400:
                        raise PublicDataError(f"{url}: HTTP {resp.status}")
                    return await resp.json(content_type=None)
            except UNREACHABLE as e:
                if attempt < len(RETRY_WAITS):
                    await asyncio.sleep(RETRY_WAITS[attempt])
                    continue
                via = _("через прокси") if route.proxy else _("напрямую")
                raise PublicDataError(f"{url} ({via}): {e or e.__class__.__name__}") from e

    def _lb_headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self.token}"} if self.token else {}

    async def top_artists(self, count: int = 500) -> list[dict]:
        """The most listened artists of all ListenBrainz users: [{"mbid", "name", "listens"}]."""
        data = await self._get(f"{LISTENBRAINZ}/stats/sitewide/artists", {"count": count, "range": "all_time"},
                               self._lb_headers())
        result = []
        for a in ((data or {}).get("payload") or {}).get("artists", []):
            mbid = a.get("artist_mbid") or (a.get("artist_mbids") or [None])[0]
            if mbid:
                result.append({"mbid": mbid, "name": a.get("artist_name", ""), "listens": a.get("listen_count", 0)})
        return result

    async def similar_artists(self, mbid: str, limit: int = 30) -> list[dict]:
        """[{"mbid", "name", "score"}] most similar first."""
        data = await self._get(f"{LISTENBRAINZ_LABS}/similar-artists/json",
                               {"artist_mbids": mbid, "algorithm": SIMILAR_ALGORITHM})
        rows = data if isinstance(data, list) else []
        # the labs API answers with result sets; the rows are the dicts that have an artist_mbid
        flat = [r for item in rows for r in (item.get("data", []) if isinstance(item, dict) and "data" in item
                                             else [item])]
        out = [{"mbid": r["artist_mbid"], "name": r.get("name", ""), "score": float(r.get("score", 0))}
               for r in flat if isinstance(r, dict) and r.get("artist_mbid") and r["artist_mbid"] != mbid]
        return sorted(out, key=lambda r: -r["score"])[:limit]

    async def top_release_groups(self, artist_mbid: str) -> list[dict]:
        """An artist's albums by how much they're played: [{"mbid", "title", "artist", "listens"}]."""
        data = await self._get(f"{LISTENBRAINZ}/popularity/top-release-groups-for-artist/{artist_mbid}",
                               headers=self._lb_headers())
        out = []
        for r in data or []:
            rg = r.get("release_group") or {}
            mbid = r.get("release_group_mbid") or rg.get("mbid") or rg.get("release_group_mbid")
            if mbid:
                out.append({"mbid": mbid, "title": rg.get("name") or r.get("release_group_name", ""),
                            "artist": r.get("artist_name") or rg.get("artist_credit_name", ""),
                            "type": rg.get("type") or "", "listens": r.get("total_listen_count", 0)})
        return out

    async def top_recordings(self, artist_mbid: str) -> list[dict]:
        """An artist's tracks by how much they're played: [{"mbid" (recording), "title", "listens"}]."""
        data = await self._get(f"{LISTENBRAINZ}/popularity/top-recordings-for-artist/{artist_mbid}",
                               headers=self._lb_headers())
        return [{"mbid": r["recording_mbid"], "title": r.get("recording_name") or "",
                 "listens": int(r.get("total_listen_count") or 0)}
                for r in data or [] if isinstance(r, dict) and r.get("recording_mbid")]

    async def _musicbrainz(self, path: str, params: dict[str, Any]) -> Any:
        """A MusicBrainz request, at most one a second (its rule), through its own route."""
        wait = 1.1 - (time.monotonic() - self._last_mb)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_mb = time.monotonic()
        return await self._get(f"{MUSICBRAINZ}/{path}", {**params, "fmt": "json"}, route=self.musicbrainz_route)

    async def track_albums(self, title: str, artist_mbid: str | None = None, limit: int = 8) -> list[dict]:
        """The studio albums a track is on (MusicBrainz's recordings and their releases), best match first:
        [{"mbid" (release group), "title", "artist", "year"}]. Compilations, live albums and singles are left out."""
        clean = re.sub(r'[\\"]', " ", title).strip()
        if not clean:
            return []
        query = f'recording:"{clean}"' + (f" AND arid:{artist_mbid}" if artist_mbid else "")
        data = await self._musicbrainz("recording", {"query": query, "limit": 25})
        out: dict[str, dict] = {}
        for rec in (data or {}).get("recordings", []):
            artist = "".join(c.get("name", "") + c.get("joinphrase", "") for c in rec.get("artist-credit", []))
            for release in rec.get("releases", []):
                group = release.get("release-group") or {}
                if (group.get("primary-type") != "Album" or group.get("secondary-types")
                        or not group.get("id") or group["id"] in out):
                    continue
                out[group["id"]] = {"mbid": group["id"], "title": group.get("title", release.get("title", "")),
                                    "artist": artist, "year": (release.get("date") or "")[:4]}
        return list(out.values())[:limit]

    async def discography(self, artist_mbid: str, limit: int = 100) -> list[dict]:
        """An artist's studio albums (MusicBrainz's release groups of type Album, no compilations, live albums or
        other secondary types), oldest first: [{"mbid", "title", "year"}]."""
        groups: list[dict] = []
        while len(groups) < limit:
            data = await self._musicbrainz("release-group", {"artist": artist_mbid, "type": "album", "limit": 100,
                                                             "offset": len(groups)})
            page = (data or {}).get("release-groups", [])
            groups += page
            if len(page) < 100:
                break
        albums = [{"mbid": g["id"], "title": g.get("title", ""), "year": (g.get("first-release-date") or "")[:4]}
                  for g in groups if g.get("primary-type") == "Album" and not g.get("secondary-types")]
        return sorted(albums, key=lambda a: a["year"] or "9999")[:limit]

    async def artist_tags(self, mbid: str) -> list[tuple[str, int]]:
        """MusicBrainz's genres and tags of an artist, most voted first (one request a second)."""
        data = await self._musicbrainz(f"artist/{mbid}", {"inc": "genres+tags"})
        tags: dict[str, int] = {}
        for t in (data or {}).get("genres", []) + (data or {}).get("tags", []):
            tags[t["name"].lower()] = max(tags.get(t["name"].lower(), 0), int(t.get("count", 0)))
        return sorted(tags.items(), key=lambda kv: -kv[1])
