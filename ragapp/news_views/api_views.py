# ragapp/news_views/api_views.py
from __future__ import annotations

import os
import json
import logging
import uuid
import hashlib
from typing import Any, Dict, List
from pathlib import Path
from urllib.parse import urlparse

from django.http import JsonResponse, HttpRequest
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.conf import settings
from django.utils import timezone

from ragapp.models import (
    MyLog,
    RagSetting,
    Feedback,
    IngestHistory,
    LegalConfig,
    ChatQueryLog,
)

# ✅ 서비스 모듈 (단일 news_services로 통일)
from ragapp.services import news_services as ns
from ragapp.services.news_services import (
    search_news_rss,
    crawl_news_bodies,
    gemini_answer_with_news,
    indexto_chroma_safe,
    rag_answer_grounded,
)

# 변경: IP는 해싱 유틸로 통일
from ragapp.services.utils import client_ip_for_log

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# 공통 로깅 helper (MyLog 최신 스키마 버전)
# ---------------------------------------------------------------------
def _safe_log(
    *,
    mode_text: str,
    query: str,
    ok_flag: bool,
    remote_addr_text: str,
    extra_payload: Dict[str, Any],
) -> None:
    """
    MyLog 레코드를 안전하게 남긴다.
    (오류 나도 전체 API 흐름은 안 죽이게 try/except)
    """
    try:
        MyLog.objects.create(
            mode_text=mode_text[:100],
            query=query[:500],
            ok_flag=ok_flag,
            remote_addr_text=remote_addr_text[:200],
            extra_json=extra_payload,
        )
    except Exception as e:
        log.warning("MyLog insert 실패: %s", e)


def _get_latest_ragsetting() -> RagSetting | None:
    try:
        return RagSetting.objects.order_by("-id").first()
    except Exception:
        return None


# 벡터 DB 경로(진단용 표기)
def _vector_db_path() -> str:
    return os.environ.get("VECTOR_DB_PATH") or str(
        Path(getattr(settings, "BASE_DIR", Path.cwd())) / "vector_store.sqlite3"
    )


# 현재 로컬 벡터 스토어 문서 수 (SQLite)
def _vector_store_count() -> int | None:
    try:
        with ns._sqlite_conn() as c:
            row = c.execute("SELECT COUNT(*) FROM vector_docs").fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return None


# ---------------------------------------------------------------------
# 헬스체크 / 설정 조회 / 진단
# ---------------------------------------------------------------------
@require_GET
def api_ping(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok", "pong": True})


@require_GET
def api_config(request: HttpRequest) -> JsonResponse:
    cfg = _get_latest_ragsetting()
    data = {
        "news_topk": getattr(cfg, "news_topk", None),
        "rag_query_topk": getattr(cfg, "rag_query_topk", None),
        "rag_fallback_topk": getattr(cfg, "rag_fallback_topk", None),
        "rag_max_sources": getattr(cfg, "rag_max_sources", None),
        "auto_ingest_after_gemini": getattr(cfg, "auto_ingest_after_gemini", None),
        # 과거 호환 키(Chroma) — 값만 전달(사용 안 해도 무관)
        "web_ingest_to_chroma": getattr(cfg, "web_ingest_to_chroma", None),
        "chroma_db_dir": getattr(cfg, "chroma_db_dir", None),
        "chroma_collection": getattr(cfg, "chroma_collection", None),
        # 신규 표기(현 벡터 스토어)
        "vector_db_path": _vector_db_path(),
        "vector_count": _vector_store_count(),
    }
    return JsonResponse({"status": "ok", "config": data})


@require_GET
def api_diag(request: HttpRequest) -> JsonResponse:
    info = {
        # 과거 호환 필드 유지(값은 의미 없음)
        "chroma_collection": getattr(settings, "CHROMA_COLLECTION", None),
        "chroma_db_dir": getattr(settings, "CHROMA_DB_DIR", None),
        # 현 사용중인 로컬 스토어 정보
        "vector_db_path": _vector_db_path(),
        "collection_count": _vector_store_count(),
    }
    return JsonResponse({"status": "ok", "diag": info})


# ---------------------------------------------------------------------
# 피드백 API
# ---------------------------------------------------------------------
@require_POST
def api_feedback(request: HttpRequest) -> JsonResponse:
    """
    /api/feedback
    - JSON / form-encoded 모두 허용
    - 케이스:
        A) log_id만 넘어옴  → 해당 ChatQueryLog 갱신
        B) question(+answer) 넘어옴 → ChatQueryLog 생성 후 갱신
    - Feedback 테이블 저장 시도, 실패하면 파일(JSONL) 폴백
    응답 예:
      {"ok": true, "chat_log_id": 12, "feedback_id": 34, "stored": "db"}
    """
    client_ip = client_ip_for_log(request)

    # 입력 파싱
    try:
        if request.content_type and "application/json" in request.content_type.lower():
            payload = json.loads((request.body or b"{}").decode("utf-8") or "{}")
        else:
            payload = {k: request.POST.get(k) for k in request.POST.keys()}
    except Exception as e:
        return JsonResponse({"ok": False, "error": "invalid_json", "detail": str(e)}, status=400)

    # 정규화
    def _boolish(v) -> bool:
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        return s in ("1", "true", "yes", "y", "on")

    def _norm_mode(s: str) -> str:
        s = (s or "").strip().lower()
        if s in ("rag", "gemini", "faq", "blocked"):
            return s
        return "rag"

    question = (payload.get("question") or "").strip()[:2000]
    answer = (payload.get("answer") or "").strip()[:8000]
    feedback_txt = (payload.get("feedback") or "").strip()[:3000]
    answer_type = (payload.get("answer_type") or payload.get("type") or "").strip()[:20]
    mode = _norm_mode(answer_type)
    is_helpful = _boolish(payload.get("is_helpful", payload.get("helpful", False)))

    # sources 정리(개인정보 과수집 방지)
    raw_sources = payload.get("sources") or payload.get("sources_json") or "[]"
    if isinstance(raw_sources, str):
        try:
            raw_sources = json.loads(raw_sources)
        except Exception:
            raw_sources = []
    sources: List[Dict[str, str]] = []
    if isinstance(raw_sources, list):
        for s in raw_sources[:10]:
            if isinstance(s, dict):
                sources.append(
                    {
                        "title": str(s.get("title", ""))[:300],
                        "url": str(s.get("url", ""))[:1000],
                        "source": str(s.get("source", ""))[:120],
                        "snippet": str(s.get("snippet", ""))[:600],
                    }
                )

    # log_id 수신 시 정수 변환
    log_id_raw = payload.get("log_id") or payload.get("id") or payload.get("chat_log_id")
    try:
        log_id = int(log_id_raw) if str(log_id_raw).strip() else None
    except Exception:
        log_id = None

    # ---- 핵심 정책: (log_id) 또는 (question) 중 하나는 반드시 있어야 함 ----
    if not (log_id or question):
        return JsonResponse({"ok": False, "error": "require log_id or question"}, status=400)

    chat_log = None
    created_new_log = False

    # A) log_id로 기존 ChatQueryLog 갱신
    if log_id:
        try:
            chat_log = ChatQueryLog.objects.get(id=log_id)
        except ChatQueryLog.DoesNotExist:
            chat_log = None

    # B) 없으면 새로 생성 (question 필수)
    if not chat_log:
        try:
            chat_log = ChatQueryLog.objects.create(
                mode=mode or "rag",
                question=question or "(no question)",
                answer_excerpt=(answer[:500] if answer else ""),
                client_ip=client_ip,
                was_helpful=is_helpful,
                feedback=feedback_txt,
            )
            created_new_log = True
            log_id = chat_log.id
        except Exception as e:
            # ChatQueryLog 생성 자체가 실패하면 파일 폴백으로만 처리
            base = Path(getattr(settings, "BASE_DIR", Path.cwd()))
            logdir = base / "feedback_logs"
            logdir.mkdir(parents=True, exist_ok=True)
            out = logdir / f"feedback-{timezone.now().strftime('%Y%m%d')}.jsonl"
            rec = {
                "question": question,
                "answer": answer,
                "answer_type": answer_type,
                "helpful": is_helpful,
                "feedback": feedback_txt,
                "sources": sources,
                "ts": timezone.now().isoformat(),
                "client_ip": client_ip,
                "ua": request.META.get("HTTP_USER_AGENT", "")[:200],
                "note": f"ChatQueryLog create failed: {e}",
            }
            out.write_text("", encoding="utf-8") if not out.exists() else None
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            _safe_log(
                mode_text="api_feedback",
                query=question or "(no question)",
                ok_flag=True,
                remote_addr_text=client_ip,
                extra_payload={"stored": "file", "reason": "chatlog_create_failed"},
            )
            return JsonResponse(
                {"ok": True, "stored": "file", "path": str(out.relative_to(base))}, status=200
            )

    # ChatQueryLog 갱신(있을 때만)
    try:
        if mode and chat_log.mode != mode:
            chat_log.mode = mode
        if question and (not chat_log.question or created_new_log):
            chat_log.question = question
        if answer:
            chat_log.answer_excerpt = answer[:500]
        chat_log.was_helpful = is_helpful
        if feedback_txt:
            if chat_log.feedback:
                chat_log.feedback = (chat_log.feedback + "\n" + feedback_txt).strip()
            else:
                chat_log.feedback = feedback_txt
        chat_log.client_ip = chat_log.client_ip or client_ip
        chat_log.save()
    except Exception as e:
        log.warning("ChatQueryLog update 실패: %s", e)

    # Feedback 테이블 저장 시도 (실패해도 전체 흐름은 살림)
    fb = None
    db_saved = False
    err_msg = None
    try:
        fb = Feedback.objects.create(
            question=question or (chat_log.question if chat_log else ""),
            answer=answer or (chat_log.answer_excerpt if chat_log else ""),
            answer_type=answer_type or mode,
            is_helpful=is_helpful,
            sources_json=sources or None,  # ← 모델 필드 이름에 맞춤
            client_ip=client_ip,
        )
        db_saved = True
    except Exception as e:
        db_saved = False
        err_msg = str(e)

    # 파일(JSONL) 폴백
    if not db_saved:
        try:
            base = Path(getattr(settings, "BASE_DIR", Path.cwd()))
            logdir = base / "feedback_logs"
            logdir.mkdir(parents=True, exist_ok=True)
            out = logdir / f"feedback-{timezone.now().strftime('%Y%m%d')}.jsonl"
            rec = {
                "question": question or (chat_log.question if chat_log else ""),
                "answer": answer or (chat_log.answer_excerpt if chat_log else ""),
                "answer_type": answer_type or mode,
                "helpful": is_helpful,
                "feedback": feedback_txt,
                "sources": sources,
                "ts": timezone.now().isoformat(),
                "client_ip": client_ip,
                "ua": request.META.get("HTTP_USER_AGENT", "")[:200],
                "chat_log_id": log_id,
            }
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

            _safe_log(
                mode_text="api_feedback",
                query=question or (chat_log.question if chat_log else "(no question)"),
                ok_flag=True,
                remote_addr_text=client_ip,
                extra_payload={"stored": "file", "chat_log_id": log_id, "err_db": err_msg},
            )
            return JsonResponse(
                {
                    "ok": True,
                    "stored": "file",
                    "chat_log_id": log_id,
                    "path": str(out.relative_to(base)),
                },
                status=200,
            )
        except Exception as e2:
            _safe_log(
                mode_text="api_feedback",
                query=question or (chat_log.question if chat_log else "(no question)"),
                ok_flag=False,
                remote_addr_text=client_ip,
                extra_payload={
                    "stored": "failed",
                    "chat_log_id": log_id,
                    "err": f"db:{err_msg} file:{e2}",
                },
            )
            return JsonResponse(
                {"ok": False, "error": f"feedback_store_failed: db:{err_msg} file:{e2}"},
                status=500,
            )

    # DB 성공 응답
    _safe_log(
        mode_text="api_feedback",
        query=question or (chat_log.question if chat_log else "(no question)"),
        ok_flag=True,
        remote_addr_text=client_ip,
        extra_payload={
            "stored": "db",
            "chat_log_id": log_id,
            "feedback_id": fb.id if fb else None,
        },
    )
    return JsonResponse(
        {
            "ok": True,
            "stored": "db",
            "chat_log_id": log_id,
            "feedback_id": fb.id if fb else None,
            "created_at": (fb.created_at.isoformat() if fb else timezone.now().isoformat()),
        },
        status=200,
    )


# ---------------------------------------------------------------------
# (신규) 원터치 인덱싱 파이프라인: /api/ingest_news
# ---------------------------------------------------------------------
@require_http_methods(["GET", "POST"])
def api_ingest_news(request: HttpRequest) -> JsonResponse:
    client_ip = client_ip_for_log(request)

    keyword = (
        request.GET.get("keyword")
        or request.POST.get("keyword")
        or ""
    ).strip()

    if not keyword:
        return JsonResponse(
            {"status": "error", "error": "keyword 파라미터가 없습니다."},
            status=400,
        )

    cfg = _get_latest_ragsetting()
    topk = int(getattr(cfg, "news_topk", 5) or 5)

    ok_flag = False
    error_msg = None

    total_candidates = 0
    ingested_count = 0
    skipped_count = 0
    failed_count = 0
    results_detail: List[Dict[str, Any]] = []

    try:
        headlines = search_news_rss(keyword, topk)
        articles_full = crawl_news_bodies(headlines, max_workers=6)

        total_candidates = len(articles_full)

        for art in articles_full:
            art_url = art.get("url") or art.get("link") or ""
            art_title = art.get("title") or ""
            art_body = art.get("news_body") or art.get("content") or art.get("body") or ""

            if not art_url or not art_body.strip():
                failed_count += 1
                results_detail.append(
                    {
                        "url": art_url[:1000],
                        "status": "skip_empty",
                        "title": art_title[:80],
                    }
                )
                continue

            try:
                fake_news_list = [
                    {
                        "title": art_title,
                        "url": art_url,
                        "source": art.get("source", "") or "news",
                        "published_at": art.get("published_at", ""),
                        "snippet": (art_body[:300] or ""),
                        "news_body": art_body,
                    }
                ]

                r = indexto_chroma_safe(
                    question=keyword,
                    answer=art_title or "",
                    news_list=fake_news_list,
                )

                status = "ok"
                if isinstance(r, dict):
                    status = r.get("status", "ok")

                if status in ("ok", "new", "inserted"):
                    ingested_count += 1
                elif status in ("duplicate", "exists", "skipped"):
                    skipped_count += 1
                else:
                    failed_count += 1

                results_detail.append(
                    {
                        "url": art_url[:1000],
                        "status": status,
                        "title": art_title[:80],
                    }
                )

            except Exception as e:
                failed_count += 1
                results_detail.append(
                    {
                        "url": art_url[:1000],
                        "status": "error",
                        "error": str(e)[:500],
                        "title": art_title[:80],
                    }
                )

        ok_flag = True

    except Exception as e:
        log.exception("api_ingest_news 처리 중 예외")
        error_msg = str(e)

    try:
        hist = IngestHistory.objects.create(
            keyword=keyword[:500],
            total_candidates=total_candidates,
            ingested_count=ingested_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            detail=results_detail[:200],
        )
        hist_id = hist.id
    except Exception as e:
        hist_id = None
        log.warning("IngestHistory 저장 실패: %s", e)

    _safe_log(
        mode_text="api_ingest_news",
        query=keyword,
        ok_flag=ok_flag,
        remote_addr_text=client_ip,
        extra_payload={
            "error_msg": error_msg,
            "total_candidates": total_candidates,
            "ingested_count": ingested_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
            "detail_preview": results_detail[:5],
            "history_id": hist_id,
        },
    )

    if not ok_flag:
        return JsonResponse(
            {
                "status": "error",
                "error": error_msg or "ingest_news 실패(상세는 서버 로그 참조)",
                "summary": {
                    "total_candidates": total_candidates,
                    "ingested_count": ingested_count,
                    "skipped_count": skipped_count,
                    "failed_count": failed_count,
                },
                "history_id": hist_id,
            },
            status=500,
        )

    return JsonResponse(
        {
            "status": "ok",
            "keyword": keyword,
            "summary": {
                "total_candidates": total_candidates,
                "ingested_count": ingested_count,
                "skipped_count": skipped_count,
                "failed_count": failed_count,
            },
            "history_id": hist_id,
            "detail_sample": results_detail[:5],
        },
        status=200,
    )


# ---------------------------------------------------------------------
# 1) ❤️ 버튼용: 뉴스 크롤링 & 인덱싱
# ---------------------------------------------------------------------
@require_GET
def api_news_ingest(request: HttpRequest) -> JsonResponse:
    q = (request.GET.get("q") or "").strip()
    client_ip = client_ip_for_log(request)

    if not q:
        return JsonResponse({"status": "error", "error": "q 파라미터가 없습니다."}, status=400)

    cfg = _get_latest_ragsetting()
    topk = int(getattr(cfg, "news_topk", 5) or 5)

    ok_flag = False
    error_msg = None
    ingest_summary: Dict[str, Any] | str | None = None
    model_answer: str = ""
    news_list_with_body: List[Dict[str, Any]] = []

    try:
        headlines = search_news_rss(q, topk)
        news_list_with_body = crawl_news_bodies(headlines, max_workers=6)
        model_answer, _tmp_headlines = gemini_answer_with_news(q)

        ingest_summary = indexto_chroma_safe(
            question=q,
            answer=model_answer or "",
            news_list=news_list_with_body,
        )

        if not ingest_summary:
            ingest_summary = {"note": "ingest_summary 비어 있음"}

        ok_flag = True

    except Exception as e:
        log.exception("api_news_ingest 처리 중 예외")
        error_msg = f"뉴스 인덱싱 실패: {e}"
        if ingest_summary is None:
            ingest_summary = {"note": "인덱싱 중 예외로 인한 실패"}

    extra_payload = {
        "keyword": q,
        "error_msg": error_msg,
        "ingest_summary": ingest_summary,
        "sample_news": [
            {
                "title": n.get("title", "")[:200],
                "url": n.get("url", "")[:1000],
                "body_len": len(n.get("news_body", "")),
                "has_body": bool(n.get("news_body")),
            }
            for n in news_list_with_body[:5]
        ],
        "answer_preview": (model_answer or "")[:300],
    }
    _safe_log(
        mode_text="api_news_ingest",
        query=q,
        ok_flag=ok_flag,
        remote_addr_text=client_ip,
        extra_payload=extra_payload,
    )

    if not ok_flag:
        return JsonResponse(
            {
                "status": "error",
                "error": error_msg or "인덱싱 실패(상세는 서버 로그 참조)",
                "ingest_summary": ingest_summary,
            },
            status=500,
        )

    return JsonResponse(
        {
            "status": "ok",
            "keyword": q,
            "ingest_summary": ingest_summary,
            "model_answer_preview": (model_answer or "")[:500],
            "news_sample": [
                {
                    "title": n.get("title", "")[:200],
                    "url": n.get("url", "")[:1000],
                    "body_len": len(n.get("news_body", "")),
                    "has_body": bool(n.get("news_body")),
                }
                for n in news_list_with_body[:5]
            ],
        }
    )


# ---------------------------------------------------------------------
# 2) RAG 인덱스 관련 API
# ---------------------------------------------------------------------
@require_POST
def api_rag_upsert(request: HttpRequest) -> JsonResponse:
    client_ip = client_ip_for_log(request)

    try:
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except Exception:
            payload = {}
        title = (payload.get("title") or "").strip()[:500] or "manual_upload"
        body = (payload.get("body") or "").strip()[:200_000]

        fake_news_list = [
            {
                "title": title,
                "url": "",
                "source": "manual",
                "published_at": "",
                "snippet": body[:300],
                "news_body": body,
            }
        ]

        ingest_summary = indexto_chroma_safe(
            question=title,
            answer=body,
            news_list=fake_news_list,
        )

        _safe_log(
            mode_text="api_rag_upsert",
            query=title,
            ok_flag=True,
            remote_addr_text=client_ip,
            extra_payload={
                "ingest_summary": ingest_summary,
                "body_len": len(body),
            },
        )

        return JsonResponse({"status": "ok", "ingest_summary": ingest_summary})

    except Exception as e:
        log.exception("api_rag_upsert 예외")
        _safe_log(
            mode_text="api_rag_upsert",
            query="(exception)",
            ok_flag=False,
            remote_addr_text=client_ip,
            extra_payload={"error": str(e)},
        )
        return JsonResponse({"status": "error", "error": f"upsert 실패: {e}"}, status=500)


@require_GET
def api_rag_seed(request: HttpRequest) -> JsonResponse:
    client_ip = client_ip_for_log(request)

    try:
        seed_docs = [
            {
                "title": "RAG 소개",
                "url": "https://example.local/rag-intro",
                "source": "seed",
                "published_at": "",
                "snippet": "RAG는 검색된 문서 조각을 근거로 답변을 생성하는 방식이다.",
                "news_body": (
                    "RAG(Retrieval-Augmented Generation)는 "
                    "질문과 연관된 외부 지식 조각을 먼저 검색한 뒤 "
                    "그 조각들을 근거로 답을 생성한다."
                ),
            },
            {
                "title": "Chroma 개요",
                "url": "https://example.local/chroma",
                "source": "seed",
                "published_at": "",
                "snippet": "Chroma는 오픈소스 벡터DB다.",
                "news_body": (
                    "Chroma는 텍스트 임베딩 벡터를 저장하고 유사도 검색할 수 있게 해주는 "
                    "오픈소스 벡터 데이터베이스다."
                ),
            },
        ]

        answer_text = (
            "이 문서는 RAG 시스템 초기 시드 데이터입니다. "
            "RAG 개념과 Chroma 개념에 대한 기본 설명을 담고 있습니다."
        )

        ingest_summary = indexto_chroma_safe(
            question="[SEED INIT]",
            answer=answer_text,
            news_list=seed_docs,
        )

        _safe_log(
            mode_text="api_rag_seed",
            query="[SEED INIT]",
            ok_flag=True,
            remote_addr_text=client_ip,
            extra_payload={"ingest_summary": ingest_summary},
        )

        return JsonResponse({"status": "ok", "ingest_summary": ingest_summary})

    except Exception as e:
        log.exception("api_rag_seed 예외")
        _safe_log(
            mode_text="api_rag_seed",
            query="[SEED INIT]",
            ok_flag=False,
            remote_addr_text=client_ip,
            extra_payload={"error": str(e)},
        )
        return JsonResponse({"status": "error", "error": f"seed 실패: {e}"}, status=500)


# ---------------------------------------------------------------------
# 3) RAG 검색 / 진단
# ---------------------------------------------------------------------
@require_http_methods(["GET", "POST"])
def api_rag_search(request: HttpRequest) -> JsonResponse:
    """
    RAG 검색 API
    - GET:  /api/rag_search?q=...
    - POST: /api/rag_search  { "q": "...", "initial_topk"?, "fallback_topk"?, "max_sources"? }

    응답 형식은 예전 JS와의 호환을 위해 아래처럼 맞춘다.
    {
        "status": "ok",
        "ok": true,
        "mode": "rag",
        "answer_type": "rag",
        "question": "...",
        "answer": "...",
        "sources": [...]
    }
    """
    client_ip = client_ip_for_log(request)

    # q 파싱 (GET/POST 공통 지원)
    if request.method == "POST":
        try:
            if request.content_type and "application/json" in request.content_type.lower():
                payload = json.loads((request.body or b"{}").decode("utf-8") or "{}")
            else:
                payload = {k: request.POST.get(k) for k in request.POST.keys()}
        except Exception:
            payload = {}
        q = (payload.get("q") or payload.get("query") or "").strip()

        # 옵션 파라미터 (POST에서만 사용, 없으면 0 → 아래에서 설정 기본값으로 대체)
        def _to_int(v, default=0):
            try:
                return int(v)
            except Exception:
                return default

        initial_topk = _to_int(payload.get("initial_topk"), 0)
        fallback_topk = _to_int(payload.get("fallback_topk"), 0)
        max_sources = _to_int(payload.get("max_sources"), 0)
    else:
        q = (request.GET.get("q") or "").strip()
        initial_topk = 0
        fallback_topk = 0
        max_sources = 0

    if not q:
        return JsonResponse(
            {"status": "error", "ok": False, "error": "q 파라미터 누락"},
            status=400,
        )

    cfg = _get_latest_ragsetting()

    # 설정값 + 기본값 병합
    def _int_cfg(attr_name: str, setting_name: str, default: int) -> int:
        try:
            if attr_name and cfg is not None:
                v = getattr(cfg, attr_name, None)
                if v not in (None, ""):
                    return int(v)
            v2 = getattr(settings, setting_name, None)
            if v2 not in (None, ""):
                return int(v2)
        except Exception:
            pass
        return default

    if initial_topk <= 0:
        initial_topk = _int_cfg("rag_query_topk", "RAG_QUERY_TOPK", 5)
    if fallback_topk <= 0:
        fallback_topk = _int_cfg("rag_fallback_topk", "RAG_FALLBACK_TOPK", 12)
    if max_sources <= 0:
        max_sources = _int_cfg("rag_max_sources", "RAG_MAX_SOURCES", 8)

    try:
        answer_text, hits = rag_answer_grounded(
            question=q,
            initial_topk=initial_topk,
            fallback_topk=fallback_topk,
            max_sources=max_sources,
        )
        ok_flag = True
        err_msg = None
    except Exception as e:
        log.exception("api_rag_search 예외")
        answer_text = ""
        hits = []
        ok_flag = False
        err_msg = str(e)

    _safe_log(
        mode_text="api_rag_search",
        query=q,
        ok_flag=ok_flag,
        remote_addr_text=client_ip,
        extra_payload={
            "error_msg": err_msg,
            "answer_preview": (answer_text or "")[:400],
            "num_hits": len(hits),
        },
    )

    if not ok_flag:
        return JsonResponse(
            {"status": "error", "ok": False, "error": err_msg or "rag_search 실패"},
            status=500,
        )

    # 🔥 여기서 예전 구조 그대로 반환
    return JsonResponse(
        {
            "status": "ok",
            "ok": True,
            "mode": "rag",           # 옛 JS 호환용
            "answer_type": "rag",    # 옛 JS 호환용
            "question": q,
            "answer": answer_text,
            "sources": hits,
        },
        status=200,
    )


@require_GET
def api_rag_diag(request: HttpRequest) -> JsonResponse:
    cfg = _get_latest_ragsetting()

    data = {
        # 과거 호환 표기
        "collection": getattr(settings, "CHROMA_COLLECTION", None),
        "dir": getattr(settings, "CHROMA_DB_DIR", None),
        # 현재 상태
        "vector_db_path": _vector_db_path(),
        "count": _vector_store_count(),
        "rag_query_topk": getattr(cfg, "rag_query_topk", None),
        "rag_fallback_topk": getattr(cfg, "rag_fallback_topk", None),
        "rag_max_sources": getattr(cfg, "rag_max_sources", None),
    }
    return JsonResponse({"status": "ok", "rag_diag": data})


@require_GET
def api_chroma_verify(request: HttpRequest) -> JsonResponse:
    """
    (호환 유지) 로컬 SQLite 벡터 스토어로 교체된 검증 엔드포인트.
    기존 /api/chroma_verify 호출을 유지하면서 내부 구현만 변경.
    """
    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"status": "error", "error": "q 파라미터 누락"}, status=400)

    try:
        res = ns._chroma_query_with_embeddings(
            col=None,
            query=q,
            topk=8,
            where=None,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        return JsonResponse({"status": "error", "error": f"search 실패: {e}"}, status=500)

    docs = res.get("documents", [[]])[0] if res.get("documents") else []
    metas = res.get("metadatas", [[]])[0] if res.get("metadatas") else []
    dists = res.get("distances", [[]])[0] if res.get("distances") else []

    clean_hits = []
    for i, d in enumerate(docs):
        snippet = (d[:500] if isinstance(d, str) else str(d)).strip()
        m = metas[i] if i < len(metas) else {}
        dist = dists[i] if i < len(dists) else None
        clean_hits.append(
            {"rank": i + 1, "distance": dist, "meta": m, "snippet": snippet}
        )

    return JsonResponse({"status": "ok", "query": q, "hits": clean_hits})


# ---------------------------------------------------------------------
# (신규) 동의 증빙 수집 엔드포인트 — 개인정보 최소화/가명처리
# ---------------------------------------------------------------------
_CONSENT_ENABLED = getattr(settings, "CONSENT_LOG_ENABLED", True)
_CONSENT_DIR = Path(getattr(settings, "BASE_DIR", Path.cwd())) / "consent_logs"
_CONSENT_RETENTION_DAYS = int(getattr(settings, "CONSENT_RETENTION_DAYS", 730))


def _sha256_hexdigest(s: str) -> str:
    salt = getattr(settings, "SECRET_KEY", "salt")
    h = hashlib.sha256()
    h.update((salt + s).encode("utf-8", errors="ignore"))
    return h.hexdigest()


def _hostname_only(ref: str) -> str:
    try:
        netloc = urlparse(str(ref)).netloc
        return netloc.lower()[:255]
    except Exception:
        return ""


def _safe_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return False


def _cleanup_old_consent_logs() -> None:
    try:
        if _CONSENT_RETENTION_DAYS <= 0 or not _CONSENT_DIR.exists():
            return
        import time

        cutoff = time.time() - (_CONSENT_RETENTION_DAYS * 86400)
        for p in _CONSENT_DIR.rglob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


@require_POST
def legal_consent_confirm(request: HttpRequest) -> JsonResponse:
    """
    /legal/consent/confirm
    - 프런트에서 보내는 동의 증빙을 '최소한'으로 저장
    """
    if not _CONSENT_ENABLED:
        return JsonResponse({"ok": True, "skipped": True}, status=200)

    try:
        raw = request.body.decode("utf-8") if request.body else "{}"
        payload = json.loads(raw)
    except Exception:
        payload = {}

    client_ip_hash = client_ip_for_log(request)
    ua = request.META.get("HTTP_USER_AGENT", "")
    ua_hash = _sha256_hexdigest(ua)[:16]

    version = str(payload.get("version", "")).strip()[:20]
    action = str(payload.get("action", "accept")).strip()[:20]
    checkbox_checked = _safe_bool(payload.get("checkbox_checked"))
    path_value = str(payload.get("path", ""))[:300]
    if not path_value.startswith("/"):
        try:
            path_value = urlparse(path_value).path[:300]
        except Exception:
            path_value = path_value[:300]

    ref_host = _hostname_only(payload.get("ref", ""))

    tz = str(payload.get("tz", ""))[:64]
    locale = str(payload.get("locale", ""))[:16]
    consent_cookie = _safe_bool(payload.get("consent_ok_cookie"))
    sess = payload.get("session_flags") or {}
    session_flags = {
        "visited_once": _safe_bool(sess.get("visited_once")),
        "consent_ok": _safe_bool(sess.get("consent_ok")),
    }

    forms_in = payload.get("forms") or []
    forms: List[Dict[str, str]] = []
    if isinstance(forms_in, list):
        for f in forms_in[:10]:
            if isinstance(f, dict):
                action_path = str(f.get("action", ""))[:300]
                if not action_path.startswith("/"):
                    try:
                        action_path = urlparse(action_path).path[:300]
                    except Exception:
                        action_path = action_path[:300]
                forms.append({"action": action_path})

    uid = uuid.uuid4().hex
    now = timezone.now()

    out_dir = _CONSENT_DIR / now.strftime("%Y-%m")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    record = {
        "id": uid,
        "received_at": now.isoformat(),
        "client_ip_hash": str(client_ip_hash)[:64],
        "user_agent_hash": ua_hash,
        "version": version,
        "action": action,
        "checkbox_checked": checkbox_checked,
        "path": path_value,
        "ref_host": ref_host,
        "tz": tz,
        "locale": locale,
        "consent_ok_cookie": consent_cookie,
        "session_flags": session_flags,
        "forms": forms,
    }

    try:
        out_file = out_dir / f"consent-{now.strftime('%Y%m%d-%H%M%S')}-{uid}.json"
        base_dir = Path(getattr(settings, "BASE_DIR", Path.cwd()))
        out_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        saved_rel = str(out_file.relative_to(base_dir))
        _cleanup_old_consent_logs()
        return JsonResponse({"ok": True, "id": uid, "saved": saved_rel}, status=200)
    except Exception as e:
        log.error("Consent save failed: %s", e, exc_info=True)
        return JsonResponse({"ok": False, "error": "save_failed"}, status=200)


# ---------------------------------------------------------------------
# 벡터 진단 API (이름 유지)
# ---------------------------------------------------------------------
@require_GET
def api_vector_verify(request: HttpRequest) -> JsonResponse:
    from ragapp.services.news_services import _chroma_query_with_embeddings

    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"status": "error", "error": "q 파라미터 누락"}, status=400)

    res = _chroma_query_with_embeddings(
        None,
        q,
        topk=8,
        where=None,
        include=["documents", "metadatas", "distances"],
    )
    docs = (res.get("documents") or [[]])[0] if isinstance(res.get("documents"), list) else []
    metas = (res.get("metadatas") or [[]])[0] if isinstance(res.get("metadatas"), list) else []
    dists = (res.get("distances") or [[]])[0] if isinstance(res.get("distances"), list) else []

    hits = []
    for i, d in enumerate(docs):
        hits.append(
            {
                "rank": i + 1,
                "distance": (dists[i] if i < len(dists) else None),
                "meta": (metas[i] if i < len(metas) else {}),
                "snippet": (d[:500] if isinstance(d, str) else str(d)).strip(),
            }
        )
    return JsonResponse({"status": "ok", "query": q, "hits": hits})


@require_GET
def api_vector_diag(_request: HttpRequest) -> JsonResponse:
    import sqlite3

    db_path = os.environ.get("VECTOR_DB_PATH") or str(
        Path(getattr(settings, "BASE_DIR", ".")) / "vector_store.sqlite3"
    )
    try:
        conn = sqlite3.connect(db_path)
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM vector_docs").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        cnt = None
    return JsonResponse({"status": "ok", "diag": {"db_path": db_path, "doc_count": cnt}})


# ---------------------------------------------------------------------
# 법적 설정 번들 조회 API (news.html에서 쓰는 용도)
# ---------------------------------------------------------------------
@require_GET
def api_legal_bundle(request: HttpRequest) -> JsonResponse:
    cfg = LegalConfig.objects.order_by("-updated_at", "id").first()
    data = {
        "service_name": getattr(cfg, "service_name", "") if cfg else "",
        "operator_name": getattr(cfg, "operator_name", "") if cfg else "",
        "contact_email": getattr(cfg, "contact_email", "") if cfg else "",
        "contact_phone": getattr(cfg, "contact_phone", "") if cfg else "",
        "guide_html": getattr(cfg, "guide_html", "") if cfg else "",
        "privacy_html": getattr(cfg, "privacy_html", "") if cfg else "",
        "cross_border_html": getattr(cfg, "cross_border_html", "") if cfg else "",
        "tester_html": getattr(cfg, "tester_html", "") if cfg else "",
        "effective_date": getattr(cfg, "effective_date", None) or "",
    }
    return JsonResponse({"ok": True, **data}, status=200)
