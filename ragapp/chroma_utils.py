# ragapp/chroma_utils.py
from __future__ import annotations
import os, importlib, uuid
from pathlib import Path
from typing import List, Dict, Any, Optional

from django.conf import settings

# ─── 임베딩 선택(기본: 로컬, 옵션: Gemini) ─────────────────────────────────
_USE_GEMINI = bool(getattr(settings, "GEMINI_API_KEY", None))

# 로컬 임베딩(한국어 멀티링구얼 모델)
_sbert_model_name = os.environ.get(
    "SBERT_MODEL",
    "jhgan/ko-sroberta-multitask"  # 가볍고 한국어 잘됨
)
_sbert = None

def _ensure_sbert():
    global _sbert
    if _sbert is None:
        from sentence_transformers import SentenceTransformer
        _sbert = SentenceTransformer(_sbert_model_name)
    return _sbert

# (옵션) Gemini 임베딩
def _gemini_embed(texts: List[str]) -> List[List[float]]:
    from google import genai
    try:
        c = genai.Client(api_key=settings.GEMINI_API_KEY)
        model = (getattr(settings, "GEMINI_EMBED_MODELS", ["text-embedding-004"]) or ["text-embedding-004"])[0]
        out = []
        for t in texts:
            # SDK 버전에 따라 contents/content/input가 다를 수 있어 contents로 우선 시도
            try:
                r = c.models.embed_content(model=model, contents=t)
                v = getattr(r, "embedding", None)
                if v and getattr(v, "values", None):
                    out.append(list(v.values))
                    continue
            except Exception:
                pass
            # 파싱 실패 시 dict 형태 폴백
            try:
                r = c.models.embed_content(model=model, contents=t)
                v = r.get("embedding", {}).get("values")
                if v:
                    out.append(list(v))
                    continue
            except Exception:
                pass
            # 마지막 폴백 실패 시 에러
            raise RuntimeError("Gemini 임베딩 파싱 실패")
        return out
    except Exception as e:
        # 실패하면 로컬로 폴백
        m = _ensure_sbert()
        return m.encode(texts, normalize_embeddings=True).tolist()

def embed_texts(texts: List[str]) -> List[List[float]]:
    if not texts:
        return []
    if _USE_GEMINI:
        try:
            return _gemini_embed(texts)
        except Exception:
            pass
    m = _ensure_sbert()
    return m.encode(texts, normalize_embeddings=True).tolist()

def _embed_dim() -> int:
    return len(embed_texts(["__dim_probe__"])[0])

# ─── Chroma 클라이언트/컬렉션 ────────────────────────────────────────────────
def _chroma_client():
    chromadb = importlib.import_module("chromadb")
    Path(settings.CHROMA_DB_DIR).mkdir(parents=True, exist_ok=True)
    PersistentClient = getattr(chromadb, "PersistentClient", None)
    if PersistentClient:
        return PersistentClient(path=settings.CHROMA_DB_DIR)
    # (구버전 폴백)
    from chromadb.config import Settings as _S
    return chromadb.Client(_S(chroma_db_impl="duckdb+parquet", persist_directory=settings.CHROMA_DB_DIR))

def get_collection():
    """기존 컬렉션 dim과 현재 임베딩 dim이 다르면 이름에 _{dim} 붙여 새 컬렉션 사용."""
    client = _chroma_client()
    base = settings.CHROMA_COLLECTION
    want = _embed_dim()

    # 우선 기본 컬렉션
    try:
        col = client.get_or_create_collection(name=base)
        try:
            got = col.get(limit=1, include=["embeddings"])
            embs = (got.get("embeddings") or [])
            if embs and embs[0]:
                cur = len(embs[0])
                if cur == want:
                    return col
        except Exception:
            return col  # 비어있으면 그냥 사용
    except Exception:
        pass

    # 차원 다르면 새 이름
    alt = f"{base}_{want}"
    return client.get_or_create_collection(name=alt)

# ─── 편의 함수들 ────────────────────────────────────────────────────────────
def upsert_texts(texts: List[str], metadatas: Optional[List[Dict[str, Any]]] = None, ids: Optional[List[str]] = None):
    texts = [t for t in (texts or []) if (t or "").strip()]
    if not texts:
        return {"inserted": 0}
    embs = embed_texts(texts)
    col = get_collection()
    if ids is None:
        ids = [str(uuid.uuid4()) for _ in texts]
    if metadatas is None:
        metadatas = [{} for _ in texts]
    if hasattr(col, "upsert"):
        col.upsert(ids=ids, documents=texts, metadatas=metadatas, embeddings=embs)
    else:
        try:
            col.delete(ids=ids)
        except Exception:
            pass
        col.add(ids=ids, documents=texts, metadatas=metadatas, embeddings=embs)
    return {"inserted": len(texts), "collection": getattr(col, "name", settings.CHROMA_COLLECTION)}

def count():
    col = get_collection()
    try:
        return int(col.count())
    except Exception:
        data = col.get(limit=1_000_000)
        return len(data.get("ids") or [])

def query(q: str, topk: int = 5):
    col = get_collection()
    qv = embed_texts([q])[0]
    res = col.query(query_embeddings=[qv], n_results=max(1, int(topk)), include=["documents", "metadatas", "distances"])
    # 정규화
    docs  = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    ids   = (res.get("ids") or [[]])[0] if "ids" in res else [""] * len(docs)
    hits = []
    for i, d in enumerate(docs):
        if not d:
            continue
        hits.append({
            "id": ids[i] if i < len(ids) else "",
            "score": float(dists[i]) if (dists and i < len(dists)) else None,
            "meta": metas[i] if i < len(metas) else {},
            "snippet": (d[:500] if isinstance(d, str) else str(d)).replace("\n", " ").strip()
        })
    return hits

def seed_minimal():
    texts = [
        "이 문서는 RAG 동작 점검용 샘플입니다. 크로마 DB가 정상인지 확인하세요.",
        "두 번째 문서입니다. 간단한 검색 테스트에 사용됩니다."
    ]
    metas = [{"source": "seed", "title": "doc1"}, {"source": "seed", "title": "doc2"}]
    return upsert_texts(texts, metas)
