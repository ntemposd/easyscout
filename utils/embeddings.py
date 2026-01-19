import json
import os
import time
from datetime import datetime

from utils.metrics import increment_metric

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

DB_PATH = os.getenv("DB_PATH", "scout_reports.db")
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


def store_embedding(conn, report_id: int, vector: list[float]):
    cur = conn.execute(
        "SELECT report_id FROM report_embeddings WHERE report_id = ?", (report_id,)
    )
    now = datetime.utcnow().isoformat()
    j = json.dumps(vector)
    if cur.fetchone():
        conn.execute(
            "UPDATE report_embeddings SET embedding_json = ?, created_at = ? WHERE report_id = ?",
            (j, now, report_id),
        )
    else:
        conn.execute(
            "INSERT INTO report_embeddings (report_id, embedding_json, created_at) VALUES (?, ?, ?)",
            (report_id, j, now),
        )
    try:
        increment_metric("report_embedding_stores")
    except Exception:
        pass


def load_all_embeddings(conn):
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

    rows = conn.execute(
        "SELECT report_id, embedding_json FROM report_embeddings"
    ).fetchall()
    out = []
    for r in rows:
        try:
            vec = json.loads(r[1])
            out.append((int(r[0]), vec))
        except Exception:
            continue

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


def _query_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_query_embedding(conn, text: str):
    """Return embedding vector for `text` from `query_embeddings` table or None."""
    qh = _query_hash(text)
    row = conn.execute(
        "SELECT embedding_json FROM query_embeddings WHERE query_hash = ? LIMIT 1",
        (qh,),
    ).fetchone()
    if not row:
        return None
    try:
        # record a cache hit for query embeddings
        try:
            increment_metric("query_embedding_cache_hits")
        except Exception:
            pass
        return json.loads(row[0])
    except Exception:
        return None


def store_query_embedding(conn, text: str, vector: list[float]):
    qh = _query_hash(text)
    now = datetime.utcnow().isoformat()
    j = json.dumps(vector)
    cur = conn.execute(
        "SELECT query_hash FROM query_embeddings WHERE query_hash = ?", (qh,)
    )
    if cur.fetchone():
        conn.execute(
            "UPDATE query_embeddings SET query_text = ?, embedding_json = ?, created_at = ? WHERE query_hash = ?",
            (text, j, now, qh),
        )
    else:
        conn.execute(
            "INSERT INTO query_embeddings (query_hash, query_text, embedding_json, created_at) VALUES (?, ?, ?, ?)",
            (qh, text, j, now),
        )
    try:
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


def find_nearest(conn, client, text: str, top_k: int = 3):
    """Compute embedding for `text` and return top_k (report_id, score) tuples sorted desc."""
    # Get or compute query embedding and cache it in DB to avoid re-encoding
    emb = load_query_embedding(conn, text)
    if emb is None:
        try:
            increment_metric("embedding_calls")
        except Exception:
            pass
        emb = embed_text(client, text)
        try:
            store_query_embedding(conn, text, emb)
            conn.commit()
        except Exception:
            pass
    rows = load_all_embeddings(conn)
    scored = []
    for rid, vec in rows:
        try:
            s = cosine(emb, vec)
            scored.append((rid, s))
        except Exception:
            continue
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]
