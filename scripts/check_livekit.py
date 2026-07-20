"""Connectivity smoke test for LiveKit Cloud credentials (TRD §16.2, Spike 1).

Loads .env, confirms the three LiveKit vars are present, then makes one
authenticated API call (list rooms) to prove the URL/key/secret actually work
against the project — not just that they're internally consistent.

Prints no secret values: only presence, lengths, and the connection result.

    python scripts/check_livekit.py
"""
from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

REQUIRED = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")


def check_present() -> bool:
    load_dotenv()
    ok = True
    for key in REQUIRED:
        value = os.getenv(key, "")
        if value:
            print(f"  {key}: set (len {len(value)})")
        else:
            print(f"  {key}: MISSING")
            ok = False
    url = os.getenv("LIVEKIT_URL", "")
    if url and not url.startswith("wss://"):
        print(f"  ! LIVEKIT_URL should start with 'wss://' (starts with {url[:6]!r})")
    return ok


async def check_connect() -> None:
    from livekit import api

    lk = api.LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )
    try:
        res = await lk.room.list_rooms(api.ListRoomsRequest())
        print(f"  connected OK — project reachable, {len(res.rooms)} active room(s)")
    finally:
        await lk.aclose()


def main() -> int:
    print("== env presence ==")
    if not check_present():
        print("Fix the missing value(s) in .env, then re-run.")
        return 1

    print("== live connectivity ==")
    try:
        asyncio.run(check_connect())
    except Exception as exc:  # noqa: BLE001 - surface any auth/network failure plainly
        print(f"  connection FAILED: {type(exc).__name__}: {exc}")
        print("  (check the URL, key, and secret are from the same project)")
        return 1

    print("All good — LiveKit credentials work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
