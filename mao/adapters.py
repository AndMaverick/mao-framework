"""Read token usage out of provider responses. No provider SDK is imported or required.

Every adapter accepts either the SDK response object or its plain dict form, and reads only the
documented usage fields. They are tested against documented response shapes, not against live calls,
so check the field names against your SDK version. They never guess: a response with no usage block,
or counts that contradict each other, raises ``ValueError``.

Providers disagree about what "input tokens" means, and that disagreement is where cost reports go
wrong. Every adapter returns the same shape: ``input_tokens`` is uncached input only, cache reads and
cache writes are separate, and ``output_tokens`` includes reasoning.

    Anthropic   input_tokens excludes cache reads and writes
    OpenAI      prompt_tokens includes cached tokens
    DeepSeek    prompt_cache_hit_tokens and prompt_cache_miss_tokens split the input
    Moonshot    prompt_tokens includes cache reads and cache writes (Kimi)
    Google      promptTokenCount includes cached content; thoughts are counted separately from the answer
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable

from .usage import Usage


def _get(obj: Any, *names: str) -> Any:
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _num(obj: Any, *names: str) -> int:
    value = _get(obj, *names)
    return int(value) if value is not None else 0


def _usage_block(response: Any) -> Any:
    block = _get(response, "usage")
    if block is None:
        raise ValueError("response carries no usage block")
    return block


def usage_from_anthropic(response: Any) -> Usage:
    """Anthropic Messages: ``input_tokens`` already excludes cache reads and cache writes."""
    u = _usage_block(response)
    return Usage(
        input_tokens=_num(u, "input_tokens"),
        output_tokens=_num(u, "output_tokens"),
        cached_input_tokens=_num(u, "cache_read_input_tokens"),
        cache_write_tokens=_num(u, "cache_creation_input_tokens"),
    )


def usage_from_openai(response: Any) -> Usage:
    """OpenAI Chat Completions or Responses: total input includes cached tokens, so they are subtracted."""
    u = _usage_block(response)
    total_in = _num(u, "prompt_tokens", "input_tokens")
    details = _get(u, "prompt_tokens_details", "input_tokens_details")
    cached = _num(details, "cached_tokens") if details is not None else 0
    if cached > total_in:
        raise ValueError("cached tokens exceed input tokens in the response")
    return Usage(
        input_tokens=total_in - cached,
        output_tokens=_num(u, "completion_tokens", "output_tokens"),
        cached_input_tokens=cached,
    )


def usage_from_deepseek(response: Any) -> Usage:
    """DeepSeek: input is split into cache hits and misses. Reasoning tokens are part of completion_tokens."""
    u = _usage_block(response)
    hit = _num(u, "prompt_cache_hit_tokens")
    miss = _get(u, "prompt_cache_miss_tokens")
    total_in = _get(u, "prompt_tokens")
    if total_in is None and miss is None:
        raise ValueError("response carries no prompt token counts")
    if total_in is not None:
        uncached = int(total_in) - hit
        if uncached < 0:
            raise ValueError("cache hit tokens exceed prompt tokens in the response")
        if miss is not None and int(miss) != uncached:
            raise ValueError("prompt_cache_miss_tokens disagrees with prompt_tokens minus cache hits")
    else:
        uncached = int(miss)
    return Usage(input_tokens=uncached, output_tokens=_num(u, "completion_tokens"), cached_input_tokens=hit)


def usage_from_moonshot(response: Any) -> Usage:
    """Moonshot (Kimi): prompt_tokens is the total input, which includes cache reads and cache writes."""
    u = _usage_block(response)
    total_in = _num(u, "prompt_tokens")
    details = _get(u, "prompt_tokens_details")
    cached = _num(details, "cached_tokens") if details is not None else 0
    if cached == 0:
        cached = _num(u, "cached_tokens")
    writes = _num(details, "cache_write_tokens") if details is not None else 0
    if cached + writes > total_in:
        raise ValueError("cache tokens exceed prompt tokens in the response")
    return Usage(
        input_tokens=total_in - cached - writes,
        output_tokens=_num(u, "completion_tokens"),
        cached_input_tokens=cached,
        cache_write_tokens=writes,
    )


def usage_from_gemini(response: Any) -> Usage:
    """Google Gemini: promptTokenCount includes cached content. Thoughts are billed as output.

    Tokens that tools feed back to the model (``toolUsePromptTokenCount``) are counted as uncached input.
    """
    meta = _get(response, "usageMetadata", "usage_metadata")
    if meta is None:
        raise ValueError("response carries no usage metadata")
    prompt = _num(meta, "promptTokenCount", "prompt_token_count")
    cached = _num(meta, "cachedContentTokenCount", "cached_content_token_count")
    tool = _num(meta, "toolUsePromptTokenCount", "tool_use_prompt_token_count")
    answer = _num(meta, "candidatesTokenCount", "candidates_token_count", "responseTokenCount", "response_token_count")
    thoughts = _num(meta, "thoughtsTokenCount", "thoughts_token_count")
    if cached > prompt:
        raise ValueError("cached content tokens exceed prompt tokens in the response")
    return Usage(input_tokens=prompt - cached + tool, output_tokens=answer + thoughts, cached_input_tokens=cached)


ADAPTERS: Dict[str, Callable[[Any], Usage]] = {
    "anthropic": usage_from_anthropic,
    "openai": usage_from_openai,
    "deepseek": usage_from_deepseek,
    "moonshot": usage_from_moonshot,
    "kimi": usage_from_moonshot,
    "gemini": usage_from_gemini,
    "google": usage_from_gemini,
}


def usage_from(provider: str, response: Any) -> Usage:
    """Dispatch by provider name. An unknown provider is an error, never a guess."""
    try:
        adapter = ADAPTERS[provider.lower()]
    except KeyError:
        raise ValueError(f"no adapter for provider '{provider}'; known: {', '.join(sorted(ADAPTERS))}") from None
    return adapter(response)


def total(usages: Iterable[Usage]) -> Usage:
    out = Usage()
    for u in usages:
        out = out + u
    return out
