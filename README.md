# holdline
I built a small collections voice agent focused on the two things that break in production — barge-in and dropped calls — with measured interruption latency and live state recovery.

## Docs

- [Project spec](docs/project-spec.md) — scope, requirements, timeline, cost
- [TRD](docs/TRD.md) — build-ready technical design: architecture, state machine, data model, latency instrumentation, crash recovery, testing plan

## Status

Early build. Both headline capabilities work end to end: (1) the **FSM-driven agent runs a full call** — a live caller negotiated a payment plan and the state machine drove every transition (STT → classify → FSM → offer → prompt → TTS); and (2) **exact-state crash recovery** — every transition is written synchronously to Postgres, and a freshly-dispatched agent resumes a non-terminal call from its persisted state, re-speaking the current prompt (verified: an agent seeded at `OFFER_PROPOSED` resumed and re-delivered the offer, with the generation fence bumped). Next: LLM paraphrasing of the prompts, then barge-in instrumentation.

- `agent/` — the orchestration layer:
  - `main.py` — the FSM-driven agent: LiveKit pipeline in, `fsm.py` drives the conversation, prompts spoken directly (no LLM yet); persists every transition and resumes on restart when `DATABASE_URL` is set
  - `fsm.py` — the explicit state machine (states, transitions, branch routing)
  - `offer.py` — deterministic offer computation (terms decided in code, not by the LLM)
  - `prompts.py` — canonical, re-derivable prompts (enables exact resume)
  - `classify.py` — keyword classification with precedence + LLM-fallback hook
  - `persistence.py` — synchronous Postgres writes + recovery lookup + generation fencing (TRD §8)
  - `models.py`, `config.py` — shared types and fixed constants
- `db/schema.sql` — Postgres schema (call state, transitions, latency/recovery events)
- `scripts/` — dev tooling:
  - `check_livekit.py` — LiveKit credential smoke test (authenticates, prints no secrets)
  - `apply_schema.py` — apply `db/schema.sql` to `DATABASE_URL`
  - `spike_pipeline.py` — minimal STT/TTS round-trip agent (proven-working pipeline reference)
  - `simulate_crash.sh` — SIGKILL the agent mid-call to demo recovery
- `harness/caller_client.py` — headless measurement client: joins a room, subscribes to the agent's audio, and detects audio start/stop on a single clock (the foundation of the end-to-end barge-in number)

## Development

Requires Python 3.9+. The real-time agent runtime targets Linux (WSL2 on Windows) — the pure core and tests run anywhere.

**Run the tests (no external services needed):**
```bash
python -m venv .venv
# Windows: .venv/Scripts/python -m pip install -e ".[dev]"
# Unix:    .venv/bin/python  -m pip install -e ".[dev]"
python -m pytest
```

**Run the agent / spikes (needs credentials):**
```bash
pip install -e .                       # installs the LiveKit + plugin runtime deps
cp .env.example .env                   # then fill in the keys (gitignored — never commit real keys)
python scripts/check_livekit.py        # smoke-test the LiveKit credentials
python scripts/spike_pipeline.py console   # talk to the STT/TTS pipeline locally
```
