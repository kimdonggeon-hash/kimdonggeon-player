# ragsite/asgi.py
import os
import django
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from channels.security.websocket import AllowedHostsOriginValidator

import ragapp.livechat.routing as livechat_routing

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ragsite.settings")

django.setup()

django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            URLRouter(livechat_routing.websocket_urlpatterns)
        )
    ),
})