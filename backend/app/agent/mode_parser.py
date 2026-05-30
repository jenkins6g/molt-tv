"""Chat-response mode parser.

Pipecat FrameProcessor that sits AFTER WaitTagFilter and BEFORE the TTS /
text-emitter stage. It:

1. Watches each LLMTextFrame the Officer emits in a turn.
2. If the response starts with ``[mode=TAKE|ROAST|ACK|IGNORE]``, extracts
   the mode, strips it from the text, and emits the cleaned text downstream.
3. Records the mode against the most recent pending chat decision in the
   FailureStore (linking ``chat_in`` to ``response_out + mode``).

Game-event responses (no preceding chat) won't start with ``[mode=...]`` —
those pass through untouched.

Placement (in bot.py's pipeline):

    llm  →  wait_tag_filter  →  ChatModeParser(store)  →  bot_speech_logger
         →  output_stage  →  transport.output()

By the time frames reach this processor WaitTagFilter has already collapsed
the streamed chunks into either zero frames (``<wait>`` was suppressed) or
one ``LLMTextFrame`` followed by ``LLMFullResponseEndFrame`` — so we don't
have to deal with multi-chunk streaming.
"""

from __future__ import annotations

import re
from typing import Optional

from loguru import logger
from pipecat.frames.frames import Frame, LLMFullResponseEndFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


_VALID_MODES = {"TAKE", "ROAST", "ACK", "IGNORE"}
_TAG_RE = re.compile(r"^\s*\[mode=(?P<mode>[A-Za-z_]+)\]\s*", re.IGNORECASE)


class ChatModeParser(FrameProcessor):
    """Strip ``[mode=X]`` tag from Officer responses; link mode to pending chat."""

    def __init__(self, store=None):
        """``store`` is a FailureStore-like object exposing ``link_chat_mode``.

        Passing ``None`` makes the parser act as a pure tag stripper (useful
        for unit tests). In production, pass the singleton FailureStore.
        """
        super().__init__()
        self._store = store
        self._last_mode: Optional[str] = None
        self._last_response: Optional[str] = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMTextFrame) and direction == FrameDirection.DOWNSTREAM:
            cleaned, mode = self._strip_tag(frame.text)
            if mode is not None:
                self._last_mode = mode
                self._last_response = cleaned
                logger.info(f"[CHAT MODE] {mode} → {cleaned[:80]!r}")
                if cleaned:
                    await self.push_frame(LLMTextFrame(cleaned), direction)
                # else: empty body (e.g. IGNORE mode with no follow-up) — drop frame
                return
            # No tag — pass through unchanged (this is a game-event response).
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame) and direction == FrameDirection.DOWNSTREAM:
            # End of a turn — if we saw a mode, persist the link.
            if self._last_mode is not None and self._store is not None:
                try:
                    self._store.link_chat_mode(
                        mode=self._last_mode,
                        response_text=self._last_response or "",
                    )
                except Exception as e:
                    logger.warning(f"[CHAT MODE] link_chat_mode failed: {e}")
            self._last_mode = None
            self._last_response = None

        await self.push_frame(frame, direction)

    def _strip_tag(self, text: str) -> tuple[str, Optional[str]]:
        m = _TAG_RE.match(text)
        if not m:
            return text, None
        mode = m.group("mode").upper()
        if mode not in _VALID_MODES:
            logger.warning(f"[CHAT MODE] invalid mode {mode!r}; treating as ACK")
            mode = "ACK"
        return text[m.end():], mode
