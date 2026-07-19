# Technical Requirements Document — Negotiation-Turn Voice Agent

Status: build-ready. Consolidates all decisions from [project-spec.md](project-spec.md) and the earlier design-brainstorm phase into one self-contained implementation spec — this is the single source of truth for the build.

This revision (v2) reworks the latency instrumentation around **end-to-end** measurement, closes a mid-utterance crash-recovery gap, extends split-brain fencing to audio, adds turn-latency and recovery-time as measured numbers, and removes every "if available / proposed / either" hedge in favor of a decided value. Build-priority phasing is in §16 so the added rigor doesn't silently overrun the one-week scope.

---

## 1. Purpose & scope recap

Portfolio project demonstrating orchestration-layer engineering for real-time voice AI, aimed at an audience that builds this for a living. It proves three measured capabilities most demo bots hand-wave:

1. **Barge-in responsiveness**, measured **end-to-end** — from the caller's speech onset to the agent's audio actually going silent at the caller, not just the in-process reaction time.
2. **Turn latency**, measured and decomposed — caller-stops-speaking to agent-audio-starts, broken into STT-final / LLM-first-token / TTS-first-byte / transport.
3. **Exact call-state recovery** after a dropped connection — a killed-and-restarted agent process, and a client-side WebRTC reconnect — reconstructing the precise pre-drop conversational position, including a prompt interrupted mid-delivery.

Single scripted call scenario, 4 objection branches, 2 outcomes. Full scope/timeline in [project-spec.md](project-spec.md); this document is technical design only.

**Why three numbers, not one:** barge-in latency is the flashy metric, but a voice-AI reviewer expects turn latency too — it's the number their product lives or dies on. Reporting only barge-in signals we optimized the demo-friendly metric and skipped the bread-and-butter one. Recovery time falls out of the crash demo for free, so we capture it.

## 2. Architecture

**Deployment: LiveKit Cloud.** No self-hosted infrastructure.

```
Caller — two distinct roles:
  (a) demo video:  human in a browser (web/index.html), WebRTC mic
  (b) measurement: headless LiveKit client (harness/caller_client.py) that
                   publishes audio and subscribes to the agent's output track
                   so it can timestamp injection and audio-cessation on ONE clock
        |
        v
   LiveKit Cloud room  <----------------  Python agent worker (LiveKit Agents SDK)
        |                                        |
        |                          +-------------+-------------+-------------+
        |                          |             |             |             |
        |                    Deepgram STT   Silero VAD +   Claude Haiku   Cartesia TTS
        |                    (streaming)    turn-detection  4.5 (phrase    (streaming;
        |                                   (barge-in +      pre-decided    Deepgram Aura-2
        |                                    endpointing)    offers &       fallback, §3)
        |                                        |           classify)
        |                                  Explicit FSM (in-process Python, agent/fsm.py)
        |                                        |
        |                                  Postgres (Supabase or Neon)
        |          calls / call_state / state_transitions / barge_in_events /
        |          turn_latency_events / recovery_events
        |
   data channel (clock-sync ping/echo, §7.4) between harness and agent
```

Dispatch is **explicit**: the "Start Call" backend endpoint creates the `calls` row, the initial `call_state` row, and the LiveKit room, then dispatches the agent worker into it — before the caller joins (§9). This gives the demo operator full control over timing.

## 3. Tech stack

| Layer | Choice | Note |
|---|---|---|
| Real-time transport | LiveKit Cloud (WebRTC) | |
| Agent runtime | Python, LiveKit Agents SDK | |
| STT | Deepgram, streaming | interim + final results both consumed (§6.1) |
| TTS | **Cartesia (streaming)**, primary | chosen for low streaming TTFB and fast, clean interruption/flush — the property the barge-in flex depends on |
| TTS fallback | Deepgram Aura-2 | switch only if Cartesia's measured flush tail (§7.2) is worse; decided by data, not preference |
| Barge-in + endpointing | LiveKit built-in Silero VAD + turn-detection plugin | one plugin supplies both speech-onset (barge-in) and end-of-turn (endpointing) events |
| LLM | Claude Haiku 4.5 | |
| Branch classification | Hybrid: keyword/phrase match, Haiku fallback | on final transcript (§6.1) |
| State machine | Explicit code (`agent/fsm.py`), not LLM-driven | |
| Storage | Postgres — Supabase or Neon free tier | |
| Hosting | Render or Fly.io free tier | web trigger page + agent worker |
| Dev environment | WSL2 | |

## 4. State machine

### 4.1 States

| State | Meaning | Terminal? |
|---|---|---|
| `OPENING` | Scripted balance statement plays | no |
| `LISTENING` | Waiting for caller speech before any branch has been classified | no |
| `HANDLING_OBJECTION` | Branch-specific handling (negotiating an offer, or collecting dispute detail) | no |
| `OFFER_PROPOSED` | An offer is on the table, awaiting accept/reject | no |
| `PAYMENT_SCHEDULED` | Outcome 1 | **yes** |
| `ESCALATED` | Outcome 2 | **yes** |
| `CALL_ENDED` | Wraps either terminal outcome, call torn down | **yes** |
| `DROPPED` | Transient — set when a simulated crash/disconnect is detected | no |
| `RESUMING` | Transient — set on process restart, before re-entering the prior state | no |

### 4.2 Data carried alongside state (FR4)

Stored in `call_state`, not encoded as separate FSM states:

- `branch` — `CANT_PAY_FULL` | `WANTS_LATER` | `DISPUTE` | `UPSET` | null before classification. Set at classification time regardless of which path follows — including `UPSET`, which never enters `HANDLING_OBJECTION` — so the field always reflects what was detected.
- `offer` (jsonb, nullable) — `{amount, installments, first_due_days}` for `CANT_PAY_FULL`, `{extension_days}` for `WANTS_LATER`, null for `DISPUTE`/`UPSET`. Computed in code and persisted **before** the offer is ever spoken (§4.7, §6.2).
- `notes` (jsonb, nullable) — context handed to a human on escalation (dispute reason, or an upset-caller flag).
- `turn_count` — increments once per caller utterance while in `HANDLING_OBJECTION`.

### 4.3 Per-branch routing

Not all branches behave the same way in `HANDLING_OBJECTION` — branch-specific behavior, not a uniform "escalate after N turns":

| Branch | Behavior in `HANDLING_OBJECTION` | Exit |
|---|---|---|
| `CANT_PAY_FULL` | LLM may ask one clarifying question (turn 1); by turn 2, `agent/offer.py` computes a concrete installment plan from fixed bounds (§6.4) and the LLM phrases it — it never chooses the numbers | always → `OFFER_PROPOSED` within 2 turns |
| `WANTS_LATER` | Same pattern; `agent/offer.py` computes a concrete date extension, LLM phrases it | always → `OFFER_PROPOSED` within 2 turns |
| `DISPUTE` | LLM asks clarifying questions to collect dispute detail, up to 2 turns | always → `ESCALATED`, never → `OFFER_PROPOSED`; detail written to `notes.dispute_reason` |
| `UPSET` | None — bypasses `HANDLING_OBJECTION` entirely | immediate `ESCALATED`; `notes.reason = "caller upset"` |

**Rationale:** negotiable branches always land on an offer within a bounded number of turns — never escalate from indecision, since "retry / no-resolution" is out of scope. `DISPUTE` collects context before an unavoidable human handoff. `UPSET` hands off immediately — scripting a bot to "handle" an upset caller reads as tone-deaf.

### 4.4 `OFFER_PROPOSED` exits (no counteroffer loop)

Exactly two exits — no loop back to `HANDLING_OBJECTION` for a counteroffer (out of scope):

- Caller accepts ("yes" / "that works" / "okay") → `PAYMENT_SCHEDULED`
- Caller rejects ("no" / "can't do that" / "not enough") → `ESCALATED`, single rejection

### 4.5 Full transition table

**Barge-in is not a `current_state` transition.** It's an audio-layer action — halt TTS output — that can fire in any state where TTS is playing. `current_state` never changes because of it; the FSM stays where it logically was, with the mic re-opened, and the event is logged to `barge_in_events` (§7), not `state_transitions`. This decouples "halt TTS" (audio layer) from `LISTENING` (a conversation-flow state). See §4.6 for per-state behavior.

```
OPENING            --statement complete-->              LISTENING
LISTENING          --classified CANT_PAY_FULL/WANTS_LATER--> HANDLING_OBJECTION(branch=X)
LISTENING          --classified DISPUTE-->               HANDLING_OBJECTION(branch=DISPUTE)
LISTENING          --classified UPSET-->                 ESCALATED   (notes.reason="caller upset")
HANDLING_OBJECTION --CANT_PAY_FULL/WANTS_LATER, offer computed (<=2 turns)--> OFFER_PROPOSED
HANDLING_OBJECTION --DISPUTE, detail collected (<=2 turns)--> ESCALATED   (notes.dispute_reason=...)
OFFER_PROPOSED     --caller accepts-->                   PAYMENT_SCHEDULED
OFFER_PROPOSED     --caller rejects-->                   ESCALATED
PAYMENT_SCHEDULED  --wrap-up-->                          CALL_ENDED
ESCALATED          --wrap-up-->                          CALL_ENDED
ANY (non-terminal) --simulated crash-->                 DROPPED
DROPPED            --process restart, call_state loaded--> RESUMING --> [prior non-terminal state]
ANY (non-terminal) --client disconnect, 30s grace expires w/o rejoin (§8.3)--> CALL_ENDED (outcome=null)
```

### 4.6 Edge cases

- **Barge-in in `LISTENING` (no TTS playing):** no-op; nothing to halt, not logged.
- **Barge-in during `OPENING`:** the primary demo scenario. TTS halts; because the caller has now spoken, the FSM treats it as if the statement finished and advances straight to classification (§4.5 `LISTENING`-origin rows) without passing through an idle `LISTENING`. Collapses "statement complete" and "classify" into one turn.
- **Barge-in during `HANDLING_OBJECTION` / `OFFER_PROPOSED` TTS:** TTS halts, `current_state` unchanged, the new utterance is classified per whatever state was interrupted — interrupting the offer pitch with "no" is still an `OFFER_PROPOSED` rejection.
- **Crash during `OPENING`:** the `calls` row and an initial `call_state` (`current_state=OPENING`) are written at dispatch, before the statement speaks (§9), so there is always a row to resume from. Resume re-delivers `OPENING` from the top (§4.7).
- **Crash during a barge-in measurement:** `barge_in_events` is independent of `call_state`; a crash leaves that one sample incomplete (excluded from the report, §7.2), and does not affect call recovery.

### 4.7 Prompt delivery is idempotent — resume always re-speaks the current state's prompt

`current_state` alone does **not** capture whether the state's spoken prompt finished playing. Without handling this, a crash mid-utterance — e.g. after `OFFER_PROPOSED` is persisted but while "I can do two installments of $241—" is still playing — would resume into a state that is listening for accept/reject on an offer the caller never fully heard. Dead air, or a nonsensical resume, on the exact scene meant to prove recovery.

**Decision: every state's spoken prompt is a pure function of persisted `call_state`, and on resume the agent always re-delivers the current state's prompt from the top.** Concretely:

- `OPENING` → the fixed balance statement.
- `HANDLING_OBJECTION` → the current turn's line, derived from `branch` + `turn_count`.
- `OFFER_PROPOSED` → the offer pitch, derived from the persisted `offer` (already computed and stored before it was first spoken).
- Terminal states → their fixed wrap-up line.

Because the prompt is always re-derivable and re-spoken, we do **not** track a partial-delivery flag. The tradeoff — if the crash landed *after* the prompt fully played, the caller hears it once more on resume — is deliberately accepted: a possible double-utterance is cheaper and far more robust than a `prompt_delivered` boolean that itself has to be persisted atomically and can desync. On camera it reads well (the agent "picks up talking where it was").

This is what makes the write-then-speak ordering in §8.1 correct: state is persisted first, so whatever prompt is about to play is always the one a resume would reconstruct.

## 5. Data model (Postgres)

```sql
create table calls (
  id            uuid primary key default gen_random_uuid(),
  room_name     text not null,
  started_at    timestamptz not null default now(),
  ended_at      timestamptz,
  outcome       text  -- 'payment_scheduled' | 'escalated' | null while in progress
);

create table call_state (             -- current/mutable, one row per call, upserted synchronously per transition
  call_id       uuid primary key references calls(id),
  current_state text not null,
  branch        text,                  -- CANT_PAY_FULL | WANTS_LATER | DISPUTE | UPSET | null
  offer         jsonb,                 -- {amount, installments, first_due_days} or {extension_days}
  notes         jsonb,                 -- context for human handoff on escalation
  turn_count    int  not null default 0,
  generation    int  not null default 1,  -- fencing token (§8.2); bumped when a process attaches/resumes this call
  updated_at    timestamptz not null default now()
);

create table state_transitions (      -- append-only audit log (FR7)
  id            bigserial primary key,
  call_id       uuid references calls(id),
  from_state    text,
  to_state      text,
  event         text,
  metadata      jsonb,
  occurred_at   timestamptz not null default now()
);

create table barge_in_events (        -- one row per interruption (§7.2)
  id                 bigserial primary key,
  call_id            uuid references calls(id),
  injected_at        timestamptz,      -- harness: when the interrupt clip started (harness clock). null for live/human
  speech_onset_at    timestamptz not null,  -- t0: VAD declares speech onset (server clock)
  tts_halt_at        timestamptz,      -- t1: cancel command issued (server clock). null if crash mid-event
  audio_stopped_at   timestamptz,      -- t2: harness observed agent audio cease (harness clock). null if crash mid-event
  end_to_end_ms      int,              -- audio_stopped_at - injected_at (single harness clock). THE headline number
  halt_decision_ms   int,              -- t1 - t0 (server clock). decomposition only, not the headline
  source             text not null default 'live'  -- 'live' | 'injected'
);

create table turn_latency_events (    -- one row per agent response turn (§7.3)
  id                 bigserial primary key,
  call_id            uuid references calls(id),
  turn_index         int  not null,
  user_stop_at       timestamptz not null,  -- endpoint/turn-detection fires "user done" (server clock)
  stt_final_at       timestamptz,           -- final transcript ready
  llm_first_token_at timestamptz,           -- Haiku first token
  tts_first_byte_at  timestamptz,           -- first TTS audio byte/frame produced
  first_audio_at     timestamptz,           -- harness observed first agent audio frame (harness clock)
  total_ms           int,                   -- first_audio_at - user_stop_at (clock-reconciled, §7.4)
  source             text not null default 'live'
);

create table recovery_events (        -- one row per simulated drop (§8.5)
  id              bigserial primary key,
  call_id         uuid references calls(id),
  path            text not null,       -- 'process_crash' | 'client_reconnect'
  crashed_at      timestamptz not null,-- harness triggered the kill/disconnect (harness clock)
  resumed_state   text not null,       -- current_state the agent came back into
  first_audio_at  timestamptz,         -- harness observed first agent audio after resume (harness clock)
  recovery_ms     int                  -- first_audio_at - crashed_at (single harness clock)
);

create index on state_transitions (call_id, occurred_at);
```

`call_state` is what recovery reads on restart (FR5); `state_transitions` is the audit log (FR7) — separate, so "what do I resume from" never conflates with "what happened." Which writes are synchronous vs. deliberately not is in §8.1.

## 6. Conversation & LLM design

### 6.1 Classification and STT: interim vs final

Two STT result types are consumed, for two different purposes — mixing them up is a classic voice-agent latency/accuracy bug, so it's decided explicitly here:

- **Interim (partial) transcripts drive the barge-in gate** (§7.1). They arrive fast; we only need "is this continued, substantive speech" to decide whether to halt, not a perfect transcript.
- **Final transcripts drive branch classification and accept/reject** (§4). Classifying on an interim risks misrouting on a half-heard phrase (e.g. "I can't—" classified before "—pay it, it's not even mine" arrives). Classification correctness matters more here than the ~100–300ms a final adds, and that wait is measured inside turn latency (§7.3), not hidden.

Classification order, on the **final** transcript:

1. **Keyword/phrase match** (case-insensitive substring/fuzzy):
   - `CANT_PAY_FULL`: "can't afford", "can't pay that much", "don't have that much", "too much money", "can't pay the full"
   - `WANTS_LATER`: "pay later", "next month", "after payday", "give me more time", "extension"
   - `DISPUTE`: "not mine", "don't recognize", "dispute", "never bought", "that's wrong", "not my charge"
   - `UPSET`: "ridiculous", "stop calling", "harassment" — sentiment doesn't reduce to keywords well, so this branch leans on the LLM fallback (note below)
2. **Haiku fallback** if no keyword match: returns one of the 4 labels, or "unclear" → the agent asks the caller to clarify and stays in `LISTENING`.

**Multi-match precedence** (e.g. "I can't pay this and it's not even mine" hits `CANT_PAY_FULL` and `DISPUTE`): fixed order **`DISPUTE` > `UPSET` > `CANT_PAY_FULL` > `WANTS_LATER`**. Misrouting a dispute into a payment-plan offer is the worse failure; upset is checked next since human handoff is always a safe fallback. Without an explicit rule the result depends on iteration order — an easy thing to get silently wrong on the recorded take.

Accept/reject in `OFFER_PROPOSED` uses the same hybrid approach on a separate phrase list. Classification runs at only two decision points: `LISTENING` (branch) and `OFFER_PROPOSED` (accept/reject). A turn-2 utterance inside `HANDLING_OBJECTION` isn't reclassified — the forced offer fires on turn count (§4.3).

**Note:** if `UPSET` keyword matching proves unreliable in testing, weight the Haiku fallback more heavily for that branch rather than growing the keyword list indefinitely.

### 6.2 LLM prompt design — offer terms computed in code, never by the LLM

This is the load-bearing guardrail, not prompt wording. `agent/offer.py` exposes a pure function `compute_offer(branch, turn_count) -> dict` that deterministically derives the installment plan or extension from the fixed bounds in §6.4 (e.g. `{amount: 482.17, installments: 2, first_due_days: 7}`), with no LLM call. The FSM calls it at the moment of transitioning to `OFFER_PROPOSED`, writes the result to `call_state.offer`, **then** speaks (§8.1). The LLM receives the already-decided offer as a fixed fact to phrase — it never chooses a number, and its output is never parsed back into `offer`.

Each branch gets its own fixed system-prompt template:

- **Goal** — phrase the pre-computed offer / collect dispute detail / (nothing for `UPSET`).
- **Given facts, not a range** — the prompt states the exact offer ("tell the caller: 2 installments of $241.09, first due in 7 days"), never "propose 2–3 installments of at least $100."
- **Do-not list** — no new legal terms, no waiving balance, no numbers other than those supplied, no promises outside the two outcomes. This backstops *wording* drift; it is not what keeps numbers in bounds — the code does that.
- **1–2 example utterances** to anchor tone.

### 6.3 Context window per turn

Minimal: `{branch, last user utterance, current offer, turn_count}` — no running transcript. Cheaper, faster, and less surface area to drift off-script.

### 6.4 Demo scenario data (fixed)

- Balance: **$482.17**
- `CANT_PAY_FULL`: 2–3 installments, minimum $100/installment. `compute_offer` default: 2 installments, first due in 7 days.
- `WANTS_LATER`: extension up to 30 days. `compute_offer` default: 14-day extension.

### 6.5 Endpointing (turn-taking)

Knowing when the caller is *done* speaking is delegated to the LiveKit turn-detection plugin's end-of-turn event — a plugin you enable and a threshold you set, not logic you write. Fix the **end-of-turn silence threshold at 500ms** (`ENDPOINT_SILENCE_MS`) for the demo — long enough not to cut the caller off mid-sentence, short enough to keep turn latency (§7.3) honest. This is called out (rather than left as a plugin default) because endpointing tuning is where most real voice-agent quality lives, and the reported turn-latency number starts from this event, so its definition can't be implicit.

## 7. Latency instrumentation (both headline metrics)

### 7.1 Barge-in mechanism — hard stop, gated against backchannel

On a VAD speech-onset **while TTS is playing**, the agent issues a hard stop on audio output (no fade-out) regardless of `current_state` (§4.5). Hard stop is chosen because it minimizes the reported end-to-end number; a graceful fade trades against the very metric this NFR measures.

**This mechanism is SDK-provided, not custom-built.** The LiveKit Agents session already interrupts TTS on user speech — you configure it (allow-interruptions on, hard stop), you don't implement it. Reinventing it would be wasted week; the engineering being demonstrated here is the *measurement* (§7.2), not the interrupt itself. *(Confirm the exact parameter names against your installed `livekit-agents` version — the capability is stable, the spelling varies by release.)*

**Backchannel gate — also an SDK knob.** Halting on *every* VAD onset is naive — VAD fires on "mhm", "okay", a cough, and an agent that stops talking every time the caller backchannels feels broken. The SDK exposes a minimum-interruption-duration setting for exactly this; set it to **150ms** (`BARGE_IN_CONFIRM_MS`, default 150) rather than writing your own debounce. Short acknowledgements below that window don't halt; genuine interruptions do. Two honest consequences, both stated rather than hidden:

- This 150ms is **included** in the reported end-to-end latency (§7.2) — we don't subtract it to make the number look better.
- The gate is kept deliberately short because collections biases toward responsiveness (a missed barge-in is worse than an occasional false halt). The knob exists precisely because this is a real, contestable tradeoff, and being able to show the number *with the gate on* is the point.

### 7.2 Barge-in metric — end-to-end, measured on one clock

The headline number is **`end_to_end_ms = audio_stopped_at − injected_at`**: from when the interrupt audio starts to when the agent's audio actually ceases at the caller. This is the number that matters, because issuing "stop" does **not** stop audio already in flight — buffered in the TTS service, the LiveKit track, the WebRTC jitter buffer, the client playout. Flushing that tail fast is the actual barge-in engineering; `t1 − t0` (in-process reaction) is a few milliseconds of function-call overhead and reporting it as the headline would signal we measured the wrong segment.

Measurement runs from the **harness** (`harness/caller_client.py`), a headless LiveKit client acting as the caller, so both boundary timestamps sit on **one clock** — no cross-domain subtraction:

- `injected_at` — the harness starts publishing the interrupt clip.
- `audio_stopped_at` — the harness, subscribed to the agent's output track, sees the last audio frame before silence.

Both are the harness's monotonic clock; `end_to_end_ms` needs no reconciliation. (It measures to frame-cessation at the client, marginally before physical playout — a well-defined, honestly-stated boundary.)

**Decomposition (secondary, for the write-up).** To show *where* the time goes, the server also logs `t0` (`speech_onset_at`) and `t1` (`tts_halt_at`). Placed on the harness timeline via the §7.4 clock-sync, they split the total into: **detection** (`t0 − injected_at`) · **decision** (`t1 − t0`) · **flush/transport** (`audio_stopped_at − t1`). These carry a ±½-RTT uncertainty from the clock offset and are labelled as such — they explain the headline, they don't replace it.

Reporting: average and worst-case (max) of `end_to_end_ms` over the injected trial set (§7.5), computed from `barge_in_events`, with the decomposition shown as a stacked breakdown.

### 7.3 Turn-latency metric — decomposed, mostly single-clock

Second headline number: **`total_ms = first_audio_at − user_stop_at`** — caller stops speaking to first agent audio at the caller. `user_stop_at` is the endpoint event (§6.5). The internal decomposition is all **server-clock**, so it's clean:

- **STT finalization**: `stt_final_at − user_stop_at`
- **LLM TTFT**: `llm_first_token_at − stt_final_at`
- **TTS TTFB**: `tts_first_byte_at − llm_first_token_at`
- **transport + playout**: `first_audio_at − tts_first_byte_at` (the only cross-clock leg; reconciled via §7.4)

**Source the decomposition from the SDK's metrics event, don't hand-place these timestamps.** The LiveKit Agents session emits a metrics event per turn carrying STT duration, LLM time-to-first-token, TTS time-to-first-byte, and end-of-utterance delay. Log those directly into `turn_latency_events` rather than instrumenting five callbacks by hand — the numbers are the ones the SDK already computes, and hand-instrumentation is both more work and more error-prone. The only piece the SDK can't give you is `first_audio_at` (that's caller-side); the harness supplies it. So the build is: SDK metrics for the breakdown + one harness timestamp for the end-to-end. This is the number a voice-AI reviewer looks for first; getting it near-free from the SDK is the point of leaning on it.

### 7.4 Clock reconciliation (for the cross-clock legs only)

Server timestamps (`t0`, `t1`, STT/LLM/TTS marks) live on the agent's clock; the harness boundary timestamps live on the harness clock. The two headline numbers are each single-clock by construction (§7.2, §7.3), so they need no reconciliation. For the cross-clock legs (barge-in decomposition; the transport leg of turn latency), the harness estimates the offset over a LiveKit **data channel**: harness sends a ping at `C1`, agent echoes its clock `S`, harness receives at `C2`; `offset ≈ S − (C1+C2)/2`, with uncertainty `≈ (C2−C1)/2` (half RTT). Sampled a handful of times per run, median taken. Any figure depending on this is reported with the ± uncertainty attached — never as if it were exact.

### 7.5 Test methodology — automated harness, not manual talk-over

`harness/caller_client.py` joins the room as the caller, plays the scripted opening trigger, then injects a pre-recorded interrupt clip at a configurable offset into a known agent TTS response, repeatably. It runs the full **20-trial** set with varied offsets, each logged with `source='injected'`. Reproducible — re-run it, get the same distribution — versus a one-off hand-logged session. The harness also drives the turn-latency and recovery-time runs (§8.5), since it already holds the caller-side clock.

Live human interruptions during the demo video are logged with `source='live'` as a sanity cross-check, but the **reported** numbers come from the injected set.

## 8. Crash & recovery mechanism

### 8.1 Write ordering and where writes are synchronous

**Ordering: persist state, then act.** On every transition the FSM (1) writes `call_state` + `state_transitions` synchronously, then (2) performs the state's side effect (speaks the prompt). Combined with the idempotent re-speak rule (§4.7), this makes resume deterministic: the persisted state always names the prompt that is about to play, so a crash between (1) and (2), or mid-(2), both recover to the same place — re-deliver the current state's prompt. The TTS side effect is therefore treated as replayable by design, because it sometimes will be replayed.

**`call_state` + `state_transitions`: synchronous, always**, in one awaited transaction before the FSM proceeds. Async/fire-and-forget is rejected: it opens a window where a crash lands after a transition but before the write flushes, showing stale state on resume — undermining the exact-recovery result. The Postgres round-trip is negligible next to the STT→LLM→TTS hops.

**`barge_in_events` / `turn_latency_events`: deliberately NOT in the hot path.** These are reporting tables; a lost sample on crash is harmless. Their timestamps are captured in-memory and the row is written *after* the action (the cancel command, the response) completes. Folding them into the synchronous policy would put a DB round-trip between VAD detection and the halt command — slowing the real interruption and inflating the exact number the project reports.

### 8.2 Process-crash path

1. `scripts/simulate_crash.sh` sends `SIGKILL` (not `SIGTERM`) to the agent worker for a given room, mid-call, and **blocks until the OS confirms the process has exited** (waits on the PID) before returning. The restart never begins while the old process might be alive — closing the obvious split-brain window on the one scene meant to prove reliability.
2. The LiveKit Cloud room persists independently of the agent process, so the caller's WebRTC connection isn't dropped — "the brain died, the phone line didn't."
3. A new agent process starts. On startup it finds the most recent `calls` row joined to `call_state` where `current_state` is non-terminal, reads `room_name`, and dispatches into that exact room. Single-call-at-a-time scope keeps this lookup unambiguous.
4. **Fencing (DB + audio).** Before writing or speaking anything, it atomically bumps the token: `update call_state set generation = generation + 1 where call_id = $1 returning generation`, and holds the returned value. Two enforcement points, not one:
   - **Every DB write** carries `where generation = <token>`; zero rows affected ⇒ this process has been superseded.
   - **Before starting any TTS utterance**, and on losing any fenced write, the process re-checks its token; on mismatch it **disconnects from the LiveKit room (stops publishing audio) and exits.** This is the fix for the failure that actually hurts a voice app: fencing only the DB would still let a zombie process *keep talking* into the room, so the caller hears two bots. The room disconnect, not just write-abstention, is what prevents that.
5. It reads `branch`, `offer`, `notes`, `turn_count`, sets `RESUMING`, logs the transition, then re-enters the FSM at the persisted `current_state` and **re-delivers that state's prompt** (§4.7) — no replay of the whole call, no lost negotiation context, no dead air after a half-spoken prompt.

### 8.3 Client-reconnect path

1. LiveKit's transport layer handles WebRTC reconnection natively.
2. The agent does **not** tear down `call_state` on a participant-disconnect within a **30s grace window** — it pauses.
3. On rejoin within the window it logs a `RESUMING` transition and re-delivers the current state's prompt (§4.7), same as §8.2 step 5. No new process attaches (the original stayed alive and paused), so the §8.2 step-4 generation bump does not apply to this path.
4. If the 30s window expires without rejoin, the call is marked `CALL_ENDED` (`outcome=null`, abandoned) — not one of the two demoed outcomes, no scripted line needed.

### 8.4 Demo proof

Split-screen recording: one pane runs the agent/call logs, the other tails `call_state` / `state_transitions` live (`watch -n1 "psql ... -c 'select current_state,branch,offer,notes from call_state where call_id=...'"`). The viewer watches `branch`/`offer`/`notes` before the crash, the crash and restart, and the resumed state land in the same row — continuous proof, and it doubles as the Postgres-log deliverable. The audio track shows the agent re-speaking the interrupted prompt (§4.7), which is the audible half of the proof.

### 8.5 Recovery-time metric

Because the harness drives the crash and holds the caller-side clock, recovery time is free to capture: **`recovery_ms = first_audio_at − crashed_at`** — from the kill/disconnect to the agent speaking again — logged to `recovery_events`, tagged `process_crash` or `client_reconnect`. Reported as a third measured number. It also honestly surfaces that a process-crash resume includes agent re-dispatch + pipeline re-subscription time (seconds, not milliseconds) — better to measure and state that than let the split-screen imply instant recovery.

## 9. Call initiation flow

1. A minimal static page (`web/index.html`, single "Start Call" button) served from the agent backend host.
2. On click, the backend, in order: generates a room name (`call-{uuid}`), creates the `calls` row with that `room_name` (satisfies the `not null` constraint), creates the initial `call_state` (`current_state=OPENING`), creates the LiveKit room, dispatches the agent worker, and returns a LiveKit access token.
3. The caller joins with that token (mic permission prompt here).
4. The agent, in the room, begins `OPENING`.

Manual trigger (over auto-dispatch-on-join) is chosen so the demo operator controls timing relative to screen recording. The measurement harness (§7.5) uses the same backend endpoint but joins programmatically instead of via the browser.

## 10. Repo structure

```
holdline/
  agent/
    main.py                 # entrypoint, LiveKit Agents SDK wiring
    fsm.py                  # explicit state machine (§4)
    prompts.py              # pure state -> prompt derivation, enables idempotent re-speak (§4.7)
    classify.py             # hybrid keyword + Haiku classification, on final transcript (§6.1)
    offer.py                # deterministic compute_offer(), no LLM (§6.2)
    branches/               # per-branch prompt templates (§6.2)
      cant_pay_full.py
      wants_later.py
      dispute.py
      upset.py
    persistence.py          # Postgres read/write; fencing-aware writes (§5, §8.1, §8.2)
    fencing.py              # generation-token check for DB writes AND room/audio (§8.2)
    instrumentation.py      # barge-in + turn-latency capture, in-memory then off-hot-path write (§7, §8.1)
    clocksync.py            # data-channel clock-offset estimation (§7.4)
  harness/
    caller_client.py        # headless LiveKit caller: injects interrupts, times audio cessation,
                            #   drives turn-latency and recovery runs (§7.5, §8.5)
    clips/                  # pre-recorded interrupt audio
  web/
    index.html              # "Start Call" button (§9)
  db/
    schema.sql              # §5
  docs/
    project-spec.md
    TRD.md
  scripts/
    simulate_crash.sh       # SIGKILL + wait-for-exit (§8.2)
    run_latency_suite.py    # runs the 20-trial barge-in + turn-latency sets, prints the report table
  .env.example              # §11
  README.md
```

## 11. Configuration & secrets

`.env` (gitignored; `.env.example` committed with placeholders):

```
LIVEKIT_URL=
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=
DEEPGRAM_API_KEY=
CARTESIA_API_KEY=
ANTHROPIC_API_KEY=
DATABASE_URL=            # Supabase or Neon connection string
BARGE_IN_CONFIRM_MS=150  # backchannel gate (§7.1)
ENDPOINT_SILENCE_MS=500  # endpointing threshold (§6.5)
```

No secrets committed at any point; `.env` loaded via `python-dotenv` in `agent/main.py`.

## 12. Non-functional requirements recap

- **Barge-in latency** — reported as **end-to-end** (`audio_stopped_at − injected_at`, §7.2), average and worst-case over 20 injected trials, with the detection/decision/flush decomposition. The <300ms target applies to this end-to-end number, gate included. *(Note: this is a stricter, more honest target than the earlier "detection-to-halt" framing, which measured a trivial in-process segment.)*
- **Turn latency** — reported as `first_audio_at − user_stop_at` (§7.3) with STT/LLM/TTS decomposition, same trial rigor.
- **Recovery time** — `first_audio_at − crashed_at` (§8.5), per path.
- **State recovery** reconstructs exact pre-drop state (`current_state`, `branch`, `offer`, `notes`, `turn_count`) including a mid-delivery prompt (§4.7), not an approximation — shown in the demo recording (§8.4).
- Single repo, documented, README leads with the measured numbers, not a feature list.

## 13. Testing & validation plan

- **FSM unit tests** — every transition in §4.5, the branch-specific `HANDLING_OBJECTION` exits (§4.3), the no-counteroffer rule (§4.4), and `OPENING`-barge-in collapsing into classification (§4.6).
- **`offer.py` unit tests** — `compute_offer()` is pure; test against the §6.4 bounds independent of LLM/FSM.
- **`prompts.py` unit tests** — every non-terminal state derives a well-formed prompt from a given `call_state` (§4.7); this is what makes idempotent resume trustworthy, so it's tested directly.
- **Classification tests** — keyword coverage per branch, the `DISPUTE > UPSET > CANT_PAY_FULL > WANTS_LATER` precedence on multi-match inputs, and unscripted-phrasing cases for the Haiku fallback; asserted on **final**-transcript inputs (§6.1).
- **Latency suite** — `scripts/run_latency_suite.py` runs the full 20-trial barge-in and turn-latency sets as pre-demo validation, not once; results committed to the README table.
- **Crash/resume tests** — a `simulate_crash.sh` run from each of `OPENING`, `HANDLING_OBJECTION`, and `OFFER_PROPOSED`, **including a crash deliberately triggered mid-prompt** to exercise §4.7 re-speak, before the final recording.
- **Fencing tests** — (a) start a second agent against the same `call_id` while the first is alive and confirm the older generation's writes are rejected; (b) confirm the superseded process disconnects from the room / stops audio, not merely stops writing (§8.2 step 4).

## 14. Assumptions & constraints

- Identity verification and balance lookup are hardcoded/scripted — no real customer data in the repo.
- `PAYMENT_SCHEDULED` / `ESCALATED` are a scripted TTS line + a state write — no real payment processing or human transfer.
- Browser-based WebRTC only; no PSTN/SIP (Twilio remains an optional, separately-scoped stretch, project-spec.md §9).
- No retry/counteroffer loop (§4.4) — a deliberate scope boundary, not an oversight.

## 15. Risks

- First-time LiveKit/WebRTC use remains the biggest schedule risk (project-spec.md §8). The added instrumentation in this revision widens scope, which is why §16 phases it.
- The end-to-end barge-in measurement depends on the harness reliably detecting audio cessation on the agent's track; if frame-level cessation is noisy, fall back to an energy-threshold on the subscribed track and state the method. This is the one measurement that must be solid — it's the headline.
- Injected-clip onset is cleaner than natural speech; live human trials (`source='live'`) are the cross-check, but injected numbers are what's reported and labelled as such.
- `UPSET` classification is the least deterministic branch (§6.1) — extra manual testing before the recorded demo.
- Clock-reconciliation uncertainty (§7.4) only touches the decomposition legs, never the two headline numbers — but if RTT is high/variable on the day, widen the reported ± rather than presenting a false-precise breakdown.

## 16. Build strategy for a hard one-week deadline

The scope here is deliberately ambitious — the goal is a core-engineering flex, so the full surface (both crash paths, live end-to-end measurement, hybrid classification, turn latency) is in. That only fits one week for a first-time LiveKit build if the week is spent on the flex and **not** on rebuilding what the SDK gives you. Two rules make that true.

### 16.1 Commodity vs. flex — know which is which

| The SDK gives you (configure, don't build) | Your flex (build well) |
|---|---|
| VAD, the barge-in interrupt mechanism, the min-interruption gate (§7.1) | The explicit FSM + branch routing (§4) |
| Endpointing / turn detection (§6.5) | Exact-state crash recovery: idempotent re-speak, write-then-speak, DB+audio fencing (§4.7, §8) |
| STT/LLM/TTS orchestration + the per-turn metrics event (§7.3) | The measurement rigor: single-clock end-to-end barge-in via the harness (§7.2, §7.5) |
| WebRTC reconnection transport (§8.3 step 1) | Code-computed offers (§6.2), sync state persistence (§8.1) |

Every hour spent reimplementing a left-column item is an hour stolen from the right column, which is the only column Domu is evaluating. If something in the left column feels like it needs custom code, that's the signal to re-read the SDK docs first.

### 16.2 Spike the two schedule-killers on Day 1 — before building features

The two things most likely to blow the week are both *unknowns*, not *work*, and a first-timer can't estimate them until they're touched:

1. **The harness capturing the agent's audio track and detecting frame-level cessation (§7.2).** This is the single riskiest build item and it's the headline number. Prototype it in isolation on Day 1 — a throwaway script that joins a room, subscribes to a track, and prints when audio starts/stops. If live frame-cessation proves stubborn, the fallback is recording the track and finding silence offline (same number, less real-time fiddliness) — decide that by Day 2, not Day 6.
2. **A bare agent session up on LiveKit Cloud with STT+TTS round-tripping (§2).** Account setup, tokens, and WebRTC quirks eat time unpredictably. Get "it says the balance and I can talk back" working end-to-end before any FSM or measurement code.

If both spikes land by end of Day 2, the rest is mostly the flex code you control. If either is still shaky, that's the early-warning signal to cut — and the first thing to cut is the **turn-latency decomposition** (keep the end-to-end barge-in and recovery numbers; note turn latency as in-progress). It's the most deferrable because it's a *second* number, not the core.

### 16.3 If time runs short, cut in this order

1. Barge-in decomposition + clock-sync (§7.2 secondary, §7.4) — the end-to-end number stands alone without it.
2. Turn-latency decomposition (§7.3) — keep whatever the SDK metrics event emits for free; drop the harness `first_audio_at` reconciliation if needed.
3. Cartesia-vs-Aura-2 comparison (§3) — pick one, move on.

Never cut, in any time crunch: the FSM correctness, exact-state recovery + fencing, and the single end-to-end barge-in number. Those three *are* the demo.
```
