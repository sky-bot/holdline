"""Hybrid classification — keyword layer (TRD §6.1).

This module is the deterministic, dependency-free half of classification: match a
final transcript against per-branch phrase lists, and resolve multi-matches by a
fixed precedence. The LLM fallback is injected as a callable so this layer stays
pure and fully testable; the runtime wires Haiku in behind it.

Matching is word-boundary based, not raw substring, on purpose: short tokens like
"no" or "ok" would otherwise match inside "know", "another", "broke" and misfire
on the recorded take. Multi-word phrases ("can't afford") match as literals.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from .models import Branch

# Per-branch trigger phrases (TRD §6.1). UPSET is intentionally sparse — sentiment
# doesn't reduce to keywords well, so it leans on the LLM fallback.
KEYWORDS: dict[Branch, list[str]] = {
    Branch.CANT_PAY_FULL: [
        "can't afford", "cannot afford", "can't pay that much",
        "don't have that much", "too much money", "can't pay the full",
        "can't pay it all",
    ],
    Branch.WANTS_LATER: [
        "pay later", "next month", "after payday", "give me more time",
        "more time", "extension", "push it back",
    ],
    Branch.DISPUTE: [
        "not mine", "don't recognize", "dispute", "never bought",
        "that's wrong", "not my charge", "didn't make this",
    ],
    Branch.UPSET: [
        "ridiculous", "stop calling", "harassment", "leave me alone",
        "sick of",
    ],
}

# Tie-break order when a transcript hits more than one branch (TRD §6.1).
# DISPUTE first: misrouting a real dispute into a payment offer is the worst
# failure. UPSET next: human handoff is always a safe fallback.
PRECEDENCE: list[Branch] = [
    Branch.DISPUTE, Branch.UPSET, Branch.CANT_PAY_FULL, Branch.WANTS_LATER,
]

# Accept/reject phrases for the OFFER_PROPOSED decision point (TRD §6.1).
ACCEPT_PHRASES: list[str] = [
    "yes", "yeah", "yep", "sure", "okay", "ok", "that works", "sounds good",
    "let's do it", "fine", "agreed", "that's fine", "works for me",
]
REJECT_PHRASES: list[str] = [
    "no", "nope", "can't do that", "cannot do that", "not enough",
    "won't work", "doesn't work", "too much", "forget it", "absolutely not",
]


def _contains(text: str, phrase: str) -> bool:
    """Word-boundary containment, case-insensitive."""
    return re.search(r"\b" + re.escape(phrase) + r"\b", text, re.IGNORECASE) is not None


def keyword_classify(text: str) -> Optional[Branch]:
    """Highest-precedence branch whose phrase list matches, or None."""
    matched = {b for b, phrases in KEYWORDS.items() if any(_contains(text, p) for p in phrases)}
    for branch in PRECEDENCE:
        if branch in matched:
            return branch
    return None


def classify(
    text: str,
    llm_fallback: Optional[Callable[[str], Optional[Branch]]] = None,
) -> Optional[Branch]:
    """Keyword match first; fall back to the injected LLM classifier if given.

    Returns None ("unclear") when neither resolves it — the agent then asks the
    caller to clarify and stays in LISTENING (TRD §6.1).
    """
    branch = keyword_classify(text)
    if branch is not None:
        return branch
    if llm_fallback is not None:
        return llm_fallback(text)
    return None


def classify_decision(text: str) -> Optional[bool]:
    """Accept (True) / reject (False) / unclear (None) for an offer response.

    If a transcript trips both an accept and a reject phrase, it's treated as
    unclear rather than guessing — safer to re-ask than to misread the one
    decision that ends the call.
    """
    is_accept = any(_contains(text, p) for p in ACCEPT_PHRASES)
    is_reject = any(_contains(text, p) for p in REJECT_PHRASES)
    if is_accept and not is_reject:
        return True
    if is_reject and not is_accept:
        return False
    return None
