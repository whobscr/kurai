"""`kurai web | worker | audio | migrate | reset-2fa <user> | reset-password <user>`.

`web`, `worker` and `audio` apply the database migrations first (under a PostgreSQL advisory lock with a 60 s
timeout, so they don't race and a stuck migration doesn't wedge the whole install): nothing has to be done by hand
after an update."""

import argparse
import asyncio
import logging
import time
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text

from kurai.config import get_config

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
LOCK = 7_340_211
LOCK_TIMEOUT_MS = 60_000


def migrate() -> None:
    """Kurai's migrations, then each plugin's (its own Alembic history, in its own version table). The advisory lock
    is taken with a deadline — lock_timeout does not apply to advisory locks, so the wait is a try-lock loop. A core
    migration failure is fatal (nothing may run on a half-migrated schema); a plugin's is logged and the next plugin
    still runs — one bad plugin no longer wedges the whole install."""
    from kurai import plugins

    url = get_config().database_url
    engine = create_engine(url)
    with engine.connect() as conn:
        deadline = time.monotonic() + LOCK_TIMEOUT_MS / 1000
        while not conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": LOCK}).scalar():
            if time.monotonic() >= deadline:
                raise SystemExit(f"another process is migrating and hasn't finished in {LOCK_TIMEOUT_MS // 1000} s")
            time.sleep(1)
        try:
            cfg = AlembicConfig()
            cfg.set_main_option("script_location", str(MIGRATIONS))
            cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
            command.upgrade(cfg, "head")
            failed: list[str] = []
            for p in plugins.installed():
                if not p.migrations:
                    continue
                try:
                    cfg = AlembicConfig()
                    cfg.set_main_option("script_location", str(p.migrations))
                    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
                    command.upgrade(cfg, "head")
                except Exception as e:
                    logging.getLogger(__name__).exception("plugin %s migration failed", p.name)
                    failed.append(f"{p.name}: {e.__class__.__name__}: {e}")
            if failed:
                logging.getLogger(__name__).error("plugin migrations failed: %s", "; ".join(failed))
        finally:
            conn.execute(text("select pg_advisory_unlock(:k)"), {"k": LOCK})
    engine.dispose()


async def _reset(username: str, what: str) -> None:
    from sqlalchemy import select

    from kurai import db as database
    from kurai import security
    from kurai.db.models import User

    async with database.session() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is None:
            raise SystemExit(f"no user {username}")
        if what == "2fa":
            user.totp_enabled, user.totp_secret, user.recovery_codes = False, None, []
            print(f"two-factor login of {username} turned off")
        else:
            password = security.random_token(9)
            user.password_hash = security.hash_password(password)
            user.must_change_password = True
            print(f"temporary password of {username}: {password} (to be changed at the next login; Navidrome's "
                  "password changes then too)")
        await db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(prog="kurai")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("web", help="the web app")
    sub.add_parser("worker", help="the background jobs and the Telegram bot")
    sub.add_parser("audio", help="the audio analysis (the audio image only)")
    sub.add_parser("migrate", help="apply the database migrations")
    for name in ("reset-2fa", "reset-password"):
        p = sub.add_parser(name)
        p.add_argument("username")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "migrate":
        migrate()
    elif args.cmd == "web":
        import uvicorn

        migrate()
        c = get_config()
        uvicorn.run("kurai.web.app:app", host=c.listen, port=c.web_port, proxy_headers=True,
                    forwarded_allow_ips="*")
    elif args.cmd == "worker":
        from kurai.jobs.worker import run

        migrate()
        run()
    elif args.cmd == "audio":
        from kurai.audio.service import run as run_audio

        migrate()
        run_audio()
    else:
        asyncio.run(_reset(args.username, "2fa" if args.cmd == "reset-2fa" else "password"))
