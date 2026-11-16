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
from kurai.artists import artist_key
from kurai.i18n import _

PAGE = 500
CLIENT = "kurai"
# the client name of Kurai's web player: its «now playing» entries are left to the player itself (it reports the
# wave's events directly, so the watching of «now playing» mustn't count them again)
WEB_PLAYER = "kurai-web"
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


def _forget(token: str) -> None:
    for key, (known, _until) in list(_tokens.items()):
        if known == token:
            del _tokens[key]


def forget_account(username: str) -> None:
    """The remembered logins of an account that was deleted (and made anew under the same name)."""
    for key in [k for k in _tokens if k[1] == username]:
        del _tokens[key]


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
                    if resp.status == 401 and token:  # e.g. Navidrome's sessions were reset: log in again
                        _forget(token)
                    raise NavidromeError(f"Navidrome {path}: HTTP {resp.status}: {(await resp.text())[:200]}",
                                         resp.status)
                try:
                    return await resp.json(content_type=None), dict(resp.headers)
                except ValueError as e:
                    raise NavidromeError(_("Navidrome {path}: не JSON: {text}").format(
                        path=path, text=(await resp.text())[:200])) from e
        except aiohttp.ClientError as e:
            raise NavidromeError(_("Navidrome не отвечает: {e}").format(e=e)) from e

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
        """The account and all that was its (Navidrome deletes them with it): plays, stars, ratings, playlists, play
        queue; one already gone counts as deleted."""
        try:
            await self._json("DELETE", f"/api/user/{user_id}", admin_token)
        except NavidromeError as e:
            if e.status != 404:
                raise

    async def songs(self, token: str, sort: str = "title", order: str = "ASC",
                    limit: int | None = None) -> AsyncIterator[dict]:
        """The library's songs, with the token's user's play count, last play, star and rating: page by page until
        a short page (not by the total in a header, which may be missing)."""
        start = 0
        while limit is None or start < limit:
            end = start + PAGE if limit is None else min(start + PAGE, limit)
            page, _headers = await self._json(
                "GET", f"/api/song?_start={start}&_end={end}&_sort={sort}&_order={order}", token)
            if not isinstance(page, list):
                raise NavidromeError(_("Navidrome /api/song: неожиданный ответ: {page}").format(page=str(page)[:200]))
            for song in page:
                yield song
            if len(page) < end - start:
                return
            start = end

    # ---------- Subsonic API (as the user) ----------

    def signed(self, account: Account, method: str, params: list[tuple[str, str]],
               client: str = CLIENT) -> tuple[str, list[tuple[str, str]]]:
        """A Subsonic request as the account: its URL and parameters with the salted token (no password sent)."""
        salt = secrets.token_hex(6)
        auth = [("u", account.username), ("t", hashlib.md5((account.password + salt).encode()).hexdigest()),
                ("s", salt), ("v", "1.16.1"), ("c", client), ("f", "json")]
        return f"{self.base_url}/rest/{method}", auth + params

    async def _subsonic(self, account: Account, method: str, params: list[tuple[str, str]]) -> dict:
        url, data = self.signed(account, method, params)
        try:
            async with outbound.local_session() as s, s.post(url, data=data) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    raise NavidromeError(_("Navidrome {method}: HTTP {status}: {text}").format(
                        method=method, status=resp.status, text=(await resp.text())[:200]), resp.status) from None
        except aiohttp.ClientError as e:
            raise NavidromeError(_("Navidrome не отвечает: {e}").format(e=e)) from e
        data = body.get("subsonic-response", {}) if isinstance(body, dict) else {}
        if data.get("status") != "ok":
            raise NavidromeError(f"Navidrome {method}: {data.get('error', {}).get('message', body)}")
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

    async def delete_playlist(self, account: Account, playlist_id: str) -> None:
        """One of the user's playlists; one already gone counts as deleted."""
        try:
            await self._subsonic(account, "deletePlaylist", [("id", playlist_id)])
        except NavidromeError as e:
            if "not found" not in str(e).lower():
                raise

    async def now_playing(self, account: Account) -> list[dict]:
        """What every user is playing right now, by player: [{"id", "username", "playerId", "duration", …}] (the
        apps report it as they start a track)."""
        data = await self._subsonic(account, "getNowPlaying", [])
        return (data.get("nowPlaying") or {}).get("entry") or []

    async def scrobble(self, account: Account, song_id: str, at_ms: int) -> None:
        """A play of the song, counted in Navidrome (its play count, last play) as if a music app reported it."""
        await self._subsonic(account, "scrobble", [("id", song_id), ("time", str(at_ms)), ("submission", "true")])

    async def rate(self, account: Account, song_id: str, starred: bool, rating: int) -> None:
        """The user's star (♥ in most apps) and rating (0: none, 1–5) of a song."""
        await self._subsonic(account, "star" if starred else "unstar", [("id", song_id)])
        await self._subsonic(account, "setRating", [("id", song_id), ("rating", str(rating))])

    async def download(self, account: Account, song_id: str, dest: str) -> None:
        """The song's original file (Subsonic «download»), streamed to `dest`."""
        url, params = self.signed(account, "download", [("id", song_id)])
        try:
            async with outbound.local_session(600) as s, s.get(url, params=params) as resp:
                kind = resp.headers.get("Content-Type", "")
                if resp.status >= 400 or "json" in kind or "xml" in kind:
                    raise NavidromeError(f"Navidrome download {song_id}: HTTP {resp.status}: "
                                         f"{(await resp.text())[:200]}")
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        f.write(chunk)
        except aiohttp.ClientError as e:
            raise NavidromeError(_("Navidrome не отвечает: {e}").format(e=e)) from e


def song_fields(song: dict) -> dict[str, Any]:
    """The parts of a native-API song Kurai keeps (Track)."""
    genres = song.get("genres") or []
    names = [g.get("name", "") for g in genres if isinstance(g, dict)] or ([song["genre"]] if song.get("genre") else [])
    return {
        "id": song["id"],
        "title": song.get("title", ""),
        "artist": song.get("artist", ""),
        "artist_key": artist_key(song.get("artist", "")),
        "album": song.get("album", ""),
        "album_id": song.get("albumId", ""),
        "artist_mbid": song.get("mbzArtistId") or None,
        "release_group_mbid": song.get("mbzReleaseGroupId") or None,
        "recording_mbid": song.get("mbzRecordingId") or None,
        "disc_number": song.get("discNumber") or None,
        "track_number": song.get("trackNumber") or None,
        "path": song.get("path", ""),
        "duration": int(song.get("duration") or 0),
        "year": song.get("year") or None,
        "genres": [n for n in names if n],
        "size": int(song.get("size") or 0),
    }
