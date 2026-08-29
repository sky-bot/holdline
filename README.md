# holdline

A collections voice agent demonstrating orchestration-layer engineering for real-time voice AI — the two things that break in production and most demos skip: **barge-in** (interrupting the agent) and **dropped-call recovery**. The conversation is driven by an **explicit state machine**, not an LLM.

Stack: LiveKit (WebRTC) · Deepgram (STT) · Cartesia (TTS) · Claude Haiku (per-branch paraphrasing) · Postgres · Python.

## Measured results

- **Barge-in latency ≈ 571 ms end-to-end** — average over 12 trials (539–629 ms), measured from a headless client injecting an interrupt to the agent's audio actually going silent, on a single clock (no cross-clock estimation). Getting there meant diagnosing and removing ~490 ms of SDK overhead — a 3 s AEC warmup and a cloud adaptive-interruption round-trip; the remainder is network round-trips, a deliberate 150 ms backchannel gate, and the TTS flush tail. It's the *honest* end-to-end number, not a narrow sub-metric.
- **Exact-state crash recovery** — every FSM transition is written synchronously to Postgres, so a killed agent, re-dispatched, resumes a non-terminal call from its exact persisted state and re-speaks the current prompt. A `generation` fencing token rejects a superseded process's writes. Verified live (agent resumed at `OFFER_PROPOSED`, re-delivered the offer, fence bumped).

Both proven live, plus a full FSM-driven negotiation call (STT → classify → FSM → offer → prompt → TTS) and 56 unit tests over the core logic.

## Docs
- [Project spec](docs/project-spec.md) — scope, requirements, cost
- [TRD](docs/TRD.md) — architecture, state machine, data model, latency instrumentation, crash recovery, testing plan

## Layout

- `agent/` — the orchestration layer:
  - `main.py` — the FSM-driven agent: LiveKit pipeline in, `fsm.py` drives the conversation; persists every transition and resumes on restart when `DATABASE_URL` is set; barge-in tuned for low latency (`aec_warmup_duration=0`, local VAD interruption); hangs up after the wrap-up line
  - `fsm.py` — the explicit state machine (states, transitions, branch routing)
  - `offer.py` — deterministic offer computation (terms decided in code, not by the LLM)
  - `prompts.py` — canonical, re-derivable prompts (enables exact resume)
  - `paraphrase.py` — Claude Haiku rewords the canonical line for tone (never the numbers); no-ops without `ANTHROPIC_API_KEY`
  - `classify.py` — keyword classification with precedence + LLM-fallback hook
  - `persistence.py` — synchronous Postgres writes + recovery lookup + generation fencing (TRD §8)
  - `models.py`, `config.py` — shared types and fixed constants
- `db/schema.sql` — Postgres schema (call state, transitions, latency/recovery events)
- `harness/` — headless measurement clients (no browser/mic):
  - `barge_in_suite.py` — publishes an interrupt clip mid-utterance and measures end-to-end barge-in latency over N trials
  - `caller_client.py` — subscribes to the agent's audio and detects start/stop on a single clock
  - `clips/interrupt.wav` — the interrupt clip
- `scripts/` — dev tooling: `check_livekit.py` (credential smoke test), `apply_schema.py`, `make_interrupt_clip.py`, `spike_pipeline.py`, `simulate_crash.sh`

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
