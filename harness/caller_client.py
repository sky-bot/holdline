"""Spike 2 — headless audio-capture harness (TRD §16.2, §7.2).

The riskiest single unknown in the project: can a *headless* LiveKit client
(no browser) subscribe to the agent's audio output and detect, on its own clock,
when that audio starts and stops? That start/stop detection is the foundation of
the end-to-end barge-in number (`audio_stopped_at - injected_at`, TRD §7.2).

This spike proves exactly that and nothing more: join a room, subscribe to the
agent's audio track, and print timestamped START / STOP events plus the measured
utterance duration. Publishing an interrupt clip and measuring barge-in latency
comes next — first we prove we can *see* the audio cease.

Requires a running agent that will speak into the same room (scripts/spike_pipeline.py).

    python harness/caller_client.py
"""
from __future__ import annotations

import asyncio
import os
import time

import numpy as np
from dotenv import load_dotenv
from livekit import api, rtc

load_dotenv(".env")

ROOM = "holdline-spike2"
RMS_THRESHOLD = 300.0   # int16 RMS above this counts as audible speech
SILENCE_HANG_S = 0.40   # declare STOP after this much continuous silence
MAX_RUN_S = 30.0        # overall timeout


def _token() -> str:
    return (
        api.AccessToken(os.getenv("LIVEKIT_API_KEY"), os.getenv("LIVEKIT_API_SECRET"))
        .with_identity("harness")
        .with_name("measurement-harness")
        .with_grants(api.VideoGrants(room_join=True, room=ROOM))
        .to_jwt()
    )


class Detector:
    """Energy-based audio start/stop detection on a single monotonic clock."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.started_at: float | None = None
        self.last_active: float | None = None
        self.stopped = asyncio.Event()

    def _rel(self, t: float) -> float:
        return (t - self.t0) * 1000.0  # ms since harness start

    def feed(self, rms: float) -> None:
        now = time.monotonic()
        if rms > RMS_THRESHOLD:
            if self.started_at is None:
                self.started_at = now
                print(f"[{self._rel(now):8.0f} ms] AUDIO START (rms {rms:.0f})")
            self.last_active = now

    async def watch(self) -> None:
        # STOP is "last audible frame + a silence gap", so the reported stop time
        # is when the audio actually ceased, not when we noticed.
        while not self.stopped.is_set():
            await asyncio.sleep(0.05)
            if self.started_at and self.last_active and (time.monotonic() - self.last_active) > SILENCE_HANG_S:
                stop = self.last_active
                dur = (stop - self.started_at) * 1000.0
                print(f"[{self._rel(stop):8.0f} ms] AUDIO STOP  (utterance {dur:.0f} ms)")
                self.stopped.set()


async def _read_track(track: rtc.Track, det: Detector) -> None:
    stream = rtc.AudioStream(track)
    async for ev in stream:
        f = ev.frame
        samples = np.frombuffer(f.data, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        det.feed(rms)
        if det.stopped.is_set():
            break
    await stream.aclose()


async def main() -> int:
    det = Detector()
    room = rtc.Room()

    @room.on("track_subscribed")
    def _on_sub(track: rtc.Track, pub: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            print(f"[{det._rel(time.monotonic()):8.0f} ms] subscribed to audio from {participant.identity!r}")
            asyncio.ensure_future(_read_track(track, det))

    print(f"connecting to room {ROOM!r} ...")
    await room.connect(os.getenv("LIVEKIT_URL"), _token())
    print(f"[{det._rel(time.monotonic()):8.0f} ms] connected; waiting for the agent to speak")

    watcher = asyncio.ensure_future(det.watch())
    try:
        await asyncio.wait_for(det.stopped.wait(), timeout=MAX_RUN_S)
        print("SPIKE 2 PASS — detected the agent's audio start and stop from a headless client")
        rc = 0
    except asyncio.TimeoutError:
        if det.started_at:
            print("Detected START but no STOP within timeout (agent may still be speaking)")
        else:
            print("TIMEOUT — no agent audio detected (is the worker running and dispatching?)")
        rc = 1
    finally:
        watcher.cancel()
        await room.disconnect()
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
