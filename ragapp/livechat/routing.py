# ragapp/livechat/routing.py
from django.urls import re_path
from .consumers import MasterConsumer, RoomConsumer

websocket_urlpatterns = [
    # 1) 운영자 로비 채널
    re_path(r"^ws/chat/master/?$", MasterConsumer.as_asgi()),
    # 선택: /ws/chat/lobby 도 같은 동작 하게 하고 싶으면
    re_path(r"^ws/chat/lobby/?$", MasterConsumer.as_asgi()),

    # 2) 개별 방 (사용자 ↔ 운영자)
    re_path(r"^ws/chat/(?P<room>[^/]+)/?$", RoomConsumer.as_asgi()),
]
