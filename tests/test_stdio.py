"""End-to-end: boot the server over stdio the way an MCP client does.

This is the test that catches an upstream mcp break: it runs
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
    "get_crux",
    "get_quota",
    "get_retest_status",
    "list_categories",
    "list_private_tests",
    "get_private_test_result",
    "get_private_raw_check_data",
    "list_audits",
    "get_audit_file",
    "get_campaign_convention",
    "build_campaign_url",
    "check_campaign_urls",
}


def run(coro):
    return asyncio.run(coro)


async def _session(env_overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith("WEBPERF_")}
    env.setdefault("WEBPERF_TEST_OFFLINE", os.environ.get("WEBPERF_TEST_OFFLINE", ""))
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

    assert init.server_info.name == "webperf"
    assert init.instructions and "check_api_key" in init.instructions

    names = {t.name for t in tools.tools}
    assert names == EXPECTED_TOOLS
    for t in tools.tools:
        assert t.annotations is not None, t.name
        assert t.annotations.read_only_hint is True, t.name
        assert t.annotations.destructive_hint is False, t.name

    body = json.loads(key_check.content[0].text)
    assert key_check.is_error is False
    assert body["status"] == "no_key_configured"
    assert body["ordering_enabled"] is False
    assert body["convention_edits_enabled"] is False
    assert "order_retest" not in init.instructions
    assert "build_campaign_url" in init.instructions

    if live is not None:
        assert live.is_error is False, live.content[0].text
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
    assert init.server_info.name == "webperf"
    body = json.loads(key_check.content[0].text)
    assert body["status"] == "misconfigured"


@pytest.mark.skipif(os.environ.get("WEBPERF_TEST_OFFLINE") == "1", reason="offline")
def test_bogus_key_is_reported_as_rejected_by_live_api():
    _, _, key_check, _ = run(_session({"WEBPERF_API_KEY": "abcdefghij1234567890"}))
    body = json.loads(key_check.content[0].text)
    assert body["status"] == "rejected"
    assert "abcdefghij1234567890" not in key_check.content[0].text


def test_ordering_is_opt_in_and_marked_as_a_write():
    init, tools, key_check, _ = run(
        _session({
            "WEBPERF_API_KEY": "",
            "WEBPERF_ALLOW_ORDERING": "true",
            "WEBPERF_TEST_OFFLINE": "1",
        })
    )
    names = {t.name for t in tools.tools}
    assert names == EXPECTED_TOOLS | {"order_retest", "order_private_test"}
    for name in ("order_retest", "order_private_test"):
        order = next(t for t in tools.tools if t.name == name)
        assert order.annotations.read_only_hint is False, name
        assert order.annotations.destructive_hint is True, name
        assert order.annotations.idempotent_hint is False, name
    for t in tools.tools:
        if t.name not in ("order_retest", "order_private_test"):
            assert t.annotations.read_only_hint is True, t.name
    assert "order_retest" in init.instructions
    assert "order_private_test" in init.instructions
    assert json.loads(key_check.content[0].text)["ordering_enabled"] is True


async def _prompts(env_overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith("WEBPERF_")}
    env.update(env_overrides)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "webperf_mcp.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_prompts()
            got = await session.get_prompt("setup_campaign_convention", {"site_id": "42"})
            return listed, got


def test_convention_edits_are_opt_in_and_marked_as_writes():
    init, tools, key_check, _ = run(
        _session({
            "WEBPERF_API_KEY": "",
            "WEBPERF_ALLOW_CONVENTION_EDITS": "true",
            "WEBPERF_TEST_OFFLINE": "1",
        })
    )
    names = {t.name for t in tools.tools}
    assert names == EXPECTED_TOOLS | {"save_campaign_convention", "add_campaign_value"}
    save = next(t for t in tools.tools if t.name == "save_campaign_convention")
    add = next(t for t in tools.tools if t.name == "add_campaign_value")
    assert save.annotations.read_only_hint is False and save.annotations.destructive_hint is True
    assert add.annotations.read_only_hint is False and add.annotations.destructive_hint is False
    assert "save_campaign_convention" in init.instructions
    assert json.loads(key_check.content[0].text)["convention_edits_enabled"] is True


def test_setup_prompt_is_listed():
    listed, got = run(_prompts({"WEBPERF_API_KEY": "", "WEBPERF_TEST_OFFLINE": "1"}))
    assert [p.name for p in listed.prompts] == ["setup_campaign_convention"]
    assert "site_id 42" in got.messages[0].content.text
