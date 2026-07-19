# Project spec: negotiation-turn voice agent

**Purpose:** portfolio project for outreach to Domu Technology Inc. (YC S24). Not production — a scoped technical demonstration of orchestration-layer engineering for real-time voice AI.

**Target role fit:** maps to the JD's "orchestration layer that coordinates our voice AI systems" and "handle interruptions/barge-in and dropped calls gracefully."

**Deadline:** soft — days/weeks, not fixed. Prioritize both measured results being solid over hitting a specific date.

---

## 1. Objective

Prove two capabilities most demo voice bots skip:
1. Low-latency barge-in / interruption handling, measured with real numbers.
2. Call state persistence and recovery after a simulated dropped connection (both a server-side crash and a client-side network drop).

## 2. Scope

**In scope**
- Single call scenario: agent opens with a scripted balance statement, then handles one live negotiation turn.
- 3–4 scripted objection branches (can't pay full amount, wants to dispute, wants to pay later, gets upset). LLM paraphrases within these branches — it does not freelance.
- Two outcomes: payment scheduled, or escalate to a human.
- Explicit state machine (states + defined transitions in code, not an LLM inferring where it is).
- Barge-in detection with instrumented latency measurement.
- Call state persistence + resume after a simulated drop — both a killed/restarted agent process and a client-side reconnect.
- Postgres log of call state transitions.

**Out of scope**
- Identity verification and balance lookup (hardcoded/scripted).
- Retry / no-resolution branch.
- Open-ended negotiation beyond the scripted objection types.
- Real PSTN integration (optional stretch only).

## 3. Functional requirements

| ID | Requirement |
|----|-------------|
| FR1 | Agent opens each call with a scripted balance statement |
| FR2 | Agent handles 3–4 defined objection types via LLM-generated utterance within each branch |
| FR3 | Barge-in halts TTS playback and returns to listening state |
| FR4 | Call state (current branch, offer on table) persists continuously, not just at call end |
| FR5 | A resumed call after a dropped connection continues from persisted state, not from the start |
| FR6 | Call ends in one of two outcomes: schedule payment or escalate to human |
| FR7 | Every state transition is logged to Postgres with a timestamp |

## 4. Non-functional requirements

- Barge-in halt latency: target under 300ms, reported as measured average and worst case across 15–20 test interruptions.
- State recovery must reconstruct the exact pre-drop state, not an approximation.
- Single repo, documented, README leads with the two measured results (not a feature list).

## 5. Confirmed technical decisions

| Decision | Choice | Why |
|---|---|---|
| Agent language/SDK | Python, LiveKit Agents SDK | Most mature LiveKit SDK; first-class Deepgram/Cartesia/Claude plugins |
| Barge-in detection | LiveKit built-in VAD (Silero) + turn-detection plugin | Least code, well-tested, latency easy to instrument via SDK events |
| Drop simulation | Both: (1) kill/restart agent process, (2) client-side WebRTC reconnect | (1) is the core "crash recovery" flex and demos cleanly on camera; (2) is closer to a real dropped call |
| Local dev environment | WSL2 | LiveKit tooling/audio libs assume Linux; avoids Windows-specific dependency friction |

See [TRD.md](TRD.md) for full architecture, state machine, data model, and the build-ready technical design.

## 6. Milestones (timeline, padded for first-time LiveKit/WebRTC use)

| Day | Work |
|---|---|
| 0 | WSL2 + account setup (LiveKit, Deepgram, Claude API, Postgres provider) — de-risk before any agent logic |
| 1–3 | Bare LiveKit room + Deepgram STT/TTS round-trip working end-to-end for a scripted turn |
| 4 | State machine implemented explicitly; LLM wired in for utterance generation per branch |
| 5 | Barge-in handling + latency instrumentation |
| 6 | Dropped-call state persistence (both crash and reconnect paths) |
| 7 | Postgres logging, cleanup, README written around the two measured results |
| 8 | Record demo video: interruption mid-sentence, then a drop-and-resume, back to back |
| 9 | Buffer / outreach prep |

## 7. Deliverables

- Working demo video (interruption + drop-and-resume)
- Public repo with README leading with the latency number and recovery demo
- Postgres log of call state transitions (screenshot or sample export)

## 8. Risks

- Real PSTN/SIP integration is a time sink relative to its signal value — the browser-based WebRTC demo proves the same orchestration points without the extra week.
- Latency budget is tight across STT → LLM → TTS hops; test the pipeline early (Day 1–3), not at the end.
- First-time LiveKit/WebRTC use is the single biggest schedule risk — Day 0 account/environment setup and Day 1–3 pipeline stand-up are padded accordingly; if they run long, compress cleanup/buffer (Day 7/9), not the core build days.

## 9. Estimated cost

Assumes light usage: roughly 100–150 minutes of test calls, well within free tiers for every service below. Prices are current publicly listed rates as of July 2026 and can change.

| Service | Free allowance | Paid rate (if exceeded) | Estimated cost |
|---|---|---|---|
| LiveKit Cloud (Build tier) | 5,000 WebRTC min/mo, 1,000 agent-session min/mo, $2.50 inference credit/mo | $0.01/min agent session, ~$0.0004–0.0005/min WebRTC | $0 |
| Deepgram STT (streaming) | $200 signup credit | ~$0.0077/min | $0 (covered by credit) |
| TTS — Deepgram Aura-2 or Cartesia | Covered by same Deepgram credit, or Cartesia free trial | ~$0.016–0.023/min | $0–3 |
| LLM — Claude Haiku 4.5 | No free tier, pay-as-you-go | $1/M input tokens, $5/M output tokens | ~$1–3 |
| Postgres (Supabase or Neon) | Free tier (500MB–1GB) | N/A at this scale | $0 |
| Hosting (Render or Fly.io) | Free tier | N/A at this scale | $0 |
| **Core build total** | | | **$1–6** |

Optional stretch — real phone number via Twilio: ~$1.15/mo number + ~$0.013–0.014/min outbound. Adds ~$5–10 for a week of light testing. Worth it only if a real callable number materially strengthens the demo video.

*Covers infrastructure/API costs only, not your time.*
