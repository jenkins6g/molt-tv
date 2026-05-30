from __future__ import annotations
import json
import os
from typing import Optional

from openai import OpenAI

from app.agent.prompts import failure_tagger_prompt

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        )
    return _client


_MODEL = os.environ.get("OPENROUTER_TAGGER_MODEL", "meta-llama/llama-3.3-70b-instruct")


def tag_failure(
    transcript: str,
    actual_ticket: dict,
    cekura_reason: str,
    expected_ticket: Optional[dict] = None,
) -> dict:
    prompt = failure_tagger_prompt(transcript, actual_ticket, cekura_reason, expected_ticket)
    r = _get_client().chat.completions.create(
        model=_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
        temperature=0,
    )
    text = r.choices[0].message.content or ""
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])
