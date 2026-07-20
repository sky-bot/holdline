"""The explicit state machine (TRD §4).

This is the "actual thing being demonstrated": conversation flow is decided by
code with a defined transition table, never by an LLM inferring where it is.

Design notes:
- `advance(call_state, event) -> Transition` is a pure function. It never mutates
  its input and never does I/O; it returns a fresh `CallState` plus a label the
  caller logs to `state_transitions`. Persistence, TTS, and the LLM live outside.
- Illegal (state, event) combinations raise `InvalidTransition` rather than
  silently no-op — an explicit machine should reject what it doesn't model.
- Barge-in is deliberately absent here: it's an audio-layer action that never
  changes `current_state` (TRD §4.5). The one place it touches flow — a barge-in
  during OPENING — is modeled by allowing `BranchClassified` from OPENING, which
  collapses "statement complete" and "classify" into one step (TRD §4.6).
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .config import MAX_HANDLING_TURNS
from .models import NEGOTIABLE, Branch, CallState, State
from .offer import compute_offer


class InvalidTransition(Exception):
    """Raised when an event is not valid for the current state."""


# --- Events (the inputs that drive transitions) ---


@dataclass(frozen=True)
class OpeningComplete:
    """The scripted opening statement finished playing."""


@dataclass(frozen=True)
class BranchClassified:
    """A caller utterance was classified into an objection branch (TRD §6.1)."""

    branch: Branch


@dataclass(frozen=True)
class ObjectionTurn:
    """A caller turn while in HANDLING_OBJECTION; `detail` is optional context."""

    detail: str | None = None


@dataclass(frozen=True)
class OfferDecision:
    """The caller accepted or rejected the offer on the table."""

    accepted: bool


@dataclass(frozen=True)
class WrapUp:
    """Tear-down after a terminal outcome."""


@dataclass(frozen=True)
class Transition:
    """Result of `advance`: the new state plus audit fields for logging."""

    call_state: CallState
    from_state: State
    to_state: State
    event: str


def _to(cs: CallState, new: CallState, event: str) -> Transition:
    return Transition(call_state=new, from_state=cs.current_state, to_state=new.current_state, event=event)


def _route_branch(cs: CallState, branch: Branch) -> Transition:
    """LISTENING/OPENING + a classified branch (TRD §4.3, §4.5)."""
    if branch in NEGOTIABLE:
        new = replace(cs, current_state=State.HANDLING_OBJECTION, branch=branch, turn_count=0)
        return _to(cs, new, f"classified_{branch.value.lower()}")
    if branch == Branch.DISPUTE:
        new = replace(cs, current_state=State.HANDLING_OBJECTION, branch=branch, turn_count=0)
        return _to(cs, new, "classified_dispute")
    if branch == Branch.UPSET:
        notes = {**(cs.notes or {}), "reason": "caller upset"}
        new = replace(cs, current_state=State.ESCALATED, branch=branch, notes=notes)
        return _to(cs, new, "classified_upset")
    raise InvalidTransition(f"unhandled branch {branch}")


def _handle_objection_turn(cs: CallState, event: ObjectionTurn) -> Transition:
    """HANDLING_OBJECTION + a caller turn (TRD §4.3).

    Negotiable branches ask at most one clarifying question, then must land on an
    offer by `MAX_HANDLING_TURNS`. DISPUTE collects detail up to the same cap and
    always escalates. UPSET never reaches this state.
    """
    branch = cs.branch
    new_turns = cs.turn_count + 1

    if branch in NEGOTIABLE:
        if new_turns >= MAX_HANDLING_TURNS:
            offer = compute_offer(branch, new_turns)
            new = replace(cs, current_state=State.OFFER_PROPOSED, turn_count=new_turns, offer=offer)
            return _to(cs, new, "offer_proposed")
        new = replace(cs, turn_count=new_turns)  # stay; agent asks its clarifying question
        return _to(cs, new, "clarifying_question")

    if branch == Branch.DISPUTE:
        notes = dict(cs.notes or {})
        if event.detail:
            prior = notes.get("dispute_reason")
            notes["dispute_reason"] = f"{prior}; {event.detail}" if prior else event.detail
        if new_turns >= MAX_HANDLING_TURNS:
            new = replace(cs, current_state=State.ESCALATED, turn_count=new_turns, notes=notes)
            return _to(cs, new, "escalated_dispute")
        new = replace(cs, turn_count=new_turns, notes=notes)
        return _to(cs, new, "collect_dispute_detail")

    raise InvalidTransition(f"branch {branch} is not valid in HANDLING_OBJECTION")


def advance(cs: CallState, event) -> Transition:
    """Compute the next transition for `cs` given `event`. Pure; no I/O."""
    s = cs.current_state

    if isinstance(event, OpeningComplete):
        if s == State.OPENING:
            return _to(cs, replace(cs, current_state=State.LISTENING), "opening_complete")

    elif isinstance(event, BranchClassified):
        # Valid in LISTENING, and in OPENING (barge-in collapse, TRD §4.6).
        if s in (State.LISTENING, State.OPENING):
            return _route_branch(cs, event.branch)

    elif isinstance(event, ObjectionTurn):
        if s == State.HANDLING_OBJECTION:
            return _handle_objection_turn(cs, event)

    elif isinstance(event, OfferDecision):
        if s == State.OFFER_PROPOSED:
            to = State.PAYMENT_SCHEDULED if event.accepted else State.ESCALATED
            return _to(cs, replace(cs, current_state=to), "offer_accepted" if event.accepted else "offer_rejected")

    elif isinstance(event, WrapUp):
        if s in (State.PAYMENT_SCHEDULED, State.ESCALATED):
            return _to(cs, replace(cs, current_state=State.CALL_ENDED), "wrap_up")

    else:
        raise InvalidTransition(f"unknown event type {type(event).__name__}")

    raise InvalidTransition(f"event {type(event).__name__} is not valid in state {s.value}")
