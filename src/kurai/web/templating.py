"""Jinja2 templates with what every page needs: `_()`, the user, the theme, the CSRF token, the art pools."""

import functools
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any

import jinja2
import segno
from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from kurai import __version__, plugins
from kurai.i18n import _

ROOT = Path(__file__).parent


class _Loader(jinja2.BaseLoader):
    """Kurai's templates, then the plugins' (each keeps its own in a subfolder named after it). The plugins are looked
    up at the first template, not at import: a plugin imports Kurai's web modules itself."""

    @functools.cached_property
    def _loaders(self) -> list[jinja2.BaseLoader]:
        return [jinja2.FileSystemLoader(ROOT / "templates"),
                *(jinja2.FileSystemLoader(p.templates) for p in plugins.plugins() if p.templates)]

    def get_source(self, environment, template):
        for loader in self._loaders:
            try:
                return loader.get_source(environment, template)
            except jinja2.TemplateNotFound:
                continue
        raise jinja2.TemplateNotFound(template)


templates = Jinja2Templates(env=jinja2.Environment(loader=_Loader(), autoescape=True))
templates.env.globals.update(_=_, version=__version__)


def local(moment: datetime, fmt: str = "%d.%m %H:%M") -> str:
    """A time in the server's time zone (TZ): the database hands them over in UTC."""
    return moment.astimezone().strftime(fmt)


templates.env.filters["local"] = local


def readable(value: Any) -> str:
    """A job's result as text: JSON with its letters as they are (tojson writes Cyrillic as \\uXXXX escapes); the page
    escapes it like any text."""
    return json.dumps(value, ensure_ascii=False)


templates.env.filters["readable"] = readable


@functools.cache
def _plugin_globals() -> None:
    templates.env.globals["nav_extra"] = [item for p in plugins.plugins() for item in p.nav]
    templates.env.globals["music_tabs"] = [item for p in plugins.plugins() for item in p.music_tabs]
    for p in plugins.plugins():
        templates.env.globals.update(p.template_globals)


# a login scene is a pair of pictures, «<name>-left.svg» and «<name>-right.svg», joined by a line across the form
LOGIN_SCENES = sorted(p.stem.removesuffix("-left") for p in (ROOT / "templates" / "art" / "login").glob("*-left.svg"))
DOODLES = sorted(p.stem for p in (ROOT / "templates" / "art" / "doodles").glob("*.svg"))

def qr_svg(data: str) -> Markup:
    """A QR code as inline SVG (the TOTP setup): black on white in both modes, which every scanner reads."""
    return Markup(segno.make(data, error="m").svg_inline(scale=5, dark="#000", light="#fff", border=2))


templates.env.globals.update(qr_svg=qr_svg)


def render(request: Request, name: str, ctx=None, status_code: int = 200, **values):
    _plugin_globals()
    user = ctx.user if ctx else None
    values.setdefault("accent", (user.accent_color if user and user.accent_color else None)
                      or request.app.state.default_accent)
    values.setdefault("mode", user.theme_mode if user else "auto")
    values.update(user=user, csrf=ctx.csrf if ctx else "", path=request.url.path)
    return templates.TemplateResponse(request, name, values, status_code=status_code)


def login_scene() -> str:
    return random.choice(LOGIN_SCENES)


def doodle() -> str:
    return random.choice(DOODLES)
