"""Serve the web pages from this checkout, with live reload, against a running quadlink.

usage: python dev/serve.py [--upstream https://quadlink.et42.net] [--host 127.0.0.1] [--port 8090]

Pages come from src/quadlink/web/ as saved; /streams/* and /api/* go to the upstream
quadlink, so the page plays the real relay. A saved file reloads every open page.

/api/* is sent as the upstream's own page would send it (its page token, no
cross-site headers): anyone who can reach this server can use the upstream's API.
"""

import argparse
import asyncio
import re
from pathlib import Path

import aiohttp
from aiohttp import web

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "quadlink" / "web"
PROXIED = ("/streams/", "/api/")
# hop-by-hop and length headers: aiohttp sets its own for the re-sent body
DROPPED = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"}
# headers a browser adds that the upstream reads as a cross-site request
CROSS_SITE = {"origin", "referer", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest"}
TOKEN_RE = re.compile(r'<meta name="quadlink-token" content="([^"]+)"')
RELOAD = """<script>
new EventSource('/__reload?page=NAME').onmessage = () => location.reload();
</script>
"""


def _mtimes(page: str) -> dict[Path, float]:
    """The page's own file and the stylesheets every page links: an edit to one page
    must not reload another, where it would throw away its state."""
    files = [WEB_DIR / f"{page}.html", *WEB_DIR.glob("*.css")]
    return {p: p.stat().st_mtime for p in files if p.is_file()}


async def _page(request: web.Request) -> web.Response:
    name = request.match_info["name"] or "quad"
    path = (WEB_DIR / f"{name}.html").resolve()
    if path.parent != WEB_DIR or not path.is_file():
        raise web.HTTPNotFound()
    html = path.read_text().replace("</body>", RELOAD.replace("NAME", name) + "</body>", 1)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def _old_quad(request: web.Request) -> web.Response:
    raise web.HTTPFound("/")


async def _asset(request: web.Request) -> web.FileResponse:
    path = (WEB_DIR / request.match_info["path"]).resolve()
    if WEB_DIR not in path.parents or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def _reload(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    page = request.query.get("page", "")
    if not page.replace("-", "").replace("_", "").isalnum():
        raise web.HTTPBadRequest()
    seen = _mtimes(page)
    while True:
        await asyncio.sleep(0.3)
        now = _mtimes(page)
        if now != seen:
            await resp.write(b"data: reload\n\n")
            return resp


async def _token(app: web.Application, fresh: bool = False) -> str:
    """The upstream page's token; it changes when the upstream restarts."""
    if fresh or "token" not in app:
        session: aiohttp.ClientSession = app["session"]
        async with session.get(app["upstream"] + "/") as resp:
            match = TOKEN_RE.search(await resp.text())
        app["token"] = match.group(1) if match else ""
    return str(app["token"])


async def _proxy(request: web.Request) -> web.StreamResponse:
    session: aiohttp.ClientSession = request.app["session"]
    url = request.app["upstream"] + request.path_qs
    api = request.path.startswith("/api/")
    skip = DROPPED | {"host"} | (CROSS_SITE if api else set())
    headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}
    body = await request.read()
    for attempt in (0, 1):
        if api:
            headers["X-Quadlink-Token"] = await _token(request.app, fresh=attempt > 0)
        upstream = await session.request(
            request.method, url, headers=headers, data=body, allow_redirects=False
        )
        if not (api and upstream.status == 403 and attempt == 0):
            break
        upstream.release()
    async with upstream:
        resp = web.StreamResponse(status=upstream.status)
        for key, value in upstream.headers.items():
            if key.lower() not in DROPPED:
                resp.headers[key] = value
        await resp.prepare(request)
        async for chunk in upstream.content.iter_chunked(1 << 16):
            await resp.write(chunk)
        return resp


@web.middleware
async def _route(request: web.Request, handler):  # type: ignore[no-untyped-def]
    if request.path.startswith(PROXIED):
        return await _proxy(request)
    return await handler(request)


async def _session(app: web.Application):  # type: ignore[no-untyped-def]
    app["session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_read=30))
    yield
    await app["session"].close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upstream", default="https://quadlink.et42.net")
    # 0.0.0.0 shares it with other machines; it has no auth, so keep it to trusted networks
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    app = web.Application(middlewares=[_route])
    app["upstream"] = args.upstream.rstrip("/")
    app.cleanup_ctx.append(_session)
    app.router.add_get("/__reload", _reload)
    app.router.add_get("/web/{path:.+}", _asset)
    app.router.add_get("/quad", _old_quad)
    app.router.add_get("/{name:[a-z0-9_-]*}", _page)
    print(f"http://{args.host}:{args.port}/")
    print(f"pages from {WEB_DIR}, video from {args.upstream}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
