"""The update check: asks api.webperf.se/info, never GitHub, and never breaks a tool."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from webperf_mcp import server

GOOD_KEY = "abcdefghij1234567890"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", GOOD_KEY)
    monkeypatch.setattr(server, "_CONFIG_ERROR", None)
    monkeypatch.setattr(server, "UPDATE_CHECK_ENABLED", True)
    monkeypatch.setattr(server, "__version__", "0.6.0")
    fresh = {"checked": False, "latest": None, "download_url": None, "noticed": False}
    monkeypatch.setattr(server, "_update", fresh)
    monkeypatch.setattr(server, "_update_done", server.threading.Event())
    server._cache.clear()
    yield
    server._CLIENT = None


def install(handler):
    server._CLIENT = server._make_client(transport=httpx.MockTransport(handler))


def info_with(latest, seen=None):
    def handler(request):
        if seen is not None:
            seen.append((request.url.host, request.url.path, request.headers.get("api-key")))
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={"mcp": {"latest_version": latest,
                              "download_url": "https://github.com/Webperf-se/webperf-mcp/releases/latest"}},
            )
        return httpx.Response(200, json={"site_count": 1})
    return handler


def test_asks_webperf_without_the_key():
    seen = []
    install(info_with("0.7.0", seen))
    server._check_for_update()
    host, path, key = seen[0]
    assert host == "api.webperf.se" and path == "/info" and key is None
    assert server._update_available() is True


@pytest.mark.parametrize("latest,expected", [
    ("0.7.0", True), ("0.6.1", True), ("1.0.0", True),
    ("0.6.0", False), ("0.5.9", False), (None, False), ("nightly", False),
])
def test_version_comparison(latest, expected):
    install(info_with(latest))
    server._check_for_update()
    assert server._update_available() is expected


def test_api_without_mcp_block_or_down_is_silent():
    install(lambda r: httpx.Response(200, json={"name": "Webperf API"}))
    server._check_for_update()
    assert server._update["checked"] and server._update_available() is False

    install(lambda r: httpx.Response(503, json={}))
    server._check_for_update()
    assert server._update_available() is False


def test_check_api_key_reports_versions():
    install(info_with("0.7.0"))
    out = server.check_api_key()
    assert out["server_version"] == "0.6.0"
    assert out["latest_version"] == "0.7.0"
    assert out["update_available"] is True
    assert out["update_url"].startswith("https://")


def test_opt_out_makes_no_request(monkeypatch):
    monkeypatch.setattr(server, "UPDATE_CHECK_ENABLED", False)
    seen = []
    install(info_with("0.7.0", seen))
    out = server.check_api_key()
    assert all(path != "/info" for _, path, _ in seen)
    assert out["update_available"] is False
    assert out["update_check_enabled"] is False


class _Ctx:
    def __init__(self, method):
        self.method = method


def _call(result, method="tools/call"):
    async def call_next(ctx):
        return result
    return asyncio.run(server._update_notice_middleware(_Ctx(method), call_next))


def test_notice_is_appended_once():
    install(info_with("0.7.0"))
    server._check_for_update()
    first = _call({"content": [{"type": "text", "text": "{}"}], "isError": False})
    assert len(first["content"]) == 2 and "0.7.0" in first["content"][1]["text"]
    second = _call({"content": [{"type": "text", "text": "{}"}], "isError": False})
    assert len(second["content"]) == 1


def test_no_notice_on_errors_other_methods_or_when_current():
    install(info_with("0.7.0"))
    server._check_for_update()
    err = _call({"content": [{"type": "text", "text": "x"}], "isError": True})
    assert len(err["content"]) == 1
    listing = _call({"tools": []}, method="tools/list")
    assert listing == {"tools": []}

    server._update["latest"] = "0.6.0"
    ok = _call({"content": [{"type": "text", "text": "{}"}], "isError": False})
    assert len(ok["content"]) == 1
