"""Shared types for the orchestration core (TRD §4).

`State` and `Branch` are `str`-backed enums so they serialize straight into the
`call_state` row (TRD §5) and compare cleanly in tests. `CallState` is a frozen
dataclass — the FSM never mutates it in place; every transition returns a fresh
one (see `fsm.advance`). That immutability is what makes "resume from persisted
state" trivially correct: the object you persisted is exactly the object you get
back.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class State(str, Enum):
    OPENING = "OPENING"
    LISTENING = "LISTENING"
    HANDLING_OBJECTION = "HANDLING_OBJECTION"
    OFFER_PROPOSED = "OFFER_PROPOSED"
    PAYMENT_SCHEDULED = "PAYMENT_SCHEDULED"
    ESCALATED = "ESCALATED"
    CALL_ENDED = "CALL_ENDED"
    DROPPED = "DROPPED"
    RESUMING = "RESUMING"


class Branch(str, Enum):
    CANT_PAY_FULL = "CANT_PAY_FULL"
    WANTS_LATER = "WANTS_LATER"
    DISPUTE = "DISPUTE"
    UPSET = "UPSET"


# Branches the agent will actually negotiate an offer with (TRD §4.3). DISPUTE
# and UPSET are handled but never produce an offer.
NEGOTIABLE: frozenset[Branch] = frozenset({Branch.CANT_PAY_FULL, Branch.WANTS_LATER})

# States from which no further transition is possible (TRD §4.1).
TERMINAL_STATES: frozenset[State] = frozenset(
    {State.PAYMENT_SCHEDULED, State.ESCALATED, State.CALL_ENDED}
)


@dataclass(frozen=True)
class CallState:
    """The mutable-per-call data (TRD §4.2), carried as an immutable snapshot.

    Mirrors the `call_state` table columns. `offer` and `notes` are plain dicts
    destined for jsonb columns; they are never edited in place — a transition
    builds a new dict and a new `CallState`.
    """

    current_state: State = State.OPENING
    branch: Optional[Branch] = None
    offer: Optional[dict] = None
    notes: Optional[dict] = None
    turn_count: int = 0
