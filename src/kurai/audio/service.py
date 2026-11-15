"""`kurai audio`: the audio container's process. A small HTTP API for the rest of Kurai (health, CLAP's vector of a
description) and the «audio» lane of the job queue (the analysis). Reached only from Kurai's own network."""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from kurai import db as database
from kurai import settings
from kurai.audio import CLAP_MODEL, EFFNET_MODEL
from kurai.config import get_config
from kurai.jobs import queue, window

log = logging.getLogger(__name__)

app = FastAPI(title="kurai-audio", docs_url=None, redoc_url=None, openapi_url=None)


class Texts(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=16)


@app.get("/healthz")
async def healthz() -> dict:
    from kurai.audio.analyze import shared_analyzer

    a = shared_analyzer()
    return {"ok": True, "effnet": EFFNET_MODEL, "clap": CLAP_MODEL, "threads": a.threads,
            "loaded": a.loaded}


@app.post("/clap/text")
async def clap_text(body: Texts) -> dict:
    from kurai.audio.analyze import ensure_models, shared_analyzer

    async with database.session() as db:
        if not await settings.get(db, "audio.clap"):
            raise HTTPException(409, "CLAP выключен")
        await ensure_models(db, Path(get_config().audio_models_dir), clap=True)
    vectors = await asyncio.to_thread(shared_analyzer().clap_text, [t[:500] for t in body.texts])
    return {"model": CLAP_MODEL, "vectors": vectors}


async def lane() -> None:
    from kurai.jobs.worker import run_one

    while True:
        try:
            async with database.session() as db:
                values = await settings.get_all(db)
                night = window.current(datetime.now().astimezone(), values["night.start"], values["night.end"])
                job = await queue.claim(db, "audio", night is not None)
            if job is not None:
                await run_one(job.id)
                continue
        except Exception:
            log.exception("the audio lane failed")
        await asyncio.sleep(10)


async def main() -> None:
    import uvicorn

    async with database.session() as db:
        await queue.requeue_interrupted(db, ("audio",))
    c = get_config()
    server = uvicorn.Server(uvicorn.Config(app, host=c.listen, port=c.audio_port, log_level="info"))
    await asyncio.gather(server.serve(), lane())


def run() -> None:
    # the CPU threads the models may use (TensorFlow inside Essentia reads these at start; PyTorch is told directly)
    threads = str(get_config().audio_threads)
    for name in ("TF_NUM_INTRAOP_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(name, threads)
    os.environ.setdefault("TF_NUM_INTEROP_THREADS", "1")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    asyncio.run(main())
