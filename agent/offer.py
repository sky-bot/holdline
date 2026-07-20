"""Deterministic offer computation (TRD §6.2).

This module is the load-bearing guardrail of the whole "LLM doesn't freelance"
claim: offer terms are decided *here*, in code, from the fixed bounds in
`config`, with no LLM involved. The FSM calls `compute_offer` at the moment it
transitions to OFFER_PROPOSED and persists the result; the LLM later only
*phrases* that already-decided offer.

Money is handled with `Decimal`, never float, and installment amounts are split
so they sum back to the balance to the cent — a wrong total on the one number the
demo puts on screen would undercut the exact-recovery story.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .config import (
    BALANCE,
    DEFAULT_EXTENSION_DAYS,
    DEFAULT_FIRST_DUE_DAYS,
    DEFAULT_INSTALLMENTS,
)
from .models import Branch

_CENT = Decimal("0.01")


def _split_balance(total: Decimal, installments: int) -> list[str]:
    """Split `total` into `installments` amounts that sum back exactly.

    All but the last installment are the balance divided evenly and rounded to
    the cent; the final installment absorbs the rounding remainder so the parts
    reconstruct the total precisely (e.g. 482.17 / 2 -> 241.09 + 241.08).
    """
    if installments < 1:
        raise ValueError("installments must be >= 1")
    base = (total / installments).quantize(_CENT, rounding=ROUND_HALF_UP)
    amounts = [base] * (installments - 1)
    last = total - sum(amounts, Decimal("0"))
    amounts.append(last)
    return [f"{a:.2f}" for a in amounts]


def compute_offer(branch: Branch, turn_count: int | None = None) -> dict:
    """Return the concrete offer for a negotiable branch.

    `turn_count` is accepted to match the TRD signature and leave room for
    turn-dependent offers later, but in this scoped demo the offer is a pure
    function of `branch` alone. Raises `ValueError` for non-negotiable branches
    (DISPUTE / UPSET), which have no offer by design.
    """
    if branch == Branch.CANT_PAY_FULL:
        return {
            "type": "installment_plan",
            "installments": DEFAULT_INSTALLMENTS,
            "installment_amounts": _split_balance(BALANCE, DEFAULT_INSTALLMENTS),
            "total": f"{BALANCE:.2f}",
            "first_due_days": DEFAULT_FIRST_DUE_DAYS,
        }
    if branch == Branch.WANTS_LATER:
        return {
            "type": "extension",
            "extension_days": DEFAULT_EXTENSION_DAYS,
            "total": f"{BALANCE:.2f}",
        }
    raise ValueError(f"{branch} is not a negotiable branch; no offer to compute")
