-- holdline schema (TRD §5)
-- Postgres (Supabase or Neon). Run once to provision.

create table calls (
  id            uuid primary key default gen_random_uuid(),
  room_name     text not null,
  started_at    timestamptz not null default now(),
  ended_at      timestamptz,
  outcome       text  -- 'payment_scheduled' | 'escalated' | null while in progress
);

-- Current/mutable state, one row per call, upserted synchronously per transition.
-- This is what recovery reads on restart (FR5).
create table call_state (
  call_id       uuid primary key references calls(id),
  current_state text not null,
  branch        text,                  -- CANT_PAY_FULL | WANTS_LATER | DISPUTE | UPSET | null
  offer         jsonb,                 -- installment_plan or extension shape (see agent/offer.py)
  notes         jsonb,                 -- context for human handoff on escalation
  turn_count    int  not null default 0,
  generation    int  not null default 1,  -- fencing token (TRD §8.2)
  updated_at    timestamptz not null default now()
);

-- Append-only audit log (FR7). Separate from call_state so "what do I resume
-- from" never conflates with "what happened".
create table state_transitions (
  id            bigserial primary key,
  call_id       uuid references calls(id),
  from_state    text,
  to_state      text,
  event         text,
  metadata      jsonb,
  occurred_at   timestamptz not null default now()
);

-- One row per interruption (TRD §7.2). Reporting-only; written off the hot path.
create table barge_in_events (
  id                 bigserial primary key,
  call_id            uuid references calls(id),
  injected_at        timestamptz,      -- harness: interrupt clip start (harness clock)
  speech_onset_at    timestamptz not null,  -- t0: VAD speech onset (server clock)
  tts_halt_at        timestamptz,      -- t1: cancel issued (server clock)
  audio_stopped_at   timestamptz,      -- t2: harness saw audio cease (harness clock)
  end_to_end_ms      int,              -- audio_stopped_at - injected_at (headline number)
  halt_decision_ms   int,              -- t1 - t0 (decomposition only)
  source             text not null default 'live'  -- 'live' | 'injected'
);

-- One row per agent response turn (TRD §7.3).
create table turn_latency_events (
  id                 bigserial primary key,
  call_id            uuid references calls(id),
  turn_index         int  not null,
  user_stop_at       timestamptz not null,  -- endpoint fired (server clock)
  stt_final_at       timestamptz,
  llm_first_token_at timestamptz,
  tts_first_byte_at  timestamptz,
  first_audio_at     timestamptz,           -- harness saw first agent audio (harness clock)
  total_ms           int,                   -- first_audio_at - user_stop_at
  source             text not null default 'live'
);

-- One row per simulated drop (TRD §8.5).
create table recovery_events (
  id              bigserial primary key,
  call_id         uuid references calls(id),
  path            text not null,       -- 'process_crash' | 'client_reconnect'
  crashed_at      timestamptz not null,-- harness triggered the kill/disconnect (harness clock)
  resumed_state   text not null,       -- current_state the agent came back into
  first_audio_at  timestamptz,         -- harness saw first agent audio after resume (harness clock)
  recovery_ms     int                  -- first_audio_at - crashed_at
);

create index on state_transitions (call_id, occurred_at);
