"""Tests run against a real PostgreSQL with pgvector (KURAI_TEST_DATABASE_URL); the services around Kurai (Navidrome,
the models) are replaced by fakes."""

import os

import pytest

TEST_DB = os.environ.get("KURAI_TEST_DATABASE_URL", "postgresql+psycopg://kurai:kurai@127.0.0.1:55432/kurai")
os.environ["KURAI_DATABASE_URL"] = TEST_DB
os.environ.setdefault("KURAI_SECRET_KEY", "test-secret-key-0123456789abcdef")
os.environ["KURAI_NAVIDROME_URL"] = "http://navidrome.invalid:4533"


@pytest.fixture(scope="session")
def database():
    """A fresh schema, migrated."""
    from sqlalchemy import create_engine, text

    engine = create_engine(TEST_DB)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    engine.dispose()
    from kurai.cli import migrate

    migrate()
    return TEST_DB


@pytest.fixture
async def db(database):
    from kurai import db as database_module

    async with database_module.session() as s:
        yield s


class FakeNavidrome:
    """Records what Kurai would do in Navidrome."""

    def __init__(self):
        self.users: dict[str, str] = {}
        self.playlists: dict[str, tuple[str, list[str]]] = {}
        self.scrobbles: list[tuple[str, str]] = []
        self.ratings: dict[tuple[str, str], tuple[bool, int]] = {}
        self.deleted: list[str] = []

    async def ensure_admin(self, account):
        return False

    async def login(self, account):
        return "token"

    async def create_user(self, token, username, name, password):
        self.users[username] = password
        return f"nd-{username}"

    async def set_password(self, token, user_id, username, name, password):
        self.users[username] = password

    async def delete_user(self, token, user_id):
        self.deleted.append(user_id)

    def signed(self, account, method, params, client="kurai"):
        return f"http://navidrome.invalid/rest/{method}", [("u", account.username), ("c", client)] + params

    async def scrobble(self, account, song_id, at_ms):
        self.scrobbles.append((account.username, song_id))

    async def rate(self, account, song_id, starred, rating):
        self.ratings[(account.username, song_id)] = (starred, rating)

    async def save_playlist(self, account, name, track_ids, playlist_id=None, comment=""):
        pid = playlist_id or f"pl{len(self.playlists) + 1}"
        self.playlists[pid] = (name, list(track_ids))
        return pid


@pytest.fixture
def navidrome(monkeypatch):
    fake = FakeNavidrome()
    import kurai.bootstrap
    import kurai.recommend.library
    import kurai.recommend.playlists

    for module in (kurai.recommend.library, kurai.recommend.playlists):
        monkeypatch.setattr(module, "navidrome", lambda: fake)
    monkeypatch.setattr(kurai.bootstrap.library, "navidrome", lambda: fake)
    return fake
