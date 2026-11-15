"""Navidrome: Kurai's own admin account, the users' accounts, the library, each user's plays and stars, and their
private playlists. Navidrome's native API (/auth, /api) for accounts and the library; the Subsonic API (/rest) for
playlists, written as the user (Navidrome makes the logged-in user a playlist's owner) and private (public=false)."""

import asyncio
import hashlib
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aiohttp

from kurai import outbound

PAGE = 500
CLIENT = "kurai"
# Navidrome's URL base (ND_BASEURL in docker-compose.yml): it lives under the path the «Приложения» tab shows it at, so
# every API of it is under this path too
BASE_PATH = "/admin/apps/navidrome"


class NavidromeError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # Navidrome's HTTP status; None when it didn't answer


# Navidrome limits logins (a few per 20 s, against password guessing) and answers more with «429 Too Many Requests»:
# a login is remembered for a while per account and reused, and one that hits the limit waits and tries again
TOKEN_SECONDS = 30 * 60
LOGIN_WAITS = (10, 20, 30)
_tokens: dict[tuple[str, str, str], tuple[str, float]] = {}  # (base URL, user, password's hash) -> (token, until)


@dataclass
class Account:
    username: str
    password: str


class Navidrome:
    def __init__(self, base_url: str):
        """`base_url`: Navidrome's address (KURAI_NAVIDROME_URL), without its URL base."""
        self.base_url = base_url.rstrip("/") + BASE_PATH

    # ---------- native API ----------

    async def _json(self, method: str, path: str, token: str | None = None, **kwargs) -> tuple[Any, dict]:
        headers = {"x-nd-authorization": f"Bearer {token}"} if token else {}
        try:
            async with outbound.local_session() as s, \
                    s.request(method, self.base_url + path, headers=headers, **kwargs) as resp:
                # the status first: some errors (429) come as plain text, not JSON
                if resp.status >= 400:
                    raise NavidromeError(f"Navidrome {path}: HTTP {resp.status}: {(await resp.text())[:200]}",
                                         resp.status)
                try:
                    return await resp.json(content_type=None), dict(resp.headers)
                except ValueError as e:
                    raise NavidromeError(f"Navidrome {path}: не JSON: {(await resp.text())[:200]}") from e
        except aiohttp.ClientError as e:
            raise NavidromeError(f"Navidrome не отвечает: {e}") from e

    async def login(self, account: Account) -> str:
        """A token of the account: a remembered one while it's fresh, else a login (waiting out Navidrome's limit)."""
        key = (self.base_url, account.username, hashlib.sha256(account.password.encode()).hexdigest())
        token, until = _tokens.get(key, ("", 0.0))
        if token and until > time.monotonic():
            return token
        for attempt in range(len(LOGIN_WAITS) + 1):
            try:
                data, _ = await self._json("POST", "/auth/login",
                                           json={"username": account.username, "password": account.password})
                break
            except NavidromeError as e:
                if e.status != 429 or attempt == len(LOGIN_WAITS):
                    raise
                await asyncio.sleep(LOGIN_WAITS[attempt])
        _tokens[key] = (data["token"], time.monotonic() + TOKEN_SECONDS)
        return data["token"]

    async def ensure_admin(self, account: Account) -> bool:
        """Kurai's own admin account: created on an empty Navidrome (its first account becomes admin). True when it
        was created now; an existing Navidrome must already have this account."""
        try:
            await self.login(account)
            return False
        except NavidromeError as e:
            if e.status != 401:  # only «wrong login» means the account is missing (not a limit or an outage)
                raise
        await self._json("POST", "/auth/createAdmin", json={"username": account.username,
                                                             "password": account.password})
        return True

    async def create_user(self, admin_token: str, username: str, name: str, password: str) -> str:
        data, _ = await self._json("POST", "/api/user", admin_token, json={
            "userName": username, "name": name or username, "password": password, "isAdmin": False})
        return data["id"]

    async def set_password(self, admin_token: str, user_id: str, username: str, name: str, password: str) -> None:
        await self._json("PUT", f"/api/user/{user_id}", admin_token, json={
            "id": user_id, "userName": username, "name": name or username, "password": password, "isAdmin": False})

    async def delete_user(self, admin_token: str, user_id: str) -> None:
        await self._json("DELETE", f"/api/user/{user_id}", admin_token)

    async def songs(self, token: str, sort: str = "title", order: str = "ASC",
                    limit: int | None = None) -> AsyncIterator[dict]:
        """The library's songs, with the token's user's play count, last play, star and rating."""
        start = 0
        while True:
            end = start + PAGE if limit is None else min(start + PAGE, limit)
            page, headers = await self._json("GET", f"/api/song?_start={start}&_end={end}&_sort={sort}&_order={order}",
                                             token)
            for song in page:
                yield song
            start = end
            total = int(headers.get("X-Total-Count", headers.get("x-total-count", 0)) or 0)
            if not page or start >= total or (limit is not None and start >= limit):
                return

    # ---------- Subsonic API (as the user) ----------

    async def _subsonic(self, account: Account, method: str, params: list[tuple[str, str]]) -> dict:
        salt = secrets.token_hex(6)
        auth = [("u", account.username), ("t", hashlib.md5((account.password + salt).encode()).hexdigest()),
                ("s", salt), ("v", "1.16.1"), ("c", CLIENT), ("f", "json")]
        try:
            async with outbound.local_session() as s, \
                    s.post(f"{self.base_url}/rest/{method}", data=auth + params) as resp:
                data = (await resp.json(content_type=None)).get("subsonic-response", {})
        except aiohttp.ClientError as e:
            raise NavidromeError(f"Navidrome не отвечает: {e}") from e
        if data.get("status") != "ok":
            raise NavidromeError(f"Navidrome {method}: {data.get('error', {}).get('message', data)}")
        return data

    async def save_playlist(self, account: Account, name: str, track_ids: list[str],
                            playlist_id: str | None = None, comment: str = "") -> str:
        """Creates the user's playlist, or replaces the songs of an existing one; private either way."""
        params = [("name", name)] + [("songId", t) for t in track_ids]
        if playlist_id:
            params.append(("playlistId", playlist_id))
        data = await self._subsonic(account, "createPlaylist", params)
        new_id = data.get("playlist", {}).get("id", playlist_id)
        await self._subsonic(account, "updatePlaylist", [("playlistId", new_id), ("public", "false"),
                                                           ("comment", comment)])
        return new_id

    async def now_playing(self, account: Account) -> list[dict]:
        """What every user is playing right now, by player: [{"id", "username", "playerId", "duration", …}] (the
        apps report it as they start a track)."""
        data = await self._subsonic(account, "getNowPlaying", [])
        return (data.get("nowPlaying") or {}).get("entry") or []

    async def download(self, account: Account, song_id: str, dest: str) -> None:
        """The song's original file (Subsonic «download»), streamed to `dest`."""
        salt = secrets.token_hex(6)
        auth = {"u": account.username, "t": hashlib.md5((account.password + salt).encode()).hexdigest(), "s": salt,
                "v": "1.16.1", "c": CLIENT, "f": "json", "id": song_id}
        try:
            async with outbound.local_session(600) as s, s.get(f"{self.base_url}/rest/download", params=auth) as resp:
                kind = resp.headers.get("Content-Type", "")
                if resp.status >= 400 or "json" in kind or "xml" in kind:
                    raise NavidromeError(f"Navidrome download {song_id}: HTTP {resp.status}: "
                                         f"{(await resp.text())[:200]}")
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        f.write(chunk)
        except aiohttp.ClientError as e:
            raise NavidromeError(f"Navidrome не отвечает: {e}") from e

    async def start_scan(self, account: Account) -> None:
        await self._subsonic(account, "startScan", [])


def song_fields(song: dict) -> dict[str, Any]:
    """The parts of a native-API song Kurai keeps (Track)."""
    genres = song.get("genres") or []
    names = [g.get("name", "") for g in genres if isinstance(g, dict)] or ([song["genre"]] if song.get("genre") else [])
    return {
        "id": song["id"],
        "title": song.get("title", ""),
        "artist": song.get("artist", ""),
        "album": song.get("album", ""),
        "album_id": song.get("albumId", ""),
        "artist_mbid": song.get("mbzArtistId") or None,
        "release_group_mbid": song.get("mbzReleaseGroupId") or None,
        "path": song.get("path", ""),
        "duration": int(song.get("duration") or 0),
        "year": song.get("year") or None,
        "genres": [n for n in names if n],
        "size": int(song.get("size") or 0),
    }
