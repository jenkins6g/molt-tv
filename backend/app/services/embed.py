from __future__ import annotations
import numpy as np

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def embed(text: str) -> np.ndarray:
    arr = _get_model().encode(text, normalize_embeddings=True)
    return arr.astype("float32")


def embed_batch(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, 384), dtype="float32")
    arrs = _get_model().encode(texts, normalize_embeddings=True)
    return arrs.astype("float32")
