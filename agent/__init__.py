"""holdline — negotiation-turn collections voice agent.

This package holds the orchestration-layer logic. The modules in this first
build are the dependency-free core (no LiveKit, no DB, no LLM calls): the state
machine, offer computation, prompt derivation, and keyword classification. They
are pure and unit-testable in isolation, per TRD §16.1 (the "flex" column).
"""
