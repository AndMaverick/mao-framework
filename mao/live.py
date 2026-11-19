"""Live provider clients: real HTTP calls whose usage is metered into MAO.

Standard library only (``urllib``). One small client per provider API shape:

* ``AnthropicClient``: Messages API.
* ``OpenAICompatClient``: Chat Completions, for OpenAI, DeepSeek, Moonshot (Kimi) and any compatible endpoint.
* ``GeminiClient``: ``generateContent``.

Each ``complete()`` returns a ``Reply`` carrying the text, the raw response, the normalised ``Usage`` (through
the same adapters the rest of MAO uses) and the provider's request id, so a call can be matched against the
provider's own usage page. Behaviour you can rely on:

* The API key comes from the environment (or the constructor), is sent only in the request header, and is
  never written to an exception, a log line or the audit log.
* Rate limits (429), overload and server errors (5xx), timeouts and dropped connections are retried with
  exponential backoff and jitter, honouring ``Retry-After``. Other 4xx errors are not retried.
* A failed request is not metered. Only a response with a usage block is, and a response without one
  raises rather than being guessed at.
* The transport is injectable, so every code path is testable without a network.

The adapters are tested on documented response shapes. Whether a given provider has changed a field since is
only answered by a live call, which is what ``examples/live_dispute_desk.py`` makes.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .adapters import usage_from
from .usage import Usage

Transport = Callable[[str, Mapping[str, str], bytes, float], Tuple[int, Mapping[str, str], bytes]]


class ProviderError(RuntimeError):
    """A provider call failed. ``status`` is the HTTP status, or 0 for a network failure. Never contains the key."""

    def __init__(self, message: str, status: int = 0, retriable: bool = False, request_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.retriable = retriable
        self.request_id = request_id


@dataclass(frozen=True)
class Reply:
    text: str
    usage: Usage
    provider: str
    model: str
    latency_ms: int
    request_id: Optional[str]
    attempts: int
    response: Dict[str, Any] = field(repr=False, compare=False, default_factory=dict)


def urllib_transport(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> Tuple[int, Mapping[str, str], bytes]:
    """POST with the standard library. HTTP errors are returned as (status, headers, body), not raised."""
    req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (scheme is checked by the clients)
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    lower = name.lower()
    for k, v in headers.items():
        if k.lower() == lower:
            return v
    return None


class _Client:
    provider = ""
    key_env = ""
    default_base = ""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, *, timeout: float = 60.0, max_attempts: int = 4,
                 backoff: float = 1.0, transport: Optional[Transport] = None, sleep: Callable[[float], None] = time.sleep,
                 rng: Optional[random.Random] = None) -> None:
        key = api_key if api_key is not None else os.environ.get(self.key_env, "")
        if not key:
            raise ProviderError(f"no API key: set {self.key_env} or pass api_key", 0, False)
        self._key = key
        self.base_url = (base_url or self.default_base).rstrip("/")
        if not self.base_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ProviderError("base_url must be https (plain http is allowed for localhost only)", 0, False)
        if max_attempts < 1 or timeout <= 0:
            raise ValueError("max_attempts must be at least 1 and timeout above 0")
        self.timeout, self.max_attempts, self.backoff = timeout, max_attempts, backoff
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self._rng = rng or random.Random()

    def __repr__(self) -> str:  # the key never appears, even in a traceback that prints the client
        return f"{type(self).__name__}(base_url={self.base_url!r})"

    # subclasses provide these
    def _request(self, model: str, system: str, prompt: str, max_tokens: int, temperature: Optional[float]) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        raise NotImplementedError

    def _text(self, response: Dict[str, Any]) -> str:
        raise NotImplementedError

    def complete(self, model: str, prompt: str, *, system: str = "", max_tokens: int = 512, temperature: Optional[float] = 0.0) -> Reply:
        if not model or max_tokens < 1:
            raise ValueError("model is required and max_tokens must be at least 1")
        path, headers, payload = self._request(model, system, prompt, max_tokens, temperature)
        body = json.dumps(payload).encode("utf-8")
        url = self.base_url + path
        last: Optional[ProviderError] = None
        for attempt in range(1, self.max_attempts + 1):
            started = time.monotonic()
            try:
                status, resp_headers, raw = self._transport(url, headers, body, self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last = ProviderError(f"{type(exc).__name__} calling {self.provider}", 0, True)
                status, resp_headers, raw = 0, {}, b""
            else:
                request_id = _header(resp_headers, "request-id") or _header(resp_headers, "x-request-id")
                if status == 200:
                    return self._reply(model, raw, request_id, int((time.monotonic() - started) * 1000), attempt)
                last = ProviderError(f"{self.provider} returned HTTP {status}: {self._error_message(raw)}", status,
                                     status == 429 or status >= 500, request_id)
            if not last.retriable or attempt == self.max_attempts:
                raise last
            self._sleep(self._delay(attempt, resp_headers))
        raise last or ProviderError("no attempt was made")  # pragma: no cover

    def _delay(self, attempt: int, headers: Mapping[str, str]) -> float:
        retry_after = _header(headers, "retry-after")
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 120.0)
            except ValueError:
                pass
        return float(min(self.backoff * 2 ** (attempt - 1), 60.0) * (0.5 + self._rng.random() / 2))

    @staticmethod
    def _error_message(raw: bytes) -> str:
        try:
            data = json.loads(raw)
            err = data.get("error", data) if isinstance(data, dict) else data
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return str(msg or "")[:300]
        except (ValueError, AttributeError):
            return raw[:200].decode("utf-8", "replace")

    def _reply(self, model: str, raw: bytes, request_id: Optional[str], latency_ms: int, attempts: int) -> Reply:
        try:
            response = json.loads(raw)
        except ValueError:
            raise ProviderError(f"{self.provider} returned a non-JSON body", 200, False, request_id) from None
        if not isinstance(response, dict):
            raise ProviderError(f"{self.provider} returned an unexpected body", 200, False, request_id)
        try:
            usage = usage_from(self.provider, response)
        except ValueError as exc:
            raise ProviderError(f"cannot meter {self.provider} response: {exc}", 200, False, request_id) from None
        return Reply(self._text(response), usage, self.provider, model, latency_ms, request_id or response.get("id"), attempts, response)


class AnthropicClient(_Client):
    provider = "anthropic"
    key_env = "ANTHROPIC_API_KEY"
    default_base = "https://api.anthropic.com"

    def _request(self, model: str, system: str, prompt: str, max_tokens: int, temperature: Optional[float]) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        payload: Dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if system:
            payload["system"] = system
        if temperature is not None:
            payload["temperature"] = temperature
        headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        return "/v1/messages", headers, payload

    def _text(self, response: Dict[str, Any]) -> str:
        return "".join(b.get("text", "") for b in response.get("content", []) if isinstance(b, dict) and b.get("type") == "text")


class OpenAICompatClient(_Client):
    """Chat Completions. ``provider`` selects the usage adapter: ``openai``, ``deepseek`` or ``moonshot``."""

    _BASES = {"openai": ("OPENAI_API_KEY", "https://api.openai.com/v1", "max_completion_tokens"),
              "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com", "max_tokens"),
              "moonshot": ("MOONSHOT_API_KEY", "https://api.moonshot.ai/v1", "max_tokens")}

    def __init__(self, provider: str = "openai", api_key: Optional[str] = None, base_url: Optional[str] = None, **kw: Any) -> None:
        if provider not in self._BASES:
            raise ValueError(f"provider must be one of {', '.join(self._BASES)}")
        self.provider = provider
        self.key_env, self.default_base, self._token_param = self._BASES[provider]
        super().__init__(api_key, base_url, **kw)

    def _request(self, model: str, system: str, prompt: str, max_tokens: int, temperature: Optional[float]) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        messages: List[Dict[str, str]] = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        payload: Dict[str, Any] = {"model": model, "messages": messages, self._token_param: max_tokens}
        if temperature is not None:
            payload["temperature"] = temperature
        return "/chat/completions", {"authorization": f"Bearer {self._key}", "content-type": "application/json"}, payload

    def _text(self, response: Dict[str, Any]) -> str:
        choices = response.get("choices") or [{}]
        return str((choices[0].get("message") or {}).get("content") or "")


class GeminiClient(_Client):
    provider = "gemini"
    key_env = "GEMINI_API_KEY"
    default_base = "https://generativelanguage.googleapis.com"

    def _request(self, model: str, system: str, prompt: str, max_tokens: int, temperature: Optional[float]) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        config: Dict[str, Any] = {"maxOutputTokens": max_tokens}
        if temperature is not None:
            config["temperature"] = temperature
        payload: Dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": config}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return f"/v1beta/models/{model}:generateContent", {"x-goog-api-key": self._key, "content-type": "application/json"}, payload

    def _text(self, response: Dict[str, Any]) -> str:
        cands = response.get("candidates") or [{}]
        parts = (cands[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))


def client_for(provider: str, **kw: Any) -> _Client:
    """The client for a provider name: anthropic, openai, deepseek, moonshot or gemini."""
    if provider == "anthropic":
        return AnthropicClient(**kw)
    if provider == "gemini":
        return GeminiClient(**kw)
    return OpenAICompatClient(provider, **kw)


def call(ctx: Dict[str, Any], client: _Client, model: str, prompt: str, **kw: Any) -> Reply:
    """Call a model from inside an ``Action`` and report its usage to the orchestrator for metering."""
    reply = client.complete(model, prompt, **kw)
    ctx["report_usage"](reply.usage)
    return reply
