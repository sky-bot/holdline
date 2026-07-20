# holdline
I built a small collections voice agent focused on the two things that break in production — barge-in and dropped calls — with measured interruption latency and live state recovery.

## Docs

- [Project spec](docs/project-spec.md) — scope, requirements, timeline, cost
- [TRD](docs/TRD.md) — build-ready technical design: architecture, state machine, data model, latency instrumentation, crash recovery, testing plan

## Status

Early build. The dependency-free orchestration core is implemented and unit-tested; the LiveKit/STT/TTS/LLM integration and instrumentation are next.

- `agent/` — orchestration logic (pure, no external services yet):
  - `fsm.py` — the explicit state machine (states, transitions, branch routing)
  - `offer.py` — deterministic offer computation (terms decided in code, not by the LLM)
  - `prompts.py` — canonical, re-derivable prompts (enables exact resume)
  - `classify.py` — keyword classification with precedence + LLM-fallback hook
  - `models.py`, `config.py` — shared types and fixed constants
- `db/schema.sql` — Postgres schema (call state, transitions, latency/recovery events)

## Development

Requires Python 3.9+.

```bash
python -m venv .venv
# Windows: .venv/Scripts/python -m pip install -e ".[dev]"
# Unix:    .venv/bin/python  -m pip install -e ".[dev]"
python -m pytest
```

Copy `.env.example` to `.env` for the runtime integration (gitignored — never commit real keys).
