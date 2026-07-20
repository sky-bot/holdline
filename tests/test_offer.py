"""compute_offer is pure and deterministic (TRD §6.2, §13)."""
from decimal import Decimal

import pytest

from agent.config import BALANCE
from agent.models import Branch
from agent.offer import compute_offer


def test_cant_pay_full_is_two_installments():
    offer = compute_offer(Branch.CANT_PAY_FULL)
    assert offer["type"] == "installment_plan"
    assert offer["installments"] == 2
    assert offer["first_due_days"] == 7
    assert offer["installment_amounts"] == ["241.09", "241.08"]


def test_installments_sum_back_to_balance_exactly():
    offer = compute_offer(Branch.CANT_PAY_FULL)
    total = sum(Decimal(a) for a in offer["installment_amounts"])
    assert total == BALANCE
    assert offer["total"] == "482.17"


def test_wants_later_is_a_date_extension():
    offer = compute_offer(Branch.WANTS_LATER)
    assert offer["type"] == "extension"
    assert offer["extension_days"] == 14
    assert offer["total"] == "482.17"


def test_deterministic_across_calls():
    assert compute_offer(Branch.CANT_PAY_FULL) == compute_offer(Branch.CANT_PAY_FULL)


@pytest.mark.parametrize("branch", [Branch.DISPUTE, Branch.UPSET])
def test_non_negotiable_branches_have_no_offer(branch):
    with pytest.raises(ValueError):
        compute_offer(branch)
