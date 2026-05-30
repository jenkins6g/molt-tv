"""Records the Officer's spoken turns into FailureStore for the beat ticker.

Sits AFTER ChatModeParser so the captured text is the audience-facing version
(with the ``[mode=X]`` tag already stripped). Game-event and audience-chat
user-turns are recorded at their source in bot.py / chat_bridge.py because
this processor only sees frames headed downstream toward the output stage.

Placement::

    llm  →  WaitTagFilter  →  ChatModeParser  →  TranscriptRecorder
         →  BotSpeechLogger  →  output_stage  →  transport.output()
"""

from __future__ import annotations

from typing import Optional

from loguru import logger
from pipecat.frames.frames import Frame, LLMFullResponseEndFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class TranscriptRecorder(FrameProcessor):
    def __init__(self, store, session_id: str):
        super().__init__()
        self._store = store
        self._session_id = session_id
        self._buffer: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMTextFrame) and direction == FrameDirection.DOWNSTREAM:
            text = frame.text or ""
            if text.strip():
                self._buffer.append(text)
        elif isinstance(frame, LLMFullResponseEndFrame) and direction == FrameDirection.DOWNSTREAM:
            self._flush()

        await self.push_frame(frame, direction)

    def _flush(self) -> None:
        text = "".join(self._buffer).strip()
        self._buffer.clear()
        if not text:
            return
        try:
            self._store.record_transcript_turn(self._session_id, "assistant", text)
        except Exception as e:
            logger.warning(f"[TRANSCRIPT] record_transcript_turn failed: {e}")
