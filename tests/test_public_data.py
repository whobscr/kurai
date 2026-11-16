"""Public data against a fake MusicBrainz over HTTP: a busy answer is tried again (as long as Retry-After says), and a
lasting one fails only that request."""

import aiohttp
import pytest
from aiohttp import web

from kurai import outbound
from kurai.integrations import public_data
from kurai.integrations.public_data import PublicData, PublicDataError, _retry_after


async def fake_musicbrainz(busy_times: int) -> tuple[web.AppRunner, list[int], str]:
    answers: list[int] = []

    async def artist(request):
        if len(answers) < busy_times:
            answers.append(503)
            return web.Response(status=503, headers={"Retry-After": "1"})
        answers.append(200)
        return web.json_response({"genres": [{"name": "Rock", "count": 5}], "tags": [{"name": "rock", "count": 7},
                                                                                    {"name": "britpop", "count": 2}]})

    app = web.Application()
    app.router.add_get("/ws/2/artist/{mbid}", artist)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, answers, f"http://127.0.0.1:{port}/ws/2"


@pytest.fixture
def waits(monkeypatch):
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(public_data.asyncio, "sleep", sleep)
    return slept


@pytest.mark.parametrize("busy, ok", [(2, True), (len(public_data.RETRY_WAITS) + 1, False)])
async def test_busy_musicbrainz_is_tried_again(monkeypatch, waits, busy, ok):
    runner, answers, url = await fake_musicbrainz(busy)
    monkeypatch.setattr(public_data, "MUSICBRAINZ", url)
    try:
        pd = PublicData(outbound.Route(proxy=None, dns=None))
        if ok:
            assert await pd.artist_tags("a-rh") == [("rock", 7), ("britpop", 2)]
            assert answers == [503, 503, 200]
            assert waits[-2:] == [2, 5]  # Retry-After (1 s) is shorter than the backoff: the backoff wins
        else:
            with pytest.raises(PublicDataError, match="HTTP 503"):
                await pd.artist_tags("a-rh")
            assert len(answers) == len(public_data.RETRY_WAITS) + 1
    finally:
        await runner.cleanup()


def test_retry_after():
    assert _retry_after("20", 2) == 20 and _retry_after("600", 2) == 60
    assert _retry_after(None, 5) == 5 and _retry_after("Wed, 21 Oct 2026 07:28:00 GMT", 5) == 5


async def test_an_unreachable_musicbrainz_is_tried_again_through_its_own_route(monkeypatch, waits):
    """A connection that fails (refused, a proxy that can't reach the site, a forged certificate) is tried again like
    a busy answer; MusicBrainz goes its own route, ListenBrainz keeps the other."""
    runner, answers, url = await fake_musicbrainz(0)
    monkeypatch.setattr(public_data, "MUSICBRAINZ", url)
    used: list[outbound.Route] = []
    real = outbound.session_for_route

    class Refusing:
        async def __aenter__(self):
            raise aiohttp.ClientConnectionError("Connection refused by destination host")

        async def __aexit__(self, *exc):
            return False

    def flaky(route, timeout=60):  # the first two connections fail
        used.append(route)
        return Refusing() if len(used) <= 2 else real(route, timeout)

    monkeypatch.setattr(public_data.outbound, "session_for_route", flaky)
    try:
        listenbrainz, musicbrainz = outbound.Route("socks5h://proxy:1080", None), outbound.Route(None, None)
        pd = PublicData(listenbrainz, musicbrainz_route=musicbrainz)
        assert await pd.artist_tags("a-rh") == [("rock", 7), ("britpop", 2)]
        assert used == [musicbrainz] * 3 and answers == [200] and waits[-2:] == [2, 5]
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("token, says", [("", "требует токен"), ("wrong", "не принял токен")])
async def test_listenbrainz_without_a_good_token_says_what_to_do(monkeypatch, waits, token, says):
    """Its popularity data needs a user's token: a 401 becomes a message pointing at the setting."""
    async def popularity(request):
        return web.json_response({"code": 401, "error": "you need to provide an Auth token"}, status=401)

    app = web.Application()
    app.router.add_get("/1/popularity/top-release-groups-for-artist/{mbid}", popularity)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(public_data, "LISTENBRAINZ", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/1")
    try:
        with pytest.raises(PublicDataError, match=says):
            await PublicData(outbound.Route(None, None), listenbrainz_token=token).top_release_groups("a-rh")
    finally:
        await runner.cleanup()


async def test_the_albums_a_track_is_on(monkeypatch, waits):
    """MusicBrainz's recordings → their releases' groups: studio albums only, each once, best match first."""
    seen = {}

    async def recordings(request):
        seen["query"] = request.query["query"]
        group = lambda gid, title, kind="Album", extra=(): {"id": gid, "title": title, "primary-type": kind,  # noqa: E731
                                                           "secondary-types": list(extra)}
        return web.json_response({"recordings": [
            {"title": "Sugar", "artist-credit": [{"name": "Maroon 5"}], "releases": [
                {"title": "V", "date": "2014-09-02", "release-group": group("rg-v", "V")},
                {"title": "V (deluxe)", "date": "2014-09-02", "release-group": group("rg-v", "V")},
                {"title": "Singles", "release-group": group("rg-best", "Singles", extra=["Compilation"])},
                {"title": "Sugar", "release-group": group("rg-sugar", "Sugar", kind="Single")},
                {"title": "Live", "release-group": group("rg-live", "Live", extra=["Live"])}]},
        ]})

    app = web.Application()
    app.router.add_get("/ws/2/recording", recordings)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(public_data, "MUSICBRAINZ", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/ws/2")
    try:
        found = await PublicData(outbound.Route(None, None)).track_albums('Sugar "x"', "a-m5")
        assert found == [{"mbid": "rg-v", "title": "V", "artist": "Maroon 5", "year": "2014"}]
        assert seen["query"] == 'recording:"Sugar  x" AND arid:a-m5'  # quotes can't break the query
    finally:
        await runner.cleanup()


async def test_an_artists_discography_is_its_studio_albums(monkeypatch, waits):
    async def release_groups(request):
        assert request.query["artist"] == "a-m5" and request.query["type"] == "album"
        g = lambda gid, title, date, extra=(): {"id": gid, "title": title, "primary-type": "Album",  # noqa: E731
                                                "secondary-types": list(extra), "first-release-date": date}
        return web.json_response({"release-groups": [
            g("rg-v", "V", "2014-09-02"), g("rg-sajane", "Songs About Jane", "2002-06-25"),
            g("rg-live", "Live Friday the 13th", "2005-01-01", ["Live"]),
            g("rg-singles", "Singles", "2015-01-01", ["Compilation"])]})

    app = web.Application()
    app.router.add_get("/ws/2/release-group", release_groups)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(public_data, "MUSICBRAINZ", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/ws/2")
    try:
        found = await PublicData(outbound.Route(None, None)).discography("a-m5")
        assert [a["title"] for a in found] == ["Songs About Jane", "V"]  # studio albums, oldest first
    finally:
        await runner.cleanup()
