"""Web UI for configuration editing."""

import os
import re
import secrets
import stat
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog
from aiohttp import web
from pydantic import ValidationError
from ruamel.yaml import YAML

from quadlink import __version__
from quadlink.api import EVENTS_PATH, STILL_PATH, Dashboard, DashboardAPI
from quadlink.config.loader import ConfigLoader
from quadlink.config.models import Config
from quadlink.relay.server import SlotRelay

logger = structlog.get_logger()

TOKEN_HEADER = "X-QuadLink-Token"
TOKEN_PLACEHOLDER = "__QUADLINK_TOKEN__"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


# urlsplit accepts userinfo, paths and fragments in a netloc; a Host header has none
_HOST_RE = re.compile(r"^(?:\[([0-9A-Fa-f:.]+)\]|([A-Za-z0-9.\-]+))(?::\d+)?$")


def _hostname(netloc: str) -> str | None:
    """Return the lowercase host name of a Host header value, without the port."""
    match = _HOST_RE.match(netloc)
    if match is None:
        return None
    return (match.group(1) or match.group(2)).lower()


def _deny(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _yaml_writer() -> YAML:
    writer = YAML()
    writer.default_flow_style = False
    writer.width = 120
    writer.indent(mapping=2, sequence=4, offset=2)
    return writer


def _write_atomic(path: Path, text: str) -> None:
    """Replace path with text so readers see the old or the new file, never a partial one."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _format_error(e: Exception) -> str:
    """Render exceptions as a short, readable string for UI display."""
    if isinstance(e, ValidationError):
        parts = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err.get("loc", ())) or "config"
            msg = err.get("msg", "invalid").lower()
            parts.append(f"{loc}: {msg}")
        return "; ".join(parts)
    return str(e).lower()


def _strip_empty(obj: Any) -> Any:
    """Recursively drop None and empty list/dict values from a config tree."""
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for k, v in obj.items():
            cleaned = _strip_empty(v)
            if cleaned is None:
                continue
            if isinstance(cleaned, (list, dict)) and not cleaned:
                continue
            result[k] = cleaned
        return result
    if isinstance(obj, list):
        return [_strip_empty(item) for item in obj]
    return obj


class WebUI:
    """Web server for viewing and editing QuadLink configuration.

    The page and the API answer only requests whose Host header names an allowed
    host (against DNS rebinding). The API also requires same-origin JSON requests
    that carry the per-process token embedded in the page (against cross-site
    request forgery). EventSource and <img> cannot send headers: the event stream
    takes the token as a query parameter, and slot stills need no token. The relay
    /streams routes take no such checks.

    Attributes:
        host: Host address to bind to.
        port: Port number to listen on.
        config_loader: ConfigLoader instance for reading config.
        config_path: Path to the config file being edited.
        allowed_hosts: Host names the page and API answer to.
        token: Per-process token the page sends in the X-QuadLink-Token header.
    """

    def __init__(
        self,
        config_loader: ConfigLoader,
        host: str = "0.0.0.0",
        port: int = 8081,
        slot_relay: SlotRelay | None = None,
        allowed_hosts: Iterable[str] = (),
        dashboard: Dashboard | None = None,
    ):
        """Initialize web UI server.

        Args:
            config_loader: ConfigLoader instance for reading config.
            host: Host address to bind to.
            port: Port number to listen on.
            slot_relay: Optional slot relay whose /streams routes are registered.
            allowed_hosts: Host names beyond the loopback names that may reach the page
                and API, such as the name a reverse proxy serves the web UI under.
            dashboard: Optional daemon whose dashboard API routes are registered.
        """
        self.host = host
        self.port = port
        self.config_loader = config_loader
        self.slot_relay = slot_relay
        self.config_path = self._find_config_path()
        self.allowed_hosts = LOOPBACK_HOSTS | {h.strip().lower() for h in allowed_hosts}
        self.token = secrets.token_urlsafe(32)
        self.app = web.Application(middlewares=[self._guard])
        self.api = DashboardAPI(dashboard) if dashboard is not None else None
        self._setup_routes()

    @web.middleware
    async def _guard(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        path = request.path
        is_api = path.startswith("/api/")
        if not (is_api or path == "/"):
            return await handler(request)

        # the page holds the token, so it needs the host check too
        if _hostname(request.host) not in self.allowed_hosts:
            logger.warning("webui request for unknown host refused", host=request.host)
            return _deny(403, "the server does not accept requests for this host name")
        if not is_api:
            return await handler(request)

        fetch_site = request.headers.get("Sec-Fetch-Site")
        origin = request.headers.get("Origin")
        cross_site = fetch_site is not None and fetch_site not in ("same-origin", "none")
        if origin is not None:
            try:
                origin_host = urlsplit(origin).netloc.lower()
            except ValueError:
                origin_host = None
            if origin_host != request.host.lower():
                cross_site = True
        if cross_site:
            logger.warning("webui cross-site request refused", origin=origin, site=fetch_site)
            return _deny(403, "the request does not come from the quadlink page")
        # request.json() ignores the content type; requiring JSON forces a CORS
        # preflight, which fails because the server sends no CORS headers
        if request.method == "POST" and request.content_type != "application/json":
            return _deny(415, "the request body must be JSON (application/json)")
        # <img> cannot send the token; the same-origin check above keeps other sites out
        if STILL_PATH.fullmatch(path):
            return await handler(request)
        sent = request.headers.get(TOKEN_HEADER)
        if sent is None and path == EVENTS_PATH:
            sent = request.query.get("token")
        sent = sent or ""
        if not secrets.compare_digest(sent.encode("utf-8", "surrogateescape"), self.token.encode()):
            return _deny(403, "the page token is missing or incorrect; reload the page")

        return await handler(request)

    def _find_config_path(self) -> Path | None:
        """Find the config file path being used."""
        if self.config_loader._explicit_path:
            return Path(self.config_loader._explicit_path).expanduser().resolve()
        return self.config_loader._find_config_file()

    def _setup_routes(self) -> None:
        """Set up HTTP routes."""
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/api/config", self._handle_get_config)
        self.app.router.add_post("/api/config", self._handle_post_config)
        self.app.router.add_post("/api/validate", self._handle_validate)
        if self.api is not None:
            self.api.register(self.app)
        if self.slot_relay is not None:
            self.slot_relay.register_routes(self.app.router)

    async def _handle_index(self, request: web.Request) -> web.Response:
        """Serve the main config editor page."""
        return web.Response(
            text=HTML_TEMPLATE.replace(TOKEN_PLACEHOLDER, self.token),
            content_type="text/html",
            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
        )

    def _read_file_config(self) -> dict[str, Any]:
        """Return the config file as written, without env overrides or defaults."""
        if self.config_path is None or not self.config_path.exists():
            return {}
        data = YAML(typ="safe").load(self.config_path.read_text())
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError("the config file does not contain a YAML mapping")
        return data

    def _add_yaml_spacing(self, yaml_str: str) -> str:
        """Add blank lines between priority levels and ruleset entries."""
        import re

        # add blank line before each priority level (number followed by colon at indent 2)
        yaml_str = re.sub(r"\n(  \d+:)", r"\n\n\1", yaml_str)
        # add blank line before each ruleset entry (- name: at indent 2)
        yaml_str = re.sub(r"\n(  - name:)", r"\n\n\1", yaml_str)
        # clean up any triple+ newlines
        yaml_str = re.sub(r"\n{3,}", "\n\n", yaml_str)
        return yaml_str

    async def _handle_get_config(self, request: web.Request) -> web.Response:
        """Return current config as formatted YAML."""
        try:
            # the file, not the merged config: a save writes back what the page shows,
            # and env overrides or defaults must not end up in the file
            config_dict = self._read_file_config()
            config_dict.pop("credentials", None)
            config_dict = _strip_empty(config_dict)

            # sort priorities if requested
            sort_order = request.query.get("sort", "desc")
            if "priorities" in config_dict:
                priorities = config_dict["priorities"]
                reverse = sort_order == "desc"
                config_dict["priorities"] = dict(
                    sorted(priorities.items(), key=lambda x: int(x[0]), reverse=reverse)
                )

            stream = StringIO()
            _yaml_writer().dump(config_dict, stream)
            yaml_str = self._add_yaml_spacing(stream.getvalue())

            return web.json_response(
                {"yaml": yaml_str, "path": str(self.config_path), "version": __version__}
            )
        except Exception as e:
            logger.error("failed to load config for webui", error=str(e))
            return web.json_response({"error": str(e)}, status=500)

    def _clean_parsed_yaml(self, obj: Any) -> Any:
        """Convert null values to empty lists/dicts where appropriate."""
        if obj is None:
            return None
        if isinstance(obj, dict):
            result: dict[str, Any] = {}
            for k, v in obj.items():
                if v is None:
                    # fields that should be lists get empty list
                    if k in (
                        "allow_categories",
                        "allow_titles",
                        "block_categories",
                        "block_titles",
                        "urls",
                        "rulesets",
                    ):
                        result[k] = []
                    elif k == "filters":
                        result[k] = {}
                    else:
                        result[k] = v
                elif isinstance(v, (dict, list)):
                    result[k] = self._clean_parsed_yaml(v)
                else:
                    result[k] = v
            return result
        if isinstance(obj, list):
            return [self._clean_parsed_yaml(item) for item in obj]
        return obj

    async def _handle_validate(self, request: web.Request) -> web.Response:
        """Validate config without saving."""
        try:
            data = await request.json()
            yaml_content = data.get("yaml", "")

            yaml = YAML(typ="safe")
            parsed = yaml.load(StringIO(yaml_content))
            parsed = self._clean_parsed_yaml(parsed)

            # validate with pydantic (but don't require credentials for validation)
            if "credentials" not in parsed:
                parsed["credentials"] = {"username": "test", "secret": "test"}

            Config(**parsed)
            return web.json_response({"valid": True})

        except Exception as e:
            return web.json_response({"valid": False, "error": _format_error(e)})

    async def _handle_post_config(self, request: web.Request) -> web.Response:
        """Save the page's config, keeping the file's credentials mapping as written."""
        if not self.config_path:
            return web.json_response({"error": "No config file path found"}, status=400)

        try:
            data = await request.json()
            yaml_content = data.get("yaml", "")

            yaml = YAML(typ="safe")
            parsed = yaml.load(StringIO(yaml_content)) or {}
            if not isinstance(parsed, dict):
                raise ValueError("the config must be a YAML mapping")
            parsed = self._clean_parsed_yaml(parsed)

            # the page never sees credentials, and values resolved from
            # credentials.file or QL_* env must never be written inline
            parsed.pop("credentials", None)
            on_disk = self._read_file_config()
            if "credentials" in on_disk:
                parsed["credentials"] = on_disk["credentials"]

            Config(**parsed)

            # write the page's values, not a model dump: that adds every default and
            # every QL_* env override to the file
            stream = StringIO()
            _yaml_writer().dump(parsed, stream)
            _write_atomic(self.config_path, stream.getvalue())

            logger.info("config saved via webui", path=str(self.config_path))
            return web.json_response({"success": True})

        except Exception as e:
            logger.error("failed to save config", error=str(e))
            return web.json_response({"error": _format_error(e)}, status=400)

    async def start(self) -> web.AppRunner:
        """Start the web server."""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        logger.info(
            "webui started", host=self.host, port=self.port, config_path=str(self.config_path)
        )
        return runner


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="color-scheme" content="light dark">
    <meta name="quadlink-token" content="__QUADLINK_TOKEN__">
    <title>quadlink</title>
    <style>
        body {
            max-width: 780px;
            margin: 2rem auto;
            padding: 0 1rem 5rem;
            font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
            line-height: 1.5;
        }
        h1 { margin-bottom: 0.25rem; }
        h1 #version {
            font-size: 0.55em; font-weight: normal; opacity: 0.55; margin-left: 0.5rem;
            font-family: ui-monospace, "SF Mono", Monaco, monospace;
        }
        .subtitle { opacity: 0.7; margin-top: 0; font-size: 0.9em; }
        #config-path { font-family: ui-monospace, "SF Mono", Monaco, monospace; }
        section { margin: 1.5rem 0; }
        textarea {
            width: 100%;
            min-height: 260px;
            font-family: ui-monospace, "SF Mono", Monaco, monospace;
            font-size: 13px;
            padding: 0.5rem;
            box-sizing: border-box;
            resize: vertical;
        }
        #editor-priorities, #editor-rulesets { min-height: 520px; }
        .sort-control { margin-bottom: 0.5rem; font-size: 0.9em; opacity: 0.8; }
        .actions {
            display: flex;
            gap: 0.5rem;
            align-items: center;
            position: sticky;
            bottom: 0;
            padding: 0.75rem 0;
            background: Canvas;
            border-top: 1px solid;
        }
        .actions output { margin-right: auto; font-size: 0.9em; opacity: 0.85; }
        .actions output.success { color: #2e7d32; }
        .actions output.error { color: #c62828; }
        @media (prefers-color-scheme: dark) {
            .actions output.success { color: #81c784; }
            .actions output.error { color: #ff8a80; }
        }
    </style>
</head>
<body>
    <h1>quadlink <span id="version"></span></h1>
    <p class="subtitle"><span id="config-path">loading...</span></p>

    <section>
        <h2>priorities</h2>
        <div class="sort-control">
            <label>sort:
                <select id="sort-order" onchange="loadConfig()">
                    <option value="desc">high to low</option>
                    <option value="asc">low to high</option>
                </select>
            </label>
        </div>
        <textarea id="editor-priorities" placeholder="loading..."></textarea>
    </section>

    <section>
        <h2>rulesets</h2>
        <textarea id="editor-rulesets" placeholder="loading..."></textarea>
    </section>

    <section>
        <h2>settings</h2>
        <textarea id="editor-settings" placeholder="loading..."></textarea>
    </section>

    <div class="actions">
        <output id="status"></output>
        <button onclick="loadConfig()">reload</button>
        <button onclick="validateConfig()">validate</button>
        <button onclick="saveConfig()">save</button>
    </div>

    <script>
        const status = document.getElementById('status');
        const configPath = document.getElementById('config-path');
        const versionEl = document.getElementById('version');
        let initialized = false;
        const token = document.querySelector('meta[name="quadlink-token"]').content;

        function api(url, options = {}) {
            const headers = {'X-QuadLink-Token': token};
            if (options.body !== undefined) headers['Content-Type'] = 'application/json';
            return fetch(url, {...options, headers, cache: 'no-store'});
        }

        const sections = {
            priorities: ['priorities'],
            rulesets: ['rulesets'],
            settings: ['diversity_bonus', 'stability_bonus', 'category_continuity_bonus',
                       'webhook', 'proxy_playlist', 'low_latency', 'lookup_timeout',
                       'relay_segments', 'log_level']
        };

        function showStatus(message, isError = false) {
            status.textContent = isError ? '✗ ' + message : message;
            status.className = isError ? 'error' : 'success';
            if (!isError) {
                setTimeout(() => {
                    if (status.textContent === message) status.textContent = '';
                }, 3000);
            }
        }

        function splitYamlSections(yaml) {
            const lines = yaml.split('\\n');
            const result = { priorities: [], rulesets: [], settings: [] };
            let currentSection = null;

            for (const line of lines) {
                const keyMatch = line.match(/^(\\w+):/);
                if (keyMatch) {
                    const key = keyMatch[1];
                    if (sections.priorities.includes(key)) {
                        currentSection = 'priorities';
                        continue;
                    } else if (sections.rulesets.includes(key)) {
                        currentSection = 'rulesets';
                        continue;
                    } else {
                        currentSection = 'settings';
                    }
                }
                if (currentSection) {
                    const nested = currentSection === 'priorities' || currentSection === 'rulesets';
                    if (nested && line.startsWith('  ')) {
                        result[currentSection].push(line.slice(2));
                    } else {
                        result[currentSection].push(line);
                    }
                }
            }

            return {
                priorities: result.priorities.join('\\n').trim(),
                rulesets: result.rulesets.join('\\n').trim(),
                settings: result.settings.join('\\n').trim()
            };
        }

        function mergeYamlSections() {
            const priorities = document.getElementById('editor-priorities').value.trim();
            const rulesets = document.getElementById('editor-rulesets').value.trim();
            const settings = document.getElementById('editor-settings').value.trim();

            const parts = [];

            if (priorities) {
                const indented = priorities.split('\\n').map(l => '  ' + l).join('\\n');
                parts.push('priorities:\\n' + indented);
            }
            if (rulesets) {
                const indented = rulesets.split('\\n').map(l => '  ' + l).join('\\n');
                parts.push('rulesets:\\n' + indented);
            }
            if (settings) {
                parts.push(settings);
            }

            return parts.join('\\n\\n');
        }

        async function loadConfig() {
            try {
                const sortOrder = document.getElementById('sort-order').value;
                const res = await api(`/api/config?t=${Date.now()}&sort=${sortOrder}`);
                const data = await res.json();
                if (data.error) throw new Error(data.error);

                configPath.textContent = data.path || 'unknown';
                if (data.version) versionEl.textContent = 'v' + data.version;

                const split = splitYamlSections(data.yaml);
                document.getElementById('editor-priorities').value = split.priorities;
                document.getElementById('editor-rulesets').value = split.rulesets;
                document.getElementById('editor-settings').value = split.settings;

                if (initialized) showStatus('✓ reloaded');
                initialized = true;
            } catch (e) {
                showStatus('load failed: ' + e.message, true);
            }
        }

        async function validateConfig() {
            try {
                const yaml = mergeYamlSections();
                const res = await api('/api/validate', {
                    method: 'POST',
                    body: JSON.stringify({yaml})
                });
                const data = await res.json();
                if (data.valid) {
                    showStatus('✓ valid');
                } else {
                    showStatus(data.error, true);
                }
            } catch (e) {
                showStatus(e.message, true);
            }
        }

        async function saveConfig() {
            try {
                const yaml = mergeYamlSections();
                const res = await api('/api/config', {
                    method: 'POST',
                    body: JSON.stringify({yaml})
                });
                const data = await res.json();
                if (data.error) throw new Error(data.error);
                showStatus('✓ saved');
            } catch (e) {
                showStatus('save failed: ' + e.message, true);
            }
        }

        loadConfig();
    </script>
</body>
</html>
"""
