"""Fixed demo constants and tunables (TRD §6.4, §7.1, §6.5).

Kept in one place so the FSM, offer computation, and prompts all agree on the
same numbers, and so the env-overridable tunables have a single documented home.
The `_MS` values mirror the env vars in `.env.example`; the runtime agent will
read those over the environment, but the defaults here are the source of truth
for tests and for anything that runs without an `.env`.
"""
from __future__ import annotations

from decimal import Decimal

# --- Demo scenario data (TRD §6.4) ---
BALANCE: Decimal = Decimal("482.17")

# --- Negotiation bounds (TRD §4.3, §6.4) ---
# HANDLING_OBJECTION is entered by speaking one clarifying question (its canonical
# prompt); the caller's answer to it is the single in-branch turn, after which
# negotiable branches propose an offer and DISPUTE escalates. So the branch exits
# after this many caller turns = 1. (Counted as caller utterances, this matches
# the TRD's "within 2 turns" framing, which counts agent responses: the clarifying
# question is agent-turn 1, the offer is agent-turn 2.) Never "no resolution".
MAX_HANDLING_TURNS: int = 1

DEFAULT_INSTALLMENTS: int = 2
DEFAULT_FIRST_DUE_DAYS: int = 7
DEFAULT_EXTENSION_DAYS: int = 14

# --- Real-time tunables (TRD §7.1, §6.5); env-overridable at runtime ---
BARGE_IN_CONFIRM_MS: int = 150   # continued voiced speech before committing a hard stop
ENDPOINT_SILENCE_MS: int = 500   # end-of-turn silence threshold
