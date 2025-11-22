from __future__ import annotations 
from django.db import models
from django.utils import timezone
from django.conf import settings
from datetime import date


# -----------------------------------------------------------------------------
# 기본 설정 헬퍼
# -----------------------------------------------------------------------------
def _retention_days(name: str, fallback: int = 0) -> int:
    """
    settings에서 보존기간(일)을 읽는다.
    예) RETENTION_DAYS_CHATLOG / RETENTION_DAYS_FEEDBACK / RETENTION_DAYS_CONSENT
    없으면 RETENTION_DAYS, 그래도 없으면 fallback
    """
    return int(
        getattr(settings, name, None)
        or getattr(settings, "RETENTION_DAYS", fallback)
        or 0
    )


def _compute_delete_at(created_at, days: int):
    if not created_at or not days or days <= 0:
        return None
    return created_at + timezone.timedelta(days=days)


# -----------------------------------------------------------------------------
# 기존 설정/로그/FAQ 등 (필드/동작 유지)
# -----------------------------------------------------------------------------
class RagSetting(models.Model):
    """
    RAG / 인덱싱 파이프라인 전체 설정.
    관리자는 /ragadmin/ 에서 이 값을 보고 수정할 수 있음.
    한 레코드만 써도 되고, 여러 버전 쌓아도 됨.
    """

    # Chroma 위치
    chroma_db_dir = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="로컬 Chroma DB 폴더 경로",
    )
    chroma_collection = models.CharField(
        max_length=128,
        blank=True,
        default="web_rag",
        help_text="Chroma 컬렉션 이름 (기존 컬렉션에 계속 append)",
    )

    # 동작 플래그들
    auto_ingest_after_gemini = models.BooleanField(
        default=True,
        help_text="웹 검색(Gemini) 직후 자동으로 인덱싱까지 수행할지 여부",
    )
    web_ingest_to_chroma = models.BooleanField(
        default=True,
        help_text="검색 결과를 Chroma에 저장(인덱싱)할지 여부",
    )
    crawl_answer_links = models.BooleanField(
        default=True,
        help_text="답변 본문 속 URL까지 따라가서 본문 크롤링/인덱싱할지",
    )

    # RAG / 뉴스 관련 파라미터
    rag_query_topk = models.IntegerField(
        default=5,
        help_text="RAG 1차 retrieval top-k",
    )
    rag_fallback_topk = models.IntegerField(
        default=12,
        help_text="RAG 2차(확장) retrieval top-k",
    )
    rag_max_sources = models.IntegerField(
        default=8,
        help_text="최종 답변에 노출할 근거 source 개수 상한",
    )
    news_topk = models.IntegerField(
        default=5,
        help_text="구글 뉴스 RSS에서 긁어올 기사 수",
    )

    # 레코드 갱신 시각
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="이 레코드를 마지막으로 저장한 시각",
    )

    def __str__(self):
        return f"RagSetting#{self.pk} ({self.chroma_collection})"


class MyLog(models.Model):
    """
    관리 액션(예: /ragadmin/crawl-news/ 에서 크롤링 실행 등) 기록.
    단순하게 텍스트/JSON 남겨서 무슨 일이 있었는지 확인용.
    """

    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="로그 생성 시각",
    )

    mode_text = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="무슨 작업인지. 예: 'admin_crawl', 'api_ingest' 등",
    )

    query = models.TextField(
        blank=True,
        default="",
        help_text="사용된 키워드/질문 등",
    )

    ok_flag = models.BooleanField(
        default=True,
        help_text="성공 여부",
    )

    remote_addr_text = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="요청자 IP 등",
    )

    extra_json = models.JSONField(
        blank=True,
        default=dict,
        help_text="결과 요약, 에러 메시지, 저장 chunk 수 등 자유롭게",
    )

    def __str__(self):
        ts = timezone.localtime(self.created_at).strftime("%Y-%m-%d %H:%M")
        status = "OK" if self.ok_flag else "FAIL"
        return f"[{status}] {self.mode_text} '{self.query[:30]}' @ {ts}"


# -----------------------------------------------------------------------------
# 동의 증빙(Consent) + 법적 문서 버전
# -----------------------------------------------------------------------------
class LegalDocumentVersion(models.Model):
    """
    정책/약관/개인정보처리방침 등 문서 버전 관리(증빙/고지 목적).
    실제 렌더링은 별도 뷰/템플릿에서 하고, 이 모델은 '어떤 버전이 언제 유효했는지' 기록한다.
    """
    slug = models.SlugField(max_length=64, db_index=True, help_text="예: privacy, terms")
    version = models.CharField(max_length=32, db_index=True, help_text="예: v1, 2025-11-02")
    title = models.CharField(max_length=200)
    content_md = models.TextField(blank=True, default="", help_text="문서 원문(Markdown 등)")
    published_at = models.DateTimeField(default=timezone.now, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        unique_together = [("slug", "version")]
        ordering = ["-published_at", "-id"]

    def __str__(self):
        return f"{self.slug}@{self.version}"


class ConsentLog(models.Model):
    """
    사용자 동의(필수/선택)의 세션 단위 증빙 로그.
    - 원시 IP 대신 해시/익명화 표현(ip_hash) 저장 (원시 IP 미보관)
    - 문서 버전/범위/부가정보/아티팩트 해시 보관
    - 보존기간 경과 시 자동 삭제를 위한 delete_at 제공
    """
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    session_key = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Django 세션 키(증빙용)",
    )
    ip_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="요청자 IP의 해시/익명화 표현(원시 IP 미저장)",
    )
    user_agent = models.CharField(
        max_length=400,
        blank=True,
        default="",
        help_text="User-Agent (최대 400자)",
    )

    consent_type = models.CharField(
        max_length=32,
        default="required",
        help_text="required / optional / marketing 등 구분",
        db_index=True,
    )
    # 문서 버전 기록
    document = models.ForeignKey(
        LegalDocumentVersion,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="consent_logs",
        help_text="동의 당시 사용된 정책/약관 버전(선택)",
    )
    version = models.CharField(
        max_length=32,
        default="v1",
        help_text="프런트에서 전달한 버전 문자열(문서 연결이 안 될 때 사용)",
        db_index=True,
    )
    scope = models.CharField(
        max_length=32,
        default="session",
        help_text="세션/계정/기간 등 범위 표기용",
        db_index=True,
    )

    artifact_hash = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="동의 스냅샷/아티팩트 해시 (선택)",
    )
    extra = models.JSONField(
        blank=True,
        null=True,
        help_text="프론트에서 전달한 부가 데이터(FormData/JSON)",
    )

    # 보존 정책
    legal_hold = models.BooleanField(
        default=False,
        db_index=True,
        help_text="법적 보존 필요 시 True (자동 파기 제외)",
    )
    delete_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="이 시각 이후 정기 파기 대상(RETENTION_DAYS_CONSENT)",
    )

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        ts = timezone.localtime(self.created_at).strftime("%Y-%m-%d %H:%M")
        return f"[{self.consent_type}/{self.version}] {self.session_key[:8]}… @ {ts}"

    def save(self, *args, **kwargs):
        if not self.delete_at and not self.legal_hold:
            days = _retention_days("RETENTION_DAYS_CONSENT", fallback=0)
            self.delete_at = _compute_delete_at(self.created_at, days)
        super().save(*args, **kwargs)


# -----------------------------------------------------------------------------
# 질의/응답 로그 + 피드백 (법 준수 필드 확장: 익명 IP/법적근거/보존/법적보존예외)
#   + 공용 대화 로그 필드(session_id/channel/role/message_type/content) 추가
# -----------------------------------------------------------------------------
class ChatQueryLog(models.Model):
    """
    사용자가 챗봇(FAQ/RAG/Gemini/실시간상담 등)에 남긴 로그.
    - 기존: 질의 1건 + 답변 요약 중심(question/answer_excerpt)
    - 확장: 세션 기반 대화 로그(session_id, channel, role, message_type, content)
      → QARAG 위젯 / 실시간 상담 콘솔 / 외부 API가 같은 테이블을 공용으로 사용.
    """

    MODE_CHOICES = [
        ("faq", "FAQ (qa_data.py)"),
        ("rag", "RAG 검색"),
        ("gemini", "Gemini / 웹 검색"),
        ("blocked", "차단/정책 위반"),
    ]

    LEGAL_BASIS_CHOICES = [
        ("consent", "동의(Consent)"),
        ("contract", "계약 이행(Contract)"),
        ("legitimate_interest", "정당한 이익(Legitimate Interest)"),
        ("legal_obligation", "법적 의무(Legal Obligation)"),
        ("other", "기타"),
    ]

    CHANNEL_CHOICES = [
        ("qarag", "QARAG 위젯"),
        ("live_console", "실시간 상담 콘솔"),
        ("api", "외부 API/연동"),
        ("system", "시스템/배치"),
    ]

    ROLE_CHOICES = [
        ("user", "사용자"),
        ("assistant", "봇/상담원"),
        ("system", "시스템"),
    ]

    MESSAGE_TYPE_CHOICES = [
        ("query", "질문"),
        ("answer", "답변"),
        ("note", "노트/코멘트"),
        ("error", "에러"),
    ]

    # 언제 찍힌 로그인지
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    # ▶ 공용 대화 필드 (QARAG / 실시간 상담 콘솔 / API에서 함께 사용)
    session_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="동일 브라우저/상담 세션을 구분하는 ID (QARAG/실시간 콘솔 공용)",
    )

    channel = models.CharField(
        max_length=32,
        choices=CHANNEL_CHOICES,
        blank=True,
        default="qarag",
        db_index=True,
        help_text="메시지가 생성된 채널(위젯/실시간콘솔/API 등)",
    )

    mode = models.CharField(
        max_length=20,
        choices=MODE_CHOICES,
        help_text="어떤 엔진 또는 단계가 최종 응답했는지",
        db_index=True,
    )

    role = models.CharField(
        max_length=16,
        choices=ROLE_CHOICES,
        blank=True,
        default="user",
        db_index=True,
        help_text="메시지 역할(user/assistant/system)",
    )

    message_type = models.CharField(
        max_length=16,
        choices=MESSAGE_TYPE_CHOICES,
        blank=True,
        default="query",
        db_index=True,
        help_text="메시지 유형(query/answer/note/error 등)",
    )

    # ▶ 기존 질의/응답 필드 (질문/답변 단위 로그에 계속 사용 가능)
    question = models.TextField(
        help_text="사용자가 실제로 입력한 질문 원문"
    )

    content = models.TextField(
        blank=True,
        default="",
        help_text=(
            "단일 메시지의 원문(질문/답변/노트 등). "
            "QARAG/실시간 상담 콘솔의 공용 대화 로그용 필드로 사용. "
            "기존 question/answer_excerpt와 병행 가능."
        ),
    )

    answer_excerpt = models.TextField(
        blank=True,
        help_text="사용자에게 돌려준 답변의 앞부분 (요약/미리보기)",
    )

    # ✅ 익명/해시 IP (원시 IP 미보관)
    client_ip = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="요청자 IP의 해시/익명화 표현(원시 IP 미저장)",
    )

    # 호출 상태
    is_error = models.BooleanField(
        default=False,
        help_text="이 호출이 실패했는지 여부 (예외 등)",
    )

    error_msg = models.TextField(
        blank=True,
        help_text="에러가 났다면 스택 대신 요약 메시지",
    )

    # 👍👎 사용자 피드백
    was_helpful = models.BooleanField(
        null=True,
        blank=True,
        default=None,
        help_text="유저가 이 답변을 도움이 됐다고 했는지 (True/False/아직없음)",
    )

    feedback = models.TextField(
        blank=True,
        default="",
        help_text="유저가 남긴 자유 코멘트 (선택)",
    )

    # RAG/웹 검색 참고 소스 및 메타데이터
    sources = models.JSONField(
        blank=True,
        default=list,
        help_text="참고 소스 목록 [{title,url},...]",
    )
    meta = models.JSONField(
        blank=True,
        default=dict,
        help_text="요청/클라이언트 메타 (UA, path, 세션정보 등)",
    )

    # ▶ 법 준수: 처리 목적/법적 근거/동의 버전/동의 로그 연결
    legal_basis = models.CharField(
        max_length=32,
        choices=LEGAL_BASIS_CHOICES,
        default="consent",
        db_index=True,
        help_text="데이터 처리의 법적 근거",
    )
    consent_version = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="동의 문구/문서 버전 문자열(선택)",
    )
    consent_log = models.ForeignKey(
        ConsentLog,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="chat_logs",
        help_text="실제 동의 증빙 레코드(선택)",
    )

    # ▶ 보존/삭제/예외
    legal_hold = models.BooleanField(
        default=False,
        db_index=True,
        help_text="법적 보존 필요 시 True (자동 파기 제외)",
    )
    delete_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="보존기간 경과 시점(RETENTION_DAYS_CHATLOG)",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["session_id", "created_at"]),
            models.Index(fields=["channel", "created_at"]),
            models.Index(fields=["mode", "created_at"]),
        ]

    def __str__(self) -> str:  # 선택: 어드민에서 보기 편하게
        return f"[{self.created_at:%Y-%m-%d %H:%M:%S}] {self.channel}/{self.role} - {self.question[:40]}"


    def short_q(self):
        txt = self.question or ""
        return (txt[:50] + ("..." if len(txt) > 50 else ""))
    short_q.short_description = "질문 미리보기"

    def short_a(self):
        txt = self.answer_excerpt or ""
        return (txt[:50] + ("..." if len(txt) > 50 else ""))
    short_a.short_description = "답변 미리보기"

    def save(self, *args, **kwargs):
        if not self.delete_at and not self.legal_hold:
            days = _retention_days("RETENTION_DAYS_CHATLOG", fallback=0)
            self.delete_at = _compute_delete_at(self.created_at, days)
        super().save(*args, **kwargs)


class FaqEntry(models.Model):
    question = models.TextField(
        "질문",
        help_text="사용자가 물어볼 수 있는 형태 그대로 적어주세요.",
    )
    answer = models.TextField(
        "답변",
        help_text="우리가 공식적으로 안내할 답변",
    )
    is_active = models.BooleanField(
        "활성화 여부",
        default=True,
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "FAQ 항목"
        verbose_name_plural = "FAQ 항목들"
        ordering = ["-updated_at", "-created_at"]

    def __str__(self):
        short_q = (self.question or "").strip().replace("\n", " ")
        if len(short_q) > 50:
            short_q = short_q[:50] + "…"
        return short_q


class Feedback(models.Model):
    ANSWER_TYPE_CHOICES = [
        ("gemini", "Gemini / Web 요약"),
        ("rag", "RAG 답변"),
        ("other", "기타 / 기타 응답"),
    ]

    LEGAL_BASIS_CHOICES = ChatQueryLog.LEGAL_BASIS_CHOICES

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    question = models.TextField(blank=True, default="")
    answer = models.TextField(blank=True, default="")

    answer_type = models.CharField(
        max_length=20,
        choices=ANSWER_TYPE_CHOICES,
        default="other",
        db_index=True,
    )

    is_helpful = models.BooleanField(default=True, db_index=True)

    # /api/feedback에서 sources_json=... 으로 저장됨
    sources_json = models.JSONField(blank=True, null=True)

    # ✅ 익명/해시 IP (원시 IP 미보관)
    client_ip = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="요청자 IP의 해시/익명화 표현(원시 IP 미저장)",
    )

    # ▶ 법 준수: 처리 목적/법적 근거/동의 버전/동의 로그 연결
    legal_basis = models.CharField(
        max_length=32,
        choices=LEGAL_BASIS_CHOICES,
        default="consent",
        db_index=True,
        help_text="데이터 처리의 법적 근거",
    )
    consent_version = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="동의 문구/문서 버전 문자열(선택)",
    )
    consent_log = models.ForeignKey(
        ConsentLog,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="feedbacks",
        help_text="실제 동의 증빙 레코드(선택)",
    )

    # ▶ 보존/삭제/예외
    legal_hold = models.BooleanField(
        default=False,
        db_index=True,
        help_text="법적 보존 필요 시 True (자동 파기 제외)",
    )
    delete_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="보존기간 경과 시점(RETENTION_DAYS_FEEDBACK)",
    )

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        base_q = (self.question or "").strip().replace("\n", " ")
        if len(base_q) > 50:
            base_q = base_q[:50] + "..."
        thumb = "👍" if self.is_helpful else "👎"
        return f"[{self.answer_type}/{thumb}] {base_q}"

    def save(self, *args, **kwargs):
        if not self.delete_at and not self.legal_hold:
            days = _retention_days("RETENTION_DAYS_FEEDBACK", fallback=0)
            self.delete_at = _compute_delete_at(self.created_at, days)
        super().save(*args, **kwargs)


class IngestHistory(models.Model):
    """
    /api/ingest_news 를 한 번 돌릴 때마다 누적되는 로그.
    어떤 키워드로 몇 건 수집했고 몇 건 DB에 넣었는지 요약 저장.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    keyword = models.CharField(max_length=200)

    total_candidates = models.IntegerField(default=0)   # RSS 등에서 후보로 뽑은 URL 수
    ingested_count = models.IntegerField(default=0)     # 실제로 새로 벡터DB에 추가된 문서 수
    skipped_count = models.IntegerField(default=0)      # 중복 등으로 스킵된 수
    failed_count = models.IntegerField(default=0)       # 크롤 실패 등

    # 상세 결과 (각 URL별 status 같은 거)
    detail = models.JSONField(blank=True, null=True)

    def __str__(self):
        return (
            f"[{self.created_at:%Y-%m-%d %H:%M}] "
            f"{self.keyword} {self.ingested_count}/{self.total_candidates} ingested"
        )


class FaqSuggestProxy(FaqEntry):
    """
    어드민 메뉴에 'FAQ 후보 추천'이라는 항목을 걸기 위한 가짜(프록시) 모델.
    실제 DB 테이블은 FaqEntry랑 같음. (proxy=True)
    changelist 화면에서 우리가 만든 /ragadmin/faq-suggest/ 페이지로 보내줄 거임.
    """
    class Meta:
        proxy = True
        verbose_name = "FAQ 후보 추천"
        verbose_name_plural = "FAQ 후보 추천"
        app_label = "ragapp"


class CrawlNewsProxy(RagSetting):
    """
    어드민 메뉴에 '뉴스 크롤 & 인덱싱' 메뉴를 띄우기 위한 프록시 모델.
    RagSetting 테이블을 재사용하지만, 여기서는 단지 메뉴 역할만.
    changelist에서 /ragadmin/crawl-news/ 로 보낼 거임.
    """
    class Meta:
        proxy = True
        verbose_name = "뉴스 크롤 & 인덱싱"
        verbose_name_plural = "뉴스 크롤 & 인덱싱"
        app_label = "ragapp"


# -----------------------------------------------------------------------------
# 권리행사(삭제/열람/정정 등) 요청 티켓 + 감사 이벤트
# -----------------------------------------------------------------------------
class DataErasureTicket(models.Model):
    """
    데이터 주체의 권리행사(주로 삭제) 요청 티켓.
    - target_ip_hash: 익명 IP 표현 기반으로 매칭/삭제 (원시 IP 미보관 정책과 일관)
    - requester_token: 요청자 확인 토큰(해시) (이메일/웹폼/코드 등)
    """
    STATUS_CHOICES = [
        ("open", "접수"),
        ("processing", "처리중"),
        ("done", "완료"),
        ("rejected", "거절"),
    ]
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    channel = models.CharField(
        max_length=32,
        default="web",
        help_text="web/form/email 등",
        db_index=True,
    )
    requester_token = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="요청자 확인 토큰(해시/토큰 일부 등, 원문 미보관 권장)",
    )
    target_ip_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="삭제 대상 식별 키(익명/해시 IP 표현)",
    )
    scope = models.CharField(
        max_length=64,
        default="chatlog,feedback,consent",
        help_text="삭제 범위 힌트(comma: chatlog,feedback,consent)",
    )
    reason = models.TextField(
        blank=True,
        default="",
        help_text="요청 사유/코멘트",
    )
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default="open",
        db_index=True,
    )
    processed_by = models.CharField(
        max_length=64,
        blank=True,
        default="system",
        help_text="처리자(계정명/시스템)",
    )
    result_json = models.JSONField(
        blank=True,
        null=True,
        help_text="처리 결과 요약(삭제된 건수, 테이블별 상세 등)",
    )

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"[{self.status}] DSR for {self.target_ip_hash[:8]}…"


class AuditEvent(models.Model):
    """
    주요 행위(동의 저장, 권리행사 처리, 보존/삭제 작업 등)에 대한 감사 이벤트 로그.
    """
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.CharField(
        max_length=64,
        default="system",
        help_text="actor: system/admin/user 등",
        db_index=True,
    )
    action = models.CharField(
        max_length=64,
        help_text="예: consent.recorded, dsr.processed, purge.run",
        db_index=True,
    )
    target_model = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="타깃 모델명",
        db_index=True,
    )
    target_pk = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="타깃 PK(문자열 보관)",
        db_index=True,
    )
    notes = models.TextField(
        blank=True,
        default="",
        help_text="요약/설명",
    )
    extra = models.JSONField(
        blank=True,
        null=True,
        help_text="부가 JSON",
    )

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.action} by {self.actor} @ {self.created_at:%Y-%m-%d %H:%M}"


# -----------------------------------------------------------------------------
# 법적 설정 (단일 클래스만 유지) + HTML sanitize 유틸
# -----------------------------------------------------------------------------
def sanitize_legal_html(value: str) -> str:
    """
    (선택) bleach가 설치돼 있으면 필터링, 없으면 원본 사용.
    pip 미설치 환경에서도 절대 에러 안나게 설계.
    """
    if not value:
        return ""
    try:
        import bleach  # pip install bleach (선택)
        allowed_tags = [
            "a","b","strong","i","em","u","br","p","ul","ol","li",
            "h2","h3","h4","h5","h6","code","pre","blockquote","span","div"
        ]
        allowed_attrs = {
            "a": ["href","title","target","rel"],
            "span": ["data-bind"],
            "div": ["data-bind"]
        }
        return bleach.clean(value, tags=allowed_tags, attributes=allowed_attrs, strip=True)
    except Exception:
        return value  # bleach 없거나 오류면 원본 반환


class LegalConfig(models.Model):
    """어드민에서 수정 가능한 법적 고지/동의 설정 (단일 레코드 사용 권장)"""
    # 기본/표기
    service_name = models.CharField(max_length=120, blank=True, default="")
    effective_date = models.DateField(blank=True, null=True)

    # 운영자/연락처
    operator_name = models.CharField(max_length=120, blank=True, default="")
    contact_email = models.EmailField(blank=True, default="")
    contact_phone = models.CharField(max_length=50, blank=True, default="")

    # ✅ 첫 방문 동의 오버레이 토글 (admin 동기화)
    consent_gate_enabled = models.BooleanField(default=True)

    # 각 탭에 들어갈 HTML (선택)
    guide_html = models.TextField(blank=True, default="")         # 이용안내
    privacy_html = models.TextField(blank=True, default="")       # 개인정보
    cross_border_html = models.TextField(blank=True, default="")  # 국외이전
    tester_html = models.TextField(blank=True, default="")        # 테스터 안내

    # 메타( admin.readonly_fields / list_display 와 정합성 )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.service_name or f"LegalConfig#{self.pk}"


    class Meta:
        verbose_name = "법적 설정"
        verbose_name_plural = "법적 설정"

    def __str__(self):
        return f"[법적설정] {self.service_name} ({self.effective_date})"

    # 템플릿에서 바로 쓸 수 있는 sanitize 프로퍼티
    @property
    def sanitized_privacy_html(self) -> str:
        return sanitize_legal_html(self.privacy_html)

    @property
    def sanitized_cross_border_html(self) -> str:
        return sanitize_legal_html(self.cross_border_html)

    @property
    def sanitized_tester_html(self) -> str:
        return sanitize_legal_html(self.tester_html)

    @classmethod
    def get_solo(cls) -> "LegalConfig":
        """
        단일 레코드 사용을 권장하므로, 없으면 자동 생성해서 반환.
        """
        obj = cls.objects.first()
        if obj:
            return obj
        return cls.objects.create()

class RagChunk(models.Model):
    """
    모든 청크와 임베딩을 SQLite에 저장.
    embedding: np.float32 배열을 bytes로 직렬화하여 BinaryField로 보관
    """
    id = models.BigAutoField(primary_key=True)
    unique_hash = models.CharField(max_length=64, unique=True, db_index=True)
    doc_id = models.CharField(max_length=191, blank=True, db_index=True)
    url = models.URLField(blank=True)
    title = models.CharField(max_length=500, blank=True)
    text = models.TextField()
    meta = models.JSONField(default=dict, blank=True)

    embedding = models.BinaryField()          # np.float32 bytes
    dim = models.PositiveSmallIntegerField()  # 임베딩 차원

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["doc_id"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.title or '(no title)'} - {self.url or ''}"


class MediaAsset(models.Model):
    """
    Admin에서 이미지 파일을 업로드해 Chroma(media_images)에 인덱싱하기 위한 간단 모델
    원본 파일은 MEDIA_ROOT에 저장, Chroma에는 임베딩+경로/메타만 저장
    """
    file = models.FileField(upload_to="media_assets/%Y/%m/")
    caption = models.CharField(max_length=255, blank=True)
    mime = models.CharField(max_length=100, blank=True)
    size = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    chroma_id = models.CharField(max_length=200, blank=True)
    indexed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"[{self.id}] {self.file.name}"

class TableDataset(models.Model):
    """
    Admin에서 CSV를 업로드해 Chroma(table_rows)에 행 단위 인덱싱하기 위한 모델
    """
    table_name = models.CharField(max_length=128)
    csv = models.FileField(upload_to="table_datasets/%Y/%m/")
    row_count = models.IntegerField(default=0)
    indexed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.table_name} ({self.csv.name})"
    

class LiveChatRoom(models.Model):
    STATUS_CHOICES = [
        ("waiting", "대기"),
        ("active", "진행 중"),
        ("closed", "종료"),
    ]

    room_id = models.CharField(max_length=64, unique=True)
    client_label = models.CharField(max_length=100, blank=True)     # 예: '웹 QARAG 사용자'
    last_question = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="waiting")
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="livechat_rooms",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"{self.room_id} ({self.get_status_display()})"
    
class LiveChatSession(models.Model):
    """
    QARAG → 상담사 실시간 상담 세션 1건에 해당하는 기록
    - 상담 요청 시 1행 생성
    - 상담 중/종료 상태 변경
    """

    STATUS_WAITING = "waiting"
    STATUS_CONNECTED = "connected"
    STATUS_CLOSED = "closed"

    STATUS_CHOICES = [
        (STATUS_WAITING, "대기"),
        (STATUS_CONNECTED, "상담 중"),
        (STATUS_CLOSED, "종료"),
    ]

    # 어떤 콘솔/방에서 보는지 (기본 master)
    room = models.CharField(
        max_length=64,
        default="master",
        db_index=True,
        help_text="어드민 콘솔에서 보는 방 이름 (예: master / room-1 등)",
    )

    # 어디서 온 요청인지 (질문 챗봇, 웹폼 등)
    source = models.CharField(
        max_length=32,
        default="qarag",
        help_text="요청 출발지 (예: QARAG, web, etc.)",
    )

    # 현재 상태
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_WAITING,
        db_index=True,
    )

    # (선택) 누가 요청했는지 표시하고 싶을 때 사용
    user_name = models.CharField(max_length=80, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)

    # 타임라인
    started_at = models.DateTimeField(
        default=timezone.now,
        help_text="QARAG에서 상담 요청한 시각",
    )
    connected_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="상담사가 실제로 연결된 시각",
    )
    ended_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="상담 종료 시각",
    )
    last_message_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="마지막 메시지가 오간 시각",
    )

    # 기타 메타데이터 (테스트 플래그, 참고용 정보 등)
    meta = models.JSONField(default=dict, blank=True)

    # 생성 시각 (정렬용)
    created_at = models.DateTimeField(auto_now_add=True)

    session_type = models.CharField(max_length=50, blank=True, default="")
    session_note = models.CharField(max_length=200, blank=True, default="")
    memo = models.TextField("상담 상세 메모", blank=True, default="")  # 👈 추가(또는 확인)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "실시간 상담 세션"
        verbose_name_plural = "실시간 상담 세션"

    def __str__(self) -> str:
        return f"[{self.get_status_display()}] {self.room} / {self.pk}"
    
class TableSchema(models.Model):
    """
    업로드한 표(CSV/엑셀)의 구조를 저장해 두는 모델.

    - table_name   : /table/index 에서 지정한 이름 (예: coffee_sales)
    - columns      : 컬럼 이름 목록 (["date","region","product","channel","sales"] 등)
    - column_types : 각 컬럼의 타입(number/text/date)
    - sample_rows  : 첫 몇 줄을 그대로 저장 (LLM한테 보여줄 설명용)
    """
    table_name = models.CharField(max_length=128, unique=True)
    columns = models.JSONField(default=list)
    column_types = models.JSONField(default=dict, blank=True)
    sample_rows = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "표 스키마"
        verbose_name_plural = "표 스키마"

    def __str__(self) -> str:  # admin 리스트에서 보기 좋게
        return self.table_name
    

class TableSearchRule(models.Model):
    """
    표 검색 규칙(집계 키워드, 컬럼 별칭 등)을
    운영자가 어드민에서 수정할 수 있게 하는 설정 모델.

    - table_name 이 비어 있으면 '전역(global) 규칙'으로 취급
    - 나중에 필요하면 특정 테이블 전용 규칙도 만들 수 있음
    """
    name = models.CharField(
        max_length=100,
        help_text="설정 이름 (예: 기본 규칙, 실험용 등)",
    )
    table_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="비우면 전체 공통 규칙, 특정 표만 대상으로 할 때 테이블 이름 기입",
    )

    # JSONField를 쓰는게 가장 편함 (Django 3.1+ / PostgreSQL / SQLite 모두 지원)
    agg_hints_json = models.JSONField(
        default=dict,
        blank=True,
        help_text='집계 키워드 힌트. 예: {"sum": ["합계","총액"], "avg": ["평균"]}',
    )
    column_synonyms_json = models.JSONField(
        default=dict,
        blank=True,
        help_text='컬럼 별칭. 예: {"region": ["지역","지점"], "sales": ["매출","금액"]}',
    )
    numeric_hints_json = models.JSONField(
        default=list,
        blank=True,
        help_text='숫자 컬럼 힌트. 예: ["sales","amount","price"]',
    )

    min_sim = models.FloatField(
        default=0.35,
        help_text="임베딩 기반 검색에서 이 값 이상이면 '비슷하다'고 인정 (0~1 사이 권장)",
    )
    hard_filter_enabled = models.BooleanField(
        default=True,
        help_text="질문 안의 단어로 한 번 더 하드 필터를 돌릴지 여부",
    )

    is_active = models.BooleanField(
        default=True,
        help_text="체크된 규칙만 실제 검색에서 사용",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "표 검색 규칙"
        verbose_name_plural = "표 검색 규칙"
        ordering = ["-updated_at", "-id"]

    def __str__(self) -> str:  # type: ignore[override]
        target = self.table_name or "전체(전역)"
        return f"{self.name} / {target}"