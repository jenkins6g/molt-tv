"""Cekura observability HTTP client.

Canonical API (verified against docs.cekura.ai and cekura-skills):

- Auth: ``X-CEKURA-API-KEY`` header (NOT Authorization Bearer).
- Ingest a call (auto-runs configured metrics, async):
    ``POST /observability/v1/observe/``  ← trailing slash required.
    Body: ``{agent, call_id, transcript_type, transcript_json, ...}``.
    Reuse the same ``call_id`` for incremental updates within a session.
- Fetch scored results (poll):
    ``GET  /observability/v1/call-logs-external/?agent=<id>``
    ``GET  /observability/v1/call-logs-external/{call_log_id}/``

Scoring is asynchronous: ``observe/`` returns immediately, metric scores
populate seconds later. The BeatTicker reuses ``session_id`` as ``call_id``
and on each tick (a) re-observes the cumulative transcript and (b) reads
back whatever metrics have populated from prior observe() calls.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from loguru import logger

from app.config import settings


# Map our internal roles to Cekura's two allowed roles.
# 'game' = Ship AI / game events; 'chat' = audience chat. Both are inputs to
# the bot, so they collapse to 'user'. 'assistant' stays 'assistant'.
_ROLE_MAP = {
    "assistant": "assistant",
    "user": "user",
    "game": "user",
    "chat": "user",
}


def _to_cekura_transcript(turns: list[dict]) -> list[dict]:
    """Map our ``{role, text, at}`` rows to Cekura's
    ``{role, content, start_time, end_time}`` schema."""
    base = turns[0]["at"] if turns else 0.0
    out = []
    for t in turns:
        role = _ROLE_MAP.get(t.get("role", "user"), "user")
        text = (t.get("text") or "").strip()
        if not text:
            continue
        at = float(t.get("at") or base)
        rel = max(0.0, at - base)
        out.append({
            "role": role,
            "content": text,
            "start_time": rel,
            "end_time": rel + 1.0,
        })
    return out


class CekuraClient:
    """Async HTTP client for Cekura observability ingest + score polling."""

    def __init__(self):
        self._client = httpx.AsyncClient(
            base_url=settings.cekura_base_url,
            timeout=15.0,
            headers={
                "X-CEKURA-API-KEY": settings.cekura_api_key,
                "Content-Type": "application/json",
            },
        )
        self._agent_id = settings.cekura_agent_id

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def configured(self) -> bool:
        return bool(settings.cekura_api_key and self._agent_id)

    async def observe_call(
        self,
        call_id: str,
        transcript: list[dict],
        ended: bool = False,
        agent_id: Optional[str] = None,
    ) -> bool:
        """Push (or re-push) a cumulative transcript for a session.

        Best-effort: True on 2xx, False on any error. Never raises so the
        BeatTicker keeps going."""
        agent = agent_id or self._agent_id
        if not agent:
            logger.warning("[CEKURA] CEKURA_AGENT_ID not set; skipping observe")
            return False
        if not transcript:
            return False
        payload: dict[str, Any] = {
            "agent": agent,
            "call_id": call_id,
            "transcript_type": "pipecat",
            "transcript_json": _to_cekura_transcript(transcript),
        }
        if ended:
            payload["call_ended_reason"] = "session_end"
        try:
            r = await self._client.post("/observability/v1/observe/", json=payload)
            r.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.warning(f"[CEKURA] observe_call failed: {e}")
            return False

    async def get_call_scores(
        self,
        call_id: str,
        agent_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Fetch the most recent scored evaluation for ``call_id``.

        Returns a dict shaped like::

            {
              "status": "pass" | "fail" | "pending" | "unknown",
              "scores": {<metric_name>: <0-1 float>, ...},
              "raw": <full call-log JSON>,
            }

        Returns ``None`` if no matching call-log is found (e.g. ingest still
        propagating, or auth fails)."""
        agent = agent_id or self._agent_id
        if not agent:
            return None
        try:
            r = await self._client.get(
                "/observability/v1/call-logs-external/",
                params={"agent_id": agent},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning(f"[CEKURA] list call-logs failed: {e}")
            return None
        body = r.json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list):
            return None
        match = next((row for row in items if row.get("call_id") == call_id), None)
        if not match:
            return None
        return self._normalize_scores(match)

    @staticmethod
    def _normalize_scores(call_log: dict) -> dict:
        """Collapse Cekura's call-log JSON to ``{status, scores, raw}``.

        Verdict precedence:
        1. Top-level ``status == "evaluating"`` → "pending".
        2. Any per-metric verdict failed → "fail".
        3. Top-level ``success == False`` (and metrics present) → "fail".
        4. Otherwise if any scores present → "pass".
        5. Else "unknown"."""
        scores: dict[str, float] = {}
        per_metric_statuses: list[str] = []
        metrics_list = (
            call_log.get("metrics")
            or call_log.get("metric_results")
            or call_log.get("evaluations")
            or []
        )
        for m in metrics_list:
            name = m.get("metric_name") or m.get("name")
            sc = m.get("score")
            st = m.get("status") or m.get("verdict") or m.get("result")
            if name and sc is not None:
                try:
                    scores[name] = float(sc)
                except (TypeError, ValueError):
                    pass
            if st:
                per_metric_statuses.append(str(st).lower())

        top_status = str(call_log.get("status") or "").lower()
        success = call_log.get("success")

        if top_status in {"evaluating", "pending", "queued", "running"}:
            status = "pending"
        elif any(s in {"fail", "failed", "below_threshold", "false"}
                 for s in per_metric_statuses):
            status = "fail"
        elif success is False and metrics_list:
            status = "fail"
        elif scores or any(s in {"pass", "passed", "true"} for s in per_metric_statuses):
            status = "pass"
        else:
            status = "unknown"
        return {"status": status, "scores": scores, "raw": call_log}

    async def close(self):
        await self._client.aclose()
