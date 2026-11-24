"""What every server shares: Streamable HTTP at /mcp behind a bearer token, and /healthz.

Environment:
  MCP_AUTH_TOKEN  required. Every request to /mcp must send "Authorization: Bearer <it>".
  PORT            listen port, default 8000.
"""

import hmac
import os
import sys

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

HEALTH_PATH = "/healthz"


class BearerAuth:
    """ASGI wrapper: answers /healthz itself and rejects any other request without the token."""

    def __init__(self, app, token):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan: the MCP session manager starts there
            await self.app(scope, receive, send)
            return
        if scope["path"] == HEALTH_PATH:
            await self._reply(send, 200, b"ok\n")
            return
        given = dict(scope.get("headers") or []).get(b"authorization", b"")
        if not hmac.compare_digest(given, self.expected):
            await self._reply(send, 401, b"unauthorized\n", [(b"www-authenticate", b"Bearer")])
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reply(send, status, body, extra=()):
        headers = [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode()), *extra]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def build_app(server, token):
    """The ASGI app for one MCPServer. Stateless JSON responses: each call stands alone."""
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
        # Host-header checks guard browsers against DNS rebinding. These servers are called
        # by other services inside a cluster, under varying Host values (with and without
        # the port), and every request already needs the token.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return BearerAuth(app, token)


def serve(server):
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    if not token:
        sys.exit("MCP_AUTH_TOKEN is not set: refusing to serve tools without authentication")
    uvicorn.run(build_app(server, token), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)
