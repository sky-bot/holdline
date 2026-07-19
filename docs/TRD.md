# Technical Requirements Document — Negotiation-Turn Voice Agent

Status: build-ready draft. Consolidates all decisions from [project-spec.md](project-spec.md) and [technical-requirements.md](technical-requirements.md) into one self-contained implementation spec. Where this document and the earlier brainstorm doc disagree, this document wins.

---

## 1. Purpose & scope recap

Portfolio project demonstrating orchestration-layer engineering for real-time voice AI: (1) sub-300ms barge-in handling with measured latency, (2) exact call-state recovery after a simulated dropped connection. Single scripted call scenario, 4 objection branches, 2 possible outcomes. Full scope/timeline in [project-spec.md](project-spec.md) — this document covers technical design only.

## 2. Architecture

**Deployment: LiveKit Cloud.** No self-hosted infrastructure.

```
Browser (caller, WebRTC mic)              Simple web page: "Start Call" button
        |
        v
   LiveKit Cloud room  <----------------  Python agent worker (LiveKit Agents SDK)
        |                                        |
        |                          +-------------+-------------+-------------+
        |                          |             |             |             |
        |                    Deepgram STT   Silero VAD /   Claude Haiku   Deepgram Aura-2
        |                    (streaming)    turn-detection   4.5 (branch    or Cartesia TTS
        |                                   (barge-in)       paraphrase +
        |                                                     classification
        |                                                     fallback)
        |                                        |
        |                                  Explicit FSM (in-process Python, agent/fsm.py)
        |                                        |
        |                                  Postgres (Supabase or Neon)
        |                                  calls / call_state / state_transitions / barge_in_events
        |
        v
  scripts/inject_barge_in.py  ---->  automated latency test harness (runs against a live call)
  scripts/simulate_crash.sh   ---->  kills the agent worker process mid-call
```

Dispatch is **explicit**: clicking "Start Call" on the web page triggers a backend call that creates the LiveKit room, dispatches the agent worker into it, and creates the `calls` row — before the agent joins. This gives the demo operator (you) full control over timing.

## 3. Tech stack

| Layer | Choice |
|---|---|
| Real-time transport | LiveKit Cloud (WebRTC) |
| Agent runtime | Python, LiveKit Agents SDK |
| STT | Deepgram, streaming |
| TTS | Deepgram Aura-2 or Cartesia |
| Barge-in detection | LiveKit built-in VAD (Silero) + turn-detection plugin |
| LLM | Claude Haiku 4.5 |
| Branch classification | Hybrid: keyword/phrase match, Haiku fallback |
| State machine | Explicit code (`agent/fsm.py`), not LLM-driven |
| Storage | Postgres — Supabase or Neon free tier |
| Hosting (web trigger page + agent worker) | Render or Fly.io free tier |
| Dev environment | WSL2 |

## 4. State machine

### 4.1 States

| State | Meaning | Terminal? |
|---|---|---|
| `OPENING` | Scripted balance statement plays | no |
| `LISTENING` | Waiting for caller speech before any branch has been classified | no |
| `HANDLING_OBJECTION` | Branch-specific handling in progress (negotiating an offer, or collecting dispute details) | no |
| `OFFER_PROPOSED` | An offer (amount/date/plan) is on the table, awaiting accept/reject | no |
| `PAYMENT_SCHEDULED` | Outcome 1 | **yes** |
| `ESCALATED` | Outcome 2 | **yes** |
| `CALL_ENDED` | Wraps either terminal outcome, call torn down | **yes** |
| `DROPPED` | Transient — set when a simulated crash/disconnect is detected | no |
| `RESUMING` | Transient — set on process restart, before rejoining prior state | no |

### 4.2 Data carried alongside state (FR4)

Stored in `call_state`, not encoded as separate FSM states:

- `branch` — one of `CANT_PAY_FULL`, `WANTS_LATER`, `DISPUTE`, `UPSET`, or null before classification. Set at classification time regardless of which path follows — including `UPSET`, which never enters `HANDLING_OBJECTION` — so the field always reflects what was detected, even on immediate-escalation paths.
- `offer` (jsonb, nullable) — `{amount, installments, plan}` for `CANT_PAY_FULL`, `{extension_days}` for `WANTS_LATER`, null for `DISPUTE`/`UPSET`
- `notes` (jsonb, nullable) — free-form context handed to a human on escalation (dispute reason, or an upset-caller flag)
- `turn_count` — increments once per caller utterance while in `HANDLING_OBJECTION`

### 4.3 Per-branch routing (this is the key design decision from this round)

Not all branches behave the same way in `HANDLING_OBJECTION` — this replaces a uniform "escalate after 2 turns" rule with branch-specific behavior:

| Branch | Behavior in `HANDLING_OBJECTION` | Exit |
|---|---|---|
| `CANT_PAY_FULL` | LLM may ask one clarifying question (turn 1); by turn 2, `agent/offer.py` computes a concrete installment plan from the fixed bounds (2–3 installments, min $100 each) — the LLM phrases that already-decided offer, it does not choose the numbers (see §6.2) | always → `OFFER_PROPOSED` within 2 turns |
| `WANTS_LATER` | Same pattern; `agent/offer.py` computes a concrete date extension (up to 30 days), LLM phrases it | always → `OFFER_PROPOSED` within 2 turns |
| `DISPUTE` | LLM asks clarifying questions to collect dispute detail ("what specifically don't you recognize about this charge?"), up to 2 turns | always → `ESCALATED`, never → `OFFER_PROPOSED`; collected detail written to `notes.dispute_reason` |
| `UPSET` | None — bypasses `HANDLING_OBJECTION` entirely | immediate `LISTENING` → `ESCALATED`; `notes.reason = "caller upset"` |

**Design rationale:** `CANT_PAY_FULL`/`WANTS_LATER` are genuinely negotiable, so the bot should always land on an offer within a bounded number of turns — never escalate from indecision, since the PRD explicitly puts "retry / no-resolution branch" out of scope. `DISPUTE` is not something a scripted bot should try to resolve, but collecting context before handoff is more useful (and a nicer demo of judgment) than a blind escalation. `UPSET` calls for immediate human handoff — attempting to "handle" an upset caller with a script reads as tone-deaf.

### 4.4 `OFFER_PROPOSED` exits (no counteroffer loop)

Per PRD out-of-scope ("retry / no-resolution branch"), `OFFER_PROPOSED` has exactly two exits — there is **no loop back to `HANDLING_OBJECTION`** for a counteroffer:

- Caller accepts (classified via the same hybrid keyword/LLM approach, e.g. "yes" / "that works" / "okay") → `PAYMENT_SCHEDULED`
- Caller rejects (e.g. "no" / "can't do that" / "not enough") → `ESCALATED` directly, single rejection

### 4.5 Full transition table

**Barge-in is not a `current_state` transition.** It's purely an audio-layer action — halt TTS output — that can fire in *any* state where TTS happens to be playing (`OPENING`'s statement, `HANDLING_OBJECTION`'s clarifying questions, `OFFER_PROPOSED`'s offer pitch, the `PAYMENT_SCHEDULED`/`ESCALATED` wrap-up lines). `current_state` never changes because of it — the FSM stays exactly where it logically was, just with the mic re-opened, and the event is logged to `barge_in_events` (§7), not `state_transitions`. This decouples "halt TTS" (audio layer) from `LISTENING` (a specific conversation-flow state), which earlier drafts of this table conflated — see §4.6 for the corrected edge-case behavior.

```
OPENING            --statement complete-->              LISTENING
LISTENING          --classified as CANT_PAY_FULL/WANTS_LATER--> HANDLING_OBJECTION(branch=X)
LISTENING          --classified as DISPUTE-->             HANDLING_OBJECTION(branch=DISPUTE)
LISTENING          --classified as UPSET-->               ESCALATED   (notes.reason = "caller upset")
HANDLING_OBJECTION --branch is CANT_PAY_FULL/WANTS_LATER, offer formed (<=2 turns)--> OFFER_PROPOSED
HANDLING_OBJECTION --branch is DISPUTE, detail collected (<=2 turns)--> ESCALATED   (notes.dispute_reason = ...)
OFFER_PROPOSED     --caller accepts-->                    PAYMENT_SCHEDULED
OFFER_PROPOSED     --caller rejects-->                     ESCALATED
PAYMENT_SCHEDULED  --wrap-up-->                            CALL_ENDED
ESCALATED          --wrap-up-->                            CALL_ENDED
ANY (non-terminal) --simulated crash-->                    DROPPED
DROPPED            --process restart, call_state loaded--> RESUMING --> [prior non-terminal state]
ANY (non-terminal) --client disconnect, grace window expires without rejoin (§8.3)--> CALL_ENDED   (outcome=null, abandoned)
```

### 4.6 Edge cases

- **Barge-in while in `LISTENING` (no TTS playing):** no-op — VAD firing with nothing to halt simply does nothing; not logged as a barge-in event.
- **Barge-in during `OPENING`:** this is the primary demo scenario ("interruption mid-sentence," project-spec.md's Day 8 milestone). TTS halts immediately, and — because the caller has now spoken — the FSM treats this exactly as if the statement had finished: it advances straight to classifying the interruption and routes per §4.5's `LISTENING`-origin rows, without ever passing through an idle `LISTENING` state. A barge-in during `OPENING` effectively collapses "statement complete" and "classify" into one turn.
- **Barge-in during `HANDLING_OBJECTION` or `OFFER_PROPOSED`'s own TTS (a clarifying question or the offer pitch):** TTS halts, `current_state` does not change, and the caller's new utterance is classified according to whatever state they interrupted — e.g. interrupting the offer pitch and immediately saying "no" is still an `OFFER_PROPOSED` rejection.
- **Crash during `OPENING`, before any `call_state` row exists:** the `calls` row is created at dispatch time (before `OPENING` starts speaking, see §9), and an initial `call_state` row (`current_state=OPENING`) is written synchronously at the same time. So there is always a row to resume from, even if the crash happens mid-opening-statement; resume simply re-enters `OPENING` from the top (the scripted line is short and idempotent to replay).
- **Crash during a barge-in event:** barge-in events are logged to a separate table (`barge_in_events`) independent of `call_state`; a crash here just means that one measurement sample is potentially incomplete (missing `tts_halt_at`) and should be excluded from the latency report, not that call recovery is affected.

## 5. Data model (Postgres)

```sql
create table calls (
  id            uuid primary key default gen_random_uuid(),
  room_name     text not null,
  started_at    timestamptz not null default now(),
  ended_at      timestamptz,
  outcome       text  -- 'payment_scheduled' | 'escalated' | null while in progress
);

create table call_state (             -- current/mutable, one row per call, upserted synchronously on every transition
  call_id       uuid primary key references calls(id),
  current_state text not null,
  branch        text,                  -- CANT_PAY_FULL | WANTS_LATER | DISPUTE | UPSET | null
  offer         jsonb,                 -- {amount, installments, plan} or {extension_days}
  notes         jsonb,                 -- context for human handoff on escalation
  turn_count    int not null default 0,
  generation    int not null default 1,   -- fencing token, see §8.2 — bumped each time a process attaches/resumes this call
  updated_at    timestamptz not null default now()
);

create table state_transitions (       -- append-only log, satisfies FR7
  id            bigserial primary key,
  call_id       uuid references calls(id),
  from_state    text,
  to_state      text,
  event         text,
  metadata      jsonb,
  occurred_at   timestamptz not null default now()
);

create table barge_in_events (
  id                 bigserial primary key,
  call_id            uuid references calls(id),
  speech_onset_at    timestamptz not null,   -- VAD fires
  tts_halt_at        timestamptz,            -- halt command issued (nullable: crash mid-event)
  latency_ms         int,                    -- tts_halt_at - speech_onset_at, null if incomplete
  source             text not null default 'live'  -- 'live' | 'injected' (from the test harness)
);

create index on state_transitions (call_id, occurred_at);
```

`call_state` is what recovery reads on restart (FR5). `state_transitions` is the audit log (FR7) — kept separate so "what do I resume from" never conflates with "what happened historically." All writes to `call_state` and the corresponding `state_transitions` row happen in the same synchronous, awaited transaction on every FSM transition (§8 rationale).

## 6. Conversation & LLM design

### 6.1 Branch classification (hybrid)

On each caller utterance transcribed by STT, classification runs in this order:

1. **Keyword/phrase match** against a per-branch phrase list (case-insensitive substring/fuzzy match):
   - `CANT_PAY_FULL`: "can't afford", "can't pay that much", "don't have that much", "too much money", "can't pay the full"
   - `WANTS_LATER`: "pay later", "next month", "after payday", "give me more time", "extension"
   - `DISPUTE`: "not mine", "don't recognize", "dispute", "never bought", "that's wrong", "not my charge"
   - `UPSET`: a smaller, lower-confidence keyword set ("ridiculous", "stop calling", "harassment") — tone/sentiment doesn't reduce well to keywords, so this branch leans more heavily on the LLM fallback than the others (see note below)
2. **Haiku fallback** if no keyword match: a small classification prompt returns one of the 4 branch labels (or "unclear," in which case the agent asks the caller to clarify and stays in `LISTENING`).

**Precedence when an utterance matches more than one branch's keyword list** (e.g. "I can't pay this and it's not even mine" hits both `CANT_PAY_FULL` and `DISPUTE`): resolved by a fixed priority order, `DISPUTE` > `UPSET` > `CANT_PAY_FULL` > `WANTS_LATER`. Misrouting a real dispute into a payment-plan offer is the worse failure mode of the two, so disputes win ties; upset sentiment is checked next since immediate human handoff is always a safe fallback. Without an explicit rule here, the outcome depends on keyword-list iteration order — an easy thing to get silently wrong on the actual recorded take.

Within `OFFER_PROPOSED`, the same hybrid approach classifies accept vs. reject using a separate small phrase list ("yes"/"sounds good"/"okay" vs. "no"/"can't do that"/"not enough").

This classification step only runs at the two decision points where it matters: in `LISTENING` (branch selection) and in `OFFER_PROPOSED` (accept/reject). A turn-2 utterance inside `HANDLING_OBJECTION` for `CANT_PAY_FULL`/`WANTS_LATER` isn't reclassified — per §4.3 the forced offer fires on turn count alone, regardless of utterance content.

**Note:** `UPSET` classification is inherently more sentiment-based than the other three, which are content-based. If keyword matching proves unreliable for `UPSET` during testing, weight the Haiku fallback more heavily for this branch specifically rather than expanding the keyword list indefinitely.

### 6.2 LLM prompt design (per branch)

**Offer terms are computed in code, never by the LLM.** This is the load-bearing guardrail, not the prompt wording below. `agent/offer.py` exposes a pure function — `compute_offer(branch, turn_count) -> dict` — that deterministically picks the installment plan or extension length from the fixed bounds in §6.4 (e.g. "2 installments of $241.09" or "14-day extension"), with no LLM call involved. The FSM calls this at the point of transitioning to `OFFER_PROPOSED` and writes the result straight to `call_state.offer`. The LLM is then given that already-decided offer as a fixed fact to phrase — it is never asked to choose a number, and its output is never parsed back into `offer`.

Each branch gets its own fixed system-prompt template — not a shared general-purpose prompt. Structure:

- **Goal** — what this turn is trying to accomplish (phrase the pre-computed offer, collect dispute detail, nothing for `UPSET`)
- **Given facts, not a range to reason within** — for `CANT_PAY_FULL`/`WANTS_LATER`, the prompt states the exact offer `compute_offer()` already produced ("tell the caller: 2 installments of $241.09, first due in 7 days"), not "propose 2–3 installments of at least $100"
- **Explicit do-not list** — no new legal terms, no waiving balance, no numbers other than the ones supplied, no promises outside the two defined outcomes. This is a defense-in-depth backstop against drift in the *wording*, not the mechanism that keeps numbers in bounds — that's §6.2's opening point
- **1–2 example utterances** to anchor tone

The LLM paraphrases within the branch, turn, and offer the FSM already decided — it never decides the branch, the transition, or the offer terms themselves.

### 6.3 Context window per turn

**Minimal, not full transcript.** Each Haiku call receives only `{branch, last user utterance, current offer state, turn_count}`. No running conversation history is passed. This keeps calls cheap and fast, and — more importantly — shrinks the model's surface area to drift outside the scripted branch.

### 6.4 Demo scenario data (defaults)

- Balance: **$482.17**
- `CANT_PAY_FULL` offer bounds: 2–3 installments, minimum $100/installment
- `WANTS_LATER` offer bounds: extension up to 30 days

## 7. Barge-in detection & latency instrumentation

### 7.1 Mechanism

LiveKit's built-in Silero VAD + turn-detection plugin fires a "speech started" event. On that event, if TTS is currently playing, the agent issues a **hard stop** on audio output immediately — no fade-out. This applies regardless of `current_state` (§4.5) — barge-in is an audio-layer action, not an FSM transition; see §4.6 for how each state's TTS segment specifically behaves when interrupted. Hard stop was chosen specifically because it minimizes the metric being reported; graceful fade would work against the <300ms target for a conversational-polish benefit this NFR isn't measuring.

### 7.2 Timestamps

1. `t0` (`speech_onset_at`) — VAD fires "speech started" while TTS is playing
2. `t1` (`tts_halt_at`) — agent code issues the TTS-cancel/stop command
3. `t2` — playback actually stops, captured only if the TTS/audio SDK exposes a stop-acknowledgment event; reported as a secondary number if available and materially different from `t1`

Primary reported metric: **`t1 - t0`** — this is what the orchestration layer directly controls. Both timestamps are captured in-memory at the moment they occur; the `barge_in_events` row recording them is written *after* the halt command is issued, never before or blocking it — see §8.1 for why this table is deliberately excluded from the project's otherwise-synchronous write policy.

**Naming the metric precisely.** `t0` is when the VAD *model* declares speech onset, not the actual acoustic moment the caller started talking — VAD models have their own inherent detection delay (buffering enough signal to be confident it's speech, not noise) before they fire. `t1 - t0` is therefore "detection-to-halt latency" — the orchestration layer's own reaction time after VAD has already decided — not "perceived latency from when the human opened their mouth." The README should label it exactly that way rather than let the number imply a broader claim than it supports.

### 7.3 Test methodology — automated harness, not manual talk-over

`scripts/inject_barge_in.py`: plays a pre-recorded audio clip into the room's caller-side track at a controlled, configurable offset into a known TTS response (e.g., "interrupt 1.5s into the opening statement"), repeatable on demand. Runs the full set of 15–20 trials with varied offsets, each logged to `barge_in_events` with `source='injected'`. This makes the reported latency distribution reproducible — re-run it and get the same distribution shape — rather than a single hand-logged live session.

Reporting: compute avg and worst-case (max) from `barge_in_events` where `source='injected'` for the README, not from eyeballing logs.

## 8. Crash & recovery mechanism

### 8.1 Why writes are synchronous — and where they deliberately aren't

**`call_state` + `state_transitions`: synchronous, always.** Every `call_state` upsert + `state_transitions` insert happens in one synchronous, awaited transaction before the FSM proceeds to its next action. Async/fire-and-forget was explicitly rejected here: it opens a race window where a crash lands after a transition but before the write flushes, which would show stale state on resume — directly undermining the exact-state-recovery result (FR5, NFR) this project exists to prove. The Postgres round-trip is negligible next to the STT→LLM→TTS hops already in the pipeline.

**`barge_in_events`: deliberately excluded from that policy.** This table is reporting-only — losing a sample on crash just means excluding it from the latency report, not a correctness problem. It must **not** be written synchronously in the barge-in hot path: `t0`/`t1` (§7.2) are captured in-memory and the TTS-cancel command is issued immediately from those in-memory values; the `barge_in_events` row is written after the fact (fire-and-forget is fine). If this table were folded into the same synchronous-write policy as `call_state`, a DB round-trip would sit directly between VAD detection and the halt command — both slowing the real interruption and inflating the exact number this project reports.

### 8.2 Process-crash path

1. `scripts/simulate_crash.sh` sends `SIGKILL` (not `SIGTERM`) to the agent worker process for a given room, mid-call, and **blocks until the OS confirms the process has actually exited** (e.g. waits on the PID) before returning — the restart step never starts while the old process might still be alive. This closes the obvious split-brain window: two workers briefly attached to the same call, both writing conflicting `call_state` updates, is exactly the kind of race that would flake live on camera during the one scene meant to prove reliability.
2. The LiveKit Cloud room persists independently of the agent process — the caller's WebRTC connection isn't necessarily dropped; this simulates "the brain died, the phone line didn't."
3. A new agent process starts (manually or via a supervisor restart). On startup, it queries for the most recent `calls` row joined to `call_state` where `current_state` is non-terminal, reads that row's `room_name`, and dispatches itself back into that exact LiveKit room (single-call-at-a-time scope, per project-spec.md §2, keeps this lookup unambiguous — no need to disambiguate between multiple concurrent calls).
4. Before writing anything, it atomically increments `call_state.generation` (`update call_state set generation = generation + 1 where call_id = $1 returning generation`) and remembers the value it got back as its own fencing token. Every subsequent write from this process includes `where generation = <its token>`; if that update affects zero rows, this process has been superseded (another one bumped `generation` further) and it stops writing and exits. This is deliberately lightweight — a full distributed lock is overkill for a single-call demo — but it removes the silent-corruption failure mode step 1's process-death wait doesn't fully cover (e.g. a stray dispatch retry).
5. It reads `branch`, `offer`, `notes`, `turn_count` from `call_state`, transitions to `RESUMING`, logs the transition, then re-enters the FSM at the persisted `current_state` — no replay of the opening statement, no loss of negotiation context.

### 8.3 Client-reconnect path

1. LiveKit's transport layer handles WebRTC reconnection natively.
2. The agent does **not** tear down `call_state` on a participant-disconnect event within a grace window (proposed: 30s) — it simply pauses.
3. On rejoin within the window, the agent logs a `RESUMING` transition and continues the conversation from the persisted state, same as §8.2 step 5. No new process attaches here (the original agent process stayed alive and just paused), so the §8.2 step 4 fencing-token bump doesn't apply to this path.
4. If the grace window expires without rejoin, the call is marked `CALL_ENDED` with `outcome=null` (abandoned) — this path is not part of the two demoed outcomes and doesn't need a scripted line.

### 8.4 Demo proof

Split-screen recording: one pane runs the agent/call logs, the other tails `state_transitions`/`call_state` live (`watch -n1 "psql ... -c 'select * from call_state where call_id=...'"` or a small script). The viewer watches `branch`/`offer`/`notes` before the crash, the crash and restart happening, and the resumed state land in the same row — continuous proof rather than a narrated claim. This recording doubles as the Postgres-log deliverable (project-spec.md §7).

## 9. Call initiation flow

1. A minimal static web page (single "Start Call" button) served from the same host as the agent backend.
2. On click, the page calls a backend endpoint that, in order: generates a room name (e.g. `call-{uuid}`), creates the `calls` row with that `room_name` (satisfies the `not null` constraint in §5), creates an initial `call_state` row (`current_state=OPENING`), creates the LiveKit room using the same name, explicitly dispatches the agent worker into it, and returns a LiveKit access token to the browser.
3. The browser joins the room with that token (mic access requested here — browser permission prompt).
4. The agent, now in the room, begins `OPENING` — the scripted balance statement.

Manual trigger (over auto-dispatch-on-join) was chosen specifically so the demo operator controls timing relative to screen recording.

## 10. Repo structure

```
holdline/
  agent/                    # LiveKit Agents SDK worker (Python)
    main.py                 # entrypoint, LiveKit Agents SDK wiring
    fsm.py                  # explicit state machine (§4)
    classify.py             # hybrid keyword + Haiku branch/accept-reject classification (§6.1)
    offer.py                 # deterministic offer computation, no LLM involved (§6.2)
    branches/                # per-branch prompt templates (§6.2)
      cant_pay_full.py
      wants_later.py
      dispute.py
      upset.py
    persistence.py           # Postgres read/write for call_state + state_transitions (§5, §8.1)
    latency.py                # barge-in instrumentation (§7.2)
  web/
    index.html               # "Start Call" button (§9)
  db/
    schema.sql                # §5
  docs/
    project-spec.md
    technical-requirements.md
    TRD.md                    # this document
  scripts/
    simulate_crash.sh         # kills the agent process for a demo (§8.2)
    inject_barge_in.py        # automated audio-injection latency harness (§7.3)
  .env.example                # see §11
  README.md
```

## 11. Configuration & secrets

`.env` (gitignored; `.env.example` committed with placeholder values):

```
LIVEKIT_URL=
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=
DEEPGRAM_API_KEY=
ANTHROPIC_API_KEY=
DATABASE_URL=          # Supabase or Neon connection string
```

No secrets committed to the repo at any point; `.env` loaded via standard Python `dotenv` in `agent/main.py`.

## 12. Non-functional requirements recap

- Barge-in halt latency: target under 300ms, reported as *detection-to-halt* latency (VAD-onset-declared to TTS-halt-issued, §7.2 — not raw acoustic onset), measured as average and worst case across 15–20 automated injected interruptions (§7.3).
- State recovery reconstructs exact pre-drop state (`branch`, `offer`, `notes`, `turn_count`), not an approximation — verified visually in the demo recording (§8.4).
- Single repo, documented, README leads with the two measured results, not a feature list.

## 13. Testing & validation plan

- **FSM unit tests** — table-driven tests covering every transition in §4.5, including the branch-specific `HANDLING_OBJECTION` exits (§4.3), the no-counteroffer-loop rule (§4.4), and barge-in during `OPENING` collapsing straight into classification (§4.6).
- **`offer.py` unit tests** — `compute_offer()` is pure and deterministic (§6.2); test it directly against the fixed bounds in §6.4, independent of any LLM or FSM code.
- **Classification tests** — keyword-list coverage for expected scripted phrasing per branch, including the `DISPUTE` > `UPSET` > `CANT_PAY_FULL` > `WANTS_LATER` precedence rule (§6.1) on multi-match inputs; a handful of unscripted-phrasing cases to sanity-check the Haiku fallback.
- **Latency harness runs** — `scripts/inject_barge_in.py` executed for the full 15–20 trial set as part of pre-demo validation, not just once; results committed to the README table.
- **Crash/resume test** — at least one scripted run of `scripts/simulate_crash.sh` per branch type (so recovery is verified from `HANDLING_OBJECTION`, `OFFER_PROPOSED`, and mid-`OPENING`, not just one state) before the final demo recording.
- **Fencing-token test** — deliberately start a second agent process pointed at the same `call_id` while the first is still alive (simulating an imperfect crash) and confirm the older generation's writes are rejected (§8.2 step 4) rather than corrupting `call_state`.

## 14. Assumptions & constraints carried from earlier decisions

- Identity verification and balance lookup are hardcoded/scripted (PRD out-of-scope) — no real customer data anywhere in this repo.
- `PAYMENT_SCHEDULED` and `ESCALATED` are both purely a scripted TTS line + a `call_state`/`state_transitions` write — no real payment processing, no real call transfer to an actual human queue.
- No real PSTN/SIP integration; browser-based WebRTC only (Twilio PSTN remains an optional, separately-scoped stretch per project-spec.md §9).
- No retry/counteroffer loop anywhere in the FSM (§4.4) — this is a deliberate scope boundary from the PRD, not an oversight.

## 15. Risks

- First-time LiveKit/WebRTC use remains the single biggest schedule risk (carried from project-spec.md §8) — this TRD's detail level is meant to reduce *design* rework, not implementation-learning-curve time.
- `UPSET` classification reliability (§6.1) is the least deterministic part of the classification design — flagged for extra manual testing before relying on it in the recorded demo.
- Latency numbers from the injection harness (§7.3) may differ from true live-speech barge-in latency (injected audio has a cleaner onset than natural speech) — worth a handful of live manual interruptions as a sanity cross-check against the harness numbers, even though the harness is the primary reported methodology.
