# ragapp/admin.py
from __future__ import annotations
import csv, hashlib, mimetypes, logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.contrib import admin, messages
from django.utils import timezone
from django.urls import reverse, path
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.conf import settings
from ragapp.models import TableSearchRule

from ragapp.models import (
    MyLog,
    RagSetting,
    FaqEntry,
    ChatQueryLog,
    Feedback,
    IngestHistory,
    LegalConfig,
    RagChunk,
    LiveChatSession,
    TableSchema,   # ✅ 표 스키마
)

# ✅ RAG 전용 AdminSite 인스턴스
from ragapp.admin_site import rag_admin_site

# 선택 모델 (있을 수도 있고, 없을 수도 있음)
try:
    from ragapp.models import MediaAsset, TableDataset  # type: ignore
    _HAS_MEDIA_MODELS = True
except Exception:
    _HAS_MEDIA_MODELS = False

log = logging.getLogger(__name__)

# ─────────────────────────────
# MyLog
# ─────────────────────────────
class MyLogAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "mode_text", "query", "ok_flag", "remote_addr_text")
    list_filter = ("mode_text", "ok_flag")
    search_fields = ("query", "remote_addr_text", "extra_json")
    readonly_fields = ("created_at", "mode_text", "query", "ok_flag", "remote_addr_text", "extra_json")
    fieldsets = (
        (None, {"fields": ("created_at", "mode_text", "query", "ok_flag", "remote_addr_text")}),
        ("추가 정보(JSON})", {"fields": ("extra_json",)}),
    )


# ─────────────────────────────
# RagSetting
# ─────────────────────────────
class RagSettingAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "chroma_db_dir",
        "chroma_collection",
        "news_topk",
        "rag_query_topk",
        "rag_fallback_topk",
        "rag_max_sources",
        "auto_ingest_after_gemini",
        "web_ingest_to_chroma",
        "crawl_answer_links",
        "action_links",
    )
    search_fields = ("chroma_db_dir", "chroma_collection")

    def action_links(self, obj):
        def pill(href: str, label: str, style: str) -> str:
            return f'<a href="{href}" style="{style}">{label}</a>'

        base = (
            "display:inline-block;margin:0 4px 4px 0;padding:4px 8px;border-radius:6px;"
            "font-size:11px;font-weight:500;line-height:1.2;text-decoration:none;border:1px solid transparent;"
            "box-shadow:0 1px 2px rgba(0,0,0,.08);"
        )
        styles = {
            "edit-main": base + "background:linear-gradient(90deg,#6366f1,#4f46e5);color:#fff;border-color:rgba(99,102,241,.6);",
            "delete-main": base + "background:linear-gradient(90deg,#ef4444,#dc2626);color:#fff;border-color:rgba(239,68,68,.5);",
            "edit-alt": base + "background:#fff;color:#4f46e5;border-color:#6366f1;",
            "plain": base + "background:#fff;color:#374151;border-color:#d1d5db;",
        }
        links = []
        try:
            links.append(
                pill(
                    reverse("ragadmin:ragapp_ragsetting_change", args=[obj.pk]),
                    "✏️ 수정(ragadmin)",
                    styles["edit-main"],
                )
            )
            links.append(
                pill(
                    reverse("ragadmin:ragapp_ragsetting_delete", args=[obj.pk]),
                    "🗑 삭제(ragadmin)",
                    styles["delete-main"],
                )
            )
        except Exception:
            pass
        try:
            links.append(
                pill(
                    reverse("admin:ragapp_ragsetting_change", args=[obj.pk]),
                    "⚙ 수정(admin)",
                    styles["edit-alt"],
                )
            )
            links.append(
                pill(
                    reverse("admin:ragapp_ragsetting_delete", args=[obj.pk]),
                    "⚠ 삭제(admin)",
                    styles["plain"],
                )
            )
        except Exception:
            pass
        from django.utils.html import mark_safe

        return mark_safe("".join(links) or "-")

    action_links.short_description = "관리 액션"


# ─────────────────────────────
# ChatQueryLog
# ─────────────────────────────
class ChatQueryLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "mode_badge",
        "short_q",
        "short_a",
        "is_error",
        "was_helpful",
        "feedback_short",
        "client_ip",
    )
    list_display_links = ("short_q",)
    list_filter = ("mode", "was_helpful", "is_error", "created_at")
    search_fields = ("question", "answer_excerpt", "client_ip", "feedback")
    ordering = ("-created_at", "-id")
    date_hierarchy = "created_at"
    list_per_page = 50
    empty_value_display = "-"
    actions = ("mark_helpful", "mark_unhelpful", "clear_feedback")
    readonly_fields = (
        "created_at",
        "mode",
        "question",
        "answer_excerpt",
        "client_ip",
        "is_error",
        "error_msg",
        "was_helpful",
        "feedback",
        "legal_basis",
        "consent_version",
        "consent_log",
        "legal_hold",
        "delete_at",
    )

    def mark_helpful(self, request, qs):
        c = qs.update(was_helpful=True)
        self.message_user(request, f"{c}개를 Helpful로 표시했습니다.")

    mark_helpful.short_description = "선택 항목 Helpful로 표시"

    def mark_unhelpful(self, request, qs):
        c = qs.update(was_helpful=False)
        self.message_user(request, f"{c}개를 Not helpful로 표시했습니다.")

    mark_unhelpful.short_description = "선택 항목 Not helpful로 표시"

    def clear_feedback(self, request, qs):
        c = qs.update(feedback="")
        self.message_user(request, f"{c}개의 코멘트를 비웠습니다.")

    clear_feedback.short_description = "선택 항목 코멘트 비우기"

    def mode_badge(self, obj):
        from django.utils.html import format_html

        color = {
            "rag": "#38bdf8",
            "gemini": "#a78bfa",
            "faq": "#10b981",
            "blocked": "#f87171",
        }.get(obj.mode, "#94a3b8")
        return format_html(
            '<span style="padding:2px 8px;border-radius:999px;background:{}20;color:{}">{}</span>',
            color,
            color,
            (obj.mode or "").upper(),
        )

    mode_badge.short_description = "Mode"

    def feedback_short(self, obj):
        txt = (obj.feedback or "").strip().replace("\n", " ")
        return (txt[:40] + "...") if len(txt) > 40 else txt

    feedback_short.short_description = "피드백 코멘트"

    def short_q(self, obj):
        q = (obj.question or "").strip().replace("\n", " ")
        return (q[:30] + "...") if len(q) > 30 else q

    short_q.short_description = "질문 미리보기"

    def short_a(self, obj):
        a = (obj.answer_excerpt or "").strip().replace("\n", " ")
        return (a[:30] + "...") if len(a) > 30 else a

    short_a.short_description = "답변 미리보기"


# ─────────────────────────────
# FaqEntry
# ─────────────────────────────
class FaqEntryAdmin(admin.ModelAdmin):
    list_display = ("short_question", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("question", "answer")
    ordering = ("-updated_at",)
    readonly_fields = ("created_at", "updated_at")

    def short_question(self, obj):
        q = (obj.question or "").strip().replace("\n", " ")
        return (q[:60] + "…") if len(q) > 60 else q

    short_question.short_description = "질문"


# ─────────────────────────────
# Feedback
# ─────────────────────────────
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "answer_type", "is_helpful", "short_question", "short_answer")
    list_filter = ("answer_type", "is_helpful", "created_at")
    search_fields = ("question", "answer")
    readonly_fields = ("created_at", "question", "answer", "answer_type", "is_helpful", "sources_json")

    def short_question(self, obj):
        txt = (obj.question or "").strip().replace("\n", " ")
        return (txt[:60] + "...") if len(txt) > 60 else txt

    short_question.short_description = "Question"

    def short_answer(self, obj):
        txt = (obj.answer or "").strip().replace("\n", " ")
        return (txt[:60] + "...") if len(txt) > 60 else txt

    short_answer.short_description = "Answer Preview"


# ─────────────────────────────
# IngestHistory
# ─────────────────────────────
class IngestHistoryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "keyword", "ingested_count", "total_candidates", "skipped_count", "failed_count")
    list_filter = ("keyword", "created_at")
    search_fields = ("keyword",)
    readonly_fields = (
        "created_at",
        "keyword",
        "total_candidates",
        "ingested_count",
        "skipped_count",
        "failed_count",
        "detail",
    )


# ─────────────────────────────
# LegalConfig
# ─────────────────────────────
class LegalConfigAdmin(admin.ModelAdmin):
    search_fields = ("service_name", "operator_name", "contact_email")


# ─────────────────────────────
# RagChunk
# ─────────────────────────────
class RagChunkAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "url", "doc_id", "dim", "created_at")
    list_filter = ("dim", "created_at")
    search_fields = ("title", "url", "text", "doc_id", "unique_hash")


# ─────────────────────────────
# LiveChatSession
# ─────────────────────────────
class LiveChatSessionAdmin(admin.ModelAdmin):
    """
    실시간 상담 세션 기록 관리용 Admin
    - 상담기록/내역은 여기에서 조회·검색
    """
    list_display = (
        "id",
        "status",
        "room",
        "source",
        "user_name",
        "client_ip",
        "session_type",
        "session_note",
        "started_at",
        "connected_at",
        "ended_at",
    )
    list_filter = ("status", "source", "room", "session_type")
    search_fields = ("id", "room", "user_name", "client_ip", "session_note")
    readonly_fields = (
        "created_at",
        "started_at",
        "connected_at",
        "ended_at",
        "last_message_at",
    )
    ordering = ("-created_at",)


# ─────────────────────────────
# TableSchema
# ─────────────────────────────
class TableSchemaAdmin(admin.ModelAdmin):
    """
    CSV/엑셀로 올린 표 구조 확인용 Admin
    - 어떤 컬럼이 있고, 샘플 데이터가 어떻게 생겼는지 한눈에 보기
    """
    list_display = ("table_name", "created_at", "updated_at")
    search_fields = ("table_name",)
    readonly_fields = ("columns", "column_types", "sample_rows", "created_at", "updated_at")
    ordering = ("table_name",)


# ─────────────────────────────
# 기본 admin.site 등록
# ─────────────────────────────
admin.site.register(MyLog, MyLogAdmin)
admin.site.register(RagSetting, RagSettingAdmin)
admin.site.register(ChatQueryLog, ChatQueryLogAdmin)
admin.site.register(FaqEntry, FaqEntryAdmin)
admin.site.register(Feedback, FeedbackAdmin)
admin.site.register(IngestHistory, IngestHistoryAdmin)
admin.site.register(LegalConfig, LegalConfigAdmin)
admin.site.register(RagChunk, RagChunkAdmin)
admin.site.register(LiveChatSession, LiveChatSessionAdmin)
admin.site.register(TableSchema, TableSchemaAdmin)   # ✅ 표 스키마


# ─────────────────────────────
# rag_admin_site 등록
# ─────────────────────────────
rag_admin_site.register(MyLog, MyLogAdmin)
rag_admin_site.register(RagSetting, RagSettingAdmin)
rag_admin_site.register(ChatQueryLog, ChatQueryLogAdmin)
rag_admin_site.register(FaqEntry, FaqEntryAdmin)
rag_admin_site.register(Feedback, FeedbackAdmin)
rag_admin_site.register(IngestHistory, IngestHistoryAdmin)
rag_admin_site.register(LegalConfig, LegalConfigAdmin)
rag_admin_site.register(RagChunk, RagChunkAdmin)
rag_admin_site.register(LiveChatSession, LiveChatSessionAdmin)
rag_admin_site.register(TableSchema, TableSchemaAdmin)   # ✅ 표 스키마


# ─────────────────────────────
# 선택: MediaAsset / TableDataset
# ─────────────────────────────
if _HAS_MEDIA_MODELS:

    class MediaAssetAdmin(admin.ModelAdmin):
        list_display = ("id", "file", "caption", "indexed_at", "size", "mime")
        search_fields = ("caption", "file")

        def get_urls(self):
            urls = super().get_urls()
            custom = [
                path(
                    "search/",
                    self.admin_site.admin_view(self.search_view),
                    name="mediaasset_search",
                )
            ]
            return custom + urls

        def search_view(self, request: HttpRequest):
            token = get_token(request)
            html_top = f"""
            <div class="ma-wrap"><div class="ma-card">
              <div class="ma-h1">Media 이미지 검색</div>
              <form method="post" class="ma-form" style="margin-bottom:12px">
                <input type="hidden" name="csrfmiddlewaretoken" value="{token}">
                <input name="q" type="text" placeholder="예: 노을 바다 풍경" style="width:420px" required>
                <input name="k" type="number" value="8" min="1" max="50" style="width:80px">
                <button class="ma-btn" type="submit">검색</button>
                <a class="ma-link" href="{reverse('ragadmin:ragapp_mediaasset_changelist')}" style="margin-left:8px">← 목록으로</a>
              </form>
            """
            if request.method != "POST":
                return HttpResponse(html_top + "</div></div>")

            from ragapp.services.vertex_embed import embed_text_mm
            from ragapp.services.chroma_media import search_images_by_text_embedding

            q = (request.POST.get("q") or "").strip()
            try:
                k = int(request.POST.get("k") or 8)
            except Exception:
                k = 8

            try:
                qv = embed_text_mm(q)
                res = search_images_by_text_embedding(text_embedding=qv, k=k) or {}
                ids = (res.get("ids") or [[]])[0] if isinstance(res.get("ids"), list) else []
                metas = (res.get("metadatas") or [[]])[0] if isinstance(res.get("metadatas"), list) else []
                docs = (res.get("documents") or [[]])[0] if isinstance(res.get("documents"), list) else []

                rows = []
                for i, (pid, meta, doc) in enumerate(zip(ids, metas, docs), 1):
                    path_val = (meta or {}).get("path", "")
                    rows.append(
                        f"<tr><td>{i}</td><td>{pid}</td><td>{doc or '-'}</td>"
                        f"<td class='mono'>{path_val or '-'}</td></tr>"
                    )

                table = f"""
                  <table class="ma-table">
                    <thead><tr><th>#</th><th>ID</th><th>캡션</th><th>파일경로</th></tr></thead>
                    <tbody>{''.join(rows) or "<tr><td colspan='4'>결과 없음</td></tr>"}</tbody>
                  </table>
                </div></div>
                """
                return HttpResponse(html_top + table)
            except Exception as e:
                return HttpResponse(
                    html_top + f"<p style='color:#fca5a5'>오류: {e}</p></div></div>"
                )

    class TableDatasetAdmin(admin.ModelAdmin):
        list_display = ("id", "table_name", "csv", "row_count", "indexed_at")
        search_fields = ("table_name", "csv")

    # 기본 admin + rag_admin_site 모두 등록
    admin.site.register(MediaAsset, MediaAssetAdmin)
    admin.site.register(TableDataset, TableDatasetAdmin)

    rag_admin_site.register(MediaAsset, MediaAssetAdmin)
    rag_admin_site.register(TableDataset, TableDatasetAdmin)


# ─────────────────────────────
# TableSearchRule (표 검색 규칙 하드코딩 대체)
# ─────────────────────────────
@admin.register(TableSearchRule)
class TableSearchRuleAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "table_name",
        "is_active",
        "min_sim",
        "hard_filter_enabled",
        "updated_at",
    )
    list_filter = ("is_active", "hard_filter_enabled")
    search_fields = ("name", "table_name")
    ordering = ("-updated_at", "-id")

    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (
            "기본 정보",
            {
                "fields": (
                    "name",
                    "table_name",
                    "is_active",
                )
            },
        ),
        (
            "검색 동작 설정",
            {
                "fields": (
                    "min_sim",
                    "hard_filter_enabled",
                )
            },
        ),
        (
            "집계/컬럼 규칙(JSON)",
            {
                "description": (
                    "JSON 형식으로 입력하세요.<br>"
                    '예시 1) agg_hints_json: {"sum": ["합계","총액"], "avg": ["평균"]}<br>'
                    '예시 2) column_synonyms_json: {"region":["지역","지점"],"sales":["매출","금액"]}<br>'
                    '예시 3) numeric_hints_json: ["sales","amount","price"]'
                ),
                "fields": (
                    "agg_hints_json",
                    "column_synonyms_json",
                    "numeric_hints_json",
                ),
            },
        ),
        (
            "시스템 정보",
            {
                "classes": ("collapse",),
                "fields": ("created_at", "updated_at"),
            },
        ),
    )


# ✅ rag_admin_site 에도 노출 (관리자 콘솔에서 바로 접근)
rag_admin_site.register(TableSearchRule, TableSearchRuleAdmin)
