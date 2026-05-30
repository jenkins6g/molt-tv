from __future__ import annotations
import json
import boto3
import numpy as np
from app.config import settings

_client = boto3.client("bedrock-runtime", region_name=settings.aws_region)


def embed(text: str) -> np.ndarray:
    body = json.dumps({"inputText": text})
    r = _client.invoke_model(modelId=settings.bedrock_embed_model, body=body)
    vec = json.loads(r["body"].read())["embedding"]
    arr = np.asarray(vec, dtype="float32")
    arr /= max(np.linalg.norm(arr), 1e-9)
    return arr


def embed_batch(texts: list[str]) -> np.ndarray:
    return np.vstack([embed(t) for t in texts]) if texts else np.zeros((0, 1024), dtype="float32")
