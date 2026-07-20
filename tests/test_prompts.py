"""Canonical prompt derivation — makes idempotent resume trustworthy (TRD §4.7, §13)."""
import pytest

from agent.models import Branch, CallState, State
from agent.offer import compute_offer
from agent.prompts import canonical_prompt


def test_opening_prompt_states_the_balance():
    p = canonical_prompt(CallState(current_state=State.OPENING))
    assert "$482.17" in p


@pytest.mark.parametrize(
    "branch, needle",
    [
        (Branch.CANT_PAY_FULL, "manageable"),
        (Branch.WANTS_LATER, "more time"),
        (Branch.DISPUTE, "don't recognize"),
    ],
)
def test_objection_prompt_per_branch(branch, needle):
    cs = CallState(current_state=State.HANDLING_OBJECTION, branch=branch, turn_count=1)
    assert needle in canonical_prompt(cs)


def test_offer_prompt_is_derived_from_persisted_offer():
    # The whole point of §4.7: the offer pitch is reconstructable from call_state,
    # so a crash mid-pitch resumes to the identical utterance.
    offer = compute_offer(Branch.CANT_PAY_FULL)
    cs = CallState(current_state=State.OFFER_PROPOSED, branch=Branch.CANT_PAY_FULL, offer=offer)
    p = canonical_prompt(cs)
    assert "$241.09" in p and "$241.08" in p
    assert "7 days" in p


def test_offer_prompt_extension():
    offer = compute_offer(Branch.WANTS_LATER)
    cs = CallState(current_state=State.OFFER_PROPOSED, branch=Branch.WANTS_LATER, offer=offer)
    assert "14 days" in canonical_prompt(cs)


def test_terminal_prompts_exist():
    assert "scheduled" in canonical_prompt(CallState(current_state=State.PAYMENT_SCHEDULED))
    assert "specialist" in canonical_prompt(CallState(current_state=State.ESCALATED))


def test_reprompt_is_stable():
    # Re-deriving the prompt from the same state yields the identical string —
    # exactly what resume relies on.
    cs = CallState(current_state=State.OPENING)
    assert canonical_prompt(cs) == canonical_prompt(cs)


@pytest.mark.parametrize("state", [State.LISTENING, State.DROPPED, State.RESUMING, State.CALL_ENDED])
def test_silent_states_have_no_prompt(state):
    with pytest.raises(ValueError):
        canonical_prompt(CallState(current_state=state))


def test_offer_proposed_without_offer_raises():
    with pytest.raises(ValueError):
        canonical_prompt(CallState(current_state=State.OFFER_PROPOSED))
