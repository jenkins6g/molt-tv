"""Retrieves similar past failures and injects them as few-shots before the LLM call.

Closes the read-side of the auto-improvement loop. The write side lives in
``beat_ticker.py``: it scores beats with Cekura, tags failures with OpenRouter,
embeds them with sentence-transformers, and stores them in FailureStore. This processor
queries FailureStore on every LLM turn for the top-k most semantically
similar past failures and prepends them to the context as a system message
right before the latest user message — the strongest steering position
without overriding the streamer system prompt.

Best-effort: any embed / search exception is logged and the turn proceeds
with the original context untouched.

Placement (BEFORE the LLM)::

    user_aggregator  →  GameContextInjector  →  FailureRetrievalInjector
                     →  llm  →  WaitTagFilter  →  ...

Env-gate: ``FAILURE_RETRIEVAL_ENABLED=false`` makes this a no-op (useful for
demo A/B: "without retrieval the bot was boring; with it, here's the same
prompt and now it's funny").
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, cast

from loguru import logger
from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


def _retrieval_enabled() -> bool:
    return os.environ.get("FAILURE_RETRIEVAL_ENABLED", "true").lower() != "false"


class FailureRetrievalInjector(FrameProcessor):
    def __init__(self, store, k: int = 3, min_score: float = 0.78):
        super().__init__()
        self._store = store
        self._k = k
        self._min_score = min_score
        self._log_memory(store)

    @staticmethod
    def _log_memory(store) -> None:
        n = store._index.ntotal
        if n == 0:
            logger.info("[MEMORY] FAISS index empty — no past failures loaded")
            return
        logger.info(f"[MEMORY] {n} past failure(s) loaded from FAISS:")
        rows = store._db.execute(
            "SELECT failure_mode, utterance, wrong_output FROM failures ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        for mode, utterance, wrong in rows:
            logger.info(f"  [{mode}] when='{utterance}' → avoid='{wrong}'")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame) or not _retrieval_enabled():
            await self.push_frame(frame, direction)
            return

        messages: list[Any] = list(frame.context.get_messages())
        last_user_idx, last_user_text = self._find_last_user_text(messages)
        if last_user_idx is None or not last_user_text:
            await self.push_frame(frame, direction)
            return

        try:
            results = await asyncio.to_thread(self._search, last_user_text)
        except Exception as e:
            logger.warning(f"[RETRIEVAL] failed: {e}")
            await self.push_frame(frame, direction)
            return

        if not results:
            await self.push_frame(frame, direction)
            return

        few_shot = self._format(results)
        logger.info(f"[RETRIEVAL] injected {len(results)} past failures:")
        for f, score in results:
            logger.info(f"  [{f.failure_mode}] score={score:.2f} — avoid: {f.wrong_output!r}")
        messages.insert(last_user_idx, {"role": "system", "content": few_shot})

        enriched = LLMContext(
            messages=messages,
            tools=frame.context.tools,
            tool_choice=frame.context.tool_choice,
        )
        await self.push_frame(LLMContextFrame(enriched), direction)

    def _search(self, text: str) -> list[tuple]:
        from app.services.embed import embed
        vec = embed(text)
        return self._store.search(vec, k=self._k, min_score=self._min_score)

    @staticmethod
    def _find_last_user_text(messages: list[Any]) -> tuple[int | None, str]:
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if isinstance(m, dict) and m.get("role") == "user":
                content = m.get("content")
                if isinstance(content, str) and content.strip():
                    return i, content
        return None, ""

    @staticmethod
    def _format(results: list[tuple]) -> str:
        lines = ["Past MoltStreamer mistakes to avoid right now (don't repeat these patterns):"]
        for idx, (failure, score) in enumerate(results, start=1):
            f = cast(Any, failure)
            lines.append(
                f"{idx}. mode={f.failure_mode} score={score:.2f} — "
                f"when input was {f.utterance!r}, "
                f"you wrongly said {f.wrong_output!r}; "
                f"better would have been {f.correct_output!r}."
            )
        lines.append("Use these as cautionary examples; don't reference them out loud.")
        return "\n".join(lines)
