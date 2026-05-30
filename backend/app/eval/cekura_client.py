from __future__ import annotations
import httpx
from typing import Optional

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
