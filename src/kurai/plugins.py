"""Plugins: optional parts of Kurai in their own Python packages. A plugin is a `Plugin` object published under the
entry point group «kurai.plugins»; installed into Kurai's environment (or image), it is found at start.

The core asks the plugins what they add, at fixed places: settings and secrets, outbound gateway subsystems, jobs
(recurring, at start, by hand, and steps of the night's work after the users' tastes and mixes), web pages and the
menu, system settings tabs, «Проверить» checks, apps' admin pages behind Kurai's login, templates, docs, Telegram
handlers (tried before «Спросить»), the share target, and database tables (the plugin's own Alembic history, in its
own version table, migrated after Kurai's).

An admin can switch an installed plugin off on «Системные настройки → Плагины»: it then adds nothing (its settings, data
and tables stay, still migrated). The switches are read once at start; the web and the worker restart on a change.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

GROUP = "kurai.plugins"
DISABLED_KEY = "plugins.disabled"  # the setting: names of the plugins switched off

log = logging.getLogger(__name__)

JobHandler = Callable[[Any, Any], Awaitable[dict]]  # (db, job) -> result
Step = Callable[[Any], Awaitable[dict]]  # (db) -> result
Check = Callable[[Any], Awaitable[str]]  # (db) -> what to show; raises on failure


@dataclass(frozen=True)
class AdminTab:
    """A tab of «Системные настройки»: its template is included in the page, with the page's form macros at hand
    (`{% from "admin_macros.html" import … %}`)."""

    label: str
    template: str
    fields: dict[str, type] = field(default_factory=dict)  # the settings its form may write, and their types
    data: Callable[[Any], Awaitable[dict]] | None = None  # (db) -> extra values for the template
    after_save: Callable[[Any, dict, dict], Awaitable[None]] | None = None  # (db, saved values, values before)
    validate: Callable[[dict], str | None] | None = None  # (values) -> an error message, or None


@dataclass(frozen=True)
class App:
    """An app's admin page shown on «Приложения», reached only through Kurai's login (web/routes/apps.py)."""

    label: str
    url: Callable[[], str]  # its base URL
    keep_prefix: bool  # whether it lives under /admin/apps/<name> itself (its URL base is set to that)
    headers: Callable[[str], dict[str, str]] | None = None  # (base URL) -> extra request headers


@dataclass(frozen=True)
class Plugin:
    name: str
    title: str = ""  # on «Плагины» (the name when empty)
    description: str = ""  # one line under it
    settings: Callable[[], dict[str, Any]] = dict  # its settings' defaults
    secret_keys: frozenset[str] = frozenset()  # stored encrypted
    outbound: dict[str, str] = field(default_factory=dict)  # gateway subsystem -> label
    jobs: dict[str, JobHandler] = field(default_factory=dict)
    every: dict[str, timedelta] = field(default_factory=dict)  # recurring jobs, outside the night too
    on_start: tuple[str, ...] = ()  # jobs queued when the worker starts
    nightly: tuple[tuple[str, Step], ...] = ()  # steps of the night's work, after the tastes and mixes
    manual_jobs: dict[str, str] = field(default_factory=dict)  # kind -> button label on «Задачи»
    router: Any = None  # a FastAPI APIRouter with its pages and actions
    nav: tuple[tuple[str, str, str], ...] = ()  # (path, icon, label) in everyone's menu
    admin_tabs: dict[str, AdminTab] = field(default_factory=dict)
    checks: dict[str, Check] = field(default_factory=dict)  # «Проверить» buttons, by name
    apps: dict[str, App] = field(default_factory=dict)
    templates: Path | None = None
    template_globals: dict[str, Any] = field(default_factory=dict)
    docs: Path | None = None  # <section>/NN-slug.md files, shown with Kurai's own
    telegram: Callable[[Any], None] | None = None  # (aiogram Dispatcher) -> registers its handlers
    share_path: str | None = None  # «Поделиться» opens this, "{q}" replaced by the shared text
    migrations: Path | None = None  # an Alembic script folder (with env.py)


@lru_cache
def installed() -> tuple[Plugin, ...]:
    """Every plugin in the environment, switched on or not: for their settings and their migrations."""
    return tuple(ep.load() for ep in sorted(entry_points(group=GROUP), key=lambda ep: ep.name))


@lru_cache
def disabled() -> frozenset[str]:
    """The plugins switched off, as they were when this process started (read once: the routes, jobs and menu are
    put together at start)."""
    from sqlalchemy import create_engine, text

    from kurai.config import get_config

    try:
        engine = create_engine(get_config().database_url)
        try:
            with engine.connect() as conn:
                value = conn.execute(text("select value from settings where key = :k"), {"k": DISABLED_KEY}).scalar()
        finally:
            engine.dispose()
    except Exception as e:  # before the first migration there's no table: nothing is off
        log.warning("plugin switches not read (all on): %s", e)
        return frozenset()
    return frozenset(value or ())


@lru_cache
def plugins() -> tuple[Plugin, ...]:
    """The plugins switched on: everything the core asks them about."""
    return tuple(p for p in installed() if p.name not in disabled())


def merged(attr: str) -> dict:
    """One of the plugins' dict attributes, all together."""
    out: dict = {}
    for p in plugins():
        out.update(getattr(p, attr))
    return out


def every() -> dict[str, timedelta]:
    return merged("every")


def jobs() -> dict[str, JobHandler]:
    return merged("jobs")
