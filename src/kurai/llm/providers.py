"""The adapters. OpenAI-compatible servers (llama.cpp, vLLM, OpenAI, OpenRouter, LM Studio…) share one adapter whose
small differences are flags of the «server type»; Ollama has its own (its native API sets the context length, which its
/v1 endpoint can't, so long prompts would be cut); Claude goes through Anthropic's official SDK."""

from typing import Any

import aiohttp

from kurai import outbound
from kurai.i18n import _
from kurai.llm import ModelError, TextRequest, strip_thinking

ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
OLLAMA_CONTEXT = 8192


def _route(values: dict[str, Any], base_url: str) -> outbound.Route:
    if outbound.is_local_url(base_url):
        return outbound.Route(None, None)
    return outbound.route_from(values, "llm")


def _http_error(name: str, e: Exception) -> ModelError:
    return ModelError(_("{name}: нет ответа ({kind}: {e})").format(name=name, kind=e.__class__.__name__, e=e))


async def _read(name: str, resp: aiohttp.ClientResponse) -> dict[str, Any]:
    """The answer's JSON; an error page (a proxy's HTML, a plain-text 502) is reported as it is."""
    try:
        data = await resp.json(content_type=None)
    except ValueError:
        raise ModelError(_("{name}: HTTP {status}, не JSON: {text}").format(
            name=name, status=resp.status, text=(await resp.text())[:300])) from None
    if resp.status >= 400:
        raise ModelError(_("{name}: HTTP {status}: {data}").format(name=name, status=resp.status,
                                                                   data=str(data)[:300]))
    if not isinstance(data, dict):
        raise ModelError(_("{name}: неожиданный ответ: {data}").format(name=name, data=str(data)[:300]))
    return data


class OpenAICompatible:
    """POST /chat/completions and /embeddings of any OpenAI-compatible server."""

    def __init__(self, values: dict[str, Any], kind: str = "llm"):
        self.base_url = values[f"{kind}.base_url"].rstrip("/")
        self.api_key = values[f"{kind}.api_key"]
        self.name = values[f"{kind}.model"]
        self.server_type = values.get("llm.server_type", "llama_cpp")
        self.timeout = float(values.get("llm.timeout", 900))
        self.route = _route(values, self.base_url)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _body(self, r: TextRequest) -> dict[str, Any]:
        prompt = r.prompt
        # Qwen3's hybrid thinking is switched per message: without /no_think it thinks first
        if not r.think and "qwen3" in self.name.lower():
            prompt += "\n/no_think"
        body: dict[str, Any] = {
            "model": self.name,
            "messages": [{"role": "system", "content": r.system}, {"role": "user", "content": prompt}],
        }
        # OpenAI's reasoning models take max_completion_tokens; the others max_tokens
        body["max_completion_tokens" if self.server_type == "openai" else "max_tokens"] = r.max_tokens
        if r.schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "answer", "schema": r.schema, "strict": True}}
        return body

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with outbound.session_for_route(self.route, self.timeout) as s, \
                    s.post(self.base_url + path, json=body, headers=self._headers()) as resp:
                return await _read(self.name, resp)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise _http_error(self.name, e) from e

    async def complete(self, request: TextRequest) -> str:
        data = await self._post("/chat/completions", self._body(request))
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            raise ModelError(_("{name}: ответ оборван (не хватило max_tokens)").format(name=self.name))
        return strip_thinking(choice.get("message", {}).get("content") or "")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        data = await self._post("/embeddings", {"model": self.name, "input": texts})
        items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        if len(items) != len(texts):
            raise ModelError(_("{name}: {n} векторов на {m} текстов").format(name=self.name, n=len(items),
                                                                            m=len(texts)))
        return [d["embedding"] for d in items]


class Ollama:
    """Ollama's native API: /api/chat with options.num_ctx, /api/embed."""

    def __init__(self, values: dict[str, Any], kind: str = "llm"):
        self.base_url = values[f"{kind}.base_url"].rstrip("/").removesuffix("/v1")
        self.name = values[f"{kind}.model"]
        self.timeout = float(values.get("llm.timeout", 900))
        self.route = _route(values, self.base_url)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with outbound.session_for_route(self.route, self.timeout) as s, \
                    s.post(self.base_url + path, json=body) as resp:
                return await _read(self.name, resp)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise _http_error(self.name, e) from e

    async def complete(self, request: TextRequest) -> str:
        body: dict[str, Any] = {
            "model": self.name,
            "stream": False,
            "think": request.think,
            "messages": [{"role": "system", "content": request.system}, {"role": "user", "content": request.prompt}],
            "options": {"num_ctx": OLLAMA_CONTEXT, "num_predict": request.max_tokens},
        }
        if request.schema is not None:
            body["format"] = request.schema
        data = await self._post("/api/chat", body)
        return strip_thinking(data.get("message", {}).get("content") or "")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        data = await self._post("/api/embed", {"model": self.name, "input": texts})
        vectors = data.get("embeddings") or []
        if len(vectors) != len(texts):
            raise ModelError(_("{name}: {n} векторов на {m} текстов").format(name=self.name, n=len(vectors),
                                                                            m=len(texts)))
        return vectors


class Anthropic:
    """Claude through the official SDK, its HTTP client given the gateway's proxy. Thinking can't be switched off on
    the current models: the task's switch maps to the effort level instead."""

    def __init__(self, values: dict[str, Any]):
        self.name = values["llm.model"] or ANTHROPIC_DEFAULT_MODEL
        self.api_key = values["llm.api_key"]
        self.base_url = values["llm.base_url"] if values["llm.base_url"].startswith("https://api.anthropic") else None
        self.timeout = float(values.get("llm.timeout", 900))
        self.route = outbound.route_from(values, "llm")

    def _client(self):
        import anthropic

        http_client = anthropic.DefaultAsyncHttpxClient(proxy=self.route.proxy) if self.route.proxy else None
        return anthropic.AsyncAnthropic(api_key=self.api_key or None, base_url=self.base_url, timeout=self.timeout,
                                        http_client=http_client)

    async def complete(self, request: TextRequest) -> str:
        import anthropic

        output_config: dict[str, Any] = {"effort": "high" if request.think else "low"}
        if request.schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": request.schema}
        try:
            async with self._client() as client:
                # server-side fallbacks: a policy decline is re-run on a fallback model inside the same call
                response = await client.beta.messages.create(
                    model=self.name,
                    max_tokens=max(request.max_tokens, 4000),
                    system=request.system,
                    messages=[{"role": "user", "content": request.prompt}],
                    output_config=output_config,
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
        except anthropic.AuthenticationError as e:
            raise ModelError(_("Claude: неверный API-ключ")) from e
        except anthropic.RateLimitError as e:
            raise ModelError(_("Claude: превышен лимит запросов, попробуйте позже")) from e
        except anthropic.APIStatusError as e:
            raise ModelError(_("Claude: HTTP {status}: {message}").format(status=e.status_code,
                                                                          message=e.message)) from e
        except anthropic.APIConnectionError as e:
            raise ModelError(_("Claude: нет соединения ({e})").format(e=e)) from e
        except ImportError as e:  # a proxy kind the HTTP client can't use
            raise ModelError(_("Claude: прокси не поддерживается ({e})").format(e=e)) from e
        if response.stop_reason == "refusal":
            raise ModelError(_("Claude отказался отвечать на этот запрос"))
        if response.stop_reason == "max_tokens":
            raise ModelError(_("Claude: ответ оборван (не хватило max_tokens)"))
        return "".join(b.text for b in response.content if b.type == "text").strip()


def make_text_model(values: dict[str, Any]):
    provider = values["llm.provider"]
    if provider == "anthropic":
        return Anthropic(values)
    if provider == "ollama":
        return Ollama(values)
    return OpenAICompatible(values)


def make_embed_model(values: dict[str, Any]):
    return Ollama(values, "embed") if values["embed.provider"] == "ollama" else OpenAICompatible(values, "embed")
