"""LLM client behind a provider setting (specs/hydration.md, config seams).

Providers:
- `ai_query`: a Databricks model serving endpoint, by name. Called through the serving REST API
  (`/serving-endpoints/<name>/invocations`) with the job's own credentials, so token usage comes back
  with every response. Same endpoint the SQL `ai_query()` function would hit.
- `openai`: any OpenAI-compatible chat-completions base URL; the key comes from a secret scope.

Callers pass messages that hold names and profile stats only (ground rule 6).
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

PROVIDERS = ("ai_query", "openai")


@dataclass(frozen=True)
class LLMResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMClient(Protocol):
    model: str

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 4000) -> LLMResponse: ...


def extract_text(body: dict) -> str:
    """Chat-completions content is a string, or a list of parts (reasoning models). Keep text parts only."""
    content = body["choices"][0]["message"].get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def parse_chat_response(body: dict, fallback_model: str) -> LLMResponse:
    usage = body.get("usage") or {}
    return LLMResponse(
        text=extract_text(body),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        model=body.get("model") or fallback_model,
    )


def _body(messages: list[dict], temperature: float, max_tokens: int, model: str | None = None) -> dict:
    b: dict[str, Any] = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if model:
        b["model"] = model
    return b


class ServingEndpointClient:
    def __init__(self, endpoint: str, workspace_client=None):
        self.model = endpoint
        self._w = workspace_client

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 4000) -> LLMResponse:
        if self._w is None:
            from databricks.sdk import WorkspaceClient

            self._w = WorkspaceClient()
        body = self._w.api_client.do("POST", f"/serving-endpoints/{self.model}/invocations",
                                     body=_body(messages, temperature, max_tokens))
        return parse_chat_response(body, self.model)


Transport = Callable[[str, dict, bytes], dict]


def _urllib_transport(url: str, headers: dict, body: bytes) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310 - URL comes from config
        return json.loads(resp.read())


class OpenAICompatibleClient:
    def __init__(self, base_url: str, api_key: str, model: str, transport: Transport | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._key = api_key
        self._transport = transport or _urllib_transport

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 4000) -> LLMResponse:
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        payload = json.dumps(_body(messages, temperature, max_tokens, self.model)).encode()
        return parse_chat_response(self._transport(f"{self.base_url}/chat/completions", headers, payload), self.model)


def make_client(provider: str, *, endpoint: str | None = None, base_url: str | None = None,
                api_key: str | None = None, model: str | None = None) -> LLMClient:
    if provider == "ai_query":
        if not endpoint:
            raise ValueError("ai_query provider needs a serving endpoint name")
        return ServingEndpointClient(endpoint)
    if provider == "openai":
        if not (base_url and api_key and model):
            raise ValueError("openai provider needs base_url, api_key and model")
        return OpenAICompatibleClient(base_url, api_key, model)
    raise ValueError(f"Unknown llm provider {provider!r}; expected one of {PROVIDERS}")
