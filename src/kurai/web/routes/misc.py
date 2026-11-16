"""Health, the PWA's manifest and service worker, the web docs, and Navidrome's Subsonic API passed through for
setups without nginx (with nginx, /rest/ goes to Navidrome directly and never reaches here)."""

import re
from functools import lru_cache
from pathlib import Path

import markdown
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sqlalchemy import text

from kurai import __version__, plugins
from kurai.config import get_config
from kurai.i18n import _
from kurai.integrations.navidrome import BASE_PATH
from kurai.web import upstream
from kurai.web.deps import Ctx, get_db, user_required
from kurai.web.templating import ROOT, render

router = APIRouter()
# what a browser needs of a song or a picture: its type, and the parts of it for seeking
MEDIA_HEADERS = upstream.keep("content-type", "content-length", "content-range", "accept-ranges",
                              "content-disposition", "cache-control", "last-modified", "etag")
DOCS = Path(__file__).resolve().parents[2] / "docs" / "ru"


@router.get("/healthz")
async def healthz(db=Depends(get_db)):
    await db.execute(text("select 1"))
    return PlainTextResponse("ok")


@router.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({
        "id": "/", "name": "Kurai", "short_name": "Kurai", "start_url": "/", "scope": "/", "display": "standalone",
        "description": _("Домашний музыкальный сервер: миксы по вашему вкусу и подборки по просьбе"),
        "background_color": "#1c1b1f", "theme_color": "#6750a4", "lang": "ru",
        "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
                   "purpose": "maskable"},
                  {"src": "/static/icons/logo.svg", "sizes": "any", "type": "image/svg+xml"}],
        # «Поделиться» from another app: /share sends it on (main.py)
        "share_target": {"action": "/share", "method": "GET",
                         "params": {"title": "title", "text": "text", "url": "url"}},
    }, media_type="application/manifest+json")


@router.get("/sw.js")
async def service_worker():
    # served from the root so its scope is the whole app
    return FileResponse(ROOT / "static" / "sw.js", media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/"})


# ---------- the docs (GitHub wiki style: a sidebar of pages, a table of contents, a compact changelog) ----------


def _files(section: str) -> list[Path]:
    """A section's pages, Kurai's and the plugins', ordered by file name (01-..., 02-...)."""
    roots = [DOCS] + [p.docs for p in plugins.plugins() if p.docs]
    return sorted((f for root in roots for f in (root / section).glob("*.md")), key=lambda f: f.name)


@lru_cache
def _pages() -> dict[str, dict[str, str]]:
    """section -> {slug: title}, from the Markdown files' first heading."""
    out: dict[str, dict[str, str]] = {}
    for section in ("user", "admin"):
        out[section] = {}
        for f in _files(section):
            first = f.read_text(encoding="utf-8").splitlines()[0]
            out[section][f.stem.split("-", 1)[-1]] = first.lstrip("# ").strip()
    return out


def _file(section: str, slug: str) -> Path | None:
    if section not in ("user", "admin") or not re.fullmatch(r"[a-z0-9-]+", slug):
        return None
    return next((f for f in _files(section) if f.stem.split("-", 1)[-1] == slug), None)


def _changelog_toc(toc_tokens: list[dict]) -> list[dict]:
    """The newest five versions as they are; the older ones folded per major version."""
    versions = [t for t in toc_tokens if re.match(r"v\d", t.get("id", ""))]
    newest, older = versions[:5], versions[5:]
    groups: dict[str, list[dict]] = {}
    for t in older:
        groups.setdefault(t["id"].split("-")[0], []).append(t)
    return [{"name": t["name"], "id": t["id"]} for t in newest] + \
        [{"name": f"{major[1:]}.x", "children": items} for major, items in groups.items()]


@router.get("/docs")
@router.get("/docs/{section}")
@router.get("/docs/{section}/{slug}")
async def docs(request: Request, section: str = "user", slug: str = "", ctx: Ctx = Depends(user_required)):
    if section == "admin" and not ctx.user.is_admin:
        raise HTTPException(403)
    if section == "changelog":
        path = DOCS / "changelog.md"
    else:
        slug = slug or next(iter(_pages().get(section, {})), "")
        path = _file(section, slug)
    if path is None or not path.exists():
        raise HTTPException(404)
    md = markdown.Markdown(extensions=["toc", "tables", "fenced_code", "admonition", "attr_list"],
                           extension_configs={"toc": {"toc_depth": "2-3"}})
    html = md.convert(path.read_text(encoding="utf-8"))
    toc = md.toc_tokens[0]["children"] if md.toc_tokens and len(md.toc_tokens) == 1 else md.toc_tokens
    if section == "changelog":
        toc = _changelog_toc(toc)
    pages = _pages()
    if not ctx.user.is_admin:
        pages = {"user": pages["user"]}
    return render(request, "docs.html", ctx, html=html, toc=toc, pages=pages, section=section, slug=slug)


# ---------- the Subsonic API for setups without nginx ----------


@router.api_route("/rest/{path:path}", methods=["GET", "POST"])
async def subsonic(path: str, request: Request):
    base = get_config().navidrome_url.rstrip("/")
    url = f"{base}{BASE_PATH}/rest/{path}" + (f"?{request.url.query}" if request.url.query else "")
    return await upstream.relay(request.method, url, MEDIA_HEADERS, "Navidrome", data=await request.body(),
                                headers=upstream.keep("content-type", "range", "accept")(request.headers))


@router.get("/about")
async def about():
    return {"name": "Kurai", "version": __version__, "license": "AGPL-3.0-or-later",
            "source": "https://github.com/whobscr/kurai"}
