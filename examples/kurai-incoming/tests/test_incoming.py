"""The example plugin inside Kurai: found, migrated, its tab and buttons, and its job moving approved albums."""

import re
from types import SimpleNamespace

import httpx
import pytest
from kurai_incoming import incoming
from kurai_incoming.models import IncomingAlbum
from sqlalchemy import select, text

from kurai import plugins, settings
from kurai.db.models import Job

pytestmark = pytest.mark.usefixtures("database")


@pytest.fixture
async def client(navidrome):
    from kurai.bootstrap import bootstrap
    from kurai.web.app import app

    await bootstrap()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://kurai.test") as c:
        yield c


def csrf_of(html: str) -> str:
    return re.search(r'data-csrf="([^"]+)"', html).group(1)


async def admin(client) -> str:
    """Logged in as the first admin (admin / admin, changed at the first login); the form token."""
    await client.post("/login", data={"username": "admin", "password": "admin"})
    token = csrf_of((await client.get("/password")).text)
    await client.post("/password", data={"csrf": token, "current": "admin", "new": "a-long-password",
                                         "repeat": "a-long-password"})
    return csrf_of((await client.get("/")).text)


def test_folder_names():
    assert incoming.parse_folder("Radiohead - OK Computer") == ("Radiohead", "OK Computer")
    assert incoming.parse_folder("Just a title") == ("", "Just a title")


async def test_found_and_migrated(db):
    assert [p.name for p in plugins.plugins()] == ["incoming"]
    assert (await db.execute(text("select version_num from alembic_version_incoming"))).scalar_one() == "i0001"
    assert (await settings.get(db, "incoming.dir")) == "/data/incoming"


async def test_an_approved_album_moves_into_the_library(client, db, tmp_path, monkeypatch):
    inbox, library = tmp_path / "incoming", tmp_path / "music"
    (inbox / "Band - Album" / "CD1").mkdir(parents=True)
    (inbox / "Band - Album" / "CD1" / "01.flac").write_bytes(b"x" * 10)
    (inbox / "no music here").mkdir()
    monkeypatch.setattr(incoming, "get_config", lambda: SimpleNamespace(music_dir=str(library)))

    token = await admin(client)
    r = await client.post("/admin/incoming", data={"csrf": token, "incoming.dir": "relative/path"})
    assert "полный путь" in httpx.URL(r.headers["location"]).params["message"]  # the tab's own validation
    await client.post("/admin/incoming", data={"csrf": token, "incoming.dir": str(inbox)})
    assert await settings.get(db, "incoming.dir") == str(inbox)

    assert await incoming.scan(db) == {"found": 1, "moved": 0}  # the empty folder is no album
    album = (await db.execute(select(IncomingAlbum))).scalar_one()
    assert (album.artist, album.title, album.files, album.status) == ("Band", "Album", 1, "new")

    page = (await client.get("/admin/incoming")).text
    assert "Band — Album" in page and f"/admin/incoming/{album.id}/approve" in page
    check = (await client.post("/admin/check/incoming", headers={"X-CSRF-Token": token})).json()
    assert check["ok"] and "папок: 2" in check["detail"]

    await client.post(f"/admin/incoming/{album.id}/approve", data={"csrf": token})
    assert (await db.execute(select(Job).where(Job.dedupe_key == "incoming_scan"))).scalars().first()
    assert await incoming.scan(db) == {"found": 0, "moved": 1}
    assert (library / "Band" / "Album" / "CD1" / "01.flac").exists() and not (inbox / "Band - Album").exists()
