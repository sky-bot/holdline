"""Spike 1 — STT/TTS round-trip (TRD §16.2).

The smallest possible proof that the real-time audio pipeline works end to end:
the agent joins a room, speaks a scripted opening line via Cartesia (TTS), and
transcribes whatever the caller says via Deepgram (STT), logging each final
transcript. No LLM, no FSM yet — this exists only to de-risk the pipeline before
we build the orchestration on top of it.

Run from the repo root, inside the WSL venv:

    # Talk to it locally through your mic/speakers (simplest):
    python scripts/spike_pipeline.py console

    # Or run as a worker and connect a browser client (LiveKit Agents Playground):
    python scripts/spike_pipeline.py dev
"""
from __future__ import annotations

import logging

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions
from livekit.plugins import cartesia, deepgram, silero

load_dotenv(".env")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("spike")

OPENING_LINE = (
    "Hi, this is a courtesy call about your account. Our records show a "
    "past-due balance of four hundred eighty-two dollars and seventeen cents. "
    "I'd like to help you take care of it today."
)


def prewarm(proc: agents.JobProcess) -> None:
    # Load the VAD model once per worker process, not once per call.
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    log.info("agent connected to room %r", ctx.room.name)

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model="nova-3"),
        tts=cartesia.TTS(),
    )

    @session.on("user_input_transcribed")
    def _on_transcript(ev) -> None:
        # Fires as the caller speaks; log finals to prove STT is round-tripping.
        if getattr(ev, "is_final", False):
            log.info("STT (final): %r", getattr(ev, "transcript", ev))

    await session.start(agent=Agent(instructions="Scripted spike; no LLM."), room=ctx.room)
    await session.say(OPENING_LINE)
    log.info("spoke opening line — now listening for the caller")


if __name__ == "__main__":
    agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
