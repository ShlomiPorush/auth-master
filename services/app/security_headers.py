"""Response headers that keep the service's pages out of frames on other sites."""
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class AntiFramingMiddleware:
    """Tell browsers not to show a response inside a frame.

    Without these headers, a page on a same-site origin could overlay the
    signed-in dashboard and steer an admin's clicks (clickjacking).  The app
    sends them itself, so the protection does not depend on a reverse proxy.

    The headers are added to every response that passes through the app's
    middleware.  Starlette's own 500 page for an unhandled exception is built
    outside it; that page is fixed plain text with nothing to click.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Frame-Options"] = "DENY"
                headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            await send(message)

        await self.app(scope, receive, send_with_headers)
