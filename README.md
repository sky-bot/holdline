# holdline
I built a small collections voice agent focused on the two things that break in production — barge-in and dropped calls — with measured interruption latency and live state recovery.

## Docs

- [Project spec](docs/project-spec.md) — scope, requirements, timeline, cost
- [TRD](docs/TRD.md) — build-ready technical design: architecture, state machine, data model, latency instrumentation, crash recovery, testing plan

## Status

Early build. The dependency-free orchestration core is implemented and unit-tested; the real-time audio pipeline (LiveKit + Deepgram STT + Cartesia TTS) is validated end to end; and a headless harness can capture the agent's audio and detect when it goes silent — the two riskiest unknowns are cleared. Wiring the FSM into the pipeline, then the barge-in/recovery instrumentation, are next.

- `agent/` — orchestration logic (pure, no external services yet):
  - `fsm.py` — the explicit state machine (states, transitions, branch routing)
  - `offer.py` — deterministic offer computation (terms decided in code, not by the LLM)
  - `prompts.py` — canonical, re-derivable prompts (enables exact resume)
  - `classify.py` — keyword classification with precedence + LLM-fallback hook
  - `models.py`, `config.py` — shared types and fixed constants
- `db/schema.sql` — Postgres schema (call state, transitions, latency/recovery events)
- `scripts/` — dev tooling:
  - `check_livekit.py` — LiveKit credential smoke test (authenticates, prints no secrets)
  - `spike_pipeline.py` — minimal STT/TTS round-trip agent (proven-working pipeline reference)
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
