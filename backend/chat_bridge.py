"""Audience chat → Officer user-turn bridge.

The pivot piece for the streamer demo. Bot-v5's loop is:

  Ship AI speaks  →  GameTurnBuffer assembles text  →  queue_game_turn(text)
  → LLMMessagesAppendFrame(role=user, content=text, run_llm=True)
  → Officer responds

We hook audience chat into the same trigger path so the Officer reacts to
chat exactly like it reacts to the Ship AI. The audience UI POSTs to
``POST /chat`` and the chat shows up as a user-turn one beat later.

Wiring (in the buddy's ``bot.py``):

    bridge = ChatBridge()
    bridge.attach_router(app)              # mount on the FastAPI sidecar
    asyncio.create_task(bridge.drain(task)) # task is the PipelineTask

The audience UI does ``POST :8000/chat`` with ``{"viewer": "...", "text": "..."}``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException
from loguru import logger
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.pipeline.task import PipelineTask
from pydantic import BaseModel


class ChatMessage(BaseModel):
    viewer: str
    text: str


@dataclass
class ChatEvent:
    viewer: str
    text: str
    at: float


class ChatBridge:
    """In-process queue + FastAPI router. The bot's pipeline drains the
    queue and turns each message into an LLM user-turn.

    When a FailureStore is passed in, each enqueued chat is also recorded
    as a pending chat_decision row — ChatModeParser later links the
    Officer's chosen mode (TAKE/ROAST/ACK/IGNORE) to that same row.
    """

    def __init__(self, max_pending: int = 32, store=None):
        self._queue: asyncio.Queue[ChatEvent] = asyncio.Queue(maxsize=max_pending)
        self._closed = False
        self._store = store

    def attach_router(self, app: FastAPI, prefix: str = "") -> APIRouter:
        router = APIRouter()

        @router.post("/chat")
        async def post_chat(msg: ChatMessage):
            viewer = (msg.viewer or "").strip() or "anon"
            text = (msg.text or "").strip()
            if not text:
                raise HTTPException(400, "text is required")
            if len(text) > 240:
                text = text[:240] + "…"
            try:
                self._queue.put_nowait(ChatEvent(viewer=viewer, text=text, at=time.time()))
            except asyncio.QueueFull:
                raise HTTPException(429, "chat queue full, slow down")
            logger.info(f"[CHAT IN][{viewer}] {text}")
            return {"ok": True, "queued": self._queue.qsize()}

        @router.get("/chat/pending")
        async def pending():
            return {"pending": self._queue.qsize()}

        app.include_router(router, prefix=prefix)
        return router

    async def drain(self, task: PipelineTask) -> None:
        """Long-running task: pop chat events, queue them as LLM user-turns."""
        while not self._closed:
            event = await self._queue.get()
            text = self._format_for_officer(event)
            logger.info(f"[CHAT → OFFICER] {text}")
            if self._store is not None:
                try:
                    self._store.record_pending_chat(event.viewer, event.text)
                except Exception as e:
                    logger.warning(f"[CHAT] record_pending_chat failed: {e}")
            try:
                await task.queue_frames([
                    LLMMessagesAppendFrame(
                        messages=[{"role": "user", "content": text}],
                        run_llm=True,
                    )
                ])
            except Exception as e:
                logger.exception(f"failed to queue chat as user-turn: {e}")

    def _format_for_officer(self, event: ChatEvent) -> str:
        return f"Audience chat from {event.viewer}: \"{event.text}\""

    def close(self) -> None:
        self._closed = True

    def enqueue_direct(self, viewer: str, text: str) -> None:
        """Synthetic / programmatic injection (for AI viewers, tests)."""
        try:
            self._queue.put_nowait(ChatEvent(viewer=viewer, text=text, at=time.time()))
        except asyncio.QueueFull:
            logger.warning("chat queue full; dropping synthetic message")
