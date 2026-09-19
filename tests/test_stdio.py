"""End-to-end: boot the server over stdio the way an MCP client does.

This is the test that would have caught the mcp 2.0 import break: it runs
the installed package in a subprocess and speaks the protocol to it.

The live-API part (list_test_types is open data) is skipped when
WEBPERF_TEST_OFFLINE=1.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "list_test_types",
    "list_public_sector_ratings",
    "search_public_sites",
    "get_public_site",
    "get_public_stats",
    "check_api_key",
    "list_my_sites",
    "get_site_details",
    "get_latest_results",
    "get_raw_check_data",
    "get_test_history",
    "get_quota",
    "list_categories",
    "list_private_tests",
    "get_private_test_result",
    "list_audits",
    "get_audit_file",
}


def run(coro):
    return asyncio.run(coro)


async def _session(env_overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith("WEBPERF_")}
    env.update(env_overrides)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "webperf_mcp.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            key_check = await session.call_tool("check_api_key", {})
            live = None
            if os.environ.get("WEBPERF_TEST_OFFLINE") != "1":
                live = await session.call_tool("list_test_types", {"lang": "en"})
            return init, tools, key_check, live


def test_server_boots_and_advertises_read_only_tools():
    init, tools, key_check, live = run(_session({"WEBPERF_API_KEY": ""}))

    assert init.serverInfo.name == "webperf"
    assert init.instructions and "check_api_key" in init.instructions

    names = {t.name for t in tools.tools}
    assert names == EXPECTED_TOOLS
    for t in tools.tools:
        assert t.annotations is not None, t.name
        assert t.annotations.readOnlyHint is True, t.name
        assert t.annotations.destructiveHint is False, t.name

    body = json.loads(key_check.content[0].text)
    assert key_check.isError is False
    assert body["status"] == "no_key_configured"

    if live is not None:
        assert live.isError is False, live.content[0].text
        payload = json.loads(live.content[0].text)
        assert payload["count"] > 0
        assert payload["tests"][0]["name"]


def test_bad_base_url_does_not_kill_the_server():
    init, tools, key_check, _ = run(
        _session(
            {
                "WEBPERF_API_KEY": "abcdefghij1234567890",
                "WEBPERF_API_BASE": "http://example.org",
                "WEBPERF_TEST_OFFLINE": "1",
            }
        )
    )
    assert init.serverInfo.name == "webperf"
    body = json.loads(key_check.content[0].text)
    assert body["status"] == "misconfigured"


@pytest.mark.skipif(os.environ.get("WEBPERF_TEST_OFFLINE") == "1", reason="offline")
def test_bogus_key_is_reported_as_rejected_by_live_api():
    _, _, key_check, _ = run(_session({"WEBPERF_API_KEY": "abcdefghij1234567890"}))
    body = json.loads(key_check.content[0].text)
    assert body["status"] == "rejected"
    assert "abcdefghij1234567890" not in key_check.content[0].text
