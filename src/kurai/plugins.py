"""Plugins: optional parts of Kurai in their own Python packages. A plugin is a `Plugin` object published under the
entry point group «kurai.plugins»; installed into Kurai's environment (or image), it is found at start.

The core asks the plugins what they add, at fixed places: settings and secrets, outbound gateway subsystems, jobs
(recurring, at start, by hand, and steps of the night's work after the users' tastes and mixes), web pages, the menu
and tabs of «Музыка», system settings tabs, «Проверить» checks, apps' admin pages behind Kurai's login, templates,
docs, Telegram handlers (tried before «Спросить»), the share target, and database tables (the plugin's own Alembic
history, in its own version table, migrated after Kurai's).

An admin can switch an installed plugin off on «Системные настройки → Плагины»: it then adds nothing (its settings, data
and tables stay, still migrated). The switches are read once at start; the web and the worker restart on a change.

A plugin that fails to load (import error, version mismatch) is collected in `load_failures` and surfaced on
«Плагины» rather than taking the whole install down: the off-switch page itself still works because the failed
entry point is never imported again."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from kurai import __version__

GROUP = "kurai.plugins"
DISABLED_KEY = "plugins.disabled"

log = logging.getLogger(__name__)

JobHandler = Callable[[Any, Any], Awaitable[dict]]
Step = Callable[[Any], Awaitable[dict]]
Check = Callable[[Any], Awaitable[str]]

# names the core reserves: a plugin declaring any of them is refused with a collision error, instead of silently
# overwriting core behaviour (a plugin's jobs={"pulse": …} used to replace the pulse builder)
CORE_RESERVED_JOBS = {"nightly", "sync_library", "embed_new", "sync_listens", "ask", "explain",
                      "pulse", "popularity", "artist_ids", "analyze_audio"}
CORE_RESERVED_CHECKS = {"telegram", "llm", "embed", "audio", "public_data", "musicbrainz", "webpush", "models"}
CORE_ADMIN_TABS = {"general", "pulse", "playlists", "models", "audio", "gateway", "users", "apps", "plugins", "jobs"}
# keep in sync with worker.EVERY: a plugin must not change a core job's interval
CORE_RESERVED_EVERY = {"sync_library", "sync_listens", "embed_new", "popularity", "artist_ids"}


@dataclass(frozen=True)
class AdminTab:
    """A tab of «Системные настройки»: its template is included in the page, with the page's form macros at hand
    (`{% from "admin_macros.html" import … %}`)."""

    label: str
    template: str
    fields: dict[str, type] = field(default_factory=dict)
    data: Callable[[Any], Awaitable[dict]] | None = None
    after_save: Callable[[Any, dict, dict], Awaitable[None]] | None = None
    validate: Callable[[dict], str | None] | None = None
    docs: str = ""


@dataclass(frozen=True)
class App:
    """An app's admin page shown on «Приложения», reached only through Kurai's login (web/routes/apps.py)."""

    label: str
    url: Callable[[], str]
    keep_prefix: bool
    headers: Callable[[str], dict[str, str]] | None = None
    frame: bool = True
    about: str = ""


@dataclass(frozen=True)
class Plugin:
    name: str
    title: str = ""
    description: str = ""
    # a PEP 440 specifier the running Kurai must satisfy; on mismatch the plugin is not loaded
    requires: str = ">=1,<2"
    settings: Callable[[], dict[str, Any]] = dict
    secret_keys: frozenset[str] = frozenset()
    outbound: dict[str, str] = field(default_factory=dict)
    jobs: dict[str, JobHandler] = field(default_factory=dict)
    every: dict[str, timedelta] = field(default_factory=dict)
    on_start: tuple[str, ...] = ()
    nightly: tuple[tuple[str, Step], ...] = ()
    manual_jobs: dict[str, str | tuple[str, str]] = field(default_factory=dict)
    router: Any = None
    nav: tuple[tuple[str, str, str], ...] = ()
    music_tabs: tuple[tuple[str, str], ...] = ()
    admin_tabs: dict[str, AdminTab] = field(default_factory=dict)
    checks: dict[str, Check] = field(default_factory=dict)
    apps: dict[str, App] = field(default_factory=dict)
    templates: Path | None = None
    template_globals: dict[str, Any] = field(default_factory=dict)
    docs: Path | None = None
    telegram: Callable[[Any], None] | None = None
    telegram_help: str = ""
    share_path: str | None = None
    migrations: Path | None = None


_load_failures: dict[str, str] = {}  # entry-point name -> why it didn't load
_loaded: tuple[Plugin, ...] | None = None


def _check_requires(name: str, requires: str) -> str | None:
    """None if the running Kurai satisfies the plugin's requirement, else a human-readable reason."""
    if not requires:
        return None
    try:
        spec = SpecifierSet(requires)
        current = Version(__version__)
    except (InvalidSpecifier, InvalidVersion) as e:
        return f"bad version specifier {requires!r}: {e}"
    if current not in spec:
        return f"needs kurai {requires}, running {__version__}"
    return None


def _check_collisions(p: Plugin) -> str | None:
    """None if the plugin's names don't collide with core's reserved ones, else a human-readable reason."""
    for kind in (p.jobs.keys() & CORE_RESERVED_JOBS):
        return f"job {kind!r} is reserved by Kurai core"
    for kind in (p.checks.keys() & CORE_RESERVED_CHECKS):
        return f"check {kind!r} is reserved by Kurai core"
    for kind in (p.admin_tabs.keys() & CORE_ADMIN_TABS):
        return f"admin tab {kind!r} is reserved by Kurai core"
    for kind in (p.every.keys() & CORE_RESERVED_EVERY):
        return f"recurring job {kind!r} is reserved by Kurai core"
    for kind in (p.manual_jobs.keys() & CORE_RESERVED_JOBS):
        return f"manual job {kind!r} is reserved by Kurai core"
    for kind in p.on_start:
        if kind not in p.jobs:
            return f"on_start job {kind!r} is not one of the plugin's own jobs"
    return None


def _check_settings_prefix(p: Plugin) -> str | None:
    """Settings keys must be namespaced `<plugin>.<name>` so they never clash with core's."""
    for key in p.settings():
        if not key.startswith(p.name + "."):
            return f"setting {key!r} must start with {p.name + '.'!r}"
    return None


def _load_all() -> tuple[Plugin, ...]:
    """Every entry point, with load failures collected. A plugin that fails here never gets imported again this
    process — so the «Плагины» page still works even when a plugin is broken."""
    global _load_failures, _loaded
    if _loaded is not None:
        return _loaded
    out: list[Plugin] = []
    failures: dict[str, str] = {}
    for ep in sorted(entry_points(group=GROUP), key=lambda e: e.name):
        try:
            p = ep.load()
            if not isinstance(p, Plugin):
                failures[ep.name] = f"entry point is {type(p).__name__}, not Plugin"
                continue
            reason = (_check_requires(ep.name, p.requires) or _check_collisions(p)
                      or _check_settings_prefix(p))
            if reason:
                failures[ep.name] = reason
                continue
            out.append(p)
        except Exception as e:
            failures[ep.name] = f"load: {e.__class__.__name__}: {e}"
            continue
    _loaded = tuple(out)
    _load_failures = failures
    return _loaded


def load_failures() -> dict[str, str]:
    """Plugins that didn't load, by entry-point name, with the reason. Read this on «Плагины» to surface them."""
    _load_all()
    return dict(_load_failures)


def installed() -> tuple[Plugin, ...]:
    """Every plugin that loaded successfully (switched on or off): for their settings and their migrations."""
    return _load_all()


_disabled_cache: frozenset[str] | None = None


def _disabled_cache_clear() -> None:
    global _disabled_cache
    _disabled_cache = None


def disabled() -> frozenset[str]:
    """The plugins switched off, read once per process: the web and the worker restart to apply a change, and
    worker.plugins_switched detects it by comparing the saved switches against this very cache. A failed read is
    NOT cached, so a transient DB error doesn't stick for the whole process; when the settings table doesn't yet
    exist (first start), returns an empty set: everything on until the admin switches something off."""
    global _disabled_cache
    from sqlalchemy import create_engine, text

    from kurai.config import get_config

    if _disabled_cache is not None:
        return _disabled_cache
    try:
        engine = create_engine(get_config().database_url)
        try:
            with engine.connect() as conn:
                value = conn.execute(text("select value from settings where key = :k"), {"k": DISABLED_KEY}).scalar()
        finally:
            engine.dispose()
    except Exception as e:
        if "does not exist" not in str(e).lower() and "no such table" not in str(e).lower():
            log.error("plugin switches not read: %s", e)
            disabled.last_read_ok = False
            return frozenset()
    disabled.last_read_ok = True
    _disabled_cache = frozenset(value or ())
    return _disabled_cache


disabled.cache_clear = _disabled_cache_clear  # the restart simulation in tests; plugins() has the same via lru_cache


def _load_plugins() -> tuple[Plugin, ...] | None:
    off = disabled()
    if not disabled.last_read_ok:
        return None  # couldn't read the switches: fail closed for this call, retry on the next
    return tuple(p for p in installed() if p.name not in off)


_plugins_cache: tuple | None = None


def _plugins_cache_clear() -> None:
    global _plugins_cache
    _plugins_cache = None


def plugins() -> tuple:
    global _plugins_cache
    if _plugins_cache is None:
        loaded = _load_plugins()
        if loaded is None:
            return ()
        _plugins_cache = loaded
    return _plugins_cache


plugins.cache_clear = _plugins_cache_clear


def merged(attr: str) -> dict:
    """One of the plugins' dict attributes, all together. Collisions between plugins (two declaring the same key)
    are refused: the second one's load would have failed in `_check_collisions` already for the reserved keys; for
    arbitrary keys, the first plugin alphabetically wins and the second's value is dropped with a warning."""
    out: dict = {}
    for p in plugins():
        for key, value in getattr(p, attr).items():
            if key in out:
                log.warning("plugin %s: %s[%r] collides with an earlier plugin, ignored", p.name, attr, key)
                continue
            out[key] = value
    return out


def every() -> dict[str, timedelta]:
    return merged("every")


def jobs() -> dict[str, JobHandler]:
    return merged("jobs")
