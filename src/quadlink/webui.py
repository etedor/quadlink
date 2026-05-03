"""Web UI for configuration editing."""

from pathlib import Path
from typing import Any

import structlog
from aiohttp import web
from pydantic import ValidationError
from ruamel.yaml import YAML

from quadlink import __version__
from quadlink.config.loader import ConfigLoader
from quadlink.config.models import Config

logger = structlog.get_logger()


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

    Attributes:
        host: Host address to bind to.
        port: Port number to listen on.
        config_loader: ConfigLoader instance for reading config.
        config_path: Path to the config file being edited.
    """

    def __init__(
        self,
        config_loader: ConfigLoader,
        host: str = "0.0.0.0",
        port: int = 8081,
    ):
        """Initialize web UI server.

        Args:
            config_loader: ConfigLoader instance for reading config.
            host: Host address to bind to.
            port: Port number to listen on.
        """
        self.host = host
        self.port = port
        self.config_loader = config_loader
        self.config_path = self._find_config_path()
        self.app = web.Application()
        self._setup_routes()

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

    async def _handle_index(self, request: web.Request) -> web.Response:
        """Serve the main config editor page."""
        return web.Response(text=HTML_TEMPLATE, content_type="text/html")

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
            config = await self.config_loader.load_or_cache()
            # convert to dict, excluding credentials for security
            config_dict = config.model_dump(exclude={"credentials"})
            config_dict = _strip_empty(config_dict)

            # sort priorities if requested
            sort_order = request.query.get("sort", "desc")
            if "priorities" in config_dict:
                priorities = config_dict["priorities"]
                reverse = sort_order == "desc"
                config_dict["priorities"] = dict(
                    sorted(priorities.items(), key=lambda x: int(x[0]), reverse=reverse)
                )

            # format as pretty YAML
            from io import StringIO

            yaml_writer = YAML()
            yaml_writer.default_flow_style = False
            yaml_writer.width = 120
            yaml_writer.indent(mapping=2, sequence=4, offset=2)

            stream = StringIO()
            yaml_writer.dump(config_dict, stream)
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

            from io import StringIO

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
        """Save updated config (YAML content only, preserves credentials)."""
        if not self.config_path:
            return web.json_response({"error": "No config file path found"}, status=400)

        try:
            data = await request.json()
            yaml_content = data.get("yaml", "")

            # parse and validate
            from io import StringIO

            yaml = YAML(typ="safe")
            parsed = yaml.load(StringIO(yaml_content))
            parsed = self._clean_parsed_yaml(parsed)

            # merge with existing credentials
            existing_config = await self.config_loader.load_or_cache()
            parsed["credentials"] = existing_config.credentials.model_dump()

            # validate full config
            config = Config(**parsed)

            # write with pretty formatting, using model_dump for plain types
            yaml_writer = YAML()
            yaml_writer.default_flow_style = False
            yaml_writer.width = 120
            yaml_writer.indent(mapping=2, sequence=4, offset=2)

            with open(self.config_path, "w") as f:
                yaml_writer.dump(config.model_dump(), f)

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
        h1 #version { font-size: 0.55em; font-weight: normal; opacity: 0.55; margin-left: 0.5rem; font-family: ui-monospace, "SF Mono", Monaco, monospace; }
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

        const sections = {
            priorities: ['priorities'],
            rulesets: ['rulesets'],
            settings: ['diversity_bonus', 'stability_bonus', 'category_continuity_bonus',
                       'skip_hosted', 'hosted_offset', 'webhook', 'logging',
                       'proxy_playlist', 'low_latency']
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
                    if ((currentSection === 'priorities' || currentSection === 'rulesets') && line.startsWith('  ')) {
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
                const res = await fetch(`/api/config?t=${Date.now()}&sort=${sortOrder}`, {
                    cache: 'no-store'
                });
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
                const res = await fetch('/api/validate', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
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
                const res = await fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
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
