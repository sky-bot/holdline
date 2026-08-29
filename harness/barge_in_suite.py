"""Barge-in latency measurement suite (TRD §7.2, §7.5).

Each trial: join a fresh room, wait for the agent to begin its opening statement,
inject a short interrupt clip, and measure the **end-to-end** barge-in latency =
(agent audio ceases) − (interrupt injected). Both timestamps are the harness's own
monotonic clock, so the headline number needs no cross-clock reconciliation.

The interrupt is injected early in the opening so the barge-in halt lands before
any natural inter-sentence pause (which would otherwise look like a stop).

Requires a running agent worker (`python -m agent.main dev`) and the interrupt
clip (`scripts/make_interrupt_clip.py`).

    python harness/barge_in_suite.py                 # 15 trials
    BARGE_IN_TRIALS=20 python harness/barge_in_suite.py
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time
import wave

import numpy as np
from dotenv import load_dotenv
from livekit import api, rtc

load_dotenv(".env")

URL = os.getenv("LIVEKIT_URL")
CLIP = os.getenv("INTERRUPT_CLIP", "harness/clips/interrupt.wav")
N_TRIALS = int(os.getenv("BARGE_IN_TRIALS", "15"))
INJECT_OFFSET_S = float(os.getenv("INJECT_OFFSET_S", "0.8"))
RMS_THRESHOLD = 300.0
SILENCE_HANG_S = 0.35
FRAME_MS = 10


def _load_clip() -> tuple[int, bytes]:
    w = wave.open(CLIP)
    assert w.getnchannels() == 1 and w.getsampwidth() == 2, "expected mono s16 WAV"
    return w.getframerate(), w.readframes(w.getnframes())


def _token(room: str) -> str:
    return (
        api.AccessToken(os.getenv("LIVEKIT_API_KEY"), os.getenv("LIVEKIT_API_SECRET"))
        .with_identity("caller")
        .with_name("caller")
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .to_jwt()
    )


class Trial:
    def __init__(self) -> None:
        self.agent_started = asyncio.Event()
        self.last_active: float | None = None
        self.injected_at: float | None = None
        self.stopped_at: float | None = None
        self.done = asyncio.Event()

    async def read_agent(self, track: rtc.Track) -> None:
        stream = rtc.AudioStream(track)
        async for ev in stream:
            f = ev.frame
            s = np.frombuffer(f.data, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(s * s))) if s.size else 0.0
            if rms > RMS_THRESHOLD:
                if not self.agent_started.is_set():
                    self.agent_started.set()
                self.last_active = time.monotonic()
            if self.done.is_set():
                break
        await stream.aclose()

    async def watch_stop(self) -> None:
        # After injection, declare STOP when the agent has been silent for the hang
        # window; report the stop time as the last audible frame (not last+hang).
        while not self.done.is_set():
            await asyncio.sleep(0.02)
            if self.injected_at and self.last_active and self.last_active >= self.injected_at:
                if (time.monotonic() - self.last_active) > SILENCE_HANG_S:
                    self.stopped_at = self.last_active
                    self.done.set()


async def _push_clip(source: rtc.AudioSource, rate: int, pcm: bytes) -> None:
    frame_samples = rate * FRAME_MS // 1000
    frame_bytes = frame_samples * 2
    for i in range(0, len(pcm), frame_bytes):
        chunk = pcm[i:i + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
        await source.capture_frame(
            rtc.AudioFrame(data=chunk, sample_rate=rate, num_channels=1, samples_per_channel=frame_samples)
        )


async def one_trial(idx: int, rate: int, pcm: bytes) -> float | None:
    room_name = f"holdline-bargein-{idx}-{int(time.time() * 1000)}"
    room = rtc.Room()
    trial = Trial()

    @room.on("track_subscribed")
    def _on_track(track, pub, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.ensure_future(trial.read_agent(track))

    await room.connect(URL, _token(room_name))
    source = rtc.AudioSource(rate, 1)
    mic = rtc.LocalAudioTrack.create_audio_track("caller-mic", source)
    await room.local_participant.publish_track(
        mic, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )

    try:
        await asyncio.wait_for(trial.agent_started.wait(), timeout=25)
    except asyncio.TimeoutError:
        await room.disconnect()
        return None

    await asyncio.sleep(INJECT_OFFSET_S)
    watcher = asyncio.ensure_future(trial.watch_stop())
    trial.injected_at = time.monotonic()
    await _push_clip(source, rate, pcm)

    try:
        await asyncio.wait_for(trial.done.wait(), timeout=8)
        e2e = (trial.stopped_at - trial.injected_at) * 1000.0
    except asyncio.TimeoutError:
        e2e = None
    finally:
        watcher.cancel()
        await room.disconnect()
    return e2e


async def main() -> None:
    rate, pcm = _load_clip()
    print(f"clip {len(pcm) / 2 / rate:.2f}s @ {rate} Hz | {N_TRIALS} trials | inject at {INJECT_OFFSET_S}s\n")
    results: list[float] = []
    for i in range(N_TRIALS):
        e2e = await one_trial(i, rate, pcm)
        if e2e is not None and e2e >= 0:
            results.append(e2e)
            print(f"  trial {i + 1:2d}: {e2e:6.0f} ms")
        else:
            print(f"  trial {i + 1:2d}: (no measurement)")
        await asyncio.sleep(1.0)  # let the room/agent tear down between trials

    if results:
        print(f"\n=== end-to-end barge-in latency ({len(results)}/{N_TRIALS} trials) ===")
        print(f"  average:    {statistics.mean(results):.0f} ms")
        print(f"  median:     {statistics.median(results):.0f} ms")
        print(f"  best:       {min(results):.0f} ms")
        print(f"  worst-case: {max(results):.0f} ms")
    else:
        print("\nNo successful measurements — is the agent worker running?")


if __name__ == "__main__":
    asyncio.run(main())
