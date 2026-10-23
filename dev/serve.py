"""Serve the web pages from this checkout, with live reload, against a running quadlink.

usage: python dev/serve.py [--upstream https://quadlink.et42.net] [--host 127.0.0.1] [--port 8090]

Pages come from src/quadlink/web/ as saved; /streams/* and /api/* go to the upstream
quadlink, so the page plays the real relay. A saved file reloads every open page.
"""

import argparse
import asyncio
from pathlib import Path

import aiohttp
from aiohttp import web

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "quadlink" / "web"
PROXIED = ("/streams/", "/api/")
# hop-by-hop and length headers: aiohttp sets its own for the re-sent body
DROPPED = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"}
RELOAD = """<script>
new EventSource('/__reload').onmessage = () => location.reload();
</script>
"""


def _mtimes() -> dict[Path, float]:
    return {p: p.stat().st_mtime for p in WEB_DIR.rglob("*") if p.is_file()}


async def _page(request: web.Request) -> web.Response:
    name = request.match_info["name"] or "quad"
    path = (WEB_DIR / f"{name}.html").resolve()
    if path.parent != WEB_DIR or not path.is_file():
        raise web.HTTPNotFound()
    html = path.read_text().replace("</body>", RELOAD + "</body>", 1)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def _asset(request: web.Request) -> web.FileResponse:
    path = (WEB_DIR / request.match_info["path"]).resolve()
    if WEB_DIR not in path.parents or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def _reload(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    seen = _mtimes()
    while True:
        await asyncio.sleep(0.3)
        now = _mtimes()
        if now != seen:
            await resp.write(b"data: reload\n\n")
            return resp


async def _proxy(request: web.Request) -> web.StreamResponse:
    session: aiohttp.ClientSession = request.app["session"]
    url = request.app["upstream"] + request.path_qs
    headers = {k: v for k, v in request.headers.items() if k.lower() not in DROPPED | {"host"}}
    async with session.request(
        request.method, url, headers=headers, data=await request.read(), allow_redirects=False
    ) as upstream:
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
    app.router.add_get("/{name:[a-z0-9_-]*}", _page)
    print(f"http://{args.host}:{args.port}/quad")
    print(f"pages from {WEB_DIR}, video from {args.upstream}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
