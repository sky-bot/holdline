"""LLM paraphrasing of canonical prompts (TRD §6.2).

The FSM decides *what* to say — the canonical line from `prompts.py`, including any
offer numbers `offer.py` computed in code. This module only rewords that line for a
warmer, more natural spoken tone via Claude Haiku. It never changes numbers, dates,
or the decision; the authoritative offer lives in `call_state.offer`, so a paraphrase
is cosmetic by construction.

Graceful degradation: with no `ANTHROPIC_API_KEY` (or on any error) it returns the
canonical line unchanged, so the agent always works — paraphrasing is an enhancement,
never a dependency.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("holdline.paraphrase")

MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = (
    "You are the voice of a calm, respectful debt-collections agent. Reword the "
    "given line so it sounds natural and warm when spoken aloud. Rules: keep it to "
    "one or two short sentences; keep EVERY number, dollar amount, date, and day "
    "count EXACTLY as written; do not introduce any new offer, term, promise, or "
    "legal language; do not ask anything the line didn't ask. Reply with only the "
    "reworded line — no preamble, no quotes."
)

_client = None
_disabled = False


def _get_client():
    """Lazily build an AsyncAnthropic client, or None if unavailable."""
    global _client, _disabled
    if _disabled:
        return None
    if not os.getenv("ANTHROPIC_API_KEY"):
        _disabled = True
        return None
    if _client is None:
        try:
            from anthropic import AsyncAnthropic

            _client = AsyncAnthropic()
        except Exception as exc:  # noqa: BLE001 - never let this break the call
            log.warning("paraphrasing disabled (client init failed: %s)", exc)
            _disabled = True
            return None
    return _client


async def paraphrase(canonical: str) -> str:
    """Return a spoken-tone rewording of `canonical`, or `canonical` unchanged."""
    client = _get_client()
    if client is None:
        return canonical
    try:
        msg = await client.messages.create(
            model=MODEL,
            max_tokens=150,
            system=_SYSTEM,
            messages=[{"role": "user", "content": canonical}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
        return text or canonical
    except Exception as exc:  # noqa: BLE001 - paraphrasing must never break a call
        log.warning("paraphrase failed, using canonical line: %s", exc)
        return canonical
