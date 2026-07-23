"""FSM-driven collections agent (TRD §4, §6, §9).

The conversation is driven by the explicit state machine in `agent/fsm.py`, not
by an LLM. The LiveKit Agents SDK supplies the real-time pipeline (Deepgram STT,
Cartesia TTS, Silero VAD, turn detection); every completed caller turn is routed
through `classify.py` + `fsm.py`, and the agent speaks the canonical prompt for
the resulting state (`prompts.py`).

This first version speaks the canonical prompts **directly** — no LLM paraphrasing
yet. That layer (which needs `ANTHROPIC_API_KEY`) wraps `prompts.py` later without
changing the control flow here. Call state is held in memory; Postgres persistence
and crash recovery come in a later step.

Run (from repo root, inside the venv) — as a module, so the `agent` package imports resolve:
    python -m agent.main console    # talk to it locally
    python -m agent.main dev        # worker + a browser client
"""
from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentSession, JobContext, StopResponse, WorkerOptions
from livekit.plugins import cartesia, deepgram, silero

from agent import classify, prompts
from agent.fsm import (
    BranchClassified,
    ObjectionTurn,
    OfferDecision,
    OpeningComplete,
    WrapUp,
    advance,
)
from agent.models import CallState, State

load_dotenv(".env")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("holdline")

_TERMINAL_OUTCOMES = (State.PAYMENT_SCHEDULED, State.ESCALATED)


class CollectionsAgent(Agent):
    """Drives one call through the FSM. The SDK handles audio; we handle flow."""

    def __init__(self) -> None:
        super().__init__(instructions="Scripted collections agent; conversation flow is code-driven.")
        self.cs = CallState()  # starts at OPENING
        self._lock = asyncio.Lock()  # serialize FSM transitions (barge-in can race on_enter)

    def _apply(self, event) -> None:
        t = advance(self.cs, event)
        log.info("FSM %s --%s--> %s", t.from_state.value, t.event, t.to_state.value)
        self.cs = t.call_state

    async def _say_current(self) -> None:
        """Speak the canonical prompt for the current state, if it has one."""
        try:
            line = prompts.canonical_prompt(self.cs)
        except ValueError:
            return  # silent states (e.g. LISTENING) have no line
        await self.session.say(line)

    async def on_enter(self) -> None:
        # Speak the opening statement, then advance to LISTENING — unless a
        # barge-in already moved us past OPENING while it was playing.
        await self._say_current()  # OPENING line
        async with self._lock:
            if self.cs.current_state == State.OPENING:
                self._apply(OpeningComplete())

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
        if self.cs.current_state == State.CALL_ENDED:
            # The call is over — ignore any trailing speech instead of falling
            # into the clarify-fallback forever.
            raise StopResponse()

        transcript = (getattr(new_message, "text_content", None) or "").strip()
        log.info("caller turn %r in state=%s", transcript, self.cs.current_state.value)

        async with self._lock:
            event = self._event_for(transcript)
            if event is None:
                # Couldn't classify — ask the caller to clarify and stay put.
                await self.session.say("Sorry, I didn't catch that — could you say it again?")
                raise StopResponse()
            self._apply(event)

        await self._say_current()  # speak the resulting state's line (question / offer / wrap-up)

        # A terminal outcome's line IS its wrap-up; after speaking it, close the call.
        async with self._lock:
            if self.cs.current_state in _TERMINAL_OUTCOMES:
                self._apply(WrapUp())  # -> CALL_ENDED
                log.info("call ended (branch=%s, outcome persisted in memory)", self.cs.branch)

        raise StopResponse()  # this turn is fully handled; no LLM generation


def prewarm(proc: agents.JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    log.info("agent connected to room %r", ctx.room.name)
    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model="nova-3"),
        tts=cartesia.TTS(),
        # no llm: the FSM drives the conversation
    )
    await session.start(agent=CollectionsAgent(), room=ctx.room)


if __name__ == "__main__":
    agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
