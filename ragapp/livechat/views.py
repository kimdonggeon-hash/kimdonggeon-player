# ragapp/livechat/views.py
from __future__ import annotations

import json
import logging

from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.utils import timezone

from ragapp.models import LiveChatSession

log = logging.getLogger(__name__)

# ─────────────────────────────────────
#  Channels / 로비 브로드캐스트 헬퍼
# ─────────────────────────────────────
try:  # channels 가 없거나 import 문제여도 전체가 죽지 않게 가드
    from channels.layers import get_channel_layer  # type: ignore
    from asgiref.sync import async_to_sync  # type: ignore
    from ragapp.livechat.consumers import LOBBY_GROUP  # type: ignore
except Exception:  # pragma: no cover
    get_channel_layer = None  # type: ignore
    async_to_sync = lambda x: x  # type: ignore
    LOBBY_GROUP = "livechat_lobby"


def _broadcast_session_saved(sess: LiveChatSession) -> None:
    """
    상담 기록이 저장되었을 때 로비(/ws/chat/master)에 알리는 헬퍼.

    - livechat_admin.js 에서는 이 이벤트를 받아서
      '최근 상담 세션' 블록을 새로고침하는 트리거로 사용할 수 있음.
    """
    if not get_channel_layer:
        return

    try:
        layer = get_channel_layer()
    except Exception:
        layer = None

    if not layer:
        return

    try:
        room = getattr(sess, "room", None) or getattr(sess, "code", None) or str(sess.pk)
        note = (
            getattr(sess, "session_note", None)
            or getattr(sess, "memo", None)
            or getattr(sess, "note", None)
        )

        payload = {
            # JS 쪽에서 분기하기 쉬운 타입
            "type": "session_saved",
            "session_id": sess.pk,
            "room": room,
            "status": getattr(sess, "status", None),
            "session_type": getattr(sess, "session_type", None),
            "session_note": note,
            "created_at": (
                sess.created_at.isoformat()
                if getattr(sess, "created_at", None)
                else None
            ),
            "ended_at": (
                sess.ended_at.isoformat()
                if getattr(sess, "ended_at", None)
                else None
            ),
        }

        async_to_sync(layer.group_send)(
            LOBBY_GROUP,
            {
                # LobbyConsumer.session_saved() 로 전달됨
                "type": "session.saved",
                "payload": payload,
            },
        )
    except Exception:  # pragma: no cover
        log.exception("livechat: session_saved broadcast 실패 (무시)")


# ─────────────────────────────────────
#  상담사 콘솔 화면 (어드민용)
# ─────────────────────────────────────
@staff_member_required
def live_chat_view(request: HttpRequest):
    """
    /ragadmin/live-chat/ 에 매핑되는 상담사 콘솔 화면.

    - ?room=XXXX 로 들어오면 initial_room 으로 넘겨서 바로 해당 방 접속 가능
    - 최근 상담 세션 리스트(sessions)를 템플릿으로 전달
    """
    room = request.GET.get("room") or "master"

    # 최근 상담 세션 최대 30개
    try:
        field_names = {
            f.name for f in LiveChatSession._meta.get_fields()
            if hasattr(f, "attname")
        }
        if "created_at" in field_names:
            qs = LiveChatSession.objects.order_by("-created_at")
        elif "requested_at" in field_names:
            qs = LiveChatSession.objects.order_by("-requested_at")
        else:
            qs = LiveChatSession.objects.order_by("-id")
        sessions = list(qs[:30])
    except Exception:
        sessions = []

    ctx = {
        "room": room,
        "initial_room": room,   # live_chat.html 의 data-initial-room 에서 사용
        "sessions": sessions,   # 최근 상담 세션 블록에서 사용
    }
    return render(request, "ragadmin/live_chat.html", ctx)


# ─────────────────────────────────────
#  내부 헬퍼: 세션 생성
# ─────────────────────────────────────
def _create_livechat_session(request: HttpRequest) -> JsonResponse:
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = {}

    # 기본 값들
    room = payload.get("room") or "master"
    source = payload.get("from") or payload.get("source") or "qarag"

    # LiveChatSession 필드에 맞춰서 있는 것만 채우기
    field_names = {
        f.name for f in LiveChatSession._meta.get_fields()
        if hasattr(f, "attname")
    }

    create_kwargs: dict = {}
    if "room" in field_names:
        create_kwargs["room"] = room
    if "source" in field_names:
        create_kwargs["source"] = source
    if "from_source" in field_names:
        create_kwargs["from_source"] = source
    if "status" in field_names:
        # choices 있으면 "waiting"이 유효한 값이어야 함
        create_kwargs.setdefault("status", "waiting")
    if "is_active" in field_names:
        create_kwargs.setdefault("is_active", True)

    now = timezone.now()
    if "requested_at" in field_names:
        create_kwargs["requested_at"] = now
    if "created_at" in field_names and "requested_at" not in field_names:
        create_kwargs["created_at"] = now
    if "created_by" in field_names and request.user.is_authenticated:
        create_kwargs["created_by"] = request.user

    # 🔑 같은 room에 '미종료' 세션이 이미 있으면 재사용
    reuse_session = None
    try:
        qs = LiveChatSession.objects.all()

        if "room" in field_names:
            qs = qs.filter(room=room)

        if "status" in field_names:
            # ended / closed / done / 완료 / 종료 등은 제외
            qs = qs.exclude(status__in=["ended", "closed", "done", "종료", "완료"])

        # is_active 플래그가 있다면 False 인 것은 제외
        if "is_active" in field_names:
            qs = qs.filter(is_active=True)

        order_fields: list[str] = []
        if "created_at" in field_names:
            order_fields.append("-created_at")
        if "requested_at" in field_names:
            order_fields.append("-requested_at")
        if not order_fields:
            order_fields.append("-pk")

        reuse_session = qs.order_by(*order_fields).first()
    except Exception:
        reuse_session = None

    if reuse_session:
        session = reuse_session
    else:
        # 🔑 새 세션 생성
        session = LiveChatSession.objects.create(**create_kwargs)

    # 대기 코드 뽑기 (code / ticket_code / queue_code / short_id / pk 순)
    code = getattr(session, "code", None)
    if not code:
        for cand in ("ticket_code", "queue_code", "short_id"):
            if hasattr(session, cand):
                code = getattr(session, cand)
                break
    if not code:
        code = str(session.pk)

    room_value = getattr(session, "room", room)

    return JsonResponse(
        {
            "ok": True,
            "session_id": session.pk,
            "code": code,
            "room": room_value,
            "greeting": "안녕하세요 김동건의 포트폴리오 입니다. 무엇을 도와드릴까요?",
        }
    )


# ─────────────────────────────────────
#  내부 헬퍼: 세션 종료 + 문의유형/메모 저장 (공통)
#   - QARAG 쪽: session_id 기반 종료 (사용자 종료 = 'soft close')
#   - 어드민 콘솔: room 기반 종료 (상담사 종료 = 'hard close')
# ─────────────────────────────────────
def _end_livechat_session(request: HttpRequest) -> JsonResponse:
    """상담 종료 공통 헬퍼

    - 질문 챗봇(일반 사용자)에서 호출된 경우:
        → 상담 기록(문의 유형/메모)만 저장하고, status / ended_at / is_active 는 건드리지 않는다.
          (UI 상으로만 종료, 상담사는 계속 기록 가능)
    - 상담사 콘솔(스태프)에서 호출된 경우:
        → 실제로 세션을 종료 상태로 변경한다.
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = {}

    # QARAG / 프론트에서 보내는 세션 ID
    session_id = payload.get("session_id") or payload.get("id")
    # 어드민 콘솔 등에서 쓰는 room
    room = (payload.get("room") or "").strip()

    # 호출 주체 구분
    is_staff = bool(getattr(request.user, "is_staff", False))

    # 🔹 문의 유형 / 메모 값 추출 (프론트 payload 키 여러 가지 대응)
    raw_type = (
        payload.get("session_type")
        or payload.get("sessionType")
        or payload.get("type")
    )
    raw_note = (
        payload.get("session_note")
        or payload.get("sessionNote")
        or payload.get("note")
    )
    raw_memo = payload.get("memo")

    # 모델 필드들
    field_names = {
        f.name for f in LiveChatSession._meta.get_fields()
        if hasattr(f, "attname")
    }

    session = None

    # 1) session_id 기준 우선 시도
    if session_id:
        try:
            session = LiveChatSession.objects.get(pk=session_id)
        except LiveChatSession.DoesNotExist:
            # session_id로 못 찾았고, room 도 없으면 바로 에러
            if not room:
                return JsonResponse(
                    {"ok": False, "error": "해당 세션을 찾을 수 없습니다."},
                    status=404,
                )

    # 2) room 기준 (상담사 콘솔처럼 room만 보내는 경우)
    if session is None and room:
        try:
            qs = LiveChatSession.objects.all()
            if "room" in field_names:
                qs = qs.filter(room=room)
            # 가장 최신 세션 하나
            session = qs.order_by("-id").first()
        except Exception:
            session = None

        if not session:
            return JsonResponse(
                {"ok": False, "error": "해당 room의 세션을 찾을 수 없습니다."},
                status=404,
            )

    # session_id도 room도 없는 경우
    if session is None:
        return JsonResponse(
            {"ok": False, "error": "session_id 또는 room 이 필요합니다."},
            status=400,
        )

    # ── 문의 유형/메모 저장 ──────────────────────────────────────────
    if raw_type and "session_type" in field_names:
        session.session_type = str(raw_type)

    if raw_note:
        if "session_note" in field_names:
            session.session_note = str(raw_note)
        elif "memo" in field_names:
            session.memo = str(raw_note)
        elif "note" in field_names:
            session.note = str(raw_note)

    if raw_memo is not None:
        # admin 콘솔에서 memo를 별도로 보내는 경우 우선 반영
        if "memo" in field_names:
            session.memo = str(raw_memo)
        elif "session_note" in field_names and not raw_note:
            # 메모만 있고 session_note가 비어있으면 session_note에라도 저장
            session.session_note = str(raw_memo)

    # ── 상태/종료 시각/활성 여부 처리 ─────────────────────────────────
    if "status" in field_names:
        # 스태프(상담사) → 실제 종료 처리
        if is_staff:
            wanted_status = payload.get("status") or "ended"
            try:
                session.status = wanted_status
            except Exception:
                # choices 등으로 인해 직접 대입이 실패하면 조용히 무시
                log.exception(
                    "livechat end: status set 실패 (wanted=%r)", wanted_status
                )
        else:
            # 일반 사용자 쪽 종료 요청:
            #  - 이미 종료된 세션이면 그대로 둔다.
            #  - 아직 진행 중이면 'user_ended' 같은 중간 상태로만 표시 (있을 때만)
            try:
                cur = (getattr(session, "status", "") or "").strip().lower()
            except Exception:
                cur = ""
            final_statuses = {"ended", "closed", "done", "종료", "완료"}
            if cur not in final_statuses:
                # 중간 상태 표기를 위한 필드만 사용 (없으면 건너뜀)
                try:
                    session.status = payload.get("status") or "user_ended"
                except Exception:
                    # choices 때문에 안 되면 그냥 그대로 둠
                    pass

    # 상담사 쪽에서 호출한 경우에만 실제 종료 시각/활성 플래그 변경
    now = timezone.now()
    if is_staff:
        if "ended_at" in field_names:
            session.ended_at = now
        if "is_active" in field_names:
            session.is_active = False
    else:
        # 사용자 종료 요청이라면 별도 필드가 있을 때만 기록 (선택적)
        # 예: user_ended_at / client_ended_at 등
        user_end_fields = [
            "user_ended_at",
            "client_ended_at",
            "user_closed_at",
            "user_end_at",
        ]
        for fn in user_end_fields:
            if fn in field_names:
                try:
                    setattr(session, fn, now)
                except Exception:
                    pass
                break

    try:
        session.save()
    except Exception as e:
        log.exception("livechat end 처리 중 오류")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

    return JsonResponse(
        {
            "ok": True,
            "session_id": session.pk,
            "status": getattr(session, "status", None),
        }
    )


# ─────────────────────────────────────
#  API: 상담 요청 (두 이름 모두 지원)
#   - /api/livechat/request/ 에 매핑 추천
# ─────────────────────────────────────
@require_POST
def livechat_request_api(request: HttpRequest):
    """urls.py 에서 views.livechat_request_api 로 연결해도 동작."""
    return _create_livechat_session(request)


@require_POST
def api_livechat_request(request: HttpRequest):
    """views.api_livechat_request 로 연결해도 동작 (이름만 다른 alias)."""
    return _create_livechat_session(request)


# ─────────────────────────────────────
#  API: 상담 종료 (두 이름 모두 지원)
#   - /api/livechat/end/ 에 매핑 추천
#   - livechat_end_api: CSRF 체크 O + staff 전용 (상담사 콘솔)
#   - api_livechat_end: CSRF 체크 X (질문 챗봇/AJAX 등에서 사용)
# ─────────────────────────────────────
@staff_member_required
@require_POST
def livechat_end_api(request: HttpRequest):
    return _end_livechat_session(request)


@csrf_exempt
@require_POST
def api_livechat_end(request: HttpRequest):
    """
    CSRF 없이도 호출 가능한 상담 종료 엔드포인트

    - 질문 챗봇(일반 사용자) / 공개 웹에서 사용할 때:
        → 세션의 status 를 '완전 종료'로 바꾸지 않고, 상담 메모만 남기거나
          별도의 user_ended_* 필드만 기록한다.
    - 상담사 콘솔에서는 가급적 livechat_end_api (staff+CSRF) 사용을 권장.
    """
    return _end_livechat_session(request)


@require_GET
def livechat_availability_api(request: HttpRequest) -> JsonResponse:
    """
    현재 상담 가능한 운영자가 있는지 간단히 알려주는 API.
    - operator_count > 0 이면 available = True
    """
    try:
        from ragapp.livechat.consumers import get_operator_count
    except Exception:
        # 컨슈머 임포트 실패하면 일단 "없음"으로 처리
        return JsonResponse({"ok": True, "available": False, "operator_count": 0})

    cnt = int(get_operator_count() or 0)
    return JsonResponse(
        {
            "ok": True,
            "available": cnt > 0,
            "operator_count": cnt,
        }
    )


# ─────────────────────────────────────
#  (추가) 개별 세션 메타만 저장하는 API
#     /livechat/api/livechat/session/save/
# ─────────────────────────────────────
@require_POST
@staff_member_required
@csrf_protect
def livechat_session_save_view(request: HttpRequest) -> JsonResponse:
    """
    어드민 콘솔에서 선택한 상담 세션의
    - 문의 유형
    - 세션 메모
    - 상세 상담 기록
    등을 Ajax로 저장하는 엔드포인트.
    (상담이 '종료' 상태여도 메모는 계속 수정 가능)
    """
    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    session_id = data.get("session_id")
    if not session_id:
        return JsonResponse({"ok": False, "error": "session_id_required"}, status=400)

    try:
        sess = LiveChatSession.objects.get(id=session_id)
    except LiveChatSession.DoesNotExist:
        return JsonResponse({"ok": False, "error": "session_not_found"}, status=404)

    update_fields: list[str] = []

    if "inquiry_type" in data:
        sess.inquiry_type = data["inquiry_type"]
        update_fields.append("inquiry_type")

    if "session_memo" in data:
        sess.session_memo = data["session_memo"]
        update_fields.append("session_memo")

    if "detail_text" in data:
        sess.detail_text = data["detail_text"]
        update_fields.append("detail_text")

    if "status" in data:
        sess.status = data["status"]
        update_fields.append("status")

    if update_fields:
        sess.save(update_fields=update_fields)
    else:
        sess.save()

    # (원하면 여기서도 _broadcast_session_saved(sess) 호출 가능)
    return JsonResponse(
        {
            "ok": True,
            "msg": "상담 메모를 저장했습니다.",
            "session_id": sess.id,
        }
    )


# ─────────────────────────────────────
#  실시간 상담 콘솔 우측 하단 "상담 기록 저장" 버튼 → 이 뷰 호출
#  (무조건 LiveChatSession 하나 생성/업데이트 + 로비에 session_saved 브로드캐스트)
# ─────────────────────────────────────
@require_POST
@staff_member_required
@csrf_protect
def live_chat_save_session_view(request: HttpRequest) -> JsonResponse:
    """
    실시간 상담 콘솔 우측 하단의 "상담 기록 저장" 버튼에서 호출되는 API.

    - room_id(또는 room/code) 기준으로 최신 LiveChatSession을 찾거나 새로 만든다.
    - session_type / session_note / session_detail 을 필드에 매핑해서 저장한다.
    - status 필드가 있으면 '종료'로 세팅하고, ended_at 필드가 있으면 현재 시각으로 기록.
    - 세션을 찾지 못해도 "무조건" 하나 생성해서 저장한다.
    """
    try:
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

        room = (payload.get("room_id") or payload.get("room") or payload.get("code") or "").strip()
        session_type = (payload.get("session_type") or "").strip()
        session_note = (payload.get("session_note") or "").strip()
        session_detail = (payload.get("session_detail") or "").strip()
        session_id = payload.get("session_id")

        if not room and not session_id:
            # 🔸 예전에는 에러를 냈지만, 지금 설계는 "무조건 저장"이므로
            #     room 이 없어도 새 세션을 만들 수 있게 해둠.
            room = ""

        field_names = {
            f.name for f in LiveChatSession._meta.get_fields() if hasattr(f, "attname")
        }

        qs = LiveChatSession.objects.all()

        # 1순위: 세션 id로 찾기
        if session_id:
            qs = qs.filter(id=session_id)
        # 2순위: room / code 로 찾기
        elif room:
            if "room" in field_names:
                qs = qs.filter(room=room)
            elif "code" in field_names:
                qs = qs.filter(code=room)

        obj = qs.order_by("-created_at", "-id").first() if qs.exists() else None

        # 못 찾으면 새로 생성 (== 무조건 LiveChatSession 하나는 생김)
        if not obj:
            obj = LiveChatSession()
            if room:
                if "room" in field_names:
                    obj.room = room
                elif "code" in field_names:
                    obj.code = room

        # 필드가 있는 것만 안전하게 세팅
        if "session_type" in field_names and session_type:
            obj.session_type = session_type

        if "session_note" in field_names:
            obj.session_note = session_note

        # 상세 기록은 memo 또는 note 로 저장 (환경에 따라 택1)
        if session_detail:
            if "memo" in field_names:
                obj.memo = session_detail
            elif "note" in field_names:
                obj.note = session_detail

        # 상태/종료 시각도 같이 기록
        now = timezone.now()
        if "status" in field_names and not getattr(obj, "status", None):
            # 이미 다른 값이 있으면 그대로 두고, 없을 때만 '종료' 기본값
            obj.status = "종료"
        if "ended_at" in field_names and not getattr(obj, "ended_at", None):
            obj.ended_at = now
        if "updated_at" in field_names:
            obj.updated_at = now
        if "created_at" in field_names and not getattr(obj, "created_at", None):
            obj.created_at = now

        obj.save()

        # 🔔 여기서 로비(WebSocket)에 "session_saved" 이벤트 브로드캐스트
        try:
            _broadcast_session_saved(obj)
        except Exception:  # pragma: no cover
            # 브로드캐스트 실패는 기능 필수는 아니므로 조용히 로그만 남김
            log.exception("live_chat_save_session_view: broadcast 실패 (무시)")

        return JsonResponse(
            {
                "ok": True,
                "session_id": obj.id,
                "room": getattr(obj, "room", None) or getattr(obj, "code", None),
                "session_type": getattr(obj, "session_type", ""),
                "session_note": getattr(obj, "session_note", ""),
            }
        )
    except Exception as e:  # pragma: no cover
        log.exception("live_chat_save_session_view error")
        return JsonResponse(
            {"ok": False, "error": "server_error", "detail": str(e)},
            status=500,
        )
