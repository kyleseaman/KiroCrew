"""Browser callback for gateway-owned remote MCP OAuth."""

from __future__ import annotations

from aiohttp import web

REMOTE_MCP_OAUTH_CALLBACK_PATH = "/api/mcp/oauth/callback"
_OAUTH_FAILURE_CODE = "remote_mcp_oauth_failed"


async def api_remote_mcp_oauth_callback(request: web.Request) -> web.Response:
    """Settle one broker attempt; the OAuth state is the one-shot credential."""
    broker = request.app.get("remote_mcp_oauth_broker")
    if broker is None:
        return web.json_response({"code": _OAUTH_FAILURE_CODE}, status=503)
    query: dict[str, object] = {}
    for key in request.query:
        values = request.query.getall(key)
        query[key] = values[0] if len(values) == 1 else values
    if not broker.complete(query):
        return web.json_response({"code": _OAUTH_FAILURE_CODE}, status=400)
    return web.Response(status=204)
