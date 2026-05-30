"""GameSession — the import surface the buddy's bot.py uses.

Bundles together everything that has to be set up before the Pipecat
pipeline can run: GB login + session creation, the GameContextStore,
the GameTurnBuffer, and helpers for loading the system + task prompts.

Usage in bot.py::

    session = GameSession()
    creds = await session.start()                   # gb_api login + /start
    transport = DailyTransport(creds.room_url, creds.room_token, ...)
    game_context = session.context_store
    turn_buffer  = session.turn_buffer
    system_instruction = session.load_system_prompt()
    # ... assemble pipeline with GameContextInjector(game_context) etc.

    @transport.event_handler("on_app_message")
    async def on_app_message(transport, message, sender):
        game_context.handle_message(message)
        text = turn_buffer.handle_message(message)
        if text:
            await task.queue_frames([LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": text}],
                run_llm=True,
            )])
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger

from gb_api import GBStartResult, login_and_start
from game_state import GameContextStore, GameTurnBuffer


SYSTEM_PROMPT_PATH = Path(__file__).with_name("prompts") / "streamer_system.md"
DEFAULT_TASK_PROMPT_PATH = Path(__file__).with_name("prompts") / "streamer_task.md"


@dataclass
class GameSession:
    """Lifecycle wrapper. Call ``await start()`` before reading credentials."""

    text_mode: bool = False
    _creds: Optional[GBStartResult] = field(default=None, init=False, repr=False)
    context_store: GameContextStore = field(default_factory=GameContextStore, init=False)
    turn_buffer: GameTurnBuffer = field(default=None, init=False, repr=False)  # type: ignore[assignment]

    def __post_init__(self):
        # In text mode, flush as soon as the Ship AI's LLM stops generating.
        # In audio mode, wait for bot-stopped-speaking (or ship.speech_stopped).
        self.turn_buffer = GameTurnBuffer(flush_on_llm_stopped=self.text_mode)

    async def start(self) -> GBStartResult:
        """Login + create or reuse character + start session. Idempotent on
        the same instance — calling twice returns the cached creds."""
        if self._creds is None:
            self._creds = await login_and_start()
        return self._creds

    @property
    def creds(self) -> GBStartResult:
        if self._creds is None:
            raise RuntimeError("GameSession.start() not called yet")
        return self._creds

    @property
    def room_url(self) -> str:
        return self.creds.room_url

    @property
    def room_token(self) -> str:
        return self.creds.room_token

    @property
    def session_id(self) -> str:
        return self.creds.session_id

    @property
    def character_id(self) -> str:
        return self.creds.character_id

    def load_system_prompt(self) -> str:
        """Concatenates ``streamer_system.md`` with the active task prompt."""
        task_path = Path(os.environ.get("GB_TASK_PROMPT_PATH", str(DEFAULT_TASK_PROMPT_PATH)))
        parts = [
            SYSTEM_PROMPT_PATH.read_text().strip(),
            task_path.read_text().strip(),
        ]
        return "\n\n".join(p for p in parts if p)

    def handle_app_message(self, message: dict) -> Optional[str]:
        """Convenience: route a Daily app-message through both the context
        store (updates state) and the turn buffer (returns text if Ship AI
        finished an utterance)."""
        self.context_store.handle_message(message)
        return self.turn_buffer.handle_message(message)
