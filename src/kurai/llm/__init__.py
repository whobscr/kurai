"""Models: one small interface for what Kurai asks of them, with adapters per kind of server.

- text: `complete(task, system, prompt, schema)` -> the answer's text (valid JSON when a schema is given)
- embeddings: `embed(texts)` -> vectors

Which provider, server, model and key, and whether the model thinks for a task, come from the system settings. Every
text request's duration is recorded (LlmCall) for the «usually takes ~N s» hints and the night window's capacity.
"""

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from kurai import db as database
from kurai import settings
from kurai.db.models import LlmCall
from kurai.i18n import _


class ModelError(Exception):
    """The model can't be reached, refused, or answered something unusable. The message is for people."""


@dataclass(frozen=True)
class TextRequest:
    system: str
    prompt: str
    schema: dict | None = None
    think: bool = False
    max_tokens: int = 2000


class TextModel(Protocol):
    name: str

    async def complete(self, request: TextRequest) -> str: ...


class EmbedModel(Protocol):
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_thinking(text: str) -> str:
    """The answer without a model's inline reasoning (Qwen3 on some servers returns it as <think>…</think>)."""
    text = _THINK.sub("", text)
    if "</think>" in text:  # an opening tag lost by the server's template: keep what follows the last closing one
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


def parse_json(text: str) -> Any:
    text = strip_thinking(text)
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ModelError(_("Модель ответила не JSON: {text}").format(text=text[:200])) from e


async def text_model(db: AsyncSession) -> TextModel:
    from kurai.llm.providers import make_text_model

    return make_text_model(await settings.get_all(db))


async def embed_model(db: AsyncSession) -> EmbedModel:
    from kurai.llm.providers import make_embed_model

    return make_embed_model(await settings.get_all(db))


async def complete(db: AsyncSession, task: str, system: str, prompt: str, schema: dict | None = None,
                   max_tokens: int = 2000) -> str:
    """One text request for a task (settings.LLM_TASKS), with that task's thinking switch; timed and recorded."""
    values = await settings.get_all(db)
    if not values["llm.enabled"]:
        raise ModelError(_("Модель выключена в системных настройках"))
    from kurai.llm.providers import make_text_model

    model = make_text_model(values)
    think = bool(values.get(f"think.{task}", False))
    started = time.monotonic()
    ok = False
    try:
        answer = await model.complete(TextRequest(system, prompt, schema, think, max_tokens))
        ok = True
        return answer
    finally:
        # in its own session: the caller's (with its unfinished work) is left as it is
        async with database.session() as own:
            own.add(LlmCall(task=task, provider=values["llm.provider"], model=model.name, thinking=think,
                            seconds=time.monotonic() - started, ok=ok))
            await own.commit()


async def complete_json(db: AsyncSession, task: str, system: str, prompt: str, schema: dict,
                        max_tokens: int = 2000) -> Any:
    return parse_json(await complete(db, task, system, prompt, schema, max_tokens))
