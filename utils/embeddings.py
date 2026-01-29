import json
import os
import time
import hashlib
from datetime import datetime

from utils.metrics import increment_metric
import db

try:
    from openai import OpenAI

    _HAS_OPENAI = True
except Exception:
    OpenAI = None
    _HAS_OPENAI = False

try:
    import numpy as np

    _HAS_NUMPY = True
except Exception:
    np = None
    _HAS_NUMPY = False

# Optional local encoder fallback using sentence-transformers
try:
    from sentence_transformers import SentenceTransformer

    _HAS_SBER = True
except Exception:
    SentenceTransformer = None
    _HAS_SBER = False

_LOCAL_MODEL_NAME = os.getenv("LOCAL_EMBED_MODEL", "all-MiniLM-L6-v2")
_LOCAL_MODEL = None

EMBED_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
_EMBED_CACHE = {"ts": 0.0, "data": None}
_EMBED_CACHE_TTL = float(os.getenv("EMBED_CACHE_TTL", "60"))


def get_openai_client():
    if not _HAS_OPENAI:
        raise RuntimeError("OpenAI library not available")
    return OpenAI()


def _get_local_model():
    global _LOCAL_MODEL
    if _LOCAL_MODEL is None:
        if not _HAS_SBER:
            raise RuntimeError("Local sentence-transformers model not available")
        _LOCAL_MODEL = SentenceTransformer(_LOCAL_MODEL_NAME)
    return _LOCAL_MODEL


def embed_text(client, text: str, model: str | None = None) -> list[float]:
    """Compute embedding for `text`.

    - If `client` is provided, use OpenAI embeddings API.
    - If `client` is None and a local SentenceTransformer is installed, use it.
    """
    if client is None:
        # local fallback
        if not _HAS_SBER:
            raise RuntimeError(
                "No embedding client available (install sentence-transformers or provide OpenAI client)"
            )
        m = _get_local_model()
        vec = m.encode(text, normalize_embeddings=True)
        return [float(x) for x in vec]

    # OpenAI path
    model = model or EMBED_MODEL
    if not _HAS_OPENAI:
        raise RuntimeError("OpenAI client not available for embeddings")
    resp = client.embeddings.create(model=model, input=text)
    emb = resp.data[0].embedding
    return emb


def store_embedding(report_id: int, vector: list[float]):
    """Store embedding in PostgreSQL"""
    try:
        db.save_report_embedding(report_id, vector)
        increment_metric("report_embedding_stores")
    except Exception:
        pass


def load_all_embeddings():
    """Load all report embeddings from PostgreSQL"""
    now = time.time()
    try:
        if (
            _EMBED_CACHE["data"] is not None
            and (now - _EMBED_CACHE["ts"]) < _EMBED_CACHE_TTL
        ):
            try:
                increment_metric("report_embedding_loads")
            except Exception:
                pass
            return _EMBED_CACHE["data"]
    except Exception:
        pass

    # Load from PostgreSQL
    try:
        embeddings_data = db.get_all_report_embeddings()
        out = []
        for report_id, embedding_vec in embeddings_data:
            out.append((int(report_id), embedding_vec))
        
        try:
            _EMBED_CACHE["ts"] = now
            _EMBED_CACHE["data"] = out
        except Exception:
            pass

        try:
            increment_metric("report_embedding_loads")
        except Exception:
            pass
        return out
    except Exception:
        return []


def _query_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_query_embedding(text: str):
    """Return embedding vector for `text` from PostgreSQL or None."""
    emb = db.get_query_embedding(_query_hash(text))
    if emb:
        try:
            increment_metric("query_embedding_cache_hits")
        except Exception:
            pass
    return emb


def store_query_embedding(text: str, vector: list[float]):
    """Store query embedding in PostgreSQL"""
    try:
        db.save_query_embedding(_query_hash(text), text, vector)
        increment_metric("query_embedding_stores")
    except Exception:
        pass


def cosine(a, b):
    if not _HAS_NUMPY:
        # fallback simple python dot/norm
        dot = sum(x * y for x, y in zip(a, b))
        import math

        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)
    a = np.array(a, dtype=float)
    b = np.array(b, dtype=float)
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def find_nearest(client, text: str, top_k: int = 3):
    """Compute embedding for `text` and return top_k (report_id, score) tuples sorted desc."""
    # Get or compute query embedding and cache it in DB to avoid re-encoding
    emb = load_query_embedding(text)
    if emb is None:
        try:
            increment_metric("embedding_calls")
        except Exception:
            pass
        emb = embed_text(client, text)
        try:
            store_query_embedding(text, emb)
        except Exception:
            pass
    rows = load_all_embeddings()
    scored = []
    for rid, vec in rows:
        try:
            s = cosine(emb, vec)
            scored.append((rid, s))
        except Exception:
            continue
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]
