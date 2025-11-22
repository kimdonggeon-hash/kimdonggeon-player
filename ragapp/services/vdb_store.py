# ragapp/services/vdb_store.py
from __future__ import annotations
import os, json, sqlite3, time
from pathlib import Path
from typing import Sequence, Mapping, Any, List, Dict
from django.conf import settings

# ─────────────────────────────────────────
# 경로 결정: settings.VECTOR_DB_PATH > ENV > BASE_DIR/vector_store.sqlite3
# ─────────────────────────────────────────
def _vdb_path() -> str:
    p = getattr(settings, "VECTOR_DB_PATH", None) or os.environ.get("VECTOR_DB_PATH")
    if not p:
        base = getattr(settings, "BASE_DIR", Path.cwd())
        p = str(Path(base) / "vector_store.sqlite3")
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    return p

def _connect():
    path = _vdb_path()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embeddings(
            id TEXT PRIMARY KEY,
            doc TEXT,
            meta TEXT,          -- JSON (UTF-8)
            embedding TEXT,     -- JSON array of floats (간단/호환성을 위해 BLOB 대신 JSON 저장)
            dim INTEGER,
            updated_at TEXT
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_dim ON embeddings(dim);")

# ─────────────────────────────────────────
# 필수: 업서트 (indexto_chroma_safe 가 호출)
# ─────────────────────────────────────────
def vdb_upsert(
    ids: Sequence[str],
    docs: Sequence[str],
    metas: Sequence[Mapping[str, Any]],
    embs: Sequence[Sequence[float]],
) -> Dict[str, Any]:
    if not (len(ids) == len(docs) == len(metas) == len(embs)):
        raise ValueError("vdb_upsert: ids/docs/metas/embs 길이가 일치해야 합니다.")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn = _connect()
    try:
        _ensure_schema(conn)
        cur = conn.cursor()
        inserted = 0
        last_dim = None

        for i, doc, meta, vec in zip(ids, docs, metas, embs):
            if not isinstance(i, str) or not i.strip():
                continue
            # float 배열 보정
            vec_list = [float(x) for x in vec]
            last_dim = len(vec_list)

            cur.execute(
                """
                INSERT INTO embeddings(id, doc, meta, embedding, dim, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  doc=excluded.doc,
                  meta=excluded.meta,
                  embedding=excluded.embedding,
                  dim=excluded.dim,
                  updated_at=excluded.updated_at
                """,
                (
                    i.strip(),
                    doc or "",
                    json.dumps(dict(meta), ensure_ascii=False),
                    json.dumps(vec_list),
                    last_dim,
                    now,
                ),
            )
            inserted += 1

        conn.commit()
        return {"ok": True, "inserted": inserted, "path": _vdb_path(), "dim": last_dim}
    finally:
        conn.close()

# ─────────────────────────────────────────
# 선택: 카운트/초기화/정보 (추후 필요 시)
# ─────────────────────────────────────────
def vdb_count() -> int:
    conn = _connect()
    try:
        _ensure_schema(conn)
        c = conn.execute("SELECT COUNT(*) FROM embeddings")
        return int(c.fetchone()[0] or 0)
    finally:
        conn.close()

def vdb_clear() -> None:
    conn = _connect()
    try:
        _ensure_schema(conn)
        conn.execute("DELETE FROM embeddings")
        conn.commit()
    finally:
        conn.close()

def vdb_info() -> Dict[str, Any]:
    return {"path": _vdb_path(), "count": vdb_count()}
