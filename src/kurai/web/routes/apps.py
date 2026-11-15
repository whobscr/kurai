"""The admin pages of the apps next to Kurai, reached only through Kurai's login: Navidrome and the plugins' apps,
each under /admin/apps/<name>/ and shown in a large frame on the «Приложения» tab.

An app configured with this path as its URL base (Navidrome: ND_BASEURL) gets the whole path; one with relative links
gets it without the prefix. Navidrome logs Kurai's admin account in by the reverse-proxy header, which nginx always
clears on the way in."""

from collections.abc import AsyncIterator

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from kurai import outbound, plugins, settings
from kurai.config import get_config
from kurai.i18n import _
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
    return {"navidrome": plugins.App("Navidrome", lambda: get_config().navidrome_url, True),
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
        raise HTTPException(403, _("Приложения открываются только со страниц Kurai"))
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
    session = outbound.local_session(aiohttp.ClientTimeout(total=None, sock_read=600))
    try:
        upstream = await session.request(request.method, url, headers=headers, data=await request.body(),
                                         allow_redirects=False)
    except aiohttp.ClientError as e:
        await session.close()
        raise HTTPException(502, f"{app}: {e}") from e

    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in DROP_RESPONSE}
    location = upstream.headers.get("Location")
    if location and location.startswith(base):
        location = location[len(base.rstrip("/")):]
    if location and location.startswith("/") and not location.startswith(PREFIX):
        location = f"{PREFIX}/{app}{location}"
    if location:
        out_headers["Location"] = location

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                yield chunk
        finally:
            upstream.release()
            await session.close()

    return StreamingResponse(body(), status_code=upstream.status, headers=out_headers)
