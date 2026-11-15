"""The audio service as the rest of Kurai sees it: CLAP's vector of a description, and its state. A service that is
down never breaks a playlist: the description's vector is just missing and the other spaces do the work."""

import logging

import aiohttp
import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import outbound, settings
from kurai.i18n import _

log = logging.getLogger(__name__)


async def _call(db: AsyncSession, method: str, path: str, timeout: float, **kwargs) -> dict:
    url = str(await settings.get(db, "audio.url")).rstrip("/") + path
    async with outbound.local_session(timeout) as s, s.request(method, url, **kwargs) as resp:
        try:
            data = await resp.json(content_type=None)
        except ValueError:
            raise RuntimeError(_("сервис анализа звука: HTTP {status}: {text}").format(
                status=resp.status, text=(await resp.text())[:200])) from None
        if resp.status >= 400:
            raise RuntimeError(_("сервис анализа звука: HTTP {status}: {text}").format(status=resp.status,
                                                                                      text=str(data)[:200]))
        return data


async def health(db: AsyncSession) -> dict:
    return await _call(db, "GET", "/healthz", 10)


async def clap_vectors(db: AsyncSession, texts: list[str]) -> list[list[float]]:
    # the first request after a start loads the model (~1 GB): give it time
    return (await _call(db, "POST", "/clap/text", 120, json={"texts": texts}))["vectors"]


async def clap_text(db: AsyncSession, text: str) -> np.ndarray | None:
    """CLAP's vector of a description of a sound, or None (analysis or CLAP off, or the service unreachable)."""
    values = await settings.get_all(db)
    if not (values["audio.enabled"] and values["audio.clap"] and text.strip()):
        return None
    try:
        return np.asarray((await clap_vectors(db, [text]))[0], dtype=np.float32)
    except (aiohttp.ClientError, TimeoutError, RuntimeError, KeyError) as e:
        log.warning("no CLAP vector for a request: %s", e)
        return None
