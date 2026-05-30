"""Smoke test for the game-integration half. Run::

    uv run python smoke_game_session.py

What it does:
1. Login to Gradient Bang, create or reuse character, start a session.
2. Join the returned Daily room via the ``daily-python`` SDK.
3. Listen for app-messages for SMOKE_DURATION seconds, accumulating state
   via GameContextStore + GameTurnBuffer.
4. Print every app-message (verbose) and the final rendered context.
5. Leave.

If this script completes with a non-empty context render, your half is
done. The buddy can take over from here using the GameSession class.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from dotenv import load_dotenv
from loguru import logger

load_dotenv(override=True)

from game_session import GameSession  # noqa: E402

SMOKE_DURATION = float(os.environ.get("SMOKE_DURATION_S", "30"))


class SmokeHandler:
    """daily-python EventHandler subclass — bridges Daily events into asyncio."""

    def __init__(self, loop: asyncio.AbstractEventLoop, session: GameSession, received: list):
        # NB: we import EventHandler lazily inside main() and subclass dynamically
        # to avoid hard-importing daily at module load time. See below.
        self._loop = loop
        self._session = session
        self._received = received
        self._left = asyncio.Event()

    @property
    def left(self) -> asyncio.Event:
        return self._left

    def on_app_message(self, message: dict[str, Any], sender: str) -> None:
        self._received.append({"sender": sender, "message": message})
        # GameSession.handle_app_message is sync and safe to call from this thread.
        text = self._session.handle_app_message(message)
        if text:
            logger.success(f"[GAME TURN ASSEMBLED] {text}")

    def on_call_state_updated(self, state: str) -> None:
        logger.info(f"[DAILY STATE] {state}")
        if state in ("left", "error"):
            self._loop.call_soon_threadsafe(self._left.set)


async def main() -> int:
    try:
        from daily import CallClient, Daily, EventHandler
    except ImportError:
        logger.error(
            "daily-python not installed. Run: uv add daily-python"
        )
        return 1

    logger.info("=== smoke_game_session ===")
    session = GameSession(text_mode=True)

    try:
        creds = await session.start()
    except Exception as e:
        logger.exception(f"gb_api login_and_start FAILED: {e}")
        return 2

    logger.info(f"[OK] login + /start  session_id={creds.session_id}")
    logger.info(f"[OK] character: {creds.character_name} ({creds.character_id})")
    logger.info(f"[OK] room: {creds.room_url}")

    # ---- join the Daily room ----------------------------------------------
    Daily.init()

    loop = asyncio.get_running_loop()
    received: list[dict[str, Any]] = []

    # Subclass EventHandler at runtime so we can mix in our state.
    class _Handler(EventHandler, SmokeHandler):
        def __init__(self):
            EventHandler.__init__(self)
            SmokeHandler.__init__(self, loop=loop, session=session, received=received)

    handler = _Handler()
    client = CallClient(event_handler=handler)
    client.set_user_name("smoke-tester")

    join_future: asyncio.Future = loop.create_future()

    def _join_complete(_data, error) -> None:
        if error:
            loop.call_soon_threadsafe(join_future.set_exception, RuntimeError(str(error)))
        else:
            loop.call_soon_threadsafe(join_future.set_result, None)

    logger.info("[joining Daily room ...]")
    client.join(
        creds.room_url,
        creds.room_token,
        client_settings={"inputs": {"camera": False, "microphone": False}},
        completion=_join_complete,
    )

    try:
        await asyncio.wait_for(join_future, timeout=15)
    except asyncio.TimeoutError:
        logger.error("Daily join timed out after 15s")
        Daily.deinit()
        return 3

    logger.success(f"[OK] joined Daily room. Listening for {SMOKE_DURATION}s ...")

    try:
        await asyncio.wait_for(handler.left.wait(), timeout=SMOKE_DURATION)
    except asyncio.TimeoutError:
        pass

    logger.info("[leaving Daily room ...]")
    leave_future: asyncio.Future = loop.create_future()
    client.leave(
        completion=lambda *_: loop.call_soon_threadsafe(leave_future.set_result, None)
    )
    try:
        await asyncio.wait_for(leave_future, timeout=5)
    except asyncio.TimeoutError:
        pass

    Daily.deinit()

    # ---- report -----------------------------------------------------------
    logger.info("=" * 60)
    logger.info(f"app-messages received: {len(received)}")
    type_counts: dict[str, int] = {}
    event_counts: dict[str, int] = {}
    for entry in received:
        msg = entry["message"]
        if not isinstance(msg, dict):
            continue
        t = msg.get("type", "?")
        type_counts[t] = type_counts.get(t, 0) + 1
        if t == "server-message":
            ev = (msg.get("data") or {}).get("event", "?")
            event_counts[ev] = event_counts.get(ev, 0) + 1
    logger.info(f"by message type: {type_counts}")
    logger.info(f"by server-message event: {event_counts}")
    logger.info("=" * 60)
    rendered = session.context_store.render()
    if rendered:
        logger.success("FINAL GAME CONTEXT:\n" + rendered)
        return 0
    else:
        logger.warning(
            "No game context accumulated. The game may have sent only "
            "events we don't summarize (combat.* / task.* / etc) without a "
            "status.snapshot. Re-run smoke or check raw message dump above."
        )
        return 0 if received else 4


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
