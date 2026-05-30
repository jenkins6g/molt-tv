"""Periodic Cekura beat evaluation + auto-improvement write side.

Every BEAT_INTERVAL_SECONDS (default 30) this loop:

1. Builds a beat from the last interval's transcript turns, chat decisions,
   and current game-state snapshot.
2. Skips if nothing happened in the window (no assistant text + no chat).
3. POSTs the beat to ``CekuraClient.evaluate_beat``.
4. Persists the beat row via ``FailureStore.record_beat``.
5. On FAIL: tags the failure with Bedrock Haiku, embeds the offending
   utterance with Bedrock Titan, and writes a row + FAISS vector via
   ``FailureStore.add_failure``. This is what the
   ``FailureRetrievalInjector`` reads on the next turn.

Disabled cleanly via ``BEAT_TICKER_ENABLED=false`` — the task starts, logs
that it's disabled, and returns.

Run as ``asyncio.create_task(BeatTicker(...).run())`` next to
``ChatBridge.drain`` in bot.py. Cancel on shutdown.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Optional

from loguru import logger


def _enabled() -> bool:
    return os.environ.get("BEAT_TICKER_ENABLED", "true").lower() != "false"


def _interval_seconds() -> float:
    try:
        return float(os.environ.get("BEAT_INTERVAL_SECONDS", "30"))
    except ValueError:
        return 30.0


class BeatTicker:
    def __init__(
        self,
        store,
        cekura_client,
        context_store,
        session_id: str,
        rubric: str = "moltstreamer.v1",
    ):
        self._store = store
        self._cekura = cekura_client
        self._context_store = context_store
        self._session_id = session_id
        self._rubric = rubric
        self._stopped = False
        self._last_tick = time.time()

    async def run(self) -> None:
        if not _enabled():
            logger.info("[BEAT] disabled via BEAT_TICKER_ENABLED=false")
            return
        interval = _interval_seconds()
        logger.info(f"[BEAT] ticker started, interval={interval}s, session={self._session_id}")
        try:
            while not self._stopped:
                await asyncio.sleep(interval)
                if self._stopped:
                    break
                try:
                    await self._tick(interval)
                except Exception as e:
                    logger.exception(f"[BEAT] tick failed: {e}")
        except asyncio.CancelledError:
            logger.info("[BEAT] ticker cancelled")
            raise

    def stop(self) -> None:
        self._stopped = True

    async def _tick(self, interval: float) -> None:
        ended_at = time.time()
        started_at = ended_at - interval

        transcript = self._store.recent_transcript(
            since=started_at, session_id=self._session_id
        )
        chat_window = [
            d for d in self._store.recent_chat_decisions(limit=40)
            if d.get("injected_at", 0) >= started_at
        ]

        if not transcript and not chat_window:
            logger.debug("[BEAT] empty window, skipping")
            return

        game_state = self._context_store.render() or ""
        beat_id = uuid.uuid4().hex

        eval_result = await self._cekura.evaluate_beat(
            beat_id=beat_id,
            transcript_slice=transcript,
            chat_slice=chat_window,
            game_state_summary=game_state,
            rubric=self._rubric,
        )
        status = eval_result.get("status", "unknown")
        score = float(eval_result.get("score") or 0.0)
        reasons = eval_result.get("reasons") or []
        suggested = eval_result.get("suggested_failure_mode")

        logger.info(
            f"[BEAT] {beat_id[:8]} status={status} score={score:.2f} "
            f"transcript_turns={len(transcript)} chat={len(chat_window)}"
        )

        self._store.record_beat(
            beat_id=beat_id,
            session_id=self._session_id,
            started_at=started_at,
            ended_at=ended_at,
            transcript=transcript,
            chat_window=chat_window,
            game_state=game_state,
            eval_status=status,
            eval_score=score,
            eval_reasons=reasons,
            suggested_mode=suggested,
        )

        if status == "fail":
            await self._record_failure(beat_id, transcript, chat_window, reasons, suggested)

    async def _record_failure(
        self,
        beat_id: str,
        transcript: list[dict],
        chat_window: list[dict],
        reasons: list[str],
        suggested_mode: Optional[str],
    ) -> None:
        last_chat = chat_window[-1] if chat_window else {}
        last_assistant = next(
            (t for t in reversed(transcript) if t.get("role") == "assistant"),
            None,
        )
        actual_ticket = {
            "viewer": last_chat.get("viewer"),
            "chat_text": last_chat.get("text"),
            "mode": last_chat.get("mode") or suggested_mode,
            "response_text": (last_assistant or {}).get("text"),
        }
        cekura_reason = "; ".join(str(r) for r in reasons) or "score below pass threshold"
        transcript_text = "\n".join(f"{t['role']}: {t['text']}" for t in transcript)

        try:
            tag = await asyncio.to_thread(
                self._tag_failure, transcript_text, actual_ticket, cekura_reason
            )
        except Exception as e:
            logger.warning(f"[BEAT] tag_failure failed: {e}")
            return

        utterance = tag.get("utterance") or actual_ticket.get("chat_text") or ""
        if not utterance:
            logger.debug("[BEAT] no utterance to embed, skipping add_failure")
            return

        try:
            vec = await asyncio.to_thread(self._embed, utterance)
        except Exception as e:
            logger.warning(f"[BEAT] embed failed: {e}")
            return

        try:
            self._store.add_failure(
                call_id=beat_id,
                lang="en",
                failure_mode=tag.get("failure_mode") or suggested_mode or "boring_commentary",
                utterance=utterance,
                wrong_output=tag.get("wrong_output", ""),
                correct_output=tag.get("correct_output", ""),
                embedding=vec,
            )
            logger.info(
                f"[BEAT] recorded failure mode={tag.get('failure_mode')} "
                f"beat={beat_id[:8]}"
            )
        except Exception as e:
            logger.warning(f"[BEAT] add_failure failed: {e}")

    @staticmethod
    def _tag_failure(transcript: str, actual_ticket: dict, cekura_reason: str) -> dict:
        from app.memory.tagger import tag_failure
        return tag_failure(
            transcript=transcript,
            actual_ticket=actual_ticket,
            cekura_reason=cekura_reason,
            expected_ticket=None,
        )

    @staticmethod
    def _embed(text: str):
        from app.services.embed import embed
        return embed(text)
