# Technical requirements & design brainstorm

Companion to [project-spec.md](project-spec.md). This is a first-pass design, not final — items marked **OPEN** need a decision before or during Day 4 (state machine day).

---

## 1. Architecture

**Deployment: LiveKit Cloud** (not self-hosted) — free tier covers this project entirely, no server/TURN setup to manage while learning the stack.

```
Caller (browser, WebRTC) <--> LiveKit Cloud room <--> Python agent worker (LiveKit Agents SDK)
                                                    |
                              +---------------------+---------------------+
                              |                      |                    |
                        Deepgram STT          Claude Haiku 4.5      Deepgram Aura-2 /
                        (streaming)           (per-branch           Cartesia TTS
                                               paraphrase)
                              |
                        Explicit FSM (in-process Python)
                              |
                        Postgres (Supabase/Neon) <-- state + transition log, every write
```

The FSM is plain code (a dict/enum-based transition table or a small state-machine library like `transitions`), not an LLM. The LLM's only job is generating the spoken utterance text for whatever branch/state the FSM already decided.

## 2. State machine (proposal)

**States**

| State | Meaning | Terminal? |
|---|---|---|
| `OPENING` | Scripted balance statement plays | no |
| `LISTENING` | Waiting for caller speech; also the state barge-in returns to | no |
| `HANDLING_OBJECTION` | LLM paraphrase within the classified branch | no |
| `OFFER_PROPOSED` | An offer (amount/date/plan) is on the table, awaiting accept/reject | no |
| `PAYMENT_SCHEDULED` | Outcome 1 | **yes** |
| `ESCALATED` | Outcome 2 | **yes** |
| `CALL_ENDED` | Wraps either terminal outcome, call torn down | **yes** |
| `DROPPED` | Transient — set when a simulated crash/disconnect is detected | no |
| `RESUMING` | Transient — set on process restart, before rejoining prior state | no |

**Data carried alongside state (not state itself)**, per FR4: `branch` (which objection type), `offer` (amount, date, plan — nullable until proposed), `turn_count`.

**Transitions (illustrative, not exhaustive)**

```
OPENING            --statement complete-->        LISTENING
LISTENING          --barge-in detected-->          LISTENING   (TTS halted, logged as barge-in event, not a call-state transition)
LISTENING          --objection classified as X-->  HANDLING_OBJECTION(branch=X)
HANDLING_OBJECTION --LLM/logic proposes offer-->   OFFER_PROPOSED
OFFER_PROPOSED     --caller accepts-->             PAYMENT_SCHEDULED
OFFER_PROPOSED     --caller rejects / upset-->     ESCALATED
HANDLING_OBJECTION --unresolved after N turns-->   ESCALATED
PAYMENT_SCHEDULED  --wrap-up-->                    CALL_ENDED
ESCALATED          --wrap-up-->                    CALL_ENDED
ANY (non-terminal) --simulated crash-->            DROPPED
DROPPED            --process restart, state loaded-->  RESUMING --> [prior non-terminal state]
```

**DECIDED — branch classification.** Hybrid: match the transcribed utterance against known scripted phrasing for each branch first (deterministic, zero added latency); fall back to a Haiku classification call only if nothing matches. Keeps the demo-day path fully deterministic while still handling unscripted phrasing credibly.

**DECIDED — escalation threshold.** 2 unresolved turns in `HANDLING_OBJECTION` before auto-escalating to `ESCALATED`.

## 3. Data model (Postgres)

```sql
calls (
  id            uuid primary key,
  room_name     text not null,
  started_at    timestamptz not null,
  ended_at      timestamptz,
  outcome       text  -- 'payment_scheduled' | 'escalated' | null while in progress
);

call_state (            -- current/mutable, one row per call, upserted on every transition
  call_id       uuid primary key references calls(id),
  current_state text not null,
  branch        text,
  offer         jsonb,   -- {amount, date, plan}
  turn_count    int default 0,
  updated_at    timestamptz not null
);

state_transitions (      -- append-only log, satisfies FR7
  id            bigserial primary key,
  call_id       uuid references calls(id),
  from_state    text,
  to_state      text,
  event         text,
  metadata      jsonb,
  occurred_at   timestamptz not null
);

barge_in_events (
  id                 bigserial primary key,
  call_id            uuid references calls(id),
  speech_onset_at    timestamptz not null,   -- VAD fires
  tts_halt_at        timestamptz not null,   -- halt command issued
  latency_ms         int not null
);
```

`call_state` is what recovery reads on restart (FR5); `state_transitions` is the audit log (FR7). Keeping them separate avoids conflating "what do I resume from" with "what happened historically."

**DECIDED — write timing.** Synchronous, awaited write on every transition. Async/fire-and-forget was rejected: it opens a race window where a crash could land after a transition but before the write flushes, which would show stale state on resume — directly undermining the exact-state-recovery result this project exists to prove. The Postgres round-trip is negligible next to the STT→LLM→TTS hops already in the pipeline.

## 4. Barge-in latency instrumentation

Three timestamps per interruption:
1. `t0` — VAD/turn-detection plugin fires "speech started" while TTS is playing.
2. `t1` — agent code issues the TTS-cancel/stop command.
3. `t2` (if obtainable from the TTS/audio track) — playback actually stops.

Primary reported metric: `t1 - t0` (detection-to-halt-command), since that's what the orchestration layer controls. Capture `t2` too if the SDK exposes it, and report both if they differ meaningfully. Log every one of the 15–20 test interruptions to `barge_in_events`; compute avg + worst-case for the README from that table rather than eyeballing logs.

**Interruption mechanism: hard stop.** On VAD speech-onset while TTS is playing, kill audio output immediately — no fade-out. This directly minimizes the reported metric; graceful fade is a conversational-polish concern this NFR isn't measuring.

**Test methodology: automated audio-injection harness**, not manual live talk-over. A script plays a pre-recorded clip at a controlled offset into a known TTS response, repeatably, for all 15–20 trials — making the reported latency distribution reproducible rather than a one-off hand-logged run. Lives at `scripts/inject_barge_in.py` (see repo structure, §7).

## 5. Crash / resume mechanism

**Process-crash path:**
- Dev-triggered kill (a CLI command or hotkey during the demo) terminates the agent worker for that room.
- LiveKit room itself persists independently of the agent process (it's server-side), so the caller's WebRTC connection isn't necessarily dropped — this is closer to "the brain died, the phone line didn't."
- On restart, a new agent process either rejoins the existing room or a dispatch check finds a `call_state` row with a non-terminal `current_state` and reattaches to it.
- It reads `call_state` (branch, offer, turn_count), sets `RESUMING`, and re-enters the FSM at the persisted state — no replay of the opening statement.

**Client-reconnect path:**
- LiveKit's transport layer already handles WebRTC reconnection. The agent needs to not tear down `call_state` on a participant-disconnect event within a grace window, and on rejoin, reconcile (log a transition, resume conversation) rather than restart.

**DECIDED — proving "exact pre-drop state" on video.** Split-screen recording: one pane runs the call/agent logs, the other tails `state_transitions`/`call_state` live (e.g. `watch psql ...` or a small script). The viewer sees `branch`/`offer` before the crash, the crash and restart, and the resumed state land in the same row — continuous proof, not a narrated claim, and it doubles as the Postgres-log deliverable.

## 6. LLM prompt design (per branch)

Each of the 3–4 objection branches gets a fixed system-prompt template, not a shared general-purpose prompt:
- The branch's negotiation goal (e.g., "propose a payment plan splitting the balance over 2–3 installments").
- Allowed moves only (date extension, partial payment plan) — an explicit **do-not** list (no new legal terms, no waiving balance, no amounts outside a defined range).
- 1–2 example utterances to anchor tone.

The LLM paraphrases within the branch the FSM already selected — it never decides the branch or the transition itself. This is what "LLM paraphrases, does not freelance" (Section 2 of the PRD) means concretely.

**Context window: minimal, not full transcript.** Each call passes only `{branch, last user utterance, current offer state}` — not the running conversation history. Cheaper and faster per Haiku call, and shrinks the surface area the model has to drift off-script with; a growing transcript would give it more room to improvise beyond the branch's intended script.

## 7. Repo structure (proposal)

```
holdline/
  agent/               # LiveKit Agents SDK worker (Python)
    main.py
    fsm.py             # explicit state machine
    branches/          # per-branch prompt templates
    persistence.py     # Postgres read/write for call_state + state_transitions
    latency.py         # barge-in instrumentation
  db/
    schema.sql
  docs/
    project-spec.md
    technical-requirements.md
  scripts/
    simulate_crash.sh    # kills the agent process for a demo
    inject_barge_in.py   # automated audio-injection harness for latency testing
  README.md
```

## 8. Decisions log

| # | Question | Decision |
|---|---|---|
| 1 | Branch classification method | Hybrid: keyword/phrase match first, Haiku LLM fallback |
| 2 | Escalation turn-count threshold | 2 unresolved turns |
| 3 | Sync vs async Postgres writes | Synchronous, awaited |
| 4 | Proof of exact-state recovery on video | Split-screen: agent logs + live DB tail |
| 5 | LiveKit deployment | Cloud (not self-hosted) |
| 6 | Barge-in latency test methodology | Automated audio-injection harness, not manual talk-over |
| 7 | TTS interruption mechanism | Hard stop, no fade-out |
| 8 | LLM context window per turn | Minimal: branch + last utterance + offer state only |
