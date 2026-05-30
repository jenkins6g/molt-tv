from __future__ import annotations
import json
from typing import Optional

import boto3

from app.config import settings
from app.agent.prompts import failure_tagger_prompt

_client = boto3.client("bedrock-runtime", region_name=settings.aws_region)


def tag_failure(
    transcript: str,
    actual_ticket: dict,
    cekura_reason: str,
    expected_ticket: Optional[dict] = None,
) -> dict:
    prompt = failure_tagger_prompt(transcript, actual_ticket, cekura_reason, expected_ticket)
    r = _client.converse(
        modelId=settings.bedrock_tagger_model,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 300, "temperature": 0},
    )
    text = r["output"]["message"]["content"][0]["text"]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])
