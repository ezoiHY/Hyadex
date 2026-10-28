"""Tokens a chat has used across all of its turns.

Each chat keeps ``token_totals``: ``total_tokens``, ``input_tokens`` (including
cached input), ``cached_input_tokens``, ``output_tokens`` and ``updated_at``.
These are the provider's own counts. Claude reports usage for each turn in its
result message; Codex reports cumulative usage per thread, so only the growth
since the last sample is added (``_token_totals_threads`` holds the last
sample per thread). Context usage, by contrast, is how full the window is now.
"""
from __future__ import annotations

from typing import Any

from hyadex.core.paths import now_iso
from hyadex.core.sessions import STORE

FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
_MAX_TRACKED_THREADS = 8


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def claude_usage_tokens(usage: Any) -> dict[str, int] | None:
    """Tokens of one Claude result's ``usage`` (Anthropic API field names)."""
    if not isinstance(usage, dict):
        return None
    cache_read = _count(usage.get("cache_read_input_tokens"))
    input_tokens = (_count(usage.get("input_tokens")) + _count(usage.get("cache_creation_input_tokens"))
                    + cache_read)
    output = _count(usage.get("output_tokens"))
    if not input_tokens and not output:
        return None
    return {"input_tokens": input_tokens, "cached_input_tokens": cache_read,
            "output_tokens": output, "total_tokens": input_tokens + output}


def codex_usage_tokens(total: Any) -> dict[str, int] | None:
    """Tokens of a Codex ``tokenUsage.total`` breakdown (cumulative per thread)."""
    if not isinstance(total, dict):
        return None
    tokens = {"input_tokens": _count(total.get("inputTokens")),
              "cached_input_tokens": _count(total.get("cachedInputTokens")),
              "output_tokens": _count(total.get("outputTokens")),
              "total_tokens": _count(total.get("totalTokens"))}
    return tokens if any(tokens.values()) else None


def add_tokens_locked(session: dict[str, Any], tokens: dict[str, int]) -> None:
    totals = session.get("token_totals")
    totals = dict(totals) if isinstance(totals, dict) else {}
    for field in FIELDS:
        totals[field] = _count(totals.get(field)) + _count(tokens.get(field))
    totals["updated_at"] = now_iso()
    session["token_totals"] = totals


def add_codex_thread_total_locked(session: dict[str, Any], thread_id: str, total: Any) -> None:
    """Add the growth of one Codex thread's cumulative usage since its last sample."""
    current = codex_usage_tokens(total)
    if current is None or not thread_id:
        return
    threads = session.get("_token_totals_threads")
    threads = dict(threads) if isinstance(threads, dict) else {}
    previous = threads.pop(thread_id, None)
    previous = previous if isinstance(previous, dict) else {}
    if _count(previous.get("total_tokens")) > current["total_tokens"]:
        # The thread restarted its count (for example after a rollback).
        previous = {}
    delta = {field: max(0, current[field] - _count(previous.get(field))) for field in FIELDS}
    threads[thread_id] = current
    while len(threads) > _MAX_TRACKED_THREADS:
        threads.pop(next(iter(threads)))
    session["_token_totals_threads"] = threads
    if any(delta.values()):
        add_tokens_locked(session, delta)


async def add_claude_turn_usage(session_id: str, usage: Any) -> None:
    tokens = claude_usage_tokens(usage)
    if tokens is None:
        return
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        if session is None:
            return
        add_tokens_locked(session, tokens)
        await STORE.save()

