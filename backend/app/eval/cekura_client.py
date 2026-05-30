from __future__ import annotations
import httpx
from typing import Any, Optional

from loguru import logger

from app.config import settings


class CekuraClient:
    """Thin async client. Verify endpoint shapes with the sponsor on hackathon day —
    the schema here is a placeholder modeled on the spec."""

    def __init__(self):
        self._client = httpx.AsyncClient(
            base_url=settings.cekura_base_url,
            timeout=15.0,
            headers={"Authorization": f"Bearer {settings.cekura_api_key}"},
        )

    async def evaluate_call(
        self,
        call_id: str,
        transcript: list[dict],
        actual_ticket: dict,
        expected_ticket: Optional[dict] = None,
        rubric: str = "civicpilot.v1",
    ) -> dict:
        payload = {
            "call_id": call_id,
            "transcript": transcript,
            "actual_output": actual_ticket,
            "expected_output": expected_ticket,
            "rubric": rubric,
        }
        r = await self._client.post("/v1/evaluations", json=payload)
        r.raise_for_status()
        return r.json()

    async def evaluate_beat(
        self,
        beat_id: str,
        transcript_slice: list[dict],
        chat_slice: list[dict],
        game_state_summary: str,
        rubric: str = "moltstreamer.v1",
    ) -> dict:
        """Score a single MoltStreamer beat (~30s window).

        Returns ``{"status": "pass"|"fail"|"unknown", "score": float,
        "reasons": [str], "suggested_failure_mode": str|None}``. On HTTP /
        network failure returns a synthetic ``unknown`` row so callers can
        keep streaming. Verify the endpoint shape with the Cekura sponsor on
        hackathon day — both the path and payload here are best-guess."""
        payload = {
            "beat_id": beat_id,
            "transcript": transcript_slice,
            "chat_window": chat_slice,
            "game_state": game_state_summary,
            "rubric": rubric,
        }
        try:
            r = await self._client.post("/v1/evaluations", json=payload)
            r.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning(f"[CEKURA] evaluate_beat failed ({type(e).__name__}); marking unknown")
            return {
                "status": "unknown",
                "score": 0.0,
                "reasons": [f"cekura unavailable: {e}"],
                "suggested_failure_mode": None,
            }
        body = r.json()
        return {
            "status": body.get("status") or body.get("eval_status") or "unknown",
            "score": float(body.get("score") or 0.0),
            "reasons": body.get("reasons") or body.get("explanations") or [],
            "suggested_failure_mode": body.get("failure_mode")
            or body.get("suggested_failure_mode"),
        }

    async def push_observability(
        self,
        call_id: str,
        transcript: list[dict],
        chat_decisions: list[dict],
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Post-session dump of the full call to Cekura's observability surface.

        Best-effort: returns True on 2xx, False on any error. Never raises.
        Endpoint shape mirrors the docs.cekura.ai observability provider."""
        payload = {
            "call_id": call_id,
            "transcript": transcript,
            "chat_decisions": chat_decisions,
            "metadata": metadata or {},
        }
        try:
            r = await self._client.post("/v1/observability/calls", json=payload, timeout=5.0)
            r.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.warning(f"[CEKURA] push_observability failed: {e}")
            return False

    async def generate_regression_variants(
        self,
        transcript: list[dict],
        expected_ticket: dict,
        n: int = 3,
    ) -> list[dict]:
        try:
            r = await self._client.post(
                "/v1/simulations/variants",
                json={"transcript": transcript, "expected": expected_ticket, "n": n},
            )
            r.raise_for_status()
            return r.json().get("variants", [])
        except httpx.HTTPError:
            return []

    async def close(self):
        await self._client.aclose()
