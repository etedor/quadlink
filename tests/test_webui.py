"""Tests for the config web UI: request guards and config saving."""

import os
import re
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from ruamel.yaml import YAML

from quadlink.config.loader import ConfigLoader
from quadlink.relay.server import SlotRelay
from quadlink.relay.store import SlotStore
from quadlink.webui import TOKEN_HEADER, TOKEN_PLACEHOLDER, WebUI

PAGE_YAML = "priorities:\n  100:\n    - urls: [alice]\n\nmode: local\n"


async def _never(url: str) -> str:  # pragma: no cover - guard
    raise AssertionError("fetch should not be called")


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "secret"
    path.write_text("someuser:hunter2\n")
    return path


@pytest.fixture
def config_file(tmp_path: Path, secret_file: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"credentials:\n  file: {secret_file}\n"
        "priorities:\n  50:\n    - urls: [bob]\n"
        "webhook:\n  enabled: true\n  url: https://hooks.example/abc\n"
    )
    path.chmod(0o640)
    return path


@pytest.fixture
def webui(config_file: Path) -> WebUI:
    relay = SlotRelay(SlotStore(), fetch=_never)
    return WebUI(
        ConfigLoader(explicit_path=str(config_file)),
        slot_relay=relay,
        allowed_hosts=["quadlink.example.net"],
    )


@pytest.fixture
async def client(webui: WebUI) -> AsyncIterator[TestClient]:
    async with TestClient(TestServer(webui.app)) as c:
        yield c


def _auth(webui: WebUI, **extra: str) -> dict[str, str]:
    return {TOKEN_HEADER: webui.token, **extra}


def _load(path: Path) -> dict:
    return YAML(typ="safe").load(path.read_text())


class TestPage:
    async def test_dashboard_is_the_landing_page(self, client: TestClient, webui: WebUI) -> None:
        resp = await client.get("/")
        assert resp.status == 200
        text = await resp.text()
        assert text.count("<video") == 4
        assert "'/streams/ll/' : '/streams/'" in text
        assert "play(video, streams + (i + 1), tile)" in text
        assert TOKEN_PLACEHOLDER not in text
        assert webui.token in text

    async def test_old_quad_path_redirects_home(self, client: TestClient) -> None:
        resp = await client.get("/quad", allow_redirects=False)
        assert resp.status == 302
        assert resp.headers["Location"] == "/"

    async def test_pages_link_each_other(self, client: TestClient) -> None:
        dashboard = await (await client.get("/")).text()
        editor = await (await client.get("/config")).text()
        assert 'href="/config" aria-label="Settings"' in dashboard
        assert 'href="/" aria-label="Quad"' in editor
        assert 'href="/quad"' not in dashboard + editor

    @pytest.mark.parametrize("path", ["/", "/config"])
    async def test_page_sets_theme_color_per_scheme(self, client: TestClient, path: str) -> None:
        html = await (await client.get(path)).text()
        for scheme in ("dark", "light"):
            assert f'media="(prefers-color-scheme: {scheme})"' in html

    async def test_page_embeds_token(self, client: TestClient, webui: WebUI) -> None:
        resp = await client.get("/config")
        assert resp.status == 200
        html = await resp.text()
        assert f'<meta name="quadlink-token" content="{webui.token}">' in html
        assert TOKEN_PLACEHOLDER not in html
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "no-referrer"

    async def test_every_fetch_sends_token_and_json(self, client: TestClient) -> None:
        html = await (await client.get("/config")).text()
        # all API calls go through api(), the one place that calls fetch
        assert html.count("fetch(") == 1
        assert len(re.findall(r"await api\(", html)) == 3
        api_fn = html[html.index("function api(") : html.index("function showStatus")]
        assert "'X-Quadlink-Token': token" in api_fn
        assert "'Content-Type'] = 'application/json'" in api_fn

    @pytest.mark.parametrize("path", ["/", "/config"])
    async def test_page_links_shared_files(self, client: TestClient, path: str) -> None:
        html = await (await client.get(path)).text()
        assert '<link rel="stylesheet" href="/web/base.css">' in html
        assert '<script src="/web/menu.js"></script>' in html

    async def test_web_serves_css(self, client: TestClient) -> None:
        resp = await client.get("/web/base.css")
        assert resp.status == 200
        assert resp.content_type == "text/css"
        assert resp.headers["Cache-Control"] == "no-store"
        assert "--ground:" in await resp.text()

    async def test_web_serves_js(self, client: TestClient) -> None:
        resp = await client.get("/web/menu.js")
        assert resp.status == 200
        assert resp.content_type == "text/javascript"
        assert "function menu(" in await resp.text()

    @pytest.mark.parametrize(
        "path",
        [
            "/web/quad.html",
            "/web/config.html",
            "/web/missing.css",
            "/web/..%2Fwebui.py",
            "/web/%2E%2E%2F%2E%2E%2Fx.css",
        ],
    )
    async def test_web_refuses_other_files(self, client: TestClient, path: str) -> None:
        resp = await client.get(path)
        assert resp.status == 404

    async def test_token_differs_per_instance(self, config_file: Path) -> None:
        loader = ConfigLoader(explicit_path=str(config_file))
        assert WebUI(loader).token != WebUI(loader).token


class TestGuards:
    async def test_token_and_same_origin_json_pass(self, client: TestClient, webui: WebUI) -> None:
        origin = f"http://{client.host}:{client.port}"
        headers = _auth(webui, Origin=origin, **{"Sec-Fetch-Site": "same-origin"})
        assert (await client.get("/api/config", headers=headers)).status == 200
        resp = await client.post("/api/validate", json={"yaml": PAGE_YAML}, headers=headers)
        assert resp.status == 200
        assert (await resp.json())["valid"] is True
        resp = await client.post("/api/config", json={"yaml": PAGE_YAML}, headers=headers)
        assert resp.status == 200

    async def test_fetch_site_none_passes(self, client: TestClient, webui: WebUI) -> None:
        headers = _auth(webui, **{"Sec-Fetch-Site": "none"})
        assert (await client.get("/api/config", headers=headers)).status == 200

    @pytest.mark.parametrize("path", ["/api/config", "/api/validate"])
    async def test_text_plain_post_is_415(
        self, client: TestClient, webui: WebUI, path: str
    ) -> None:
        resp = await client.post(
            path,
            data='{"yaml": "x: 1"}',
            headers=_auth(webui, **{"Content-Type": "text/plain"}),
        )
        assert resp.status == 415

    @pytest.mark.parametrize("path", ["/api/config", "/api/validate"])
    async def test_form_post_is_415(self, client: TestClient, webui: WebUI, path: str) -> None:
        resp = await client.post(path, data={"yaml": "x"}, headers=_auth(webui))
        assert resp.status == 415

    @pytest.mark.parametrize(
        "headers",
        [
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        ],
    )
    @pytest.mark.parametrize("method,path", [("POST", "/api/config"), ("POST", "/api/validate")])
    async def test_cross_site_is_403(
        self, client: TestClient, webui: WebUI, headers: dict, method: str, path: str
    ) -> None:
        resp = await client.request(
            method, path, json={"yaml": PAGE_YAML}, headers=_auth(webui, **headers)
        )
        assert resp.status == 403
        assert "quadlink page" in (await resp.json())["error"]

    async def test_cross_site_get_is_403(self, client: TestClient, webui: WebUI) -> None:
        headers = _auth(webui, Origin="https://evil.example")
        assert (await client.get("/api/config", headers=headers)).status == 403

    @pytest.mark.parametrize("token", [None, "", "wrong"])
    async def test_bad_token_is_403(self, client: TestClient, token: str | None) -> None:
        headers = {} if token is None else {TOKEN_HEADER: token}
        for resp in (
            await client.get("/api/config", headers=headers),
            await client.post("/api/config", json={"yaml": PAGE_YAML}, headers=headers),
            await client.post("/api/validate", json={"yaml": PAGE_YAML}, headers=headers),
        ):
            assert resp.status == 403
            assert "token" in (await resp.json())["error"]

    async def test_rejected_post_leaves_config(self, client: TestClient, config_file: Path) -> None:
        before = config_file.read_text()
        await client.post(
            "/api/config",
            data='{"yaml": "proxy_playlist: https://evil.example"}',
            headers={"Content-Type": "text/plain"},
        )
        assert config_file.read_text() == before

    @pytest.mark.parametrize("path", ["/", "/config", "/api/config"])
    async def test_unknown_host_is_403(self, client: TestClient, webui: WebUI, path: str) -> None:
        resp = await client.get(path, headers=_auth(webui, Host="rebind.evil.example:8081"))
        assert resp.status == 403
        assert "host name" in (await resp.json())["error"]

    @pytest.mark.parametrize(
        "host",
        ["evil.example@localhost", "localhost#.evil.example", "localhost/.x", "localhost:abc"],
    )
    async def test_malformed_host_is_403(self, client: TestClient, webui: WebUI, host: str) -> None:
        resp = await client.get("/api/config", headers=_auth(webui, Host=host))
        assert resp.status == 403

    @pytest.mark.parametrize("origin", ["null", "http://[::1"])
    async def test_odd_origin_is_403(self, client: TestClient, webui: WebUI, origin: str) -> None:
        resp = await client.get("/api/config", headers=_auth(webui, Origin=origin))
        assert resp.status == 403

    async def test_non_ascii_token_is_403(self, client: TestClient) -> None:
        resp = await client.get("/api/config", headers={TOKEN_HEADER: "\xff"})
        assert resp.status == 403

    async def test_unknown_host_post_is_403(self, client: TestClient, webui: WebUI) -> None:
        resp = await client.post(
            "/api/config",
            json={"yaml": PAGE_YAML},
            headers=_auth(webui, Host="rebind.evil.example"),
        )
        assert resp.status == 403

    @pytest.mark.parametrize(
        "host", ["localhost:8081", "[::1]:8081", "127.0.0.1", "QuadLink.Example.NET"]
    )
    async def test_allowed_hosts_pass(self, client: TestClient, webui: WebUI, host: str) -> None:
        resp = await client.get("/api/config", headers=_auth(webui, Host=host))
        assert resp.status == 200

    async def test_proxied_origin_passes(self, client: TestClient, webui: WebUI) -> None:
        headers = _auth(
            webui,
            Host="quadlink.example.net",
            Origin="https://quadlink.example.net",
            **{"Sec-Fetch-Site": "same-origin"},
        )
        resp = await client.post("/api/validate", json={"yaml": PAGE_YAML}, headers=headers)
        assert resp.status == 200

    async def test_loopback_only_by_default(self, config_file: Path) -> None:
        webui = WebUI(ConfigLoader(explicit_path=str(config_file)))
        async with TestClient(TestServer(webui.app)) as c:
            resp = await c.get("/", headers={"Host": "quadlink.example.net"})
            assert resp.status == 403

    @pytest.mark.parametrize(
        "path", ["/streams/1", "/streams/1/seg/x.ts", "/streams/1/init/1", "/streams/filler/x"]
    )
    async def test_stream_routes_skip_guards(self, client: TestClient, path: str) -> None:
        baseline = await client.get(path)
        resp = await client.get(
            path,
            headers={
                "Host": "tv.anything.example",
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert resp.status == baseline.status
        assert resp.status != 403


class TestSave:
    async def _save(self, client: TestClient, webui: WebUI, yaml: str) -> None:
        resp = await client.post("/api/config", json={"yaml": yaml}, headers=_auth(webui))
        assert resp.status == 200, await resp.text()

    async def test_keeps_file_credentials_mapping(
        self, client: TestClient, webui: WebUI, config_file: Path, secret_file: Path
    ) -> None:
        await self._save(client, webui, "priorities:\n  100:\n    - urls: [alice]\n")
        text = config_file.read_text()
        assert "hunter2" not in text
        assert "someuser" not in text
        data = _load(config_file)
        assert data["credentials"] == {"file": str(secret_file)}
        assert data["priorities"] == {100: [{"urls": ["alice"]}]}

    async def test_ignores_page_credentials(
        self, client: TestClient, webui: WebUI, config_file: Path, secret_file: Path
    ) -> None:
        await self._save(
            client,
            webui,
            PAGE_YAML + "credentials:\n  file: /etc/shadow\n",
        )
        assert _load(config_file)["credentials"] == {"file": str(secret_file)}

    async def test_no_credentials_when_file_has_none(
        self, client: TestClient, webui: WebUI, config_file: Path
    ) -> None:
        config_file.write_text("mode: local\npriorities:\n  1:\n    - urls: [a]\n")
        await self._save(client, webui, PAGE_YAML + "credentials:\n  username: x\n  secret: y\n")
        assert "credentials" not in _load(config_file)

    async def test_twitch_client_secret_stays_hidden_and_kept(
        self, client: TestClient, webui: WebUI, config_file: Path
    ) -> None:
        config_file.write_text(
            config_file.read_text() + "twitch_api:\n  client_id: id\n  client_secret: shh\n"
        )
        body = await (await client.get("/api/config", headers=_auth(webui))).json()
        assert "shh" not in body["yaml"]
        assert "client_id: id" in body["yaml"]
        page = PAGE_YAML + "twitch_api:\n  client_id: id2\n"
        resp = await client.post("/api/validate", json={"yaml": page}, headers=_auth(webui))
        assert (await resp.json())["valid"] is True
        await self._save(client, webui, page + "  client_secret: from-page\n")
        assert _load(config_file)["twitch_api"] == {"client_id": "id2", "client_secret": "shh"}

    async def test_writes_no_defaults_or_env(
        self,
        client: TestClient,
        webui: WebUI,
        config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("QL_DIVERSITY_BONUS", "77")
        monkeypatch.setenv("QL_PROXY_PLAYLIST", "https://env.example")
        await self._save(client, webui, "priorities:\n  100:\n    - urls: [alice]\n")
        data = _load(config_file)
        assert set(data) == {"priorities", "credentials"}

    async def test_get_shows_file_not_env(
        self,
        client: TestClient,
        webui: WebUI,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("QL_PROXY_PLAYLIST", "https://env.example")
        body = await (await client.get("/api/config", headers=_auth(webui))).json()
        assert "env.example" not in body["yaml"]
        assert "credentials" not in body["yaml"]
        assert "hooks.example" in body["yaml"]
        assert "diversity_bonus" not in body["yaml"]

    async def test_preserves_mode(
        self, client: TestClient, webui: WebUI, config_file: Path
    ) -> None:
        await self._save(client, webui, PAGE_YAML)
        assert stat.S_IMODE(config_file.stat().st_mode) == 0o640

    async def test_leaves_no_temp_files(
        self, client: TestClient, webui: WebUI, config_file: Path
    ) -> None:
        before = set(os.listdir(config_file.parent))
        await self._save(client, webui, PAGE_YAML)
        assert set(os.listdir(config_file.parent)) == before

    async def test_failed_write_keeps_old_file(
        self, client: TestClient, webui: WebUI, config_file: Path
    ) -> None:
        before = config_file.read_text()
        listing = set(os.listdir(config_file.parent))
        with patch("quadlink.webui.os.replace", side_effect=OSError("disk full")):
            resp = await client.post("/api/config", json={"yaml": PAGE_YAML}, headers=_auth(webui))
        assert resp.status == 400
        assert config_file.read_text() == before
        assert set(os.listdir(config_file.parent)) == listing

    async def test_invalid_config_not_written(
        self, client: TestClient, webui: WebUI, config_file: Path
    ) -> None:
        before = config_file.read_text()
        resp = await client.post(
            "/api/config", json={"yaml": "diversity_bonus: 5\n"}, headers=_auth(webui)
        )
        assert resp.status == 400
        assert "priorities" in (await resp.json())["error"]
        assert config_file.read_text() == before
