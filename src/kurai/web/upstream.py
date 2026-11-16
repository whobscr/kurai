"""A response of a service next to Kurai (Navidrome, the apps of plugins) passed on to the browser as it comes: music,
cover art, the apps' pages. Never proxied (outbound.local_session); the connection lives as long as the body."""

from collections.abc import AsyncIterator, Callable, Mapping

import aiohttp
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from kurai import outbound


async def relay(method: str, url: str, headers_out: Callable[[Mapping[str, str]], dict[str, str]], name: str,
                read_timeout: int = 3600, **request) -> StreamingResponse:
    """`headers_out`: the browser's headers from the service's ones; `name`: whose error a 502 names; `request`:
    aiohttp's request arguments (params, data, headers, allow_redirects…)."""
    session = outbound.local_session(aiohttp.ClientTimeout(total=None, sock_read=read_timeout))
    try:
        upstream = await session.request(method, url, **request)
    except aiohttp.ClientError as e:
        await session.close()
        raise HTTPException(502, f"{name}: {e}") from e

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                yield chunk
        finally:
            upstream.release()
            await session.close()

    return StreamingResponse(body(), status_code=upstream.status, headers=headers_out(upstream.headers))


def keep(*names: str) -> Callable[[Mapping[str, str]], dict[str, str]]:
    """`headers_out` that passes only these headers (lowercase names)."""
    return lambda headers: {k: v for k, v in headers.items() if k.lower() in names}
