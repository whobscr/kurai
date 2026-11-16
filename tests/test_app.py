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
    apps = (await client.get("/admin/apps")).text
    assert 'id="app-full"' in apps and 'id="app-stage"' in apps
    assert 'href="/admin/apps/navidrome/" target="_blank"' in apps  # the new tab
    # the band's light / dark switch remembers the choice
    home = (await client.get("/")).text
    assert 'id="mode-toggle"' in home and 'data-play="wave"' in home and 'id="playlists-open"' in home
    assert 'class="main-nav' in home and 'id="player"' in home and 'id="page"' in home
    from kurai import __version__

    for asset in ("css/kurai.css", "js/kurai.js", "js/player.js"):  # a release never meets the cached files before it
        assert f"/static/{asset}?v={__version__}" in home
    token = csrf_of(home)
    assert (await client.post("/me/mode", json={"mode": "dark"}, headers={"X-CSRF-Token": token})).status_code == 204
    assert 'data-mode="dark"' in (await client.get("/")).text
    await client.post("/me/mode", json={"mode": "auto"}, headers={"X-CSRF-Token": token})
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


async def test_jobs_are_paged_filtered_and_pruned(client, db):
    from datetime import UTC, datetime, timedelta

    from kurai.jobs import queue

    old = datetime.now(UTC) - timedelta(days=40)
    for i in range(70):
        db.add(Job(kind="paged_job", status="failed" if i % 10 == 0 else "done", payload={}, finished_at=old,
                   result={"n": i}))
    await db.commit()
    await login(client, "admin", "a-long-password")
    first = (await client.get("/admin/jobs")).text
    assert "Старее" in first and "Новее" not in first and first.count("<tr>") == 31  # the head and 30 jobs
    assert "Забрать список треков из Navidrome" in first  # each button says what it does
    second = (await client.get("/admin/jobs", params={"page": 2})).text
    assert "Новее" in second and "Страница 2" in second
    failed = (await client.get("/admin/jobs", params={"status": "failed"})).text
    assert "paged_job" in failed and "<td>done</td>" not in failed

    assert await queue.prune(db) >= 70
    left = (await db.execute(select(func.count()).select_from(Job).where(Job.kind == "paged_job"))).scalar_one()
    assert left == 0


async def test_every_settings_tab_links_its_help_page(client):
    from kurai.web.routes.admin import CORE_DOCS

    await login(client, "admin", "a-long-password")
    for tab, link in CORE_DOCS.items():
        assert f'href="{link}"' in (await client.get(f"/admin/{tab}")).text, tab
        path, _, anchor = link.partition("#")
        page = await client.get(path)
        assert page.status_code == 200 and (not anchor or f'id="{anchor}"' in page.text), link


async def test_push_on_a_device_switches_push_on_and_sends_a_test(client, db, monkeypatch):
    """«Включить push на этом устройстве»: the subscription is kept, push is on for the user, a test push goes to this
    device, and its result is what the page shows."""
    from kurai import notify

    sent = []

    async def fake_test(db, endpoint, keys):
        sent.append(endpoint)
        return None if "good" in endpoint else "410 Gone"

    monkeypatch.setattr(notify, "push_test", fake_test)
    await login(client, "admin", "a-long-password")
    token = csrf_of((await client.get("/me")).text)
    await client.post("/me/notifications", data={"csrf": token})  # both switches off
    sub = {"endpoint": "https://push.example.com/good", "keys": {"p256dh": "k", "auth": "a"}}
    r = await client.post("/me/push", json=sub, headers={"X-CSRF-Token": token})
    assert r.json() == {"ok": True, "detail": ""} and sent == [sub["endpoint"]]
    admin = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
    await db.refresh(admin)
    assert admin.notify_push
    sub["endpoint"] = "https://push.example.com/bad"
    assert (await client.post("/me/push", json=sub, headers={"X-CSRF-Token": token})).json() == \
        {"ok": False, "detail": "410 Gone"}
    me = (await client.get("/me")).text
    assert 'id="push-status"' in me and 'id="install"' in me and "install-now" in me


async def test_bot_help_apps_info_and_the_architecture_page(client):
    from kurai.telegram.bot import help_text

    assert "/help" in help_text() and "Напишите, что хотите послушать" in help_text()
    await login(client, "admin", "a-long-password")
    apps = (await client.get("/admin/apps")).text
    assert 'id="app-info"' in apps and 'data-about="Музыкальный сервер' in apps
    jobs = (await client.get("/admin/jobs")).text
    assert 'class="info-tip"' in jobs  # the run-now buttons' «i», the same piece
    page = await client.get("/docs/admin/architecture")
    assert page.status_code == 200 and '<svg viewBox' in page.text and 'class="diagram"' in page.text
    assert 'href="/docs/admin/architecture"' in page.text  # in the sidebar
    assert 'id="install"' in (await client.get("/docs/user/start")).text
    assert 'id="install-banner"' in (await client.get("/")).text
    assert (await client.get("/manifest.webmanifest")).json()["id"] == "/"


async def test_navidromes_login_limit_is_waited_out_and_logins_are_reused(monkeypatch):
    """Navidrome answers «429 Too Many Requests» (plain text) to logins beyond its limit: Kurai waits and tries again,
    never takes it for a missing account, and reuses a login instead of logging in for every job."""
    from aiohttp import web

    from kurai.integrations import navidrome as nd_module
    from kurai.integrations.navidrome import BASE_PATH, Account, Navidrome, NavidromeError

    calls = {"login": 0, "createAdmin": 0}

    async def login(request):
        calls["login"] += 1
        if calls["login"] <= 2:
            return web.Response(status=429, text="Too Many Requests")
        return web.json_response({"token": "tok"})

    async def limited(request):
        return web.Response(status=429, text="Too Many Requests")

    async def create_admin(request):
        calls["createAdmin"] += 1
        return web.json_response({"error": "already has an admin"}, status=403)

    app = web.Application()
    app.router.add_post(f"{BASE_PATH}/auth/login", login)
    app.router.add_post(f"{BASE_PATH}/auth/createAdmin", create_admin)
    app.router.add_get(f"{BASE_PATH}/api/limited", limited)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(nd_module, "LOGIN_WAITS", (0, 0, 0))
    try:
        nd = Navidrome(f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}")
        account = Account("kurai", "pw")
        assert await nd.ensure_admin(account) is False  # it exists: the limit was waited out, nothing created
        assert calls == {"login": 3, "createAdmin": 0}
        assert await nd.login(account) == "tok" and calls["login"] == 3  # reused, no new login
        with pytest.raises(NavidromeError, match="HTTP 429: Too Many Requests"):  # a plain-text error, as it is
            await nd._json("GET", "/api/limited")
    finally:
        await runner.cleanup()


async def test_the_web_player_plays_the_wave_and_reports_plays_and_skips(client, db, navidrome, monkeypatch):
    """The wave's queue (made first when there's none), a playlist's, the music as the user with the browser's Range,
    and a track's end: a play (scrobbled) or a skip, both events of the wave at once."""
    from kurai import security
    from kurai.db.models import WaveEvent
    from kurai.recommend import wave
    from kurai.web.routes import player

    await settings.put(db, {"wave.enabled": True})
    for i in range(3):
        db.add(Track(id=f"p{i}", title=f"Song {i}", artist="Pia's band", album="A", duration=200))
    pia = User(username="pia", display_name="Пиа", password_hash=security.hash_password("pia-password"),
               navidrome_id="nd-pia", navidrome_password=security.encrypt("nd-pw"))
    db.add(pia)
    await db.commit()
    await login(client, "pia", "pia-password")
    token = csrf_of((await client.get("/")).text)

    first = (await client.get("/player/queue/wave")).json()
    assert first == {"pending": True}
    assert (await db.execute(select(Job).where(Job.dedupe_key == f"wave:{pia.id}"))).scalar_one().kind == "wave"

    playlist = Playlist(user_id=pia.id, kind="wave", name="Моя волна", track_ids=["p2", "gone", "p0"])
    other = Playlist(user_id=1, kind="nightly", name="Чужой", track_ids=["p1"])
    db.add_all([playlist, other])
    await db.commit()
    queue = (await client.get("/player/queue/wave")).json()
    assert [t["id"] for t in queue["tracks"]] == ["p2", "p0"] and queue["version"]  # in order, unknown left out
    assert (await client.get(f"/player/queue/{playlist.id}")).json()["name"] == "Моя волна"
    assert (await client.get(f"/player/queue/{other.id}")).status_code == 404

    asked = {}

    async def fake_relay(method, url, headers_out, name, read_timeout=3600, **request):
        from fastapi.responses import Response

        asked.update(url=url, **request)
        return Response(status_code=206)

    monkeypatch.setattr(player.upstream, "relay", fake_relay)
    assert (await client.get("/player/stream/p2", headers={"Range": "bytes=100-"})).status_code == 206
    assert asked["url"].endswith("/rest/stream") and ("id", "p2") in asked["params"]
    assert ("c", "kurai-web") in asked["params"] and asked["headers"] == {"range": "bytes=100-"}

    form = {"X-CSRF-Token": token}
    played = await client.post("/player/event", json={"track_id": "p2", "seconds": 190}, headers=form)
    skipped = await client.post("/player/event", json={"track_id": "p0", "seconds": 9}, headers=form)
    assert played.json()["skipped"] is False and skipped.json()["skipped"] is True
    assert navidrome.scrobbles == [("pia", "p2")]  # only a play counts in Navidrome
    events = (await db.execute(select(WaveEvent).where(WaveEvent.user_id == pia.id))).scalars().all()
    assert sorted((e.track_id, e.skipped) for e in events) == [("p0", True), ("p2", False)]
    assert (await client.post("/player/event", json={"track_id": "p0", "seconds": 1})).status_code == 403  # no token

    # the web player's own «now playing» isn't counted again by the watching of the other apps
    watcher = wave.Watcher()
    watcher.update(datetime_now(), [{"id": "p1", "username": "pia", "playerId": "7", "playerName": "kurai-web"}])
    assert watcher.playing == {}


def datetime_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def test_a_new_listener_is_asked_what_they_like_and_the_wave_starts_from_it(client, db, navidrome, monkeypatch):
    """Nothing played: «Для вас» asks for styles of the library; the answers make a first taste (the styles' tracks and
    the own words), and the wave is made of it with the library's popular tracks between. Without answers either,
    the popular tracks alone."""
    from kurai import security
    from kurai.db.models import Listen, Taste
    from kurai.recommend import library, start, wave

    class Words:
        async def embed(self, texts):
            return [np.eye(8)[2].tolist() for _ in texts]  # «jazz» in the test's space

    async def embedder(db):
        return Words()

    monkeypatch.setattr(start.llm, "embed_model", embedder)
    model_name = await settings.get(db, "embed.model")
    for i in range(30):
        genre, axis = ("Jazz", 2) if i < 15 else ("Metal", 3)
        db.add(Track(id=f"cs{i}", title=f"Start {i}", artist=f"{genre} band {i % 5}", album="S", duration=200,
                     genres=[genre], embedding=(np.eye(8)[axis] * 5 + 0.05 * i).tolist(), embedding_model=model_name))
    neo = User(username="neo", display_name="Нео", password_hash=security.hash_password("neo-password"),
               navidrome_id="nd-neo", navidrome_password=security.encrypt("nd"))
    db.add(neo)
    await db.commit()
    # someone else's plays make «popular»
    db.add_all([Listen(user_id=1, track_id=f"cs{i}", play_count=50 - i) for i in range(20, 26)])
    await db.commit()

    assert set(await start.library_styles(db)) >= {"Jazz", "Metal"}
    await login(client, "neo", "neo-password")
    home = (await client.get("/")).text
    assert 'id="start-styles"' in home and 'value="Jazz"' in home
    token = csrf_of(home)
    r = await client.post("/me/start", data={"csrf": token, "style": ["Jazz", "not a style"], "text": "тихий джаз"})
    assert r.headers["location"] == "/"
    await db.refresh(neo)
    assert neo.start_styles == ["Jazz"] and neo.start_text == "тихий джаз" and neo.start_asked_at
    job = (await db.execute(select(Job).where(Job.dedupe_key == f"wave-start:{neo.id}"))).scalar_one()
    assert job.payload == {"retaste": True}
    assert 'id="start-styles"' not in (await client.get("/")).text  # asked once
    assert 'value="Jazz" checked' in (await client.get("/me")).text  # changed later in «Кабинет»

    built = await library.build_taste(db, neo)
    assert built["from_answers"] and built["tracks_used"] == 0
    taste = await db.get(Taste, neo.id)
    assert int(np.argmax(taste.vector)) == 2 and taste.top_artists[0]["name"].startswith("Jazz")
    result = await wave.build(db, neo)
    ids = (await db.execute(select(Playlist.track_ids).where(Playlist.user_id == neo.id,
                                                              Playlist.kind == "wave"))).scalar_one()
    assert result["popular"] > 0 and "cs20" in ids  # the most played, between
    ours = [int(i[2:]) for i in ids if i.startswith("cs")]
    jazz, metal = [n for n in ours if n < 15], [n for n in ours if n >= 15]
    assert len(jazz) >= 10 and len(jazz) > len(metal)  # but mostly what the answers point at

    # no answers and nothing played: the popular tracks alone
    await db.delete(taste)
    neo.start_styles, neo.start_text = [], ""
    await db.commit()
    alone = await wave.build(db, neo)
    assert alone["popular"] == alone["tracks"] > 0


async def test_the_worlds_popularity_fills_in_and_leads_when_nobody_here_has_played(db, monkeypatch):
    """ListenBrainz's listens of the library's artists' albums are stored a part at a time; with no plays here, the
    popular tracks are those of the most listened albums (an unknown album: a tenth of its artist's)."""
    from kurai.db.models import Popularity
    from kurai.recommend import popularity, start

    assert "skipped" in await popularity.refresh(db)  # no token: nothing asked
    await settings.put(db, {"listenbrainz.token": "t"})
    db.add_all([Track(id="lb1", title="Hit", artist="Big", album="Famous", artist_mbid="art-big",
                      release_group_mbid="rg-famous"),
                Track(id="lb2", title="Deep cut", artist="Big", album="Obscure", artist_mbid="art-big",
                      release_group_mbid="rg-obscure"),
                Track(id="lb3", title="Other", artist="Small", album="Small album", artist_mbid="art-small")])
    await db.commit()
    asked = []

    class World:
        async def top_release_groups(self, mbid):
            asked.append(mbid)
            return {"art-big": [{"mbid": "rg-famous", "listens": 900000}, {"mbid": "rg-obscure", "listens": 1000}],
                    "art-small": [{"mbid": "rg-small-other", "listens": 50}]}.get(mbid, [])

    async def client(db_):
        return World()

    monkeypatch.setattr(popularity.public_data, "client", client)
    monkeypatch.setattr(popularity, "PER_RUN", 1)
    first = await popularity.refresh(db)
    assert first["artists"] == 1 and first["left"] >= 1  # a part at a time
    while (await popularity.refresh(db))["left"]:
        pass
    assert {"art-big", "art-small"} <= set(asked)
    big = await db.get(Popularity, "art-big")
    assert big.kind == "artist" and big.listens == 901000
    again = len(asked)
    await popularity.refresh(db)
    assert len(asked) == again  # not asked again before a month

    # nobody here has played these: the world decides (other tests' plays come first, so only ours are compared)
    order = [t for t in await start.popular(db, 200, set()) if t.startswith("lb")]
    assert order[0] == "lb1" and order.index("lb2") > order.index("lb1")


async def test_likes_dislikes_and_the_history(client, db, navidrome):
    """♥ stars a track (in Navidrome and Kurai), 👎 rates it 1 and is a skip of the wave; ♥ takes back only a 👎's low
    rating; a play from the web player is in «История» at once, which plays on from any of its tracks."""
    from kurai import security
    from kurai.db.models import Listen, WaveEvent

    for i in range(3):
        db.add(Track(id=f"h{i}", title=f"Track {i}", artist="Hist", album="H", duration=200))
    hana = User(username="hana", display_name="Хана", password_hash=security.hash_password("hana-password"),
                navidrome_id="nd-hana", navidrome_password=security.encrypt("nd"))
    db.add(hana)
    await db.commit()
    db.add(Listen(user_id=hana.id, track_id="h2", play_count=1, rating=4))
    await db.commit()
    await login(client, "hana", "hana-password")
    page = (await client.get("/history")).text
    assert "Пока пусто" in page and 'href="/history"' in page  # in the menu, empty yet
    form = {"X-CSRF-Token": csrf_of(page)}

    # a play in the web player: in the history at once
    await client.post("/player/event", json={"track_id": "h0", "seconds": 190}, headers=form)
    page = (await client.get("/history")).text
    assert "Track 0" in page and 'data-play="history" data-start="h0"' in page
    queue = (await client.get("/player/queue/history")).json()
    assert [t["id"] for t in queue["tracks"]] == ["h0"] and queue["tracks"][0]["liked"] is False

    liked = await client.post("/player/rate", json={"track_id": "h0", "value": "like"}, headers=form)
    assert liked.json() == {"liked": True, "disliked": False} and navidrome.ratings[("hana", "h0")] == (True, 0)
    assert 'data-rate="like" data-track="h0"' in (await client.get("/history")).text
    disliked = await client.post("/player/rate", json={"track_id": "h1", "value": "dislike"}, headers=form)
    assert disliked.json() == {"liked": False, "disliked": True} and navidrome.ratings[("hana", "h1")] == (False, 1)
    await client.post("/player/rate", json={"track_id": "h1", "value": "none"}, headers=form)
    assert navidrome.ratings[("hana", "h1")] == (False, 0)  # the 👎's rating taken back
    await client.post("/player/rate", json={"track_id": "h2", "value": "like"}, headers=form)
    assert navidrome.ratings[("hana", "h2")] == (True, 4)  # a 4★ from an app stays
    unknown = await client.post("/player/rate", json={"track_id": "h2", "value": "love"}, headers=form)
    assert unknown.status_code == 404

    # 👎 on what plays: a skip of the wave however long it played
    await settings.put(db, {"wave.enabled": True})
    r = await client.post("/player/event", json={"track_id": "h1", "seconds": 190, "disliked": True}, headers=form)
    assert r.json()["skipped"] is True
    event = (await db.execute(select(WaveEvent).where(WaveEvent.user_id == hana.id, WaveEvent.track_id == "h1"))
             ).scalar_one()
    assert event.skipped

    # «Плейлисты»: each with its own play button
    mix = Playlist(user_id=hana.id, kind="nightly", name="Ночной", track_ids=["h0", "h2"])
    db.add(mix)
    await db.commit()
    assert f'class="circle mix-play" data-play="{mix.id}"' in (await client.get("/")).text
