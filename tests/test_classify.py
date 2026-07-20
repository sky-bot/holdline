"""Keyword classification, precedence, and word-boundary matching (TRD §6.1, §13)."""
import pytest

from agent.classify import classify, classify_decision, keyword_classify
from agent.models import Branch


@pytest.mark.parametrize(
    "text, expected",
    [
        ("I can't afford that", Branch.CANT_PAY_FULL),
        ("can you give me more time", Branch.WANTS_LATER),
        ("this charge is not mine", Branch.DISPUTE),
        ("this is harassment, stop calling", Branch.UPSET),
    ],
)
def test_single_branch_matches(text, expected):
    assert keyword_classify(text) == expected


def test_precedence_dispute_beats_cant_pay():
    # Hits both CANT_PAY_FULL and DISPUTE; DISPUTE wins (TRD §6.1).
    assert keyword_classify("I can't afford this and it's not mine") == Branch.DISPUTE


def test_precedence_order_upset_over_negotiable():
    # UPSET beats WANTS_LATER when both trip.
    assert keyword_classify("this is ridiculous, give me more time") == Branch.UPSET


def test_no_match_returns_none():
    assert keyword_classify("hello, who is this") is None


def test_word_boundary_avoids_false_positive():
    # "another" must not match the reject phrase "no"; no branch keywords either.
    assert keyword_classify("let me ask another question") is None


def test_classify_uses_llm_fallback_only_when_keywords_miss():
    calls = []

    def fallback(text):
        calls.append(text)
        return Branch.UPSET

    # Keyword hit -> fallback never called.
    assert classify("not mine", fallback) == Branch.DISPUTE
    assert calls == []
    # Keyword miss -> fallback consulted.
    assert classify("hmm I'm not sure", fallback) == Branch.UPSET
    assert calls == ["hmm I'm not sure"]


def test_classify_returns_none_without_fallback():
    assert classify("hmm I'm not sure") is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("yes that works", True),
        ("sure, sounds good", True),
        ("no, that's not enough", False),
        ("nope", False),
        ("I don't know", None),          # neither
        ("yes but no", None),            # ambiguous -> unclear
    ],
)
def test_offer_decision(text, expected):
    assert classify_decision(text) == expected


def test_decision_word_boundary_no_false_positive():
    # "know" contains "no" as a substring but must not read as a rejection.
    assert classify_decision("let me know") is None
