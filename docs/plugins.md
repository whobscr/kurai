# Writing a Kurai plugin

A plugin adds a part to Kurai without changing Kurai: its own settings, background jobs, pages, system settings tabs,
database tables and docs. It's an ordinary Python package installed into Kurai's environment (its Docker image).

The working example is [`examples/kurai-incoming`](../examples/kurai-incoming). Albums put into an «incoming» folder
appear on a new admin tab and move into the library once approved. It has tests and uses most of the hooks.
Copying it is the quickest start.

- [How Kurai finds a plugin](#how-kurai-finds-a-plugin)
- [The `Plugin` object](#the-plugin-object)
- [Settings](#settings)
- [Jobs](#jobs)
- [Pages and the menu](#pages-and-the-menu)
- [System settings tabs](#system-settings-tabs)
- [Checks, apps, docs, Telegram, sharing](#checks-apps-docs-telegram-sharing)
- [Database tables](#database-tables)
- [What a plugin may use from Kurai](#what-a-plugin-may-use-from-kurai)
- [Building and running it](#building-and-running-it)
- [Developing and testing](#developing-and-testing)
- [Versions](#versions)
- [Rules](#rules)

## How Kurai finds a plugin

At start, Kurai loads every entry point in the group `kurai.plugins`. Each must be a `kurai.plugins.Plugin` object.

```toml
# pyproject.toml
[project]
name = "kurai-incoming"
dependencies = ["kurai>=0.2.10,<0.3"]

[project.entry-points."kurai.plugins"]
incoming = "kurai_incoming.plugin:plugin"
```

Kurai never imports a plugin by name: it only reads that registry (`src/kurai/plugins.py`). Several plugins can be
installed at once; they're loaded in the order of their entry point names.

An admin can switch an installed plugin off on «Системные настройки → Плагины». A switched-off plugin adds nothing (no
pages, tabs, jobs, Telegram handlers…); its settings, data and tables stay, and its migrations still run, so switching
it on again picks up where it was. Its queued jobs wait. The switches are read once at start: on a change the web and
the worker restart themselves (Docker's `restart: unless-stopped` starts them again). In your code, `plugins.plugins()`
is the switched-on ones, `plugins.installed()` all of them.

## The `Plugin` object

One object lists everything the plugin adds. Every field is optional:

```python
from datetime import timedelta
from pathlib import Path

from kurai import plugins
from kurai.i18n import _

ROOT = Path(__file__).parent

plugin = plugins.Plugin(
    name="incoming",
    settings=lambda: {"incoming.dir": "/data/incoming", "incoming.auto_approve": False},
    jobs={"incoming_scan": lambda db, job: incoming.scan(db)},
    every={"incoming_scan": timedelta(minutes=30)},
    manual_jobs={"incoming_scan": _("Проверить входящие")},
    router=web.router,
    admin_tabs={"incoming": plugins.AdminTab(_("Входящие"), "incoming/admin_incoming.html",
                                             {"incoming.dir": str, "incoming.auto_approve": bool},
                                             data=web.tab_data, validate=validate)},
    checks={"incoming": check},
    templates=ROOT / "templates",
    docs=ROOT / "docs" / "ru",
    migrations=ROOT / "migrations",
)
```

| Field | What Kurai does with it |
|---|---|
| `name` | A short identifier: the prefix of your settings, template folder and job names. |
| `title`, `description` | How it's listed on «Системные настройки → Плагины» (the name when empty), where an admin can switch it off. |
| `settings` | A function returning your settings' defaults. They're stored in Kurai's settings table, read with `settings.get` / `get_all`. |
| `secret_keys` | Which of your settings are secrets (tokens, passwords): stored encrypted, shown as empty fields. |
| `outbound` | Your subsystems for the outbound gateway, `{name: label}`: each gets a «through the proxy» checkbox on «Шлюз». |
| `jobs` | Background job handlers by kind: `async (db, job) -> dict`. The dict is shown on «Задачи». |
| `every` | Recurring jobs: `{kind: timedelta}`, queued by the worker outside the night window too. |
| `on_start` | Jobs queued when the worker starts and again after «Шлюз» is saved: use them to apply settings to outside services. |
| `nightly` | Steps of the night's work, `((name, async (db) -> dict), …)`, run after the users' tastes and mixes. |
| `manual_jobs` | `{kind: label}`: buttons on «Задачи» that run a job at once. |
| `router` | A FastAPI `APIRouter` with your pages and form actions. |
| `nav` | `((path, icon, label), …)`: entries in everyone's menu (icons are Material Symbols names). |
| `admin_tabs` | Tabs of «Системные настройки» (see below). |
| `checks` | «Проверить» buttons, `{name: async (db) -> str}`: return what to show, raise on a problem. |
| `apps` | Admin pages of other programs, shown on «Приложения» behind Kurai's login (`plugins.App`). |
| `templates` | A folder of Jinja2 templates; keep yours in a subfolder named after the plugin. |
| `template_globals` | Values or functions available in every template. Times from the database: `{{ t \| local }}` shows them in the server's time zone (`TZ`), `{{ t \| local("%d.%m") }}` in another format. |
| `docs` | A folder `<section>/NN-slug.md` (`user` or `admin`), shown in Kurai's «Справка» among its own pages. |
| `telegram` | `(dispatcher) -> None`: registers aiogram handlers, tried before Kurai's own «Спросить». |
| `share_path` | Where «Поделиться» from another app goes, `"{q}"` replaced by the shared text (the first plugin that sets it wins). |
| `migrations` | An Alembic script folder with your tables (see below). |

## Settings

Name your keys `<plugin>.<setting>` so they never clash with Kurai's or another plugin's. Read them anywhere with:

```python
from kurai import settings

folder = await settings.get(db, "incoming.dir")    # one key
values = await settings.get_all(db)                 # all of them, secrets decrypted
```

Things that belong to the deployment rather than the admin (paths inside the container, other services' addresses) go
in environment variables instead, read with pydantic-settings and the `KURAI_` prefix like Kurai's own (see
`kurai_incoming/config.py`).

## Jobs

A job is a row in Kurai's queue, run by the worker:

```python
from kurai.jobs import queue

await queue.enqueue(db, "incoming_scan", {"why": "approved"}, priority=30, dedupe_key="incoming_scan")
```

- `dedupe_key` prevents a second copy while one is queued or running.
- Lower `priority` runs first.
- `night_only=True` keeps a job for the night window.
- Handlers run one at a time in the general lane, so a long job delays the others: split big work into batches, or
  limit how much one run does.
- An exception marks the job failed with its message (shown on «Задачи»); the worker goes on.
- Blocking work (files, CPU) goes through `asyncio.to_thread`, so the worker's other tasks keep running.

## Pages and the menu

Pages are FastAPI routes rendered with Kurai's templates, so they get its look, menu and theme:

```python
from fastapi import APIRouter, Depends, Request
from kurai.web.deps import Ctx, user_required, admin_required
from kurai.web.templating import render

router = APIRouter()

@router.get("/incoming")
async def page(request: Request, ctx: Ctx = Depends(user_required)):
    return render(request, "incoming/page.html", ctx, albums=[...])
```

- `user_required` / `admin_required` give a context with `ctx.user` and `ctx.db` (an open session), and check the
  form token (CSRF) on POST.
- Forms carry the token: `<input type="hidden" name="csrf" value="{{ csrf }}">`; HTMX requests send it by themselves.
- A page template extends Kurai's: `{% extends "base.html" %}` with the blocks `title`, `heading` and `content`.
- Every user-facing text is wrapped in `_()`, in templates and in code, even before there are translations.

## System settings tabs

```python
plugins.AdminTab(
    label=_("Входящие"),
    template="incoming/admin_incoming.html",
    fields={"incoming.dir": str, "incoming.auto_approve": bool},  # what the tab's form may save, and their types
    data=tab_data,            # async (db) -> extra values for the template
    validate=validate,        # (values) -> an error message, or None
    after_save=after_save,    # async (db, saved, before): e.g. queue a job that applies the new values
)
```

The tab's template is included into Kurai's admin page. Kurai's form macros build its fields, and Kurai saves them
itself when the form posts to `/admin/<tab>`:

```jinja
{% from "admin_macros.html" import text, secret, switch, select, check, form_start as _form_start, form_end with context %}
{% macro form_start() %}{{ _form_start(tab) }}{% endmacro %}
<article class="round">{{ form_start() }}
  {{ text("incoming.dir", _("Папка")) }}
  {{ switch("incoming.auto_approve", _("Переносить без одобрения")) }}
  <div class="row">{{ check("incoming", _("Проверить папку")) }}</div>
{{ form_end() }}</article>
```

Buttons that do something other than saving post to your own routes (e.g. `/admin/incoming/{id}/approve`); answer
with `kurai.web.routes.admin.back(tab, message)` to return to the tab with a message.

## Checks, apps, docs, Telegram, sharing

- **Checks**: `{name: async (db) -> str}`. The button `{{ check(name) }}` posts to `/admin/check/<name>` and shows the
  returned text with the time it took, or the exception's message.
- **Apps**: `plugins.App(label, url, keep_prefix, headers)`. The app is proxied under `/admin/apps/<name>/` for
  admins only. Set the app's own URL base to that path where it can have one (`keep_prefix=True`); otherwise the
  prefix is cut.
- **Docs**: Markdown files in `docs/<section>/NN-slug.md`, the first line a `# Heading`. They're merged with Kurai's
  pages, ordered by file name, and appear at `/docs/<section>/<slug>`.
- **Telegram**: `telegram=register`, where `register(dp)` adds aiogram handlers with filters narrow enough to leave
  other messages to «Спросить». `kurai.telegram.bot.user_of(chat_id)` finds the linked Kurai user;
  `current_bot()` sends messages outside a handler (`None` while the bot is off).
- **Sharing**: `share_path="/incoming?q={q}"` sends what's shared to Kurai's installed app on a phone to your page.

## Database tables

A plugin keeps its own tables with its own Alembic history, in its own version table, so Kurai's migrations and
every plugin's stay independent. Kurai runs them at start, after its own (`kurai web` and `kurai worker` both do,
under a lock).

1. Declare the models on Kurai's `Base` (`from kurai.db.models import Base`), so foreign keys to Kurai's tables
   (`users.id`) resolve.
2. Put an Alembic folder in the package (`migrations/` with `env.py`, `script.py.mako`, `versions/`). Its `env.py`
   sets the version table and limits autogenerate to your tables. Copy `kurai_incoming/migrations/env.py` and change
   the two names.
3. Give the revisions their own prefix (`i0001`, `i0002`…) and point `Plugin.migrations` at the folder.

Never change Kurai's tables from a plugin; keep what you need about Kurai's rows in your own tables, keyed by their
ids.

## What a plugin may use from Kurai

These are the parts meant for plugins. Everything else is internal and may change in any release:

| Import | For |
|---|---|
| `kurai.plugins` | `Plugin`, `AdminTab`, `App` |
| `kurai.settings` | `get`, `get_all`, `put` |
| `kurai.db.session()` | a database session outside a request: `async with database.session() as db` |
| `kurai.db.models` | `Base`; reading `User`, `Track`, `Listen`, `Playlist`… (not changing them) |
| `kurai.jobs.queue` | `enqueue` |
| `kurai.notify` | `notify(db, user_id, text, link)`, `notify_admins(db, text, link)`: in the app, by push and Telegram |
| `kurai.outbound` | `session_for(db, subsystem)` for the internet (through the gateway when its box is ticked); `local_session()` for services next to Kurai |
| `kurai.web.deps` | `Ctx`, `user_required`, `admin_required` |
| `kurai.web.templating` | `render` |
| `kurai.web.routes.admin` | `back` |
| `kurai.telegram.bot` | `user_of`, `current_bot` |
| `kurai.i18n` | `_` |
| `kurai.recommend.library` | `navidrome()`, `admin_login(db)`: Navidrome's API as Kurai's admin |

## Building and running it

Kurai's image already has Kurai and its libraries; a plugin's image adds only the plugin, one small layer on top of
the Kurai version it was written for:

```dockerfile
ARG KURAI_VERSION=0.2.10
FROM ghcr.io/astral-sh/uv:0.9.7 AS uv

FROM ghcr.io/whobscr/kurai:${KURAI_VERSION}
USER root
RUN --mount=type=bind,from=uv,source=/uv,target=/bin/uv --mount=type=bind,source=.,target=/src,rw \
    uv pip install --python /app/.venv/bin/python --no-deps --no-sources --no-cache /src
USER kurai
```

A compose file next to Kurai's makes the web app and the worker run it, and adds what the plugin needs (volumes,
other services):

```yaml
# docker-compose.incoming.yml; in Kurai's .env: COMPOSE_FILE=docker-compose.yml:docker-compose.incoming.yml
services:
  web:
    image: kurai-incoming:${KURAI_VERSION:-latest}
    build: {context: ./kurai-incoming, args: {KURAI_VERSION: "${KURAI_VERSION:-latest}"}}
    volumes: ["${KURAI_DIR}/data:/data"]
  worker:
    image: kurai-incoming:${KURAI_VERSION:-latest}
    volumes: ["${KURAI_DIR}/data:/data"]
```

Then `docker compose up -d --build`. The audio container doesn't need the plugin: it runs only Kurai's own analysis.

## Developing and testing

Keep a Kurai checkout next to the plugin and install it in editable mode:

```toml
[tool.uv.sources]
kurai = { path = "../kurai", editable = true }
```

```sh
uv sync
docker run -d --name kurai-pg -e POSTGRES_USER=kurai -e POSTGRES_PASSWORD=kurai -e POSTGRES_DB=kurai \
  -p 127.0.0.1:55432:5432 pgvector/pgvector:pg17
KURAI_TEST_DATABASE_URL=postgresql+psycopg://kurai:kurai@127.0.0.1:55432/kurai uv run pytest
```

The example's `tests/conftest.py` is a copy of Kurai's own test setup: a fresh database migrated by Kurai and the
plugin, a fake Navidrome, and a test client of the web app. Its tests show how to drive a tab, a button, a check and
a job from a test.

## Versions

The plugin interface may still change between Kurai's minor versions (0.2 → 0.3) until 1.0. Pin the range you've
tested (`kurai>=0.2.10,<0.3`), and build each plugin release on the Kurai version it was tested with. Kurai's
changelog says when a hook changes.

## Rules

- Don't change Kurai's tables, templates or settings; add your own.
- Connections to the internet go through `kurai.outbound` (`session_for` with your own subsystem), so the admin's
  proxy settings apply to the plugin too.
- Secrets are listed in `secret_keys`, so they're stored encrypted; never write them to logs or job results.
- User-facing text goes through `_()`; Kurai's interface is in Russian for now.
- A plugin is responsible for what it does: data it downloads, services it talks to, files it moves. Kurai only
  runs it.
