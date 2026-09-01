"""Browser callback for gateway-owned remote MCP OAuth."""

from __future__ import annotations

from aiohttp import web

REMOTE_MCP_OAUTH_CALLBACK_PATH = "/api/mcp/oauth/callback"
_OAUTH_FAILURE_CODE = "remote_mcp_oauth_failed"
_CALLBACK_HTML = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorization complete</title>
<body>
<h1>Authorization complete</h1>
<p>You can close this window and return to Kiro Crew.</p>
</body>
</html>
"""
_CALLBACK_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; base-uri 'none'; form-action 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


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
    return web.Response(
        text=_CALLBACK_HTML,
        content_type="text/html",
        headers=_CALLBACK_HEADERS,
    )
