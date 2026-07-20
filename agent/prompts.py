"""Canonical prompt derivation (TRD §4.7).

Every state's spoken prompt is a *pure function of persisted CallState*. That's
what makes idempotent resume work: on recovery the agent re-derives and re-speaks
the current state's prompt, so a crash mid-utterance can't strand the caller on a
half-heard offer. These strings are also the "facts" the LLM paraphrases (TRD
§6.2) — the LLM rewords them for tone, it never invents the content.

`canonical_prompt` returns text only for states that actually speak. States that
are silent (LISTENING) or have no utterance (DROPPED/RESUMING/CALL_ENDED) raise,
so a caller can't accidentally ask for a prompt that shouldn't exist.
"""
from __future__ import annotations

from .config import BALANCE
from .models import Branch, CallState, State


def _objection_prompt(branch: Branch | None) -> str:
    if branch == Branch.CANT_PAY_FULL:
        return (
            "I understand paying the full amount today is difficult. "
            "What amount would be manageable for you right now?"
        )
    if branch == Branch.WANTS_LATER:
        return (
            "I hear that you need a little more time. "
            "When do you expect to be able to make a payment?"
        )
    if branch == Branch.DISPUTE:
        return (
            "I want to make sure we get this right. Can you tell me what "
            "specifically you don't recognize about this charge?"
        )
    # UPSET never enters HANDLING_OBJECTION (TRD §4.3); None means unclassified.
    raise ValueError(f"no HANDLING_OBJECTION prompt for branch {branch}")


def _offer_prompt(offer: dict | None) -> str:
    if not offer:
        raise ValueError("OFFER_PROPOSED requires a computed offer")
    if offer["type"] == "installment_plan":
        amounts = " and ".join(f"${a}" for a in offer["installment_amounts"])
        return (
            f"Here's what I can do: {offer['installments']} installments of "
            f"{amounts}, with the first due in {offer['first_due_days']} days. "
            "Does that work for you?"
        )
    if offer["type"] == "extension":
        return (
            f"I can extend your due date by {offer['extension_days']} days to "
            "give you more time. Does that work for you?"
        )
    raise ValueError(f"unknown offer type {offer.get('type')!r}")


def canonical_prompt(cs: CallState) -> str:
    """The base utterance for `cs.current_state`, re-derivable on resume."""
    s = cs.current_state
    if s == State.OPENING:
        return (
            "Hi, this is a courtesy call about your account. Our records show a "
            f"past-due balance of ${BALANCE:.2f}. I'd like to help you take care "
            "of it today."
        )
    if s == State.HANDLING_OBJECTION:
        return _objection_prompt(cs.branch)
    if s == State.OFFER_PROPOSED:
        return _offer_prompt(cs.offer)
    if s == State.PAYMENT_SCHEDULED:
        return (
            "Great — I've scheduled that payment for you. You'll receive a "
            "confirmation shortly. Thank you."
        )
    if s == State.ESCALATED:
        return (
            "I understand. Let me connect you with a specialist who can help "
            "further — please hold."
        )
    raise ValueError(f"no canonical prompt for state {s}")
