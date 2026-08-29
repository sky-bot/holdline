"""FSM-driven collections agent (TRD §4, §6, §8, §9).

The conversation is driven by the explicit state machine in `agent/fsm.py`, not by
an LLM. The LiveKit Agents SDK supplies the real-time pipeline (Deepgram STT,
Cartesia TTS, Silero VAD, turn detection); every completed caller turn is routed
through `classify.py` + `fsm.py`, and the agent speaks the canonical prompt for the
resulting state (`prompts.py`).

Persistence + recovery (TRD §8): if `DATABASE_URL` is set, every transition is
written synchronously to Postgres, and on (re)joining a room the agent resumes any
non-terminal call from its exact persisted state. Without `DATABASE_URL` the agent
runs in memory (no recovery) — handy for pipeline demos that don't need it.

No LLM yet — prompts are spoken directly; the paraphrasing layer wraps `prompts.py`
later without changing this control flow.

Run (from repo root, inside the venv) as a module:
    python -m agent.main console    # talk to it locally
    python -m agent.main dev        # worker + a browser client
"""
from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentSession, JobContext, StopResponse, WorkerOptions
from livekit.plugins import cartesia, deepgram, silero

from agent import classify, prompts
from agent.config import BARGE_IN_CONFIRM_MS
from agent.fsm import (
    BranchClassified,
    ObjectionTurn,
    OfferDecision,
    OpeningComplete,
    WrapUp,
    advance,
)
from agent.models import CallState, State
from agent.persistence import Persistence

load_dotenv(".env")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("holdline")

_OUTCOME_STATES = (State.PAYMENT_SCHEDULED, State.ESCALATED)

# One persistence pool per worker process, created lazily on first use.
_persistence: Persistence | None = None
_persistence_lock = asyncio.Lock()


async def get_persistence() -> Persistence | None:
    """Return the shared Persistence, or None if DATABASE_URL isn't configured."""
    global _persistence
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return None
    async with _persistence_lock:
        if _persistence is None:
            _persistence = await Persistence.connect(dsn)
    return _persistence


class CollectionsAgent(Agent):
    """Drives one call through the FSM. The SDK handles audio; we handle flow + persistence."""

    def __init__(
        self,
        persist: Persistence | None = None,
        call_id=None,
        generation: int = 1,
        resume_state: CallState | None = None,
    ) -> None:
        super().__init__(instructions="Scripted collections agent; conversation flow is code-driven.")
        self.persist = persist
        self.call_id = call_id
        self.generation = generation
        self.cs = resume_state or CallState()  # OPENING for a new call
        self._resuming = resume_state is not None
        self._fenced = False  # set if a newer process took over this call (TRD §8.2)
        self._lock = asyncio.Lock()  # serialize FSM transitions (barge-in can race on_enter)

    async def _apply(self, event) -> None:
        """Advance the FSM and persist the transition synchronously (TRD §8.1)."""
        t = advance(self.cs, event)
        log.info("FSM %s --%s--> %s", t.from_state.value, t.event, t.to_state.value)
        self.cs = t.call_state

        if self.persist is not None and self.call_id is not None:
            ok = await self.persist.persist_transition(self.call_id, self.generation, t)
            if not ok:
                # A newer generation has claimed this call — stop writing and speaking.
                self._fenced = True
                log.warning("fenced out by a newer process; this agent will stop responding")
                return
            if self.cs.current_state in _OUTCOME_STATES:
                await self.persist.set_outcome(self.call_id, self.cs.current_state.value.lower())

    async def _say_current(self) -> None:
        """Speak the canonical prompt for the current state, if it has one."""
        try:
            line = prompts.canonical_prompt(self.cs)
        except ValueError:
            return  # silent states (e.g. LISTENING) have no line
        await self.session.say(line)

    async def on_enter(self) -> None:
        if self._resuming:
            # Recovery: re-deliver the persisted state's prompt exactly (TRD §4.7, §8.2).
            log.info("resuming call at state=%s", self.cs.current_state.value)
            if self.persist is not None and self.call_id is not None:
                await self.persist.log_resume(self.call_id, self.cs.current_state)
            await self._say_current()
            return

        # New call: speak the opening, then advance to LISTENING — unless a barge-in
        # already moved us past OPENING while it was playing.
        await self._say_current()  # OPENING line
        async with self._lock:
            if self.cs.current_state == State.OPENING:
                await self._apply(OpeningComplete())

    def _event_for(self, transcript: str):
        """Map a caller utterance to the FSM event valid for the current state."""
        s = self.cs.current_state
        if s in (State.OPENING, State.LISTENING):
            branch = classify.classify(transcript)  # LLM fallback wired in later
            return BranchClassified(branch) if branch is not None else None
        if s == State.HANDLING_OBJECTION:
            return ObjectionTurn(detail=transcript)
        if s == State.OFFER_PROPOSED:
            decision = classify.classify_decision(transcript)
            return OfferDecision(accepted=decision) if decision is not None else None
        return None

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        if self._fenced or self.cs.current_state == State.CALL_ENDED:
            raise StopResponse()  # call is over or superseded — ignore trailing speech

        transcript = (getattr(new_message, "text_content", None) or "").strip()
        log.info("caller turn %r in state=%s", transcript, self.cs.current_state.value)

        async with self._lock:
            event = self._event_for(transcript)
            if event is None:
                await self.session.say("Sorry, I didn't catch that — could you say it again?")
                raise StopResponse()
            await self._apply(event)
            if self._fenced:
                raise StopResponse()

        await self._say_current()  # speak the resulting state's line (question / offer / wrap-up)

        # A terminal outcome's line IS its wrap-up; after speaking it, close the call.
        async with self._lock:
            if self.cs.current_state in _OUTCOME_STATES:
                await self._apply(WrapUp())  # -> CALL_ENDED
                log.info("call ended (branch=%s)", self.cs.branch)

        raise StopResponse()  # this turn is fully handled; no LLM generation


def prewarm(proc: agents.JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def _build_agent(room_name: str) -> CollectionsAgent:
    """Resume an in-progress call for this room, or start a fresh one (TRD §8.2)."""
    persist = await get_persistence()
    if persist is None:
        log.info("no DATABASE_URL set — running without persistence (no recovery)")
        return CollectionsAgent()

    active = await persist.find_active_call(room_name)
    if active is not None:
        gen = await persist.bump_generation(active.call_id)  # fence: claim the call
        log.info("resuming call %s at %s (generation %d)", active.call_id, active.call_state.current_state.value, gen)
        return CollectionsAgent(persist, active.call_id, gen, resume_state=active.call_state)

    fresh = await persist.create_call(room_name)
    log.info("new call %s", fresh.call_id)
    return CollectionsAgent(persist, fresh.call_id, fresh.generation)


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    log.info("agent connected to room %r", ctx.room.name)
    agent = await _build_agent(ctx.room.name)
    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model="nova-3"),
        tts=cartesia.TTS(),
        # no llm: the FSM drives the conversation
        #
        # Barge-in tuning for low latency (TRD §7.1). Two SDK defaults otherwise
        # dominate the number:
        #  - aec_warmup_duration (default 3.0s) disables interruptions at call
        #    start to calibrate echo cancellation. Our WebRTC path has no acoustic
        #    echo (no shared speaker/mic), so we skip it.
        #  - the "adaptive" interruption detector is a LiveKit Cloud inference call
        #    (~0.4s round-trip) that trades latency for fewer false interruptions.
        #    "vad" mode decides locally on voice activity — far lower latency.
        aec_warmup_duration=0.0,
        turn_handling={
            "interruption": {
                "enabled": True,
                "mode": "vad",
                "min_duration": BARGE_IN_CONFIRM_MS / 1000.0,  # short voiced-onset gate
                "min_words": 0,  # react to speech, not transcribed words (off the STT path)
            },
        },
    )
    await session.start(agent=agent, room=ctx.room)


if __name__ == "__main__":
    agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
