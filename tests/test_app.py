"""The app against the database: settings, the queue, «Спросить» end to end with a fake model, and the web flows."""

import re

import httpx
import numpy as np
import pyotp
import pytest
from sqlalchemy import func, select

from kurai import settings
from kurai.db.models import Job, Playlist, Setting, Track, User

pytestmark = pytest.mark.usefixtures("database")


# ---------- settings ----------


async def test_secret_settings_are_stored_encrypted(db):
    await settings.put(db, {"llm.api_key": "sk-secret", "night.start": "07:00"})
    await db.commit()
    assert await settings.get(db, "llm.api_key") == "sk-secret"
    row = await db.get(Setting, "llm.api_key")
    assert "sk-secret" not in row.value
    assert (await settings.get_all(db))["night.start"] == "07:00"
    with pytest.raises(KeyError):
        await settings.put(db, {"no.such.key": 1})


# ---------- the queue ----------


async def test_queue_dedupes_and_keeps_night_jobs_for_the_night(db):
    from kurai.jobs import queue

    a = await queue.enqueue(db, "sync_library", dedupe_key="sync_library")
    b = await queue.enqueue(db, "sync_library", dedupe_key="sync_library")
    assert a.id == b.id
    await queue.enqueue(db, "nightly", night_only=True, priority=1)
    first = await queue.claim(db, "general", in_night=False)
    assert first.kind == "sync_library"  # the night job waits outside the window
    night = await queue.claim(db, "general", in_night=True)
    assert night.kind == "nightly"
    await queue.enqueue(db, "ask", {"text": "x"}, priority=10)
    await queue.enqueue(db, "analyze_audio", {"limit": 5}, priority=5)
    assert (await queue.claim(db, "general", in_night=True)) is None  # «ask» and the analysis are other lanes'
    assert (await queue.claim(db, "interactive", in_night=False)).kind == "ask"
    audio = await queue.claim(db, "audio", in_night=False)
    assert audio.kind == "analyze_audio"
    await queue.enqueue(db, "some_plugins_job", priority=1)
    # a switched-off plugin's job waits: the worker claims only the kinds it knows
    assert (await queue.claim(db, "general", in_night=True, kinds={"sync_library", "nightly"})) is None
    assert (await queue.claim(db, "general", in_night=True)).kind == "some_plugins_job"
    await queue.requeue_interrupted(db, ("general", "interactive"))  # the main worker restarting
    await db.refresh(audio)
    assert audio.status == "running"  # the audio container's job is not its to requeue


# ---------- «Спросить» end to end ----------


async def test_ask_plans_with_the_model_and_picks_tracks_by_vectors(db, navidrome, monkeypatch):
    from kurai import llm
    from kurai.recommend import playlists

    model_name = await settings.get(db, "embed.model")
    rng = np.random.default_rng(1)
    calm, loud = rng.normal(size=8), rng.normal(size=8)
    for i in range(30):
        base = calm if i < 15 else loud
        db.add(Track(id=f"t{i}", title=f"Song {i}", artist=f"Artist {i % 6}", album="A", duration=240,
                     embedding=(base + 0.05 * rng.normal(size=8)).tolist(), embedding_model=model_name))
    user = User(username="ann", display_name="Ann", password_hash="x", navidrome_id="nd-ann",
                navidrome_password=__import__("kurai").security.encrypt("pw"))
    db.add(user)
    await db.commit()

    async def fake_complete_json(db_, task, system, prompt, schema, max_tokens=2000):
        assert task == "ask"
        return {"intent": "playlist", "answer": "", "mood_text": "calm", "sound_en": "calm", "seed_artists": [],
                "exclude_artists": [], "minutes": 20, "name": "Тихий вечер"}

    class FakeEmbedder:
        name = model_name

        async def embed(self, texts):
            return [calm.tolist()]

    async def fake_embed_model(db_):
        return FakeEmbedder()

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    monkeypatch.setattr(llm, "embed_model", fake_embed_model)
    result = await playlists.ask(db, user, "что-нибудь спокойное")
    assert result["name"] == "Тихий вечер" and 4 <= result["tracks"] <= 6  # 20 minutes of 4-minute tracks
    playlist = await db.get(Playlist, result["playlist_id"])
    assert all(int(t[1:]) < 15 for t in playlist.track_ids)  # all from the calm cluster
    name, written = next(iter(navidrome.playlists.values()))
    assert name == "Тихий вечер" and written == playlist.track_ids


# ---------- the web app ----------


@pytest.fixture
async def client(navidrome):
    from kurai.bootstrap import bootstrap
    from kurai.web.app import app

    await bootstrap()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://kurai.test", follow_redirects=False) as c:
        yield c


def csrf_of(html: str) -> str:
    return re.search(r'data-csrf="([^"]+)"', html).group(1)


async def login(client, username, password):
    r = await client.post("/login", data={"username": username, "password": password})
    return r


async def test_first_admin_must_change_password_then_reaches_admin(client, navidrome, db):
    assert (await client.get("/")).headers["location"] == "/login"
    assert (await login(client, "admin", "wrong")).status_code == 401
    r = await login(client, "admin", "admin")
    assert r.status_code == 303
    assert (await client.get("/")).headers["location"] == "/password"
    page = await client.get("/password")
    token = csrf_of(page.text)
    r = await client.post("/password", data={"csrf": token, "current": "admin", "new": "short", "repeat": "short"})
    assert r.status_code == 400
    r = await client.post("/password", data={"csrf": token, "current": "admin", "new": "a-long-password",
                                             "repeat": "a-long-password"})
    assert r.status_code == 303
    assert navidrome.users["admin"] == "a-long-password"  # the music account follows
    assert (await client.get("/")).status_code == 200
    general = await client.get("/admin/general")
    assert general.status_code == 200 and "Ночное окно" in general.text


async def test_csrf_settings_validation_and_users(client, navidrome):
    await login(client, "admin", "a-long-password")
    page = await client.get("/admin/general")
    token = csrf_of(page.text)
    assert (await client.post("/admin/general", data={"night.start": "05:00"})).status_code == 403  # no token
    r = await client.post("/admin/general", data={"csrf": token, "night.start": "25:99", "night.end": "10:00",
                                                  "ui.default_accent": "#123456", "playlists.per_night": "3"})
    assert "message=" in r.headers["location"]
    r = await client.post("/admin/gateway", data={"csrf": token, "outbound.proxy": "socks5h://10.0.0.2:1080",
                                                  "outbound.dns": "not-an-ip"})
    assert "DNS" in httpx.URL(r.headers["location"]).params["message"]
    r = await client.post("/admin/users/create", data={"csrf": token, "username": "Bob", "display_name": "Боб",
                                                       "password": "bob-password", "role": "user"})
    assert "добавлен" in httpx.URL(r.headers["location"]).params["message"]
    assert navidrome.users["bob"] == "bob-password"


async def test_user_cannot_open_admin_and_totp_flow(client, db):
    await login(client, "bob", "bob-password")
    assert (await client.get("/admin/general")).status_code == 403
    me = await client.get("/me")
    token = csrf_of(me.text)
    setup = await client.post("/me/totp/start", data={"csrf": token})
    secret = re.search(r"<code>([A-Z2-7]{16,})</code>", setup.text).group(1)
    done = await client.post("/me/totp/confirm", data={"csrf": token, "code": pyotp.TOTP(secret).now()})
    codes = re.search(r'<pre class="codes">(.*?)</pre>', done.text, re.S).group(1).split()
    assert len(codes) == 8
    await client.post("/logout", data={"csrf": token})
    client.cookies.clear()
    r = await login(client, "bob", "bob-password")
    assert r.headers["location"] == "/login"  # the code is still needed
    assert (await client.get("/")).headers["location"] == "/login"
    assert (await client.post("/login/totp", data={"code": "123456"})).status_code == 401
    assert (await client.post("/login/totp", data={"code": codes[0]})).status_code == 303  # a recovery code works
    assert (await client.get("/")).status_code == 200
    user = (await db.execute(select(User).where(User.username == "bob"))).scalar_one()
    await db.refresh(user)
    assert len(user.recovery_codes) == 7


async def test_pages_docs_errors_and_pwa(client):
    await login(client, "admin", "a-long-password")
    for path in ("/", "/ask", "/me", "/docs", "/docs/admin/audio", "/docs/user/wave", "/docs/changelog",
                 "/admin/models", "/admin/gateway", "/admin/users", "/admin/apps", "/admin/plugins", "/admin/jobs"):
        r = await client.get(path)
        assert r.status_code == 200, (path, r.status_code)
    assert "Плагины не установлены" in (await client.get("/admin/plugins")).text
    missing = await client.get("/no/such/page")
    assert missing.status_code == 404 and "<svg" in missing.text and "Такой страницы нет" in missing.text
    manifest = (await client.get("/manifest.webmanifest")).json()
    assert manifest["share_target"]["action"] == "/share"
    assert (await client.get("/share", params={"text": "Radiohead OK Computer"})).headers["location"].startswith(
        "/ask?text=Radiohead")  # no plugin offers a page for it
    assert "/music" not in (await client.get("/")).text  # no plugin, no «Музыка»
    assert (await client.get("/login")).headers["location"] == "/"  # logged in already
    client.cookies.clear()
    page = (await client.get("/login")).text
    assert page.count("<svg") >= 2  # two scene halves


async def test_ask_from_the_web_queues_a_job(client, db):
    await login(client, "admin", "a-long-password")
    token = csrf_of((await client.get("/ask")).text)
    assert (await client.post("/ask", data={"csrf": token, "text": "джаз на вечер"})).status_code == 303
    job = (await db.execute(select(Job).where(Job.kind == "ask").order_by(Job.id.desc()))).scalars().first()
    assert job.payload == {"text": "джаз на вечер"}
    partial = await client.get("/ask/jobs")
    assert "джаз на вечер" in partial.text and 'hx-trigger="every 5s"' in partial.text


async def test_audio_tab_saves_checks_and_starts_a_run(client, db):
    await login(client, "admin", "a-long-password")
    page = await client.get("/admin/audio")
    assert page.status_code == 200 and "Ход анализа" in page.text
    token = csrf_of(page.text)
    r = await client.post("/admin/audio", data={"csrf": token, "audio.mix_weight": "1.5", "audio.ask_weight": "0.5"})
    assert "Доля звука" in httpx.URL(r.headers["location"]).params["message"]
    r = await client.post("/admin/audio", data={"csrf": token, "audio.enabled": "on", "audio.clap": "on",
                                                "audio.url": "http://127.0.0.1:9", "audio.mix_weight": "0.4",
                                                "audio.ask_weight": "0.6"})
    assert httpx.URL(r.headers["location"]).params["message"] == "Сохранено"
    assert await settings.get(db, "audio.mix_weight") == 0.4
    check = (await client.post("/admin/check/audio", headers={"X-CSRF-Token": token})).json()
    assert check["ok"] is False  # nothing listens there
    await client.post("/admin/audio/run", data={"csrf": token})
    job = (await db.execute(select(Job).where(Job.dedupe_key == "manual:analyze_audio"))).scalars().one()
    assert job.payload == {"limit": 100, "manual": True}
    await settings.put(db, {"audio.enabled": False, "audio.url": "http://audio:8097"})
    await db.execute(Job.__table__.delete().where(Job.kind == "analyze_audio"))
    await db.commit()


# ---------- Navidrome over HTTP ----------


async def test_navidrome_is_reached_under_its_url_base_and_kurais_admin_made(db, monkeypatch):
    """A real HTTP server that answers only under Navidrome's URL base, as Navidrome with ND_BASEURL does: Kurai's
    admin account is made when missing, then used."""
    from aiohttp import web

    from kurai.integrations.navidrome import BASE_PATH, Navidrome
    from kurai.recommend import library

    admins: dict[str, str] = {}

    async def login(request):
        body = await request.json()
        if admins.get(body["username"]) != body["password"]:
            return web.json_response({"error": "Invalid username or password"}, status=401)
        return web.json_response({"token": "tok-" + body["username"]})

    async def create_admin(request):
        body = await request.json()
        if admins:
            return web.json_response({"error": "already has an admin"}, status=403)
        admins[body["username"]] = body["password"]
        return web.json_response({"id": "1"})

    app = web.Application()
    app.router.add_post(f"{BASE_PATH}/auth/login", login)
    app.router.add_post(f"{BASE_PATH}/auth/createAdmin", create_admin)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        monkeypatch.setattr(library, "navidrome", lambda: Navidrome(f"http://127.0.0.1:{port}"))
        nd, token = await library.admin_login(db)
        assert token == f"tok-{await settings.get(db, 'navidrome.admin_user')}"
        assert list(admins) == [await settings.get(db, "navidrome.admin_user")]
        await library.admin_login(db)  # the second time it only logs in
        assert len(admins) == 1
    finally:
        await runner.cleanup()


# ---------- the apps' pages through Kurai ----------


async def test_an_app_is_asked_only_for_encodings_the_proxy_unpacks(client, monkeypatch):
    """Browsers accept brotli, and some apps use it when asked: the proxy can't unpack it, so it never asks."""
    import gzip

    from aiohttp import web

    from kurai import plugins
    from kurai.web.routes import apps

    async def page(request):
        if "br" in request.headers.get("Accept-Encoding", ""):
            return web.Response(body=b"not really brotli", headers={"Content-Encoding": "br"})
        return web.Response(body=gzip.compress(b"<h1>An app</h1>"),
                            headers={"Content-Encoding": "gzip", "Content-Type": "text/html"})

    upstream = web.Application()
    upstream.router.add_get("/admin/apps/fake/", page)
    runner = web.AppRunner(upstream)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    monkeypatch.setattr(apps, "targets", lambda: {"fake": plugins.App("Fake", lambda: url, True)})
    try:
        await login(client, "admin", "a-long-password")
        r = await client.get("/admin/apps/fake/", headers={"Accept-Encoding": "gzip, deflate, br, zstd"})
        assert r.status_code == 200 and r.text == "<h1>An app</h1>"
    finally:
        await runner.cleanup()


# ---------- «Моя волна» ----------


async def test_wave_follows_plays_and_skips(db, navidrome):
    from datetime import UTC, datetime, timedelta

    from kurai import security
    from kurai.db.models import WaveEvent
    from kurai.recommend import wave

    model_name = await settings.get(db, "embed.model")
    rng = np.random.default_rng(7)
    calm, loud = np.eye(8)[0] * 5, np.eye(8)[1] * 5
    for i in range(24):
        base = calm if i < 12 else loud
        db.add(Track(id=f"w{i}", title=f"Wave {i}", artist=f"Wave artist {i}", album="W", duration=200,
                     embedding=(base + 0.1 * rng.normal(size=8)).tolist(), embedding_model=model_name))
    user = User(username="wava", display_name="Wava", password_hash="x", navidrome_id="nd-wava",
                navidrome_password=security.encrypt("pw"))
    db.add(user)
    await db.commit()

    now = datetime.now(UTC)
    # she plays two calm tracks on and skips two loud ones: the first wave is due at once
    events = [("wava", "w0", False, 190), ("wava", "w1", False, 200), ("wava", "w12", True, 10),
              ("wava", "w13", True, 8), ("someone", "w2", False, 200), ("wava", "unknown", False, 100)]
    assert await wave.record(db, events, now) == [user.id]
    stored = (await db.execute(select(WaveEvent).where(WaveEvent.user_id == user.id))).scalars().all()
    assert len(stored) == 4  # not other people's, not unknown tracks
    job = (await db.execute(select(Job).where(Job.dedupe_key == f"wave:{user.id}"))).scalar_one()
    assert job.kind == "wave" and job.status == "queued"

    result = await wave.build(db, user)
    assert result["played"] == 2 and result["skips"] == 2
    playlist = (await db.execute(select(Playlist).where(Playlist.user_id == user.id,
                                                        Playlist.kind == "wave"))).scalar_one()
    calm_ids = {f"w{i}" for i in range(12)}
    assert playlist.name == "Моя волна" and set(playlist.track_ids[:3]) <= calm_ids  # near what she played on
    assert not {"w0", "w1", "w12", "w13"} & set(playlist.track_ids)  # nothing she just heard
    assert navidrome.playlists[playlist.navidrome_id][1] == playlist.track_ids

    # the same playlist next time, and not more often than every few minutes
    job.status = "done"
    await db.commit()
    assert not await wave.due(db, user, now + timedelta(minutes=1))
    await wave.record(db, [("wava", "w2", True, 5)], now + timedelta(minutes=5))
    assert await wave.due(db, user, now + timedelta(minutes=5))  # a skip after the gap
    await wave.build(db, user)
    assert (await db.execute(select(func.count()).select_from(Playlist).where(
        Playlist.user_id == user.id, Playlist.kind == "wave"))).scalar_one() == 1
