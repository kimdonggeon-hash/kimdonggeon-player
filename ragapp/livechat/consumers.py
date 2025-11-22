# ragapp/livechat/consumers.py
from __future__ import annotations

import logging
from typing import Any, Dict

from channels.generic.websocket import AsyncJsonWebsocketConsumer

log = logging.getLogger(__name__)


class MasterConsumer(AsyncJsonWebsocketConsumer):
    """
    /ws/chat/master 에 연결되는 로비(대기실)용 WebSocket Consumer.

    - QARAG 쪽 livechat_client.js 의 handoffOnce() 가 여기로 type: "handoff" 이벤트를 보냄
    - 운영자 콘솔 livechat_admin.js 도 같은 URL로 접속해서
      새 상담 요청(handoff), 상담 종료(end/closed), session_saved 등을 실시간으로 수신
    """

    group_name = "livechat_lobby"

    async def connect(self) -> None:
        try:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
        except Exception as e:
            log.exception("MasterConsumer group_add error: %s", e)
        await self.accept()
        log.info("MasterConsumer connected: %s", self.channel_name)

    async def disconnect(self, close_code: int) -> None:
        try:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        except Exception as e:
            log.exception("MasterConsumer group_discard error: %s", e)
        log.info("MasterConsumer disconnected: %s (code=%s)", self.channel_name, close_code)

    async def receive_json(self, content: Dict[str, Any], **kwargs: Any) -> None:
        """
        클라이언트(주로 QARAG)에서 온 JSON 메시지를
        그대로 로비 그룹에 브로드캐스트.

        예:
        {
          "type": "handoff",
          "room": "client-xxxx",
          "url": ".../ragadmin/live-chat/?room=client-xxxx",
          "text": "새 상담 요청이 도착했습니다.",
          "ts": 1710000000000,
          "page": { "title": "...", "path": "..." }
        }
        """
        try:
            event = {
                "type": "broadcast_event",  # 그룹 핸들러 이름
                "event": content or {},
            }
            await self.channel_layer.group_send(self.group_name, event)
        except Exception as e:
            log.exception("MasterConsumer.receive_json error: %s", e)

    async def broadcast_event(self, event: Dict[str, Any]) -> None:
        """
        group_send(type='broadcast_event', event={...}) 로 올라온 것을
        그대로 한 클라이언트에게 내려보내는 핸들러.
        """
        payload = event.get("event") or {}
        await self.send_json(payload)

    async def lobby_message(self, event: Dict[str, Any]) -> None:
        """
        뷰 코드에서 예전 스타일로
            group_send("livechat_lobby", {"type": "lobby_message", "event": {...}})
        이런 식으로 보냈을 때도 호환되도록 만든 핸들러.
        """
        payload = event.get("event") or {}
        await self.send_json(payload)


class RoomConsumer(AsyncJsonWebsocketConsumer):
    """
    /ws/chat/<room> 에 연결되는 개별 상담 방 Consumer.

    - QARAG (사용자)와 실시간 상담 콘솔(운영자)이 같은 room 으로 접속
    - 한쪽에서 보내면 group_send 를 통해 반대쪽에 그대로 전달
    """

    room: str
    group_name: str

    async def connect(self) -> None:
        # URL 패턴에서 room 이름 가져오기 (routing.py 에 따라 key 이름이 달 수 있음)
        url_kwargs = self.scope.get("url_route", {}).get("kwargs", {}) or {}
        room = (
            url_kwargs.get("room")
            or url_kwargs.get("room_id")
            or url_kwargs.get("room_name")
        )
        if not room:
            # room 파라미터가 없으면 접속 거부
            await self.close(code=4000)
            return

        self.room = str(room)
        self.group_name = f"livechat_room_{self.room}"

        try:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
        except Exception as e:
            log.exception("RoomConsumer group_add error: %s", e)

        await self.accept()
        log.info("RoomConsumer connected: room=%s ch=%s", self.room, self.channel_name)

    async def disconnect(self, close_code: int) -> None:
        try:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        except Exception as e:
            log.exception("RoomConsumer group_discard error: %s", e)
        log.info(
            "RoomConsumer disconnected: room=%s ch=%s code=%s",
            getattr(self, "room", "?"),
            self.channel_name,
            close_code,
        )

    async def receive_json(self, content: Dict[str, Any], **kwargs: Any) -> None:
        """
        양쪽(사용자/상담사)에서 보내는 JSON 메시지를
        같은 room 그룹에 그대로 중계.

        예)
          사용자: { "sender": "user", "text": "...", "ts": ... }
          상담사: { "sender": "operator", "text": "...", "ts": ... }
          종료:   { "sender": "operator", "type": "end", "text": "...", "ts": ... }
        """
        try:
            msg = dict(content or {})
            msg.setdefault("room", self.room)
            # ts 없으면 대충 서버시간 넣어줌
            msg.setdefault("ts", self._now_ms())

            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "room_message",  # 아래 room_message 핸들러 이름
                    "message": msg,
                },
            )
        except Exception as e:
            log.exception("RoomConsumer.receive_json error: %s", e)

    async def room_message(self, event: Dict[str, Any]) -> None:
        """
        group_send(type='room_message', message={...}) 로 들어온 것을
        그대로 클라이언트에게 내려보내는 핸들러.
        """
        msg = event.get("message") or {}
        await self.send_json(msg)

    @staticmethod
    def _now_ms() -> int:
        import time

        return int(time.time() * 1000)


# 예전 이름으로 임포트하는 코드 호환용
ChatConsumer = RoomConsumer  # from ragapp.livechat.consumers import ChatConsumer
LobbyConsumer = MasterConsumer  # 혹시 이 이름으로도 쓰고 있을 수 있어서
__all__ = ["MasterConsumer", "RoomConsumer", "ChatConsumer", "LobbyConsumer"]
