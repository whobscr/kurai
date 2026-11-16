"""The admin pages of the apps next to Kurai, reached only through Kurai's login: Navidrome and the plugins' apps,
each under /admin/apps/<name>/ and shown in a large frame on the «Приложения» tab.

An app configured with this path as its URL base (Navidrome: ND_BASEURL) gets the whole path; one with relative links
gets it without the prefix. Navidrome logs Kurai's admin account in by the reverse-proxy header, which nginx always
clears on the way in."""

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, Request

from kurai import plugins, settings
from kurai.config import get_config
from kurai.i18n import _
from kurai.web import upstream
from kurai.web.deps import Ctx, LoginRequired, context

router = APIRouter(prefix="/admin/apps")

PREFIX = "/admin/apps"
# hop-by-hop headers and the ones the proxy sets itself
DROP_REQUEST = {"host", "connection", "keep-alive", "transfer-encoding", "te", "upgrade", "content-length",
                "remote-user", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "accept-encoding"}
# what the proxy unpacks itself: not the browser's list, which has brotli (some apps would answer in it)
ACCEPT_ENCODING = "gzip, deflate"
DROP_RESPONSE = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length"}


def targets() -> dict[str, plugins.App]:
    return {"navidrome": plugins.App(
        "Navidrome", lambda: get_config().navidrome_url, True,
        about=_("Музыкальный сервер, на котором стоит Курай: хранит и сканирует библиотеку (папку с музыкой), "
                "отдаёт её музыкальным приложениям по протоколу Subsonic и записывает, что и сколько раз слушали. "
                "Курай читает отсюда библиотеку и прослушивания, а свои плейлисты пишет обратно — так они появляются "
                "в любом плеере. Пользователи и пароли заводятся в Курае и переносятся сюда сами; здесь — "
                "сканирование, библиотеки, транскодирование и общие плейлисты.")),
        **plugins.merged("apps")}


# what a browser says of where a request comes from (Sec-Fetch-Site): from this site, or typed into the address bar
SAME_SITE = {"same-origin", "none"}


async def admin_any_method(request: Request, ctx: Ctx = Depends(context)) -> Ctx:
    """Admins only, without Kurai's form token: the apps' own pages post to their own APIs. The session cookie is
    SameSite=Lax, so other sites can't post with it, but a link from another site still brings it along: such a
    request is refused (the proxy adds the app's admin login, and some apps change things on a GET)."""
    if ctx.user is None or ctx.session is None or ctx.session.pending_totp:
        raise LoginRequired()
    if not ctx.user.is_admin:
        raise HTTPException(403)
    if request.headers.get("sec-fetch-site", "same-origin") not in SAME_SITE:
        raise HTTPException(403, _("Приложения открываются только со страниц Курая"))
    return ctx


@router.api_route("/{app}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(app: str, path: str, request: Request, ctx: Ctx = Depends(admin_any_method)):
    if app not in targets():
        raise HTTPException(404)
    target = targets()[app]
    base = target.url()
    upstream_path = f"{PREFIX}/{app}/{path}" if target.keep_prefix else f"/{path}"
    url = base.rstrip("/") + upstream_path + (f"?{request.url.query}" if request.url.query else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST}
    headers["Accept-Encoding"] = ACCEPT_ENCODING
    # Kurai's own cookie stays here
    if "cookie" in headers:
        headers["cookie"] = "; ".join(c for c in headers["cookie"].split("; ") if not c.startswith("kurai_session="))
    if app == "navidrome":
        headers["Remote-User"] = await settings.get(ctx.db, "navidrome.admin_user")
    if target.headers:
        headers.update(target.headers(base))

    def out_headers(got: Mapping[str, str]) -> dict[str, str]:
        out = {k: v for k, v in got.items() if k.lower() not in DROP_RESPONSE}
        location = got.get("Location")
        if location and location.startswith(base):
            location = location[len(base.rstrip("/")):]
        if location and location.startswith("/") and not location.startswith(PREFIX):
            location = f"{PREFIX}/{app}{location}"
        if location:
            out["Location"] = location
        return out

    return await upstream.relay(request.method, url, out_headers, app, read_timeout=600, headers=headers,
                                data=await request.body(), allow_redirects=False)
