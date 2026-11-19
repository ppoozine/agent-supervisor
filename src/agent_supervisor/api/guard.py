"""只接受來自本機的請求。

API 沒有登入。只綁 `127.0.0.1` 擋得住別台機器，擋不住使用者瀏覽器裡的其他網頁：
沒有 body 的 POST（Stop、封存）不需要預檢就能跨來源送出，DNS rebinding 還能讓
別的網域指到本機。所以每個請求都檢查兩個標頭：

- `Host` 必須是本機的名稱，擋掉 DNS rebinding。
- 有 `Origin` 的話必須是本機的網頁（連接埠不限，開發時前端在另一個連接埠）。
"""

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from agent_supervisor.api.errors import error_response

LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})
FORBIDDEN = 403


def is_local_host(host: str | None) -> bool:
    """`Host` 標頭（可能帶連接埠）是不是本機的名稱。"""
    if not host:
        return False
    return urlsplit(f"//{host}").hostname in LOCAL_HOSTNAMES


def is_local_origin(origin: str) -> bool:
    """`Origin` 標頭是不是本機上的網頁。"""
    parts = urlsplit(origin)
    return parts.scheme in {"http", "https"} and parts.hostname in LOCAL_HOSTNAMES


class LocalRequestsOnly:
    """ASGI middleware：拒絕不是對本機、或不是從本機網頁發出的請求。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if not is_local_host(headers.get("host")):
            problem = "requests must be addressed to localhost"
        elif origin is not None and not is_local_origin(origin):
            problem = f"requests from {origin} are not allowed"
        else:
            await self._app(scope, receive, send)
            return
        await error_response(FORBIDDEN, "forbidden", problem)(scope, receive, send)
