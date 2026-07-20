"""State machine transitions (TRD §4.5, §4.3, §4.4, §4.6, §13)."""
import pytest

from agent.fsm import (
    BranchClassified,
    InvalidTransition,
    ObjectionTurn,
    OfferDecision,
    OpeningComplete,
    WrapUp,
    advance,
)
from agent.models import Branch, CallState, State


def _opening():
    return CallState(current_state=State.OPENING)


def test_opening_to_listening():
    t = advance(_opening(), OpeningComplete())
    assert t.to_state == State.LISTENING
    assert t.event == "opening_complete"


# --- Branch routing from LISTENING (TRD §4.3) ---


@pytest.mark.parametrize("branch", [Branch.CANT_PAY_FULL, Branch.WANTS_LATER])
def test_negotiable_branch_enters_handling(branch):
    cs = CallState(current_state=State.LISTENING)
    t = advance(cs, BranchClassified(branch))
    assert t.to_state == State.HANDLING_OBJECTION
    assert t.call_state.branch == branch
    assert t.call_state.turn_count == 0


def test_dispute_enters_handling():
    t = advance(CallState(current_state=State.LISTENING), BranchClassified(Branch.DISPUTE))
    assert t.to_state == State.HANDLING_OBJECTION
    assert t.call_state.branch == Branch.DISPUTE


def test_upset_escalates_immediately_and_records_reason():
    t = advance(CallState(current_state=State.LISTENING), BranchClassified(Branch.UPSET))
    assert t.to_state == State.ESCALATED
    assert t.call_state.branch == Branch.UPSET
    assert t.call_state.notes == {"reason": "caller upset"}


def test_barge_in_during_opening_collapses_into_classification():
    # TRD §4.6: a classified utterance is valid straight from OPENING.
    t = advance(_opening(), BranchClassified(Branch.CANT_PAY_FULL))
    assert t.to_state == State.HANDLING_OBJECTION


# --- HANDLING_OBJECTION exits (TRD §4.3) ---


@pytest.mark.parametrize("branch", [Branch.CANT_PAY_FULL, Branch.WANTS_LATER])
def test_negotiable_asks_once_then_offers(branch):
    cs = CallState(current_state=State.HANDLING_OBJECTION, branch=branch, turn_count=0)
    t1 = advance(cs, ObjectionTurn())
    assert t1.to_state == State.HANDLING_OBJECTION  # clarifying question, stays
    assert t1.call_state.turn_count == 1
    t2 = advance(t1.call_state, ObjectionTurn())
    assert t2.to_state == State.OFFER_PROPOSED       # offer by turn 2
    assert t2.call_state.offer is not None


def test_negotiable_never_escalates_from_indecision():
    # No matter the utterances, negotiable branches land on an offer, never
    # escalate for "no resolution" (TRD §4.3 rationale).
    cs = CallState(current_state=State.HANDLING_OBJECTION, branch=Branch.CANT_PAY_FULL)
    for _ in range(5):
        t = advance(cs, ObjectionTurn())
        cs = t.call_state
        if cs.current_state == State.OFFER_PROPOSED:
            break
    assert cs.current_state == State.OFFER_PROPOSED


def test_dispute_collects_detail_then_escalates():
    cs = CallState(current_state=State.HANDLING_OBJECTION, branch=Branch.DISPUTE, turn_count=0)
    t1 = advance(cs, ObjectionTurn(detail="I never opened this account"))
    assert t1.to_state == State.HANDLING_OBJECTION
    assert t1.call_state.notes["dispute_reason"] == "I never opened this account"
    t2 = advance(t1.call_state, ObjectionTurn(detail="the amount is wrong too"))
    assert t2.to_state == State.ESCALATED
    assert "amount is wrong" in t2.call_state.notes["dispute_reason"]


# --- OFFER_PROPOSED exits, no counteroffer loop (TRD §4.4) ---


def _at_offer():
    return CallState(current_state=State.OFFER_PROPOSED, branch=Branch.CANT_PAY_FULL, offer={"type": "extension"})


def test_offer_accepted_schedules_payment():
    t = advance(_at_offer(), OfferDecision(accepted=True))
    assert t.to_state == State.PAYMENT_SCHEDULED


def test_offer_rejected_escalates_no_loop():
    t = advance(_at_offer(), OfferDecision(accepted=False))
    assert t.to_state == State.ESCALATED  # straight to escalation, never back to HANDLING


# --- Wrap-up ---


@pytest.mark.parametrize("terminal", [State.PAYMENT_SCHEDULED, State.ESCALATED])
def test_wrap_up_ends_call(terminal):
    t = advance(CallState(current_state=terminal), WrapUp())
    assert t.to_state == State.CALL_ENDED


# --- Purity & invalid transitions ---


def test_advance_does_not_mutate_input():
    cs = CallState(current_state=State.LISTENING)
    advance(cs, BranchClassified(Branch.DISPUTE))
    assert cs.current_state == State.LISTENING and cs.branch is None


@pytest.mark.parametrize(
    "cs, event",
    [
        (CallState(current_state=State.LISTENING), OpeningComplete()),
        (CallState(current_state=State.OPENING), OfferDecision(accepted=True)),
        (CallState(current_state=State.OFFER_PROPOSED, branch=Branch.CANT_PAY_FULL), ObjectionTurn()),
        (CallState(current_state=State.CALL_ENDED), WrapUp()),
        (CallState(current_state=State.HANDLING_OBJECTION, branch=Branch.UPSET), ObjectionTurn()),
    ],
)
def test_invalid_transitions_raise(cs, event):
    with pytest.raises(InvalidTransition):
        advance(cs, event)
