"""Periodic Cekura observability ingest + auto-improvement write side.

Cekura's runtime model: one ``call_id`` per session; the cumulative
transcript is re-posted to ``/observability/v1/observe/`` as it grows;
metric scores populate asynchronously. So every BEAT_INTERVAL_SECONDS
(default 30) this loop:

1. Reads the cumulative session transcript + chat decisions from
   FailureStore.
2. POSTs them to Cekura with ``call_id = session_id`` (ingest).
3. GETs the scored result for that ``call_id`` — which reflects whatever
   Cekura has finished scoring from prior ticks (the first ~30s of a
   session is therefore "pending").
4. Persists a ``beats`` row capturing this snapshot.
5. On a fail verdict with a NEW assistant utterance (deduped against the
   last tagged one), tags the failure with Bedrock Haiku, embeds the
   offending utterance with Bedrock Titan, and writes to FAISS via
   ``FailureStore.add_failure``. The next LLM turn picks it up through
   ``FailureRetrievalInjector``.

Toggle off entirely with ``BEAT_TICKER_ENABLED=false`` for a "baseline
without auto-improvement" demo run.
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
    ):
        self._store = store
        self._cekura = cekura_client
        self._context_store = context_store
        self._session_id = session_id
        self._stopped = False
        self._last_tagged_assistant_text: Optional[str] = None

    async def run(self) -> None:
        if not _enabled():
            logger.info("[BEAT] disabled via BEAT_TICKER_ENABLED=false")
            return
        if not self._cekura.configured:
            logger.warning(
                "[BEAT] Cekura not configured (CEKURA_API_KEY or "
                "CEKURA_AGENT_ID missing); ticker will still snapshot beats "
                "but eval_status will be 'unknown'"
            )
        interval = _interval_seconds()
        logger.info(f"[BEAT] ticker started, interval={interval}s, session={self._session_id}")
        try:
            while not self._stopped:
                await asyncio.sleep(interval)
                if self._stopped:
                    break
                try:
                    await self._tick()
                except Exception as e:
                    logger.exception(f"[BEAT] tick failed: {e}")
        except asyncio.CancelledError:
            logger.info("[BEAT] ticker cancelled")
            raise

    def stop(self) -> None:
        self._stopped = True

    async def _tick(self) -> None:
        ended_at = time.time()

        transcript = self._store.recent_transcript(
            since=0.0, limit=10000, session_id=self._session_id
        )
        chat_window = self._store.recent_chat_decisions(limit=200)

        if not transcript and not chat_window:
            logger.debug("[BEAT] nothing happened yet, skipping")
            return

        # Ingest cumulative transcript (no-op if Cekura unconfigured)
        ingested = await self._cekura.observe_call(
            call_id=self._session_id, transcript=transcript
        )

        # Poll for scores — reflects what Cekura finished scoring from
        # earlier ingests; first tick of a session almost always pending.
        result = None
        if self._cekura.configured:
            result = await self._cekura.get_call_scores(call_id=self._session_id)

        status = (result or {}).get("status", "pending" if ingested else "unknown")
        scores = (result or {}).get("scores", {})
        agg_score = sum(scores.values()) / len(scores) if scores else 0.0
        reasons = [f"{k}: {v:.2f}" for k, v in scores.items()]

        beat_id = uuid.uuid4().hex
        started_at = ended_at - _interval_seconds()
        game_state = self._context_store.render() or ""

        logger.info(
            f"[BEAT] {beat_id[:8]} status={status} avg_score={agg_score:.2f} "
            f"metrics={len(scores)} transcript_turns={len(transcript)} "
            f"chat={len(chat_window)}"
        )

        self._store.record_beat(
            beat_id=beat_id,
            session_id=self._session_id,
            started_at=started_at,
            ended_at=ended_at,
            transcript=transcript[-50:],  # cap stored slice to keep row size sane
            chat_window=chat_window[:50],
            game_state=game_state,
            eval_status=status,
            eval_score=agg_score,
            eval_reasons=reasons,
            suggested_mode=None,
        )

        if status == "fail":
            await self._maybe_record_failure(beat_id, transcript, chat_window, scores)

    async def _maybe_record_failure(
        self,
        beat_id: str,
        transcript: list[dict],
        chat_window: list[dict],
        scores: dict,
    ) -> None:
        last_assistant = next(
            (t for t in reversed(transcript) if t.get("role") == "assistant"),
            None,
        )
        if not last_assistant:
            logger.debug("[BEAT] fail but no assistant turn yet; skip tag")
            return
        assistant_text = (last_assistant.get("text") or "").strip()
        if assistant_text == self._last_tagged_assistant_text:
            logger.debug("[BEAT] fail repeats prior tagged utterance; skip")
            return

        last_chat = chat_window[-1] if chat_window else {}
        actual_ticket = {
            "viewer": last_chat.get("viewer"),
            "chat_text": last_chat.get("text"),
            "mode": last_chat.get("mode"),
            "response_text": assistant_text,
            "metric_scores": scores,
        }
        cekura_reason = ", ".join(f"{k}={v:.2f}" for k, v in scores.items()) or "fail"
        transcript_text = "\n".join(
            f"{t['role']}: {t['text']}" for t in transcript[-20:]
        )

        try:
            tag = await asyncio.to_thread(
                self._tag_failure, transcript_text, actual_ticket, cekura_reason
            )
        except Exception as e:
            logger.warning(f"[BEAT] tag_failure failed: {e}")
            return

        utterance = tag.get("utterance") or assistant_text
        try:
            vec = await asyncio.to_thread(self._embed, utterance)
        except Exception as e:
            logger.warning(f"[BEAT] embed failed: {e}")
            return

        try:
            self._store.add_failure(
                call_id=beat_id,
                lang="en",
                failure_mode=tag.get("failure_mode") or "boring_commentary",
                utterance=utterance,
                wrong_output=tag.get("wrong_output", assistant_text),
                correct_output=tag.get("correct_output", ""),
                embedding=vec,
            )
            self._last_tagged_assistant_text = assistant_text
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

    async def final_flush(self) -> bool:
        """End-of-session: post the cumulative transcript with ``ended=True``
        so Cekura's UI shows the call as completed. Best-effort."""
        transcript = self._store.recent_transcript(
            since=0.0, limit=10000, session_id=self._session_id
        )
        if not transcript:
            return False
        return await self._cekura.observe_call(
            call_id=self._session_id, transcript=transcript, ended=True
        )
