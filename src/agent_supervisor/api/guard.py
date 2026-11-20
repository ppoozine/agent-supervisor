"""Accept only requests from the local machine.

The API has no login. Binding only to `127.0.0.1` blocks other machines but not other web
pages in the user's browser: a POST without a body (Stop, archive) can be sent cross-origin
without a preflight, and DNS rebinding can point another domain at the local machine. So every
request is checked on two headers:

- `Host` must be a local name, which blocks DNS rebinding.
- If present, `Origin` must be a local web page (any port, since in development the frontend
  runs on another port).
"""

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from agent_supervisor.api.errors import error_response

LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})
FORBIDDEN = 403


def is_local_host(host: str | None) -> bool:
    """Whether the `Host` header (possibly with a port) is a local name."""
    if not host:
        return False
    return urlsplit(f"//{host}").hostname in LOCAL_HOSTNAMES


def is_local_origin(origin: str) -> bool:
    """Whether the `Origin` header is a web page on the local machine."""
    parts = urlsplit(origin)
    return parts.scheme in {"http", "https"} and parts.hostname in LOCAL_HOSTNAMES


class LocalRequestsOnly:
    """ASGI middleware: reject requests not addressed to, or not sent from a page on, localhost."""

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
