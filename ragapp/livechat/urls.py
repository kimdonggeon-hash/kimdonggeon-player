# ragapp/livechat/urls.py
from django.urls import path
from ragapp.livechat import views as livechat_views
from . import views

app_name = "livechat"

urlpatterns = [
    path("console/", views.live_chat_view, name="console"),
    path("api/request/", views.livechat_request_api, name="request_api"),
    path("api/end/", livechat_views.api_livechat_end, name="api_livechat_end"),
    path(
        "api/livechat/session/save/",
        livechat_views.livechat_session_save_view,
        name="api_livechat_session_save",
    ),
]
