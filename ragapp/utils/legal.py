# ragapp/utils/legal.py
from __future__ import annotations
from typing import Tuple, Optional

from django.core.exceptions import FieldError
from django.db import connection, models

# 모델 임포트 (없어도 동작하게 가드)
try:
    from ragapp.models import LegalConfig, sanitize_legal_html  # type: ignore
except Exception:  # pragma: no cover
    LegalConfig = None  # type: ignore

    def sanitize_legal_html(html: str) -> str:  # type: ignore
        return html


def _has_table(name: str) -> bool:
    try:
        with connection.cursor() as c:
            c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=%s", [name])
            return c.fetchone() is not None
    except Exception:
        return False


# -------------------------------
# 1) Manager 패치: enabled 별칭 제공
# -------------------------------
class _LegalConfigManager(models.Manager):
    """
    모든 쿼리셋에 enabled = consent_gate_enabled 라는 주석을 자동 부여.
    이렇게 하면 기존의 filter(enabled=True) 코드가 그대로 동작함.
    """
    def get_queryset(self):
        qs = super().get_queryset()
        try:
            # annotate로 별칭 생성 (Django 5 호환, filter/order_by에 사용 가능)
            return qs.annotate(enabled=models.F("consent_gate_enabled"))
        except Exception:
            return qs


def _patch_manager_if_possible():
    """
    런타임에서 기본 매니저를 _LegalConfigManager로 교체.
    템플릿 태그/기존 코드가 LegalConfig.objects.filter(enabled=...)를 호출해도 에러 없이 동작.
    """
    if LegalConfig is None or not _has_table("ragapp_legalconfig"):
        return
    try:
        # Django가 model binding을 해주도록 add_to_class 사용
        LegalConfig.add_to_class("objects", _LegalConfigManager())
        # 기본 매니저도 동일 객체가 되도록
        LegalConfig._default_manager = LegalConfig.objects  # type: ignore[attr-defined]
    except Exception:
        # 실패하더라도 치명적이지 않게 무시
        pass


_patch_manager_if_possible()


# -------------------------------
# 2) 활성 LegalConfig 조회 헬퍼
# -------------------------------
def _active_legal_qs():
    """
    LegalConfig를 안전하게 조회하는 헬퍼.
    - 새로운 스키마: consent_gate_enabled=True 우선
    - 구 스키마: enabled=True 필터도 허용 (위 매니저 패치로 가능)
    - 어떤 필터도 통과 못하면 최신(updated_at DESC) 1개로 폴백
    """
    if LegalConfig is None or not _has_table("ragapp_legalconfig"):
        return None

    qs = LegalConfig.objects.order_by("-updated_at")

    # 신필드 우선 시도
    try:
        q = qs.filter(consent_gate_enabled=True)
        if q.exists():
            return q
    except FieldError:
        pass

    # 구필드(호환) 시도 — 매니저가 annotate(enabled=...) 제공
    try:
        q = qs.filter(enabled=True)  # type: ignore[attr-defined]
        if q.exists():
            return q
    except FieldError:
        pass

    return qs


def get_active_legal_config():
    """
    활성화된 LegalConfig 1개 반환(없으면 최신 1개, 없으면 None)
    """
    qs = _active_legal_qs()
    if qs is None:
        return None
    return qs.first()


# -------------------------------
# 3) 서버 측 동의 체크
# -------------------------------
def validate_required_consents(request) -> Tuple[bool, Optional[str]]:
    """
    서버 측 동의 게이트 체크.
    - consent_gate_enabled(또는 구호환 enabled)가 True면 동의 필요
    - 세션/쿠키에 동의 흔적 있으면 통과
    """
    # 세션/쿠키에 이미 동의 흔적이 있으면 OK
    try:
        if request.session.get("consent_ok") in (True, "1", "on"):
            return True, None
        for k in ("consent_ok", "consent_required", "agree_privacy"):
            v = request.COOKIES.get(k)
            if isinstance(v, str) and v.strip().lower() in ("1", "true", "on", "yes", "y"):
                return True, None
    except Exception:
        pass

    cfg = get_active_legal_config()
    if cfg is None:
        # 설정이 없으면 게이트 미적용으로 간주
        return True, None

    # 필드 존재 유무에 따라 안전 체크
    gate_on = False
    if hasattr(cfg, "consent_gate_enabled"):
        gate_on = bool(getattr(cfg, "consent_gate_enabled"))
    elif hasattr(cfg, "enabled"):  # 구버전 호환
        gate_on = bool(getattr(cfg, "enabled"))

    if not gate_on:
        return True, None

    # 게이트가 켜져 있고, 동의 흔적이 없다면 실패 메시지
    return False, "❌ 개인정보 수집·이용(필수)에 동의해 주세요."
