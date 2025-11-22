# ragapp/news_views/news_views.py
from __future__ import annotations

import os
import io
import json
import secrets
import logging
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Any

from django.shortcuts import render
from django.http import JsonResponse, HttpRequest, HttpResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.utils import timezone
from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import connection
from django.utils.text import slugify
from django.db.utils import OperationalError, ProgrammingError
from django.contrib import messages  # ✅ Django messages

# LegalConfig / sanitize_legal_html가 없는 환경도 안전하게 동작하도록 가드
try:
    from ragapp.models import LegalConfig, sanitize_legal_html  # type: ignore
except Exception:  # pragma: no cover
    LegalConfig = None  # type: ignore

    def sanitize_legal_html(html: str) -> str:  # type: ignore
        return html

# Feedback 모델이 없을 수 있으므로 안전 가드
try:
    from ragapp.models import ChatQueryLog, Feedback  # type: ignore
except Exception:  # pragma: no cover
    from ragapp.models import ChatQueryLog  # type: ignore
    Feedback = None  # type: ignore

from ragapp.services.safety import is_sensitive_question, safe_block_response
from ragapp.services.utils import client_ip_for_log
from ragapp.qa_data import find_best_faq_answer
from ragapp.utils.legal import validate_required_consents

# 서비스 레이어
from ragapp.services.news_services import (
    gemini_answer_with_news,
    rag_answer_grounded,
    rag_answer_grounded_with_history,
    search_news_rss,
    crawl_news_bodies,
    indexto_chroma_safe,
)

from ragapp.services.news_fetcher import (
    crawl_news_bodies as _fetcher_crawl_news_bodies,
    search_news_rss as _fetcher_search_news_rss,
)

from ragapp.log_utils import log_success, log_error

log = logging.getLogger(__name__)


def _normalize_rag_sources(raw_sources: Any) -> List[Dict[str, Any]]:
    """
    템플릿(card_rag.html)에서 바로 쓸 수 있게
    rag_sources 를 통일된 형태로 정리해 주는 함수.

    반환 형태:
    [
      {"title": "...", "url": "...", "chunk": "...", "score": 0.87},
      ...
    ]
    """
    norm: List[Dict[str, Any]] = []

    if not raw_sources:
        return norm

    for i, s in enumerate(raw_sources):
        # 1) dict 형태로 들어오는 경우 (title/url/chunk/snippet/text/score 등)
        if isinstance(s, dict):
            title = (
                s.get("title")
                or s.get("page_title")
                or s.get("file_name")
                or s.get("id")
                or f"근거 {i + 1}"
            )
            url = s.get("url") or s.get("link") or ""
            chunk = (
                s.get("chunk")
                or s.get("snippet")
                or s.get("text")
                or s.get("page_content")
                or ""
            )
            score = s.get("score") or s.get("_score") or s.get("similarity")

        # 2) 그냥 문자열 리스트로 들어오는 경우
        else:
            title = str(s)
            url = ""
            chunk = ""
            score = None

        norm.append(
            {
                "title": title,
                "url": url,
                "chunk": chunk,
                "score": score,
            }
        )

    return norm


# ─────────────────────────────────────────────────────────────
# ★ 모델 표시명은 무조건 .env에서만 읽기 (없으면 즉시 에러)
# ─────────────────────────────────────────────────────────────
def _require_env(keys: tuple[str, ...], label: str) -> str:
    for k in keys:
        v = os.environ.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    raise RuntimeError(
        f"{label} 모델명이 .env에 없습니다. 다음 키 중 하나를 .env에 설정하세요: {', '.join(keys)}"
    )


def _env_model_direct() -> str:
    return _require_env(
        ("GEMINI_MODEL_DIRECT", "GEMINI_TEXT_MODEL", "VERTEX_TEXT_MODEL", "GEMINI_MODEL", "GEMINI_MODEL_DEFAULT"),
        label="웹/Gemini",
    )


def _env_model_rag() -> str:
    return _require_env(
        ("GEMINI_MODEL_RAG", "GEMINI_TEXT_MODEL", "VERTEX_TEXT_MODEL", "GEMINI_MODEL", "GEMINI_MODEL_DEFAULT"),
        label="RAG",
    )


def _has_table(table_name: str) -> bool:
    try:
        with connection.cursor() as c:
            c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=%s", [table_name])
            return c.fetchone() is not None
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# LegalConfig 로딩
# ─────────────────────────────────────────────────────────────
def _load_legal_config():
    try:
        if LegalConfig is None:
            return None
        if not _has_table("ragapp_legalconfig"):
            return None
        qs = LegalConfig.objects.order_by("-updated_at")
        return qs.filter(consent_gate_enabled=True).first() or qs.first()
    except Exception:
        return None


def build_legal_context() -> dict:
    """
    LegalConfig를 읽어서 법적/서비스 관련 컨텍스트를 만들어 준다.
    - 소문자 키: 기존/new 템플릿용
    - 대문자 키: 예전 템플릿 호환용 (SERVICE_NAME 등)
    """
    try:
        cfg = _load_legal_config()
    except Exception:
        cfg = None

    service_name = cfg.service_name if cfg else "AI 뉴스 분석 콘솔"
    effective_date = cfg.effective_date.isoformat() if (cfg and cfg.effective_date) else "2025-11-02"
    operator_name = cfg.operator_name if cfg else "김동건"
    contact_email = cfg.contact_email if cfg else "privacy@example.com"
    contact_phone = cfg.contact_phone if cfg and cfg.contact_phone else ""
    privacy_html = sanitize_legal_html(getattr(cfg, "privacy_html", "") if cfg else "")
    cross_border_html = sanitize_legal_html(getattr(cfg, "cross_border_html", "") if cfg else "")
    tester_html = sanitize_legal_html(getattr(cfg, "tester_html", "") if cfg else "")

    return {
        "legal_config": cfg,
        # 🔽 소문자 키 (기본)
        "service_name": service_name,
        "effective_date": effective_date,
        "operator_name": operator_name,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "privacy_html": privacy_html,
        "cross_border_html": cross_border_html,
        "tester_html": tester_html,
        # 🔽 대문자 호환 키 (기존 템플릿 보호용)
        "SERVICE_NAME": service_name,
        "EFFECTIVE_DATE": effective_date,
        "OPERATOR_NAME": operator_name,
        "CONTACT_EMAIL": contact_email,
        "CONTACT_PHONE": contact_phone,
        "PRIVACY_HTML": privacy_html,
        "CROSS_BORDER_HTML": cross_border_html,
        "TESTER_HTML": tester_html,
    }


# ─────────────────────────────────────────────────────────────
# 공용 JSON 응답
# ─────────────────────────────────────────────────────────────
def _ok(d):
    d.setdefault("ok", True)
    return JsonResponse(d, status=200, json_dumps_params={"ensure_ascii": False})


def _fail(msg, extra=None, status_code: int = 200):
    p = {"ok": False, "error": msg}
    if extra:
        p.update(extra)
    return JsonResponse(p, status=status_code, json_dumps_params={"ensure_ascii": False})


# ─────────────────────────────────────────────────────────────
# 설정: 메타-전용 인덱싱
# ─────────────────────────────────────────────────────────────
_WEB_INGEST_META_ONLY = getattr(settings, "WEB_INGEST_META_ONLY", None)
if _WEB_INGEST_META_ONLY is None:
    _WEB_INGEST_META_ONLY = not bool(getattr(settings, "ALLOW_STORE_NEWS_BODY", False))


# 레이트리밋(세션)
def _ratelimit(request: HttpRequest, key: str, seconds: int) -> bool:
    now = timezone.now()
    last = request.session.get(key)
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
        except Exception:
            last_dt = None
        if last_dt and (now - last_dt).total_seconds() < seconds:
            return False
    request.session[key] = now.isoformat()
    request.session.modified = True
    return True


# 서버측 관대한 동의 체크
def _truthy(v):
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("on", "1", "true", "yes", "y")


def _consent_ok_server(request: HttpRequest) -> bool:
    keys = ("consent_ok", "consent_required", "agree_privacy")
    if any(_truthy(request.POST.get(k)) for k in keys):
        return True
    if any(_truthy(request.COOKIES.get(k)) for k in keys):
        return True
    if request.session.get("consent_ok") in (True, "1", "on"):
        return True
    return False


# FIX: 어떤 형태로 리턴돼도 안전 언패킹
def _unpack_answer_sources(res) -> tuple[str, list]:
    ans = ""
    srcs: list = []
    if res is None:
        return ans, srcs
    if isinstance(res, tuple):
        if len(res) >= 1 and isinstance(res[0], str):
            ans = res[0]
        if len(res) >= 2 and isinstance(res[1], (list, tuple)):
            srcs = list(res[1])
        return ans or "", srcs or []
    if isinstance(res, dict):
        ans = str(res.get("answer", "") or res.get("text", "") or "")
        raw = res.get("sources") or res.get("headlines") or []
        if isinstance(raw, (list, tuple)):
            srcs = list(raw)
        return ans or "", srcs or []
    if isinstance(res, str):
        return res, []
    try:
        return str(res), []
    except Exception:
        return "", []


# ─────────────────────────────────────────────────────────────
# ✅ 현재 사용 중인 벡터 DB 경로
# ─────────────────────────────────────────────────────────────
def _vector_db_path() -> str:
    try:
        from ragapp.services.vdb_store import vdb_path as _vdb_path  # type: ignore
        p = _vdb_path()
        if p:
            return str(p)
    except Exception:
        pass
    try:
        from ragapp.services.vector_store import vdb_path as _vdb_path2  # type: ignore
        p2 = _vdb_path2()
        if p2:
            return str(p2)
    except Exception:
        pass
    env_path = os.environ.get("VECTOR_DB_PATH")
    if env_path:
        return env_path
    return str(getattr(settings, "BASE_DIR", ".")) + "/vector_store.sqlite3"


# ─────────────────────────────────────────────────────────────
# ★ API 경로 공통 컨텍스트 (.env / settings에서 읽기)
#   - .env에 WEB_API_PATH, RAG_API_PATH 있으면 그 값 사용
#   - 없으면 기본값: /api/web_qa, /api/rag_qa
# ─────────────────────────────────────────────────────────────
def _api_paths_ctx() -> dict:
    return {
        "WEB_API_PATH": (
            os.environ.get("WEB_API_PATH")
            or getattr(settings, "WEB_API_PATH", "/api/web_qa")
        ),
        "RAG_API_PATH": (
            os.environ.get("RAG_API_PATH")
            or getattr(settings, "RAG_API_PATH", "/api/rag_qa")
        ),
    }


def _compat_aliases_web(web_state: dict, rag_state: dict) -> dict:
    def _srcs(slist):
        out = []
        for s in (slist or []):
            if isinstance(s, dict):
                out.append({
                    "title": s.get("title", ""),
                    "url": s.get("url", ""),
                    "source": s.get("source", ""),
                    "snippet": s.get("snippet", ""),
                })
            else:
                out.append({"title": str(s), "url": "", "source": "", "snippet": ""})
        return out

    return {
        "q_gemini": web_state.get("query", ""),
        "gemini_answer": web_state.get("answer", ""),
        "gemini_error": web_state.get("error", ""),
        "news_list": _srcs(web_state.get("sources", [])),
        "q_rag": rag_state.get("query", ""),
        "rag_sources": rag_state.get("sources", []),
    }


# ─────────────────────────────────────────────────────────────
# 메인 홈 (웹/Gemini + RAG 패널)
# ─────────────────────────────────────────────────────────────
@require_http_methods(["GET", "POST"])
@ensure_csrf_cookie
def home(request: HttpRequest):
    def get_web_state():
        st = request.session.get("web_state", {})
        return {
            "query": st.get("query", ""),
            "answer": st.get("answer", ""),
            "sources": st.get("sources", []),
            "msg": st.get("msg", None),
            "error": st.get("error", None),
            "log_id": st.get("log_id", None),
        }

    def get_rag_state():
        st = request.session.get("rag_state", {})
        return {
            "query": st.get("query", ""),
            "answer": st.get("answer", ""),
            "sources": st.get("sources", []),
            "msg": st.get("msg", None),
            "error": st.get("error", None),
            "log_id": st.get("log_id", None),
        }

    def save_web_state(new_state):
        request.session["web_state"] = new_state
        request.session.modified = True

    def save_rag_state(new_state):
        request.session["rag_state"] = new_state
        request.session.modified = True

    # 첫 진입(GET, 쿼리스트링 없음)이면 세션 초기화
    if request.method == "GET" and not request.GET:
        request.session.pop("web_state", None)
        request.session.pop("rag_state", None)
        web_state = {"query": "", "answer": "", "sources": [], "msg": None, "error": None, "log_id": None}
        rag_state = {"query": "", "answer": "", "sources": [], "msg": None, "error": None, "log_id": None}

    else:
        web_state = get_web_state()
        rag_state = get_rag_state()

        if request.method == "POST":
            # 🔍 디버그: 홈에서 들어오는 POST 전체 확인
            print("DEBUG HOME POST:", dict(request.POST))

            action = (request.POST.get("action") or request.POST.get("act") or "").strip()
            if not action:
                if (request.POST.get("query_web") or "").strip():
                    action = "web_search"
                elif (request.POST.get("query_rag") or "").strip():
                    action = "rag_search"

            # ── 웹 인덱싱 시 동의 체크 ────────────────────────
            if action == "web_ingest":
                if _consent_ok_server(request):
                    request.session["consent_ok"] = True
                    request.session.modified = True
                else:
                    ok_consent, err_consent = validate_required_consents(request)
                    if not ok_consent:
                        web_state["error"] = err_consent
                        save_web_state(web_state)
                        save_rag_state(rag_state)
                        ctx = {
                            "web_query": web_state["query"],
                            "web_answer": web_state["answer"],
                            "web_sources": web_state["sources"],
                            "web_sources_json": json.dumps(web_state["sources"], ensure_ascii=False),
                            "web_error": web_state["error"],
                            "web_msg": web_state["msg"],
                            "web_log_id": web_state.get("log_id"),
                            "rag_query": rag_state["query"],
                            "rag_answer": rag_state["answer"],
                            "rag_chunks": [],  # (예전 템플릿 호환용)
                            "rag_error": rag_state["error"],
                            "rag_msg": rag_state["msg"],
                            "rag_sources": rag_state["sources"],
                            "rag_log_id": rag_state.get("log_id"),
                            "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
                            "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
                            "VECTOR_DB_PATH": _vector_db_path(),
                            "model_name_gemini": _env_model_direct(),
                            "model_name_rag": _env_model_rag(),
                        }
                        ctx.update(_api_paths_ctx())
                        ctx.update(_compat_aliases_web(web_state, rag_state))
                        ctx.update(build_legal_context())
                        return render(request, "ragapp/news.html", ctx)

            # ── 웹 검색 ───────────────────────────────────────
            if action == "web_search":
                q = (request.POST.get("query_web") or "").strip()
                if not q:
                    web_state = {
                        "query": "",
                        "answer": "",
                        "sources": [],
                        "msg": None,
                        "error": "검색어를 입력해 주세요.",
                        "log_id": None,
                    }
                else:
                    try:
                        ans_text, headlines = _unpack_answer_sources(gemini_answer_with_news(q))
                        srcs = []
                        for h in (headlines or []):
                            try:
                                srcs.append(
                                    {
                                        "title": (h.get("title") if isinstance(h, dict) else "")
                                        or (h.get("url") if isinstance(h, dict) else "")
                                        or "(제목 없음)",
                                        "url": (h.get("url") if isinstance(h, dict) else "") or "",
                                        "snippet": (h.get("snippet") if isinstance(h, dict) else "")
                                        or (h.get("summary") if isinstance(h, dict) else ""),
                                        "source": (h.get("source") if isinstance(h, dict) else "") or "",
                                    }
                                )
                            except Exception:
                                srcs.append({"title": str(h), "url": "", "snippet": "", "source": ""})

                        log_obj = ChatQueryLog.objects.create(
                            mode="gemini",
                            question=q,
                            answer_excerpt=(ans_text or "")[:500],
                            client_ip=client_ip_for_log(request),
                            created_at=timezone.now(),
                            is_error=False,
                            error_msg="",
                            feedback="",
                            was_helpful=None,
                        )

                        web_state = {
                            "query": q,
                            "answer": ans_text or "",
                            "sources": srcs,
                            "msg": "웹 검색 완료",
                            "error": None,
                            "log_id": log_obj.id,
                        }

                    except Exception as e:
                        log.exception("web_search 실패")
                        err_log = ChatQueryLog.objects.create(
                            mode="gemini",
                            question=q,
                            answer_excerpt="",
                            client_ip=client_ip_for_log(request),
                            created_at=timezone.now(),
                            is_error=True,
                            error_msg=str(e),
                            feedback="",
                            was_helpful=None,
                        )
                        web_state = {
                            "query": q,
                            "answer": web_state.get("answer", ""),
                            "sources": web_state.get("sources", []),
                            "msg": None,
                            "error": f"웹 검색 중 오류: {e}",
                            "log_id": err_log.id,
                        }

            # ── 웹 검색 결과 인덱싱 ───────────────────────────
            elif action == "web_ingest":
                if not _ratelimit(request, "rate_web_ingest", 5):
                    web_state["error"] = "요청이 너무 잦습니다. 잠시 후 다시 시도하세요."
                    save_web_state(web_state)
                    save_rag_state(rag_state)
                    ctx = {
                        "web_query": web_state["query"],
                        "web_answer": web_state["answer"],
                        "web_sources": web_state["sources"],
                        "web_sources_json": json.dumps(web_state["sources"], ensure_ascii=False),
                        "web_error": web_state["error"],
                        "web_msg": web_state["msg"],
                        "web_log_id": web_state.get("log_id"),
                        "rag_query": rag_state["query"],
                        "rag_answer": rag_state["answer"],
                        "rag_chunks": [],  # (예전 템플릿 호환용)
                        "rag_error": rag_state["error"],
                        "rag_msg": rag_state["msg"],
                        "rag_sources": rag_state["sources"],
                        "rag_log_id": rag_state.get("log_id"),
                        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
                        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
                        "VECTOR_DB_PATH": _vector_db_path(),
                        "model_name_gemini": _env_model_direct(),
                        "model_name_rag": _env_model_rag(),
                    }
                    ctx.update(_api_paths_ctx())
                    ctx.update(_compat_aliases_web(web_state, rag_state))
                    ctx.update(build_legal_context())
                    return render(request, "ragapp/news.html", ctx)

                q = (request.POST.get("query_web") or "").strip()
                answer_payload = request.POST.get("web_answer_payload", "") or ""
                raw_sources = request.POST.get("web_sources_payload", "") or "[]"

                try:
                    src_list = json.loads(raw_sources)
                except Exception:
                    src_list = []

                try:
                    pseudo_news_list = []
                    for s in src_list:
                        if isinstance(s, dict):
                            pseudo_news_list.append(
                                {
                                    "title": s.get("title", ""),
                                    "url": s.get("url", ""),
                                    "source": s.get("source", ""),
                                    "published_at": "",
                                    "snippet": s.get("snippet", ""),
                                    "news_body": ("" if _WEB_INGEST_META_ONLY else s.get("snippet", "")),
                                }
                            )
                        else:
                            pseudo_news_list.append(
                                {
                                    "title": str(s),
                                    "url": "",
                                    "source": "",
                                    "published_at": "",
                                    "snippet": "",
                                    "news_body": "",
                                }
                            )

                    ingest_info = indexto_chroma_safe(q, answer_payload, pseudo_news_list)
                    log.info("web_ingest 완료: %s", ingest_info)

                    web_state = {
                        "query": q,
                        "answer": answer_payload,
                        "sources": src_list if isinstance(src_list, list) else [],
                        "msg": "웹 검색 결과 인덱싱 완료",
                        "error": None,
                        "log_id": web_state.get("log_id"),
                    }

                except Exception as e:
                    log.exception("web_ingest 실패")
                    web_state = {
                        "query": q,
                        "answer": answer_payload,
                        "sources": src_list if isinstance(src_list, list) else [],


                        "msg": None,
                        "error": f"웹결과 인덱싱 실패: {e}",
                        "log_id": web_state.get("log_id"),
                    }

            # ── RAG 검색 ──────────────────────────────────────
            elif action == "rag_search":
                q = (request.POST.get("query_rag") or "").strip()
                if not q:
                    rag_state = {
                        "query": "",
                        "answer": "",
                        "sources": [],
                        "msg": None,
                        "error": "질문을 입력해 주세요.",
                        "log_id": None,
                    }
                else:
                    try:
                        topk = max(1, int(getattr(settings, "RAG_QUERY_TOPK", 5)))
                        fallback_topk = max(topk + 5, int(getattr(settings, "RAG_FALLBACK_TOPK", 12)))
                        max_sources = int(getattr(settings, "RAG_MAX_SOURCES", 8))

                        res = rag_answer_grounded(
                            q,
                            initial_topk=topk,
                            fallback_topk=fallback_topk,
                            max_sources=max_sources,
                        )
                        if isinstance(res, tuple) and len(res) >= 2:
                            rag_answer_text, used_hits = res[0], res[1]
                        elif isinstance(res, dict):
                            rag_answer_text = res.get("answer") or res.get("text") or ""
                            used_hits = res.get("hits") or res.get("sources") or []
                        else:
                            rag_answer_text = str(res)
                            used_hits = []

                        # 근거(hit)들을 템플릿에서 바로 쓰기 좋게 평탄화
                        hits_payload: list[Dict[str, Any]] = []
                        for i, h in enumerate(used_hits or [], start=1):
                            if isinstance(h, dict):
                                meta = h.get("meta") or {}
                                title = (
                                    (meta.get("title") if isinstance(meta, dict) else None)
                                    or (meta.get("url") if isinstance(meta, dict) else None)
                                    or h.get("title")
                                    or h.get("url")
                                    or f"문서 {i}"
                                )
                                source = (
                                    (meta.get("source_name") if isinstance(meta, dict) else None)
                                    or (meta.get("source") if isinstance(meta, dict) else None)
                                    or h.get("source")
                                    or ""
                                )
                                url = (
                                    (meta.get("url") if isinstance(meta, dict) else None)
                                    or h.get("url")
                                    or ""
                                )
                                snippet = (
                                    h.get("snippet")
                                    or (meta.get("snippet") if isinstance(meta, dict) else None)
                                    or ""
                                )
                                if isinstance(meta, dict) and "score" in meta:
                                    score = meta.get("score")
                                else:
                                    score = h.get("score")

                                hits_payload.append(
                                    {
                                        "title": title,
                                        "source": source,
                                        "url": url,
                                        "snippet": snippet,
                                        "score": score,
                                    }
                                )
                            else:
                                hits_payload.append(
                                    {
                                        "title": str(h),
                                        "source": "",
                                        "url": "",
                                        "snippet": "",
                                        "score": None,
                                    }
                                )

                        normalized_sources = _normalize_rag_sources(hits_payload)

                        log_obj = ChatQueryLog.objects.create(
                            mode="rag",
                            question=q,
                            answer_excerpt=(rag_answer_text or "")[:500],
                            client_ip=client_ip_for_log(request),
                            created_at=timezone.now(),
                            is_error=False,
                            error_msg="",
                            feedback="",
                            was_helpful=None,
                        )

                        rag_state = {
                            "query": q,
                            "answer": rag_answer_text or "",
                            "sources": normalized_sources,
                            "msg": "RAG 검색 완료",
                            "error": None,
                            "log_id": log_obj.id,
                        }

                    except Exception as e:
                        log.exception("rag_search 실패")
                        err_log = ChatQueryLog.objects.create(
                            mode="rag",
                            question=q,
                            answer_excerpt="",
                            client_ip=client_ip_for_log(request),
                            created_at=timezone.now(),
                            is_error=True,
                            error_msg=str(e),
                            feedback="",
                            was_helpful=None,
                        )
                        rag_state = {
                            "query": q,
                            "answer": rag_state.get("answer", ""),
                            "sources": rag_state.get("sources", []),
                            "msg": None,
                            "error": f"RAG 검색 중 오류: {e}",
                            "log_id": err_log.id,
                        }

            # ── 예전용 dummy 액션 ─────────────────────────────
            elif action == "rag_seed":
                q = (request.POST.get("query_rag") or "").strip()
                rag_state = {
                    "query": q,
                    "answer": rag_state.get("answer", ""),
                    "sources": rag_state.get("sources", []),
                    "msg": "시드 업서트 완료 (예시)",
                    "error": None,
                    "log_id": rag_state.get("log_id"),
                }

            elif action == "chroma_init":
                q = (request.POST.get("query_rag") or "").strip()
                rag_state = {
                    "query": q,
                    "answer": rag_state.get("answer", ""),
                    "sources": rag_state.get("sources", []),
                    "msg": "컬렉션 초기화 완료 (예시)",
                    "error": None,
                    "log_id": rag_state.get("log_id"),
                }

            # ── action값 해석 실패 ────────────────────────────
            else:
                if (request.POST.get("query_web") or "").strip():
                    web_state = {
                        "query": (request.POST.get("query_web") or "").strip(),
                        "answer": web_state.get("answer", ""),
                        "sources": web_state.get("sources", []),
                        "msg": None,
                        "error": "요청을 해석할 수 없습니다. (action=web_search 폴백 실패)",
                        "log_id": web_state.get("log_id"),
                    }
                elif (request.POST.get("query_rag") or "").strip():
                    rag_state = {
                        "query": (request.POST.get("query_rag") or "").strip(),
                        "answer": rag_state.get("answer", ""),
                        "sources": rag_state.get("sources", []),
                        "msg": None,
                        "error": "요청을 해석할 수 없습니다. (action=rag_search 폴백 실패)",
                        "log_id": rag_state.get("log_id"),
                    }

        save_web_state(web_state)
        save_rag_state(rag_state)

    try:
        web_sources_json = json.dumps(web_state["sources"], ensure_ascii=False)
    except Exception:
        web_sources_json = "[]"

    ctx = {
        "web_query": web_state["query"],
        "web_answer": web_state["answer"],
        "web_sources": web_state["sources"],
        "web_sources_json": web_sources_json,
        "web_error": web_state["error"],
        "web_msg": web_state["msg"],
        "web_log_id": web_state.get("log_id"),
        "rag_query": rag_state["query"],
        "rag_answer": rag_state["answer"],
        "rag_chunks": [],  # (예전 템플릿 호환용)
        "rag_error": rag_state["error"],
        "rag_msg": rag_state["msg"],
        "rag_sources": rag_state["sources"],
        "rag_log_id": rag_state.get("log_id"),
        "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),

        "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),

        "VECTOR_DB_PATH": _vector_db_path(),
        "model_name_gemini": _env_model_direct(),
        "model_name_rag": _env_model_rag(),
    }

    ctx.update(_api_paths_ctx())
    ctx.update(_compat_aliases_web(web_state, rag_state))
    ctx.update(build_legal_context())

    return render(request, "ragapp/news.html", ctx)


# ─────────────────────────────────────────────────────────────
# 예전 news 뷰 (호환용)
# ─────────────────────────────────────────────────────────────
def news(request: HttpRequest):
    if request.method == "POST":
        print("DEBUG NEWS POST:", dict(request.POST))
    if request.method == "GET" and not request.GET:
        request.session.pop("gemini_state", None)
        request.session.pop("rag_state", None)
        ctx = {
            "model_name_gemini": _env_model_direct(),
            "model_name_rag": _env_model_rag(),
            "q_gemini": "",
            "gemini_answer": "",
            "gemini_error": "",
            "news_list": [],
            "ingest_result": "",
            "ingest_error": "",
            "q_rag": "",
            "rag_answer": "",
            "rag_error": "",
            "rag_sources": [],
            "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
            "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
            "VECTOR_DB_PATH": _vector_db_path(),
        }
        ctx.update(_api_paths_ctx())
        ctx.update(build_legal_context())
        resp = render(request, "ragapp/news.html", ctx)
        resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp["Pragma"] = "no-cache"
        resp["Expires"] = "0"
        return resp

    ctx = {}
    ctx.update(_api_paths_ctx())
    ctx.update(build_legal_context())
    return render(request, "ragapp/news.html", ctx)


# ===========================
# ✅ API: 뉴스 인덱싱 (POST + CSRF)
# ===========================
@csrf_protect
@require_http_methods(["POST"])
def api_news_ingest(request: HttpRequest):
    if not _ratelimit(request, "rate_api_news_ingest", 5):
        return _fail("요청이 너무 잦습니다. 잠시 후 다시 시도하세요.", status_code=429)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    q = (payload.get("q") or payload.get("query") or payload.get("question") or "").strip()
    if not q:
        return _fail("q(또는 query) 파라미터가 필요합니다.", status_code=400)

    try:
        topk = int(getattr(settings, "NEWS_TOPK", os.environ.get("NEWS_TOPK", "5")))

        if _WEB_INGEST_META_ONLY:
            news_headers = search_news_rss(q, topk)
            detailed_news_list = []
            for h in (news_headers or []):
                detailed_news_list.append({
                    "title": h.get("title", "") if isinstance(h, dict) else str(h),
                    "url": h.get("url", "") if isinstance(h, dict) else "",
                    "source": h.get("source", "") if isinstance(h, dict) else "",
                    "published_at": h.get("published_at", "") if isinstance(h, dict) else "",
                    "snippet": (h.get("snippet", "") if isinstance(h, dict) else ""),
                    "news_body": "",
                })
        else:
            news_headers = search_news_rss(q, topk)
            detailed_news_list = crawl_news_bodies(news_headers)

        ingest_summary = indexto_chroma_safe(q, "", detailed_news_list)

        safe_news = [{
            "title": n.get("title", ""),
            "url": n.get("url", ""),
            "source": n.get("source", ""),
            "published_at": n.get("published_at", ""),
            "snippet": n.get("snippet", ""),
        } for n in (detailed_news_list or [])]

        log_success(
            mode_label="crawl",
            query_text=q,
            preview="ingest ok",
            request=request,
            extra={"where": "api_news_ingest", "indexto_chroma": ingest_summary},
        )

        return _ok({"query": q, "news": safe_news, "indexto_chroma": ingest_summary})

    except Exception as e:
        log_error(
            mode_label="crawl",
            query_text=q,
            err_msg=str(e),
            request=request,
            extra={"where": "api_news_ingest", "stage": "exception"},
        )
        return _fail(f"뉴스 인덱싱 실패: {e}")


# ─────────────────────────────────────────────────────────────
# web_qa_view — CSRF
# ─────────────────────────────────────────────────────────────
@csrf_protect
@require_http_methods(["POST"])
def web_qa_view(request: HttpRequest):
    """
    JSON 또는 form:
      - q / query / question
    응답:
      { ok: true, answer_text: "...", answer: "...", sources: [...] }
    """
    try:
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            payload = request.POST
        q = (payload.get("q") or payload.get("query") or payload.get("question") or "").strip()
        if not q:
            return _fail("query가 비었습니다.", status_code=400)

        ans_text, headlines = _unpack_answer_sources(gemini_answer_with_news(q))

        log_obj = ChatQueryLog.objects.create(
            mode="gemini",
            question=q,
            answer_excerpt=(ans_text or "")[:500],
            client_ip=client_ip_for_log(request),
            created_at=timezone.now(),
            is_error=False,
            error_msg="",
            feedback="",
            was_helpful=None,
        )

        return _ok({
            "answer_text": ans_text or "",
            "answer": ans_text or "",
            "sources": headlines or [],
            "model": _env_model_direct(),
            "log_id": log_obj.id,
        })
    except Exception as e:
        log.exception("web_qa_view 실패")
        ChatQueryLog.objects.create(
            mode="gemini",
            question="(web_qa_view)",
            answer_excerpt="",
            client_ip=client_ip_for_log(request),
            created_at=timezone.now(),
            is_error=True,
            error_msg=str(e),
            feedback="",
            was_helpful=None,
        )
        return _fail(f"웹 QA 오류: {e}")


# ─────────────────────────────────────────────────────────────
# 공용: 세션 ID + 대화 로그 헬퍼 (QARAG/실시간 콘솔 공용 사용)
# ─────────────────────────────────────────────────────────────
def get_chat_session_id(request: HttpRequest) -> str:
    """
    QARAG / 실시간 상담 콘솔이 공용으로 쓸 세션 ID.
    - Django 세션에 chat_session_id라는 랜덤 토큰을 한 번 생성해서 재사용.
    """
    sid = request.session.get("chat_session_id")
    if not sid:
        sid = secrets.token_hex(16)
        request.session["chat_session_id"] = sid
        request.session.modified = True
    return sid


def log_chat_message(
    *,
    request: HttpRequest,
    session_id: str,
    channel: str,
    mode: str,
    role: str,
    message_type: str,
    question: str,
    content: str,
    answer_excerpt: str = "",
    sources: list | None = None,
    meta_extra: dict | None = None,
    is_error: bool = False,
    error_msg: str = "",
) -> ChatQueryLog:
    """
    ChatQueryLog 한 줄 생성 (질문/답변/에러 포함 공용).
    - QARAG / 실시간 상담 콘솔 / API 에서 모두 이 함수만 쓰면
      같은 테이블/같은 필드로 대화 로그 관리 가능.
    """
    client_ip = client_ip_for_log(request)
    meta = dict(meta_extra or {})
    meta.setdefault("path", request.path)

    return ChatQueryLog.objects.create(
        created_at=timezone.now(),
        session_id=session_id,
        channel=channel,
        mode=mode,
        role=role,
        message_type=message_type,
        question=question,
        content=content,
        answer_excerpt=answer_excerpt,
        client_ip=client_ip,
        is_error=is_error,
        error_msg=error_msg,
        was_helpful=None,
        feedback="",
        sources=sources or [],
        meta=meta,
        legal_basis="consent",
        consent_version="",
        consent_log=None,
        legal_hold=False,
        delete_at=None,
    )


# ===========================
# ✅ API: RAG QA (POST + CSRF)
# ===========================
@csrf_protect
@require_http_methods(["POST"])
def rag_qa_view(request: HttpRequest):
    from django.utils.html import escape

    def _build_faq_html(q_txt: str, a_txt: str) -> str:
        q_safe = escape(q_txt or "")
        a_safe = escape(a_txt or "").replace("\n", "<br/>")
        return (
            '<div class="qarag-faq-card">'
            '  <div class="qarag-faq-card-title">🔍 자주 묻는 질문</div>'
            f'  <div class="qarag-faq-q"><strong>Q.</strong> {q_safe}</div>'
            f'  <div class="qarag-faq-card-body">{a_safe}</div>'
            "</div>"
        )

    def _serialize_log_entry(entry: ChatQueryLog) -> dict:
        """
        프론트(QARAG/실시간 콘솔)에서 공용으로 쓸 수 있는 메시지 형태로 직렬화.
        """
        return {
            "id": entry.id,
            "role": entry.role,
            "message_type": entry.message_type,
            "mode": entry.mode,
            "channel": entry.channel,
            "content": entry.content,
            "created_at": entry.created_at.isoformat(),
        }

    # ── 입력 파싱 ───────────────────────────────────────────
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    q = (payload.get("query") or payload.get("q") or payload.get("question") or "").strip()
    _ = (payload.get("model") or "").strip()

    if not q:
        return _fail("query가 비었습니다.", status_code=400)

    # 공용 세션 ID (QARAG / 실시간 상담 콘솔 공용)
    session_id = get_chat_session_id(request)

    # 공통: 사용자 질문 로그 1줄 (우선 mode="rag"로 찍고, 나중에 faq/blocked면 보정)
    user_log = log_chat_message(
        request=request,
        session_id=session_id,
        channel="qarag",
        mode="rag",
        role="user",
        message_type="query",
        question=q,
        content=q,
        answer_excerpt="",
        sources=[],
        meta_extra={"where": "rag_qa_view"},
    )

    # ── 민감 질문 차단 ──────────────────────────────────────
    if is_sensitive_question(q):
        safe_ans = safe_block_response(q)

        # 최종 엔진이 blocked였다는 걸 사용자 로그에도 반영
        user_log.mode = "blocked"
        user_log.save(update_fields=["mode"])

        answer_log = log_chat_message(
            request=request,
            session_id=session_id,
            channel="qarag",
            mode="blocked",
            role="assistant",
            message_type="answer",
            question=q,
            content=safe_ans,
            answer_excerpt=safe_ans[:500],
            sources=[],
            meta_extra={"where": "rag_qa_view", "blocked": True},
        )

        hist_block = request.session.get("chat_history", [])
        hist_block.append({"q": q, "a": safe_ans})
        request.session["chat_history"] = hist_block
        request.session.modified = True

        return _ok({
            "mode": "blocked",
            "model": _env_model_rag(),
            "answer_text": safe_ans,
            "answer": safe_ans,
            "answer_html": "",
            "hits": [],
            "log_id": answer_log.id,
            # 🔽 공용 대화 스키마
            "session_id": session_id,
            "messages": [
                _serialize_log_entry(user_log),
                _serialize_log_entry(answer_log),
            ],
        })

    # ── FAQ 우선 매칭 ───────────────────────────────────────
    try:
        faq_answer = find_best_faq_answer(q)
    except Exception as e:
        log.warning("find_best_faq_answer 예외: %s", e)
        faq_answer = None

    if faq_answer:
        # 사용자 로그 mode를 faq로 보정
        user_log.mode = "faq"
        user_log.save(update_fields=["mode"])

        answer_log = log_chat_message(
            request=request,
            session_id=session_id,
            channel="qarag",
            mode="faq",
            role="assistant",
            message_type="answer",
            question=q,
            content=faq_answer,
            answer_excerpt=(faq_answer or "")[:500],
            sources=[],
            meta_extra={"where": "rag_qa_view", "faq": True},
        )

        log_success(
            mode_label="faq",
            query_text=q,
            preview="faq hit",
            request=request,
            extra={"where": "rag_qa_view", "faq": True},
        )

        hist = request.session.get("chat_history", [])
        hist.append({"q": q, "a": faq_answer})
        request.session["chat_history"] = hist
        request.session.modified = True

        return _ok({
            "mode": "faq",
            "model": _env_model_rag(),
            "answer_text": faq_answer,
            "answer": faq_answer,
            "answer_html": _build_faq_html(q, faq_answer),
            "hits": [],  # FAQ는 별도 hit 없음
            "log_id": answer_log.id,
            # 🔽 공용 대화 스키마
            "session_id": session_id,
            "messages": [
                _serialize_log_entry(user_log),
                _serialize_log_entry(answer_log),
            ],
        })

    # ── RAG 본 처리 ────────────────────────────────────────
    try:
        history_list = request.session.get("chat_history", [])

        topk = max(1, int(getattr(settings, "RAG_QUERY_TOPK", 5)))
        fallback_topk = max(topk + 5, int(getattr(settings, "RAG_FALLBACK_TOPK", 12)))
        max_sources = int(getattr(settings, "RAG_MAX_SOURCES", 8))

        res = rag_answer_grounded_with_history(
            q,
            history_list,
            base_retriever_func=rag_answer_grounded,
            initial_topk=topk,
            fallback_topk=fallback_topk,
            max_sources=max_sources,
        )
        if isinstance(res, tuple) and len(res) >= 2:
            rag_text, used_hits = res[0], res[1]
        elif isinstance(res, dict):
            rag_text = res.get("answer") or res.get("text") or ""
            used_hits = res.get("hits") or res.get("sources") or []
        else:
            rag_text = str(res)
            used_hits = []

        hits_payload = []
        for i, h in enumerate(used_hits or [], start=1):
            if isinstance(h, dict):
                m = h.get("meta") or {}
                hits_payload.append(
                    {
                        "idx": i,
                        "title": m.get("title") or m.get("url") or h.get("title") or h.get("url") or "문서",
                        "source": m.get("source_name") or m.get("source") or h.get("source") or "",
                        "url": m.get("url") or h.get("url") or "",
                        "snippet": h.get("snippet") or "",
                        "score": m.get("score") if "score" in m else h.get("score"),
                    }
                )
            else:
                hits_payload.append({"idx": i, "title": str(h), "source": "", "url": "", "snippet": "", "score": None})

        # 사용자 로그 mode를 rag로 명시 (기본값이 rag라서 큰 변화는 없음)
        user_log.mode = "rag"
        user_log.save(update_fields=["mode"])

        answer_log = log_chat_message(
            request=request,
            session_id=session_id,
            channel="qarag",
            mode="rag",
            role="assistant",
            message_type="answer",
            question=q,
            content=rag_text,
            answer_excerpt=(rag_text or "")[:500],
            sources=hits_payload,
            meta_extra={"where": "rag_qa_view", "hit_count": len(used_hits or [])},
        )

        log_success(
            mode_label="rag",
            query_text=q,
            preview="rag ok (rag_qa_view)",
            request=request,
            extra={"where": "rag_qa_view", "hit_count": len(used_hits or [])},
        )

        history_list.append({"q": q, "a": rag_text})
        request.session["chat_history"] = history_list
        request.session.modified = True

        return _ok({
            "mode": "rag",
            "model": _env_model_rag(),
            "answer_text": rag_text,
            "answer": rag_text,
            "answer_html": "",
            "hits": hits_payload,
            "log_id": answer_log.id,
            # 🔽 공용 대화 스키마
            "session_id": session_id,
            "messages": [
                _serialize_log_entry(user_log),
                _serialize_log_entry(answer_log),
            ],
        })

    except Exception as e:
        # 에러 로그 (system/error)
        err_log = log_chat_message(
            request=request,
            session_id=session_id,
            channel="qarag",
            mode="rag",
            role="system",
            message_type="error",
            question=q,
            content="",
            answer_excerpt="",
            sources=[],
            meta_extra={"where": "rag_qa_view", "stage": "rag_answer_grounded"},
            is_error=True,
            error_msg=str(e),
        )
        log_error(
            mode_label="rag",
            query_text=q,
            err_msg=str(e),
            request=request,
            extra={"where": "rag_qa_view", "stage": "rag_answer_grounded", "log_id": err_log.id},
        )
        return _fail(f"RAG 검색 실패: {e}")


# ===========================
# ✅ API: RAG 대화 (POST + CSRF)
# ===========================
@csrf_protect
@require_http_methods(["POST"])
def qa_rag_chat(request: HttpRequest):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception as e:
        ChatQueryLog.objects.create(
            mode="rag",
            question="(invalid json body)",
            answer_excerpt="",
            client_ip=client_ip_for_log(request),
            created_at=timezone.now(),
            is_error=True,
            error_msg=f"invalid json: {e}",
            feedback="",
            was_helpful=None,
        )
        return JsonResponse({"ok": False, "error": f"invalid json: {e}"}, status=400, json_dumps_params={"ensure_ascii": False})

    q = (payload.get("question") or payload.get("q") or "").strip()
    if not q:
        ChatQueryLog.objects.create(
            mode="rag",
            question="(empty question)",
            answer_excerpt="",
            client_ip=client_ip_for_log(request),
            created_at=timezone.now(),
            is_error=True,
            error_msg="empty question",
            feedback="",
            was_helpful=None,
        )
        return _fail("질문이 비어 있습니다.", status_code=400)

    topk = max(1, int(getattr(settings, "RAG_QUERY_TOPK", 5)))
    fallback_topk = max(topk + 5, int(getattr(settings, "RAG_FALLBACK_TOPK", 12)))
    max_sources = int(getattr(settings, "RAG_MAX_SOURCES", 8))

    try:
        history_list = request.session.get("chat_history", [])

        res = rag_answer_grounded_with_history(
            q,
            history_list,
            base_retriever_func=rag_answer_grounded,
            initial_topk=topk,
            fallback_topk=fallback_topk,
            max_sources=max_sources,
        )
        if isinstance(res, tuple) and len(res) >= 2:
            rag_answer_text, used_hits = res[0], res[1]
        elif isinstance(res, dict):
            rag_answer_text = res.get("answer") or res.get("text") or ""
            used_hits = res.get("hits") or res.get("sources") or []
        else:
            rag_answer_text = str(res)
            used_hits = []

        hits_payload = []
        for i, h in enumerate(used_hits or [], start=1):
            if isinstance(h, dict):
                m = h.get("meta") or {}
                hits_payload.append(
                    {
                        "idx": i,
                        "title": m.get("title") or m.get("url") or h.get("title") or h.get("url") or "문서",
                        "source": m.get("source_name") or m.get("source") or h.get("source") or "",
                        "url": m.get("url") or h.get("url") or "",
                        "snippet": (h.get("snippet") or "")[:500],
                        "score": h.get("score"),
                    }
                )
            else:
                hits_payload.append({"idx": i, "title": str(h), "source": "", "url": "", "snippet": "", "score": None})

        ChatQueryLog.objects.create(
            mode="rag",
            question=q,
            answer_excerpt=(rag_answer_text or "")[:500],
            client_ip=client_ip_for_log(request),
            created_at=timezone.now(),
            is_error=False,
            error_msg="",
            feedback="",
            was_helpful=None,
        )

        log_success(
            mode_label="rag",
            query_text=q,
            preview="qa_rag_chat ok",
            request=request,
            extra={"where": "qa_rag_chat", "hit_count": len(hits_payload)},
        )

        history_list.append({"q": q, "a": rag_answer_text})
        request.session["chat_history"] = history_list
        request.session.modified = True

        return _ok({
            "answer_text": rag_answer_text or "(빈 응답)",
            "answer": rag_answer_text or "(빈 응답)",
            "hits": hits_payload,
            "model": _env_model_rag(),
        })

    except Exception as e:
        ChatQueryLog.objects.create(
            mode="rag",
            question=q,
            answer_excerpt="",
            client_ip=client_ip_for_log(request),
            created_at=timezone.now(),
            is_error=True,
            error_msg=str(e),
            feedback="",
            was_helpful=None,
        )
        log_error(
            mode_label="rag",
            query_text=q,
            err_msg=str(e),
            request=request,
            extra={"where": "qa_rag_chat", "stage": "rag_answer_grounded"},
        )
        return _fail(f"RAG 오류: {e}")


# ─────────────────────────────────────────────────────────────
# 어시스턴트 단독 뷰
# ─────────────────────────────────────────────────────────────
def assistant_view(request: HttpRequest) -> HttpResponse:
    ctx = {"model_name_rag": _env_model_rag()}
    return render(request, "ragapp/assistant.html", ctx)


# ─────────────────────────────────────────────────────────────
# indexto_chroma_safe (로컬 shim)
# ─────────────────────────────────────────────────────────────
def indexto_chroma_safe(query: str, answer: str, news_list: list[dict]):
    from urllib.parse import urlparse
    from ragapp.services.news_services import (
        _chunk_text,
        _sha,
        _slug,
        _iso,
    )

    try:
        from ragapp.services.vertex_embed import embed_texts as _embed_texts  # type: ignore
    except Exception:
        from ragapp.services.news_services import _embed_texts  # type: ignore

    vdb_upsert = None
    try:
        from ragapp.services.vdb_store import vdb_upsert as _vup  # type: ignore
        vdb_upsert = _vup
    except Exception:
        try:
            from ragapp.services.vector_store import vdb_upsert as _vup2  # type: ignore
            vdb_upsert = _vup2
        except Exception:
            vdb_upsert = None

    if vdb_upsert is None:
        raise RuntimeError("벡터 DB 어댑터를 찾을 수 없습니다. ragapp.services.vdb_store.vdb_upsert 를 구현해 주세요.")

    size = int(getattr(settings, "EMBED_CHUNK_SIZE", 1600))
    overlap = int(getattr(settings, "EMBED_CHUNK_OVERLAP", 200))
    min_body = int(getattr(settings, "MIN_NEWS_BODY_CHARS", 400))
    now = datetime.utcnow().isoformat()

    all_ids, all_docs, all_metas = [], [], []

    # ── 답변 텍스트 인덱싱 ─────────────────────────────────
    a_chunks = _chunk_text(answer or "", size=size, overlap=overlap)
    base_a = f"answer:{_sha(query)}"
    for i, ch in enumerate(a_chunks):
        ch_clean = (ch or "").strip()
        if not ch_clean:
            continue
        all_ids.append(f"{base_a}:{i}")
        all_docs.append(ch_clean)
        all_metas.append(
            {
                "source": "web_answer",
                "title": "웹검색 답변",
                "question": query,
                "ingested_at": now,
            }
        )

    # ── 뉴스 헤더/본문 인덱싱 ───────────────────────────────
    news_summaries = []
    for art in (news_list or []):
        url = (art.get("final_url") or art.get("url") or "").strip()
        title = (art.get("title") or "").strip() or "(제목 없음)"
        body = (art.get("news_body") or "").strip()

        base = f"news:{_slug(title)}:{_sha(url or title)}"

        meta_doc_lines = [
            f"[META ONLY] {title}",
            f"URL: {url}" if url else "URL: (없음)",
            f"출처: {art.get('source','')}",
            f"게시: {_iso(art.get('published_at'))}",
            (art.get("snippet") or "")[:500],
        ]
        meta_doc = "\n".join([ln for ln in meta_doc_lines if ln]).strip()

        all_ids.append(f"{base}:meta")
        all_docs.append(meta_doc)
        all_metas.append(
            {
                "source": "news",
                "meta_only": (len(body) < min_body) or _WEB_INGEST_META_ONLY,
                "url": url,
                "title": title,
                "source_name": art.get("source", ""),
                "published_at": art.get("published_at", ""),
                "ingested_at": now,
            }
        )

        chunks_for_this_news = 1

        if (not _WEB_INGEST_META_ONLY) and len(body) >= min_body:
            from ragapp.services.news_services import _chunk_text as _chunk_text_body
            body_chunks = _chunk_text_body(body, size=size, overlap=overlap)
            body_cnt = 0
            for j, ch in enumerate(body_chunks):
                ch_clean = (ch or "").strip()
                if not ch_clean:
                    continue
                all_ids.append(f"{base}:{j}")
                all_docs.append(ch_clean)
                all_metas.append(
                    {
                        "source": "news",
                        "url": url,
                        "title": title,
                        "source_name": art.get("source", ""),
                        "published_at": art.get("published_at", ""),
                        "ingested_at": now,
                    }
                )
                body_cnt += 1
            chunks_for_this_news += body_cnt

        news_summaries.append(
            {
                "title": title,
                "url": url,
                "chunks": chunks_for_this_news,
                "meta_only": _WEB_INGEST_META_ONLY or (len(body) < min_body),
            }
        )

    clean_rows = [
        (doc_id, doc_text, meta)
        for (doc_id, doc_text, meta) in zip(all_ids, all_docs, all_metas)
        if isinstance(doc_text, str) and doc_text.strip()
    ]

    if not clean_rows:
        return {
            "inserted": 0,
            "answer_chunks": 0,
            "news_total_chunks": 0,
            "news_items": news_summaries,
            "collection": getattr(settings, "VECTOR_DB_LABEL", getattr(settings, "CHROMA_COLLECTION", "")),
            "dir": _vector_db_path(),
            "ingested_at": now,
            "note": "인덱싱할 데이터가 없습니다. (뉴스가 없거나 본문/메타가 비었습니다.)",
        }

    final_ids, final_docs, final_metas = map(list, zip(*clean_rows))
    try:
        from ragapp.services.vertex_embed import embed_texts as _embed_texts  # type: ignore
    except Exception:
        from ragapp.services.news_services import _embed_texts  # type: ignore
    embs = _embed_texts(final_docs)

    vdb_upsert(final_ids, final_docs, final_metas, embs)

    ans_chunks = sum(1 for m in final_metas if m.get("source") == "web_answer")
    news_chunks = sum(1 for m in final_metas if m.get("source") == "news" and not m.get("meta_only"))

    return {
        "inserted": len(final_ids),
        "answer_chunks": ans_chunks,
        "news_total_chunks": news_chunks,
        "news_items": news_summaries,
        "collection": getattr(settings, "VECTOR_DB_LABEL", getattr(settings, "CHROMA_COLLECTION", "")),
        "dir": _vector_db_path(),
        "ingested_at": now,
    }


# ===========================
# ✅ 업로드/인덱싱 (PDF/TXT + 붙여넣기 텍스트)
# ===========================
@csrf_protect
@require_http_methods(["GET", "POST"])
def upload_doc_view(request: HttpRequest):
    """
    /ragadmin/upload-doc/
    - 파일(PDF/TXT 여러 개: name=files/docfiles/file) 또는 붙여넣기 텍스트(rawtext/direct_text/pasted_text)
    - 텍스트 추출 → 청킹(1600/200) → 임베딩 → vdb_upsert
    - 템플릿 호환: error_msg / file_errors / result_summaries 제공
    """
    # ✅ 공통 환경값
    media_root = getattr(settings, "MEDIA_ROOT", None) or os.path.join(str(getattr(settings, "BASE_DIR", ".")), "uploads")
    media_url  = getattr(settings, "MEDIA_URL", "/uploads/")
    if not str(media_url).endswith("/"):
        media_url = str(media_url) + "/"

    def _pdf_to_text(fp: io.BufferedIOBase) -> str:
        try:
            from pdfminer.high_level import extract_text  # pdfminer.six
            return extract_text(fp) or ""
        except Exception as e1:
            try:
                from pypdf import PdfReader
                fp.seek(0)
                reader = PdfReader(fp)
                return "\n".join([(p.extract_text() or "") for p in reader.pages]).strip()
            except Exception as e2:
                raise RuntimeError(f"PDF 추출 실패(pdfminer/pypdf 필요): {e1 or e2}")

    def _chunk(text: str, maxlen: int = 1600, overlap: int = 200) -> List[str]:
        t = (text or "").strip()
        if not t:
            return []
        out, n, i = [], len(t), 0
        while i < n:
            j = min(n, i + maxlen)
            out.append(t[i:j])
            if j >= n:
                break
            i = max(0, j - overlap)
        return out

    # GET
    if request.method == "GET":
        ctx = {
            "MEDIA_URL": str(media_url),
            "MEDIA_ROOT": str(media_root),
            "VECTOR_DB_PATH": _vector_db_path(),
            "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
            "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        }
        return render(request, "ragadmin/upload_doc.html", ctx)

    # 입력 수집
    common_title = (request.POST.get("common_title") or request.POST.get("title") or "").strip()
    source_label = (request.POST.get("source_label") or request.POST.get("source_name") or "").strip()
    pasted_text = (
        (request.POST.get("direct_text") or "") or
        (request.POST.get("pasted_text") or "") or
        (request.POST.get("rawtext") or "")
    ).strip()

    files: List = []
    files += list(request.FILES.getlist("files"))
    files += list(request.FILES.getlist("docfiles"))
    if request.FILES.get("file"):
        files.append(request.FILES["file"])

    extracted: List[Tuple[str, str]] = []
    file_errors: List[str] = []

    if pasted_text:
        extracted.append(("__pasted__.txt", pasted_text))

    for f in files:
        try:
            name = getattr(f, "name", "uploaded")
            ext = os.path.splitext(name.lower())[1]
            buf = io.BytesIO(f.read())
            if ext == ".pdf":
                text = _pdf_to_text(buf)
            else:
                try:
                    text = buf.getvalue().decode("utf-8", errors="ignore")
                except Exception:
                    text = buf.getvalue().decode("cp949", errors="ignore")
            text = (text or "").strip()
            if not text:
                file_errors.append(f"{name}: 추출된 텍스트가 없습니다.")
            else:
                extracted.append((name, text))
        except Exception as e:
            log.exception("파일 처리 실패: %s", getattr(f, "name", "?"))
            file_errors.append(f"{getattr(f,'name','?')}: {e}")

    if not extracted:
        messages.error(request, "유효한 텍스트가 없어 인덱싱을 진행하지 않았습니다.")
        return render(request, "ragadmin/upload_doc.html", {
            "error_msg": "유효한 텍스트가 없어 인덱싱을 진행하지 않았습니다.",
            "file_errors": file_errors,
            "MEDIA_URL": str(media_url),
            "MEDIA_ROOT": str(media_root),
            "VECTOR_DB_PATH": _vector_db_path(),
            "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
            "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        })

    size = int(getattr(settings, "EMBED_CHUNK_SIZE", 1600))
    overlap = int(getattr(settings, "EMBED_CHUNK_OVERLAP", 200))
    now_iso = timezone.now().strftime("%Y-%m-%d %H:%M:%S")

    all_ids: List[str] = []
    all_docs: List[str] = []
    all_metas: List[Dict] = []
    from collections import defaultdict
    per_file_cnt = defaultdict(int)

    from ragapp.services.vector_store import _sha as _sha_vs  # 안전 해시

    for name, text in extracted:
        chunks = _chunk(text, maxlen=size, overlap=overlap)
        doc_id = _sha_vs(f"{name}::{now_iso}")[:20]
        for i, ch in enumerate(chunks):
            meta = {
                "title": common_title or name,
                "file_name": name,
                "source": source_label or "upload",
                "doc_id": doc_id,
                "chunk_index": i,
                "ingested_at": now_iso,
            }
            all_docs.append(ch)
            all_metas.append(meta)
            all_ids.append(_sha_vs(f"{doc_id}::{i}")[:64])
            per_file_cnt[name] += 1

    if not all_docs:
        messages.warning(request, "청킹 결과가 비어 업서트를 진행하지 않았습니다.")
        return render(request, "ragadmin/upload_doc.html", {
            "error_msg": "청킹 결과가 비어 업서트를 진행하지 않았습니다.",
            "file_errors": file_errors,
            "MEDIA_URL": str(media_url),
            "MEDIA_ROOT": str(media_root),
            "VECTOR_DB_PATH": _vector_db_path(),
            "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
            "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        })

    # 임베딩 + 업서트
    try:
        try:
            from ragapp.services.vertex_embed import embed_texts as _embed_texts  # Vertex 우선
        except Exception:
            from ragapp.services.news_services import _embed_texts       # 폴백
        embs = _embed_texts(all_docs)

        try:
            from ragapp.services.vdb_store import vdb_upsert as _vup
        except Exception:
            from ragapp.services.vector_store import vdb_upsert as _vup
        _vup(all_ids, all_docs, all_metas, embs)

        result_summaries = [
            {
                "title": common_title or fname,
                "file_name": fname,
                "inserted_chunks": cnt,
                "uploaded_at": now_iso,
            }
            for fname, cnt in per_file_cnt.items()
        ]

        messages.success(request, f"인덱싱 완료: 총 {len(all_ids)} 청크 업서트")
        return render(request, "ragadmin/upload_doc.html", {
            "error_msg": None,
            "file_errors": file_errors,
            "result_summaries": result_summaries,
            "MEDIA_URL": str(media_url),
            "MEDIA_ROOT": str(media_root),
            "VECTOR_DB_PATH": _vector_db_path(),
            "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
            "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        })

    except Exception as e:
        log.exception("업서트 실패")
        messages.error(request, f"업서트 실패: {e}")
        return render(request, "ragadmin/upload_doc.html", {
            "error_msg": f"업서트 실패: {e}",
            "file_errors": file_errors,
            "MEDIA_URL": str(media_url),
            "MEDIA_ROOT": str(media_root),
            "VECTOR_DB_PATH": _vector_db_path(),
            "CHROMA_COLLECTION": getattr(settings, "CHROMA_COLLECTION", ""),
            "CHROMA_DB_DIR": getattr(settings, "CHROMA_DB_DIR", ""),
        })


@csrf_protect
@require_http_methods(["POST"])
def qarag_live_chat_request(request: HttpRequest):
    """
    QARAG에서 '상담사 연결' 버튼 눌렀을 때 호출하는 API.
    - 마지막 질문/답변 일부를 남겨서 운영자가 무슨 맥락인지 보게.
    """
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    q = (payload.get("question") or payload.get("q") or "").strip()
    answer_excerpt = (payload.get("answer_excerpt") or "").strip()
    client_label = (payload.get("client_label") or "").strip() or "웹 QARAG 사용자"

    # room_id 는 간단히 랜덤으로
    room_id = f"client-{timezone.now().strftime('%Y%m%d%H%M%S')}-{os.urandom(3).hex()[:6]}"

    from ragapp.models import LiveChatRoom  # 위에서 만든 모델 사용

    room = LiveChatRoom.objects.create(
        room_id=room_id,
        client_label=client_label,
        last_question=q or "(질문 없음)",
        status="waiting",
    )

    # 운영자 Live Chat 화면에서 이 room 들을 조회해서
    # '대기' 목록으로 보여주면 됨.
    return _ok({
        "room_id": room.room_id,
        "status": room.status,
    })
