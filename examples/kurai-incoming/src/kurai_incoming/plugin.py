"""Everything the plugin adds to Kurai, in one object. Kurai reads it through the entry point in pyproject.toml."""

from datetime import timedelta
from pathlib import Path

from kurai import plugins, settings
from kurai.i18n import _
from kurai_incoming import incoming, web

ROOT = Path(__file__).parent


def defaults() -> dict:
    """Its settings and their defaults (keys prefixed with the plugin's name, so they never clash with Kurai's)."""
    return {"incoming.dir": "/data/incoming", "incoming.auto_approve": False}


def validate(values: dict) -> str | None:
    if "incoming.dir" in values and not values["incoming.dir"].startswith("/"):
        return _("Папка — полный путь, например /data/incoming")
    return None


async def check(db) -> str:
    """The «Проверить» button: raises on a problem, returns what to show otherwise."""
    root = Path(await settings.get(db, "incoming.dir"))
    if not root.is_dir():
        raise RuntimeError(f"нет папки {root} (смонтирована ли она в контейнеры web и worker?)")
    return f"папок: {sum(1 for p in root.iterdir() if p.is_dir())}"


plugin = plugins.Plugin(
    name="incoming",
    title=_("Входящие"),
    description=_("Альбомы из папки входящих — в библиотеку, после одобрения"),
    settings=defaults,
    jobs={"incoming_scan": lambda db, job: incoming.scan(db)},
    every={"incoming_scan": timedelta(minutes=30)},
    manual_jobs={"incoming_scan": (_("Проверить входящие"),
                                   _("Найти новые папки во входящих и перенести одобренные. Само — раз в 30 минут."))},
    router=web.router,
    admin_tabs={
        "incoming": plugins.AdminTab(_("Входящие"), "incoming/admin_incoming.html",
                                     {"incoming.dir": str, "incoming.auto_approve": bool},
                                     data=web.tab_data, validate=validate, docs="/docs/admin/incoming"),
    },
    checks={"incoming": check},
    templates=ROOT / "templates",
    docs=ROOT / "docs" / "ru",
    migrations=ROOT / "migrations",
)
