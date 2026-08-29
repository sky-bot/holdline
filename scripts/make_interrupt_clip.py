"""Generate the interrupt audio clip used for barge-in latency testing (TRD §7).

Synthesizes a short, clearly-voiced phrase via Cartesia and writes it as a 48 kHz
mono PCM WAV that the harness publishes to interrupt the agent mid-sentence.

    python scripts/make_interrupt_clip.py
"""
from __future__ import annotations

import json
import os
import pathlib
import urllib.request
import wave

from dotenv import load_dotenv

load_dotenv(".env")

API = "https://api.cartesia.ai"
VERSION = "2024-11-13"
PHRASE = "Wait, hold on a second."
OUT = pathlib.Path("harness/clips/interrupt.wav")
SAMPLE_RATE = 48000


def _get(url: str):
    req = urllib.request.Request(url, headers={
        "X-API-Key": os.environ["CARTESIA_API_KEY"],
        "Cartesia-Version": VERSION,
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _first_voice_id() -> str:
    data = _get(f"{API}/voices")
    voices = data.get("data", data) if isinstance(data, dict) else data
    return voices[0]["id"]


def _synth_pcm(voice_id: str) -> bytes:
    # Ask for raw PCM (not a WAV container) so we can write a correct header
    # ourselves — Cartesia's WAV container uses a streaming placeholder length.
    body = json.dumps({
        "model_id": "sonic-2",
        "transcript": PHRASE,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE},
    }).encode()
    req = urllib.request.Request(
        f"{API}/tts/bytes", data=body, method="POST",
        headers={
            "X-API-Key": os.environ["CARTESIA_API_KEY"],
            "Cartesia-Version": VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    voice_id = _first_voice_id()
    pcm = _synth_pcm(voice_id)
    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # pcm_s16le
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    dur = len(pcm) / 2 / SAMPLE_RATE
    print(f"wrote {OUT} ({len(pcm)} PCM bytes, {dur:.2f}s) @ {SAMPLE_RATE} Hz, voice={voice_id}, phrase={PHRASE!r}")


if __name__ == "__main__":
    main()
