"""Pipecat FrameProcessors for the Officer pipeline.

All of these are verbatim ports from chadbailey59/gb-bot bot-v5.py with
their original line numbers noted. They have no Gradient-Bang-specific
state — they only depend on ``GameContextStore`` from ``game_state.py``.

Drop them into ``Pipeline([...])`` in this order:

    transport.input()
        ↓
    transport_input_timing_logger          (TransportSpeechTimingLogger)
        ↓
    user_aggregator                        (Pipecat built-in)
        ↓
    game_context_injector                  (GameContextInjector)
        ↓
    llm                                    (Nemotron via VLLMOpenAILLMService)
        ↓
    wait_tag_filter                        (WaitTagFilter)
        ↓
    bot_speech_logger                      (BotSpeechLogger)
        ↓
    output_stage                           (GradiumTTSService for audio mode,
                                            or TextModeEmitter for text mode)
        ↓
    transport.output()
        ↓
    transport_output_timing_logger         (TransportSpeechTimingLogger)
        ↓
    assistant_aggregator                   (Pipecat built-in)
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from typing import Any, cast

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputTransportMessageUrgentFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from game_state import GameContextStore


class GameContextInjector(FrameProcessor):
    """Injects current game context into the latest user message before the LLM.

    Verbatim port of bot-v5.py lines 525–569.
    """

    def __init__(self, game_context: GameContextStore):
        super().__init__()
        self._game_context = game_context

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        game_context = self._game_context.render()
        if not game_context:
            await self.push_frame(frame, direction)
            return

        messages: list[Any] = list(frame.context.get_messages())
        last_user_index = self._find_last_user_text_message(messages)
        if last_user_index is None:
            await self.push_frame(frame, direction)
            return

        original = dict(cast(dict[str, Any], messages[last_user_index]))
        original_content = cast(str, original["content"])
        original["content"] = (
            f"{game_context}\n\nMost recent Ship AI speech:\n{original_content}"
        )
        messages[last_user_index] = original

        enriched_context = LLMContext(
            messages=messages,
            tools=frame.context.tools,
            tool_choice=frame.context.tool_choice,
        )
        await self.push_frame(LLMContextFrame(enriched_context), direction)

    def _find_last_user_text_message(self, messages: list[Any]) -> int | None:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not isinstance(message, dict):
                continue
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                return index
        return None


class WaitTagFilter(FrameProcessor):
    """Suppresses the model's <wait> control response before it reaches TTS.

    Verbatim port of bot-v5.py lines 593–631. The system prompt instructs
    the Officer to reply with exactly ``<wait>`` while the Ship AI is
    executing — this filter strips that turn so TTS doesn't speak it and
    the Ship AI hears silence.
    """

    def __init__(self, on_wait: Callable[[], None] | None = None):
        super().__init__()
        self._on_wait = on_wait
        self._parts: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._parts.clear()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMTextFrame):
            self._parts.append(frame.text)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            text = "".join(self._parts)
            self._parts.clear()
            if self._is_wait_tag(text):
                logger.info("[BOT WAIT] Suppressing <wait> while Ship AI continues")
                if self._on_wait:
                    self._on_wait()
                await self.push_frame(frame, direction)
                return
            if text:
                await self.push_frame(LLMTextFrame(text), direction)
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    def _is_wait_tag(self, text: str) -> bool:
        normalized = re.sub(r"\s+", "", text.strip().lower())
        return normalized in {"<wait>", "<wait/>", "<wait></wait>"}


class BotSpeechLogger(FrameProcessor):
    """Logs each completed bot utterance to stdout for observability.

    Verbatim port of bot-v5.py lines 572–590.
    """

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMTextFrame):
            self._parts.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            text = " ".join("".join(self._parts).split())
            if text:
                logger.info(f"[BOT SPEECH] {text}")
            self._parts.clear()

        await self.push_frame(frame, direction)


class TextModeEmitter(FrameProcessor):
    """Sends each LLM response to the Ship AI as a Daily 'user-text-input'
    app-message instead of running it through TTS.

    Verbatim port of bot-v5.py lines 634–656. The buddy probably won't use
    this for the streamer demo (we want audio mode so the audience hears the
    bot), but it's the right fallback if Gradium fails.
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame) and direction == FrameDirection.DOWNSTREAM:
            text = (frame.text or "").strip()
            if text:
                msg = {
                    "label": "rtvi-ai",
                    "type": "client-message",
                    "id": uuid.uuid4().hex,
                    "data": {"t": "user-text-input", "d": {"text": text}},
                }
                logger.info(f"[TEXT OUT] -> Ship AI: {text}")
                await self.push_frame(
                    OutputTransportMessageUrgentFrame(message=msg), direction
                )
        await self.push_frame(frame, direction)


class SpeechTimingState:
    """Shared latency-measurement state between input and output timing loggers.

    Verbatim port of bot-v5.py lines 659–676.
    """

    def __init__(self):
        self.last_bot_stopped_at: float | None = None
        self.user_speaking = False

    def elapsed_since_bot_stopped(self, now: float, consume: bool = False) -> float | None:
        if self.last_bot_stopped_at is None:
            return None
        elapsed = now - self.last_bot_stopped_at
        if consume:
            self.last_bot_stopped_at = None
        return elapsed

    def reset_for_wait_response(self) -> None:
        if self.last_bot_stopped_at is not None:
            logger.info("[SPEECH TIMING] Reset pending bot-stop timer after <wait>")
        self.last_bot_stopped_at = None
        self.user_speaking = False


class TransportSpeechTimingLogger(FrameProcessor):
    """Measures latency between bot stopping and the next user/game event.

    Verbatim port of bot-v5.py lines 679–744. Drop one instance at
    ``transport input`` and another at ``transport output`` (per the
    pipeline order at the top of this file).
    """

    def __init__(self, location: str, timing: SpeechTimingState):
        super().__init__()
        self._location = location
        self._timing = timing

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        now = time.perf_counter()
        if self._is_target_output_frame(frame, direction, BotStoppedSpeakingFrame):
            self._timing.last_bot_stopped_at = now
            self._timing.user_speaking = False
            logger.info(
                f"[SPEECH TIMING] BotStoppedSpeaking after {self._location} "
                f"direction={direction.name}"
            )
        elif self._is_target_input_frame(frame, direction, UserStartedSpeakingFrame):
            self._timing.user_speaking = True
            elapsed = self._timing.elapsed_since_bot_stopped(now, consume=True)
            elapsed_text = "unknown" if elapsed is None else f"{elapsed:.3f}s"
            logger.info(
                f"[SPEECH TIMING] UserStartedSpeaking after {self._location} "
                f"direction={direction.name} elapsed_since_bot_stopped={elapsed_text}"
            )
        elif self._is_target_input_frame(frame, direction, BotStartedSpeakingFrame):
            elapsed = self._timing.elapsed_since_bot_stopped(now)
            elapsed_text = "unknown" if elapsed is None else f"{elapsed:.3f}s"
            logger.info(
                f"[SPEECH TIMING] BotStartedSpeaking after {self._location} "
                f"direction={direction.name} elapsed_since_bot_stopped={elapsed_text}"
            )

        await self.push_frame(frame, direction)

    def _is_target_output_frame(
        self, frame: Frame, direction: FrameDirection, frame_type: type[Frame]
    ) -> bool:
        return (
            self._location == "transport output"
            and direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, frame_type)
        )

    def _is_target_input_frame(
        self, frame: Frame, direction: FrameDirection, frame_type: type[Frame]
    ) -> bool:
        return (
            self._location == "transport input"
            and direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, frame_type)
        )
