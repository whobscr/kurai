"""What goes wrong around Kurai: services down or answering garbage, guessing, links from other sites, Telegram."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from kurai import security, settings
from kurai.db.models import Job, Listen, Setting, Track, User

pytestmark = pytest.mark.usefixtures("database")


async def _server(routes):
    """A small real HTTP server: [(method, path, handler)] -> (base URL, runner to clean up)."""
    from aiohttp import web

    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}", runner


# ---------- jobs ----------


async def test_a_failing_recurring_job_waits_its_interval(db, monkeypatch):
    from kurai.jobs import queue, worker

    monkeypatch.setattr(worker.plugins, "every", lambda: {})
    await db.execute(delete(Job))
    now = datetime.now().astimezone()
    db.add(Job(kind="sync_library", status="failed", error="Navidrome не отвечает", finished_at=now))
    await db.commit()
    await worker.schedule(now)
    queued = (await db.execute(select(Job).where(Job.kind == "sync_library", Job.status == "queued"))).scalars().all()
    assert queued == []  # not asked again at once
    assert await queue.last_finished(db, "sync_library") is not None
    await db.execute(delete(Job))
    await db.commit()


async def test_one_failing_part_of_the_night_doesnt_stop_the_rest(db, monkeypatch):
    from kurai.jobs import worker

    calls = []

    async def ok(*args):
        calls.append("ok")
        return {"ok": True}

    async def broken(*args):
        raise RuntimeError("Navidrome не отвечает")

    monkeypatch.setattr(worker.library, "sync_library", broken)
    monkeypatch.setattr(worker.library, "embed_tracks", ok)
    monkeypatch.setattr(worker.library, "active_users", lambda db: _users(db))
    monkeypatch.setattr(worker.library, "sync_listens", ok)
    monkeypatch.setattr(worker.library, "build_taste", ok)
    monkeypatch.setattr(worker.playlists, "nightly", ok)
    monkeypatch.setattr(worker.playlists, "remove_old_mixes", ok)
    monkeypatch.setattr(worker.wave, "build", ok)
    job = Job(kind="nightly", payload={})
    db.add(job)
    await db.commit()
    await worker.run_one(job.id)
    await db.refresh(job)
    assert job.status == "failed" and "library" in job.error
    assert job.result["embeddings"] == {"ok": True} and any(k.startswith("user ") for k in job.result)
    await db.delete(job)
    await db.commit()


async def _users(db):
    return list((await db.execute(select(User).where(User.active.is_(True)))).scalars())


async def test_a_model_server_answering_an_error_page_is_a_model_error():
    from aiohttp import web

    from kurai.llm import ModelError
    from kurai.llm.providers import OpenAICompatible

    async def html(request):
        return web.Response(status=502, text="<html>Bad Gateway</html>", content_type="text/html")

    url, runner = await _server([("POST", "/v1/chat/completions", html)])
    try:
        model = OpenAICompatible({"llm.base_url": url + "/v1", "llm.api_key": "", "llm.model": "m"})
        from kurai.llm import TextRequest

        with pytest.raises(ModelError, match="502"):
            await model.complete(TextRequest("s", "p"))
    finally:
        await runner.cleanup()


def test_a_proxy_with_an_ipv6_address_keeps_its_brackets():
    from kurai.outbound import _with_credentials

    assert _with_credentials("http://[fd00::1]:3128", "u", "p") == "http://u:p@[fd00::1]:3128"


async def test_secrets_of_another_key_are_never_replaced(db, monkeypatch):
    from kurai import bootstrap

    await settings.put(db, {"navidrome.admin_password": "kept"})
    await db.commit()
    row = await db.get(Setting, "navidrome.admin_password")
    row.value = "encrypted-with-another-key"
    await db.commit()

    class Down:
        async def ensure_admin(self, account):
            raise RuntimeError("down")

    monkeypatch.setattr(bootstrap.library, "navidrome", lambda: Down())
    await bootstrap.bootstrap()
    await db.refresh(row)
    assert row.value == "encrypted-with-another-key"
    await settings.put(db, {"navidrome.admin_password": "kept"})
    await db.commit()


# ---------- the library ----------


async def test_an_empty_answer_from_navidrome_removes_nothing(db, monkeypatch):
    from kurai.recommend import library

    db.add(Track(id="keep-me", title="T"))
    await db.commit()

    class Empty:
        async def ensure_admin(self, account):
            return False

        async def login(self, account):
            return "t"

        async def songs(self, token):
            return
            yield

    monkeypatch.setattr(library, "navidrome", lambda: Empty())
    with pytest.raises(RuntimeError, match="не вернул"):
        await library.sync_library(db)
    assert await db.get(Track, "keep-me") is not None
    await db.delete(await db.get(Track, "keep-me"))
    await db.commit()


async def test_a_star_taken_back_is_synced(db, monkeypatch):
    from kurai.recommend import library

    user = User(username="stars", password_hash="x", navidrome_password=security.encrypt("pw"))
    db.add_all([user, Track(id="s1", title="One"), Track(id="s2", title="Two")])
    await db.commit()
    songs = [{"id": "s1", "starred": True}, {"id": "s2", "rating": 1}]

    class Nd:
        async def login(self, account):
            return "t"

        async def songs(self, token):
            for song in songs:
                yield song

    monkeypatch.setattr(library, "navidrome", lambda: Nd())
    assert (await library.sync_listens(db, user))["listens"] == 2
    songs[:] = [{"id": "s1"}, {"id": "s2"}]  # unstarred, rating removed, never played
    assert (await library.sync_listens(db, user))["cleared"] == 2
    assert (await db.execute(select(Listen).where(Listen.user_id == user.id))).scalars().all() == []
    for obj in (user, await db.get(Track, "s1"), await db.get(Track, "s2")):
        await db.delete(obj)
    await db.commit()


# ---------- the web ----------


@pytest.fixture
async def web(navidrome):
    import httpx

    from kurai.bootstrap import bootstrap
    from kurai.web.app import app

    await bootstrap()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://kurai.test") as c:
        yield c


async def _admin(web, db):
    admin = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
    admin.password_hash, admin.must_change_password = security.hash_password("a-long-password"), False
    await db.commit()
    await web.post("/login", data={"username": "admin", "password": "a-long-password"})
    return re.search(r'data-csrf="([^"]+)"', (await web.get("/me")).text).group(1)


async def test_guessing_a_password_is_throttled(web):
    from kurai.web.routes import auth

    for _ in range(auth.LOGIN_TRIES.limit):
        assert (await web.post("/login", data={"username": "nobody", "password": "x"})).status_code == 401
    assert auth.LOGIN_TRIES.blocked("127.0.0.1|nobody")
    auth.LOGIN_TRIES.clear("127.0.0.1|nobody")
    auth.NAME_TRIES.clear("nobody")


async def test_an_app_page_opened_from_another_site_is_refused(web, db):
    await _admin(web, db)
    r = await web.get("/admin/apps/navidrome/rest/ping", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


async def test_a_push_subscription_must_be_a_push_service(web, db):
    token = await _admin(web, db)
    bad = {"endpoint": "http://navidrome:4533/admin/apps/navidrome/api/user", "keys": {"p256dh": "a", "auth": "b"}}
    assert (await web.post("/me/push", json=bad, headers={"X-CSRF-Token": token})).status_code == 400
    good = {"endpoint": "https://fcm.googleapis.com/fcm/send/x", "keys": {"p256dh": "a", "auth": "b"}}
    assert (await web.post("/me/push", json=good, headers={"X-CSRF-Token": token})).status_code == 200


async def test_a_secret_can_be_cleared(web, db):
    token = await _admin(web, db)
    await settings.put(db, {"telegram.token": "123:abc"})
    await db.commit()
    await web.post("/admin/general", data={"csrf": token, "night.start": "06:00", "night.end": "10:00",
                                          "ui.default_accent": "#6750a4", "playlists.per_night": "3",
                                          "telegram.token.clear": "on"})
    db.expire_all()
    assert await settings.get(db, "telegram.token") == ""


# ---------- Telegram ----------


async def test_a_chat_is_linked_by_a_fresh_code_with_or_without_start(db):
    from kurai.telegram import bot

    user = User(username="tg", password_hash="x", telegram_link_code="fresh-code-1234",
                telegram_link_at=datetime.now(UTC))
    old = User(username="tg-old", password_hash="x", telegram_link_code="stale-code-1234",
               telegram_link_at=datetime.now(UTC) - timedelta(hours=2))
    other = User(username="tg-other", password_hash="x", telegram_chat_id=777)
    db.add_all([user, old, other])
    await db.commit()
    ids = [user.id, old.id, other.id]
    assert await bot.link("stale-code-1234", 555) is None
    linked = await bot.link("fresh-code-1234", 777)  # the chat was another account's: it moves
    assert linked is not None and linked.telegram_chat_id == 777
    db.expire_all()
    assert (await db.get(User, ids[2])).telegram_chat_id is None
    assert (await bot.user_of(777)).username == "tg"
    for user_id in ids:
        await db.delete(await db.get(User, user_id))
    await db.commit()


async def test_an_answer_asked_in_telegram_is_sent_back_there(db, monkeypatch):
    from kurai.jobs import worker

    sent = []

    async def answer(db, user, text):
        return {"answer": "Да, есть."}

    async def send(db, chat, text, link=""):
        sent.append((chat, text))

    monkeypatch.setattr(worker.playlists, "ask", answer)
    monkeypatch.setattr(worker, "send_telegram", send)
    user = User(username="asker", password_hash="x")
    db.add(user)
    await db.commit()
    job = Job(kind="ask", payload={"text": "есть джаз?", "reply_to": 42}, user_id=user.id)
    db.add(job)
    await db.commit()
    await worker.run_one(job.id)
    assert sent == [(42, "Да, есть.")]
    await db.delete(await db.get(User, user.id))
    await db.commit()
