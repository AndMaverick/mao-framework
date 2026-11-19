"""Read token usage out of provider responses. No provider SDK is imported or required.

Both functions accept either the SDK response object or its plain dict form, and read only the
documented usage fields. They are tested against response shapes, not against live calls, so check
the field names against your SDK version. They never guess: a response with no usage block raises.
"""

from __future__ import annotations

from typing import Any, Iterable

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


def total(usages: Iterable[Usage]) -> Usage:
    out = Usage()
    for u in usages:
        out = out + u
    return out
