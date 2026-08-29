"""Paraphrasing degrades gracefully to the canonical line (TRD §6.2)."""
import asyncio

import pytest

from agent import paraphrase


@pytest.fixture(autouse=True)
def _reset_module_state():
    # paraphrase caches a client / disabled flag across calls; reset per test.
    paraphrase._client = None
    paraphrase._disabled = False
    yield
    paraphrase._client = None
    paraphrase._disabled = False


def test_without_api_key_returns_canonical_unchanged(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    line = "Here's what I can do: 2 installments of $241.09 and $241.08, first due in 7 days."
    assert asyncio.run(paraphrase.paraphrase(line)) == line


def test_disabled_after_first_miss_short_circuits(monkeypatch):
    # Once it sees no key, it stays disabled without re-checking the env.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    asyncio.run(paraphrase.paraphrase("hi"))
    assert paraphrase._disabled is True
    assert paraphrase._get_client() is None
