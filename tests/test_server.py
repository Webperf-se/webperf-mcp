"""Unit tests for the tool functions, with the HTTP layer mocked.

The FastMCP decorator returns the original function, so tools are called
directly. httpx.MockTransport stands in for api.webperf.se.
"""

from __future__ import annotations

import json
from html import escape

import httpx
import pytest

from webperf_mcp import server

GOOD_KEY = "abcdefghij1234567890"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", GOOD_KEY)
    monkeypatch.setattr(server, "_CONFIG_ERROR", None)
    server._cache.clear()
    yield
    server._cache.clear()
    server._CLIENT = None


def install(handler):
    """Route every request through `handler(request) -> httpx.Response`."""
    server._CLIENT = server._make_client(transport=httpx.MockTransport(handler))


def json_response(status, body, headers=None):
    return httpx.Response(status, json=body, headers=headers or {})


# --- key handling ---------------------------------------------------------


def test_missing_key_is_a_friendly_error(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "")
    with pytest.raises(server.WebperfError) as exc:
        server.list_my_sites()
    assert exc.value.kind == "auth"
    assert "open-data tools work without one" in str(exc.value)


def test_bad_format_key(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "not a key!")
    with pytest.raises(server.WebperfError) as exc:
        server.list_my_sites()
    assert exc.value.kind == "auth"


def test_key_is_sent_only_as_header_and_never_echoed():
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        seen["url"] = str(request.url)
        return json_response(400, {"message": "Please provide a valid API key"})

    install(handler)
    with pytest.raises(server.WebperfError) as exc:
        server.list_my_sites()
    assert seen["headers"]["api-key"] == GOOD_KEY
    assert GOOD_KEY not in seen["url"]
    assert GOOD_KEY not in str(exc.value)
    assert seen["headers"]["user-agent"].startswith("webperf-mcp/")


def test_open_data_tools_send_no_key(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "")
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return json_response(200, {"tests": [{"id": 1, "name": "Perf", "active": True}]})

    install(handler)
    out = server.list_test_types(lang="en")
    assert "api-key" not in seen["headers"]
    assert out["tests"][0]["name"] == "Perf"


# --- status mapping -------------------------------------------------------


@pytest.mark.parametrize(
    "status, body, kind",
    [
        (400, {"message": "Please provide a valid API key"}, "auth"),
        (401, {"message": "Invalid API key"}, "auth"),
        (
            400,
            {"message": "type_of_test must be a comma-separated list of integers"},
            "bad_request",
        ),
        (403, {"message": "Access denied to this site"}, "forbidden"),
        (404, {"message": "Site not found"}, "not_found"),
        (500, {"error": "Internal Server Error"}, "server"),
    ],
)
def test_status_mapping(status, body, kind):
    install(lambda r: json_response(status, body))
    with pytest.raises(server.WebperfError) as exc:
        server.get_test_history(1)
    assert exc.value.kind == kind
    assert exc.value.status == status


def test_rate_limit_uses_headers():
    install(
        lambda r: json_response(
            429, {"error": "Rate Limit Exceeded"}, {"Retry-After": "42", "X-RateLimit-Limit": "60"}
        )
    )
    with pytest.raises(server.WebperfError) as exc:
        server.get_test_history(1)
    assert exc.value.kind == "rate_limited"
    assert "42 seconds" in str(exc.value)
    assert "60 requests" in str(exc.value)
    assert "200 requests/hour" not in str(exc.value)


def test_unreachable():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    install(handler)
    with pytest.raises(server.WebperfError) as exc:
        server.get_test_history(1)
    assert exc.value.kind == "unreachable"


# --- data shaping ---------------------------------------------------------


def stats_payload():
    return {
        "data": [
            {
                "test_date": "2026-09-01 10:00:00",
                "type_of_test": "10",
                "rating": "4.5",
                "check_report": escape('Missing "alt" on <img>'),
                "check_report_a11y": escape("it's fine"),
                "check_report_perf": "",
                "check_report_stand": "",
                "check_report_sec": "",
                "json_check_data": escape(json.dumps({"issues": [{"id": "alt"}]})),
            },
            {
                "test_date": "2026-09-01 10:00:00",
                "type_of_test": "15",
                "rating": "-1",
                "check_report": "",
                "check_report_a11y": "",
                "check_report_perf": "",
                "check_report_stand": "",
                "check_report_sec": "",
                "json_check_data": "",
            },
        ]
    }


def test_latest_results_unescapes_reports_and_asks_for_no_raw_data():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return json_response(200, stats_payload())

    install(handler)
    out = server.get_latest_results(3843)
    assert seen["params"]["include_raw_data"] == "false"
    assert out["result_count"] == 2
    first = out["results"][0]
    assert first["report_overall"] == 'Missing "alt" on <img>'
    assert first["report_a11y"] == "it's fine"
    assert first["rating"] == 4.5
    assert out["results"][1]["rating"] is None  # -1 means not rated
    assert "json_check_data" not in json.dumps(out)


def test_latest_results_filters_client_side_when_api_ignores_param():
    install(lambda r: json_response(200, stats_payload()))
    out = server.get_latest_results(3843, type_of_test=15)
    assert [r["type_of_test"] for r in out["results"]] == ["15"]


def test_latest_results_flags_when_the_api_truncated_the_rows():
    """/0.1/stats/{id} silently applies LIMIT 30 and gives no hint in the body.

    Reporting result_count without saying so presents a truncated list as if
    it were the whole picture.
    """
    rows = [
        {"test_date": "2026-09-01", "type_of_test": str(i), "rating": "3"}
        for i in range(server.API_MAX_TEST_ROWS)
    ]
    install(lambda r: json_response(200, {"data": rows}))
    out = server.get_latest_results(3843)
    assert out["truncated"] is True
    assert "30" in out["note"]


def test_latest_results_is_not_flagged_when_rows_fit():
    install(lambda r: json_response(200, stats_payload()))
    out = server.get_latest_results(3843)
    assert out["truncated"] is False


def test_truncation_is_judged_on_api_rows_not_filtered_rows():
    """A client-side filter shrinks the list; that is not the API truncating."""
    rows = [
        {"test_date": "2026-09-01", "type_of_test": str(i), "rating": "3"}
        for i in range(server.API_MAX_TEST_ROWS)
    ]
    install(lambda r: json_response(200, {"data": rows}))
    out = server.get_latest_results(3843, type_of_test=7)
    assert out["result_count"] == 1
    assert out["truncated"] is True


def test_raw_check_data_is_parsed():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return json_response(200, stats_payload())

    install(handler)
    out = server.get_raw_check_data(3843, 10)
    assert seen["params"]["type_of_test"] == "10"
    assert out["raw_check_data"] == {"issues": [{"id": "alt"}]}


def test_raw_check_data_missing_type():
    install(lambda r: json_response(200, stats_payload()))
    with pytest.raises(server.WebperfError) as exc:
        server.get_raw_check_data(3843, 99)
    assert exc.value.kind == "not_found"


def test_list_my_sites_uses_names_when_present():
    install(
        lambda r: json_response(
            200,
            {"sites": [{"site-ID": "3843", "title": "Exempel", "website": "https://exempel.se"}]},
        )
    )
    out = server.list_my_sites()
    assert out["sites"] == [
        {"site_id": 3843, "title": "Exempel", "website": "https://exempel.se"}
    ]
    assert "note" not in out


def test_list_my_sites_notes_when_api_lists_ids_only():
    install(lambda r: json_response(200, {"sites": [{"site-ID": "3843", "uri": "x"}]}))
    out = server.list_my_sites()
    assert out["sites"] == [{"site_id": 3843}]
    assert "get_site_details" in out["note"]


def test_get_cache_reuses_response_within_ttl():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return json_response(200, {"data": []})

    install(handler)
    server.get_test_history(1)
    server.get_test_history(1)
    assert calls["n"] == 1


def test_cache_evicts_expired_entries_instead_of_growing_forever():
    """A long-lived stdio process must not accumulate every response it ever saw.

    Each call here is a distinct site, so every response is a fresh cache key.
    With a TTL that has already lapsed, none of them may survive.
    """
    install(lambda r: json_response(200, {"data": []}))
    for site in range(1, 6):
        server.get_test_history(site)
    assert len(server._cache) == 5

    # Every entry is now stale; the next request must not leave them behind.
    server._cache = {k: (0.0, v) for k, (_, v) in server._cache.items()}
    server.get_test_history(99)
    assert len(server._cache) == 1


def test_cache_is_bounded_even_when_entries_are_live():
    install(lambda r: json_response(200, {"data": []}))
    for site in range(1, server.CACHE_MAX_ENTRIES + 20):
        server.get_test_history(site)
    assert len(server._cache) <= server.CACHE_MAX_ENTRIES


def test_site_id_validation():
    with pytest.raises(server.WebperfError):
        server.get_test_history(0)
    with pytest.raises(server.WebperfError):
        server.get_test_history("abc")


def test_quota_sends_site_id_header():
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return json_response(
            200, {"monthly_quota": 500, "used": 12, "available": 488, "period_days": 30}
        )

    install(handler)
    out = server.get_quota(3843)
    assert seen["headers"]["site-id"] == "3843"
    assert out["available"] == 488


def test_audit_file_allowlist():
    with pytest.raises(server.WebperfError):
        server.get_audit_file(1, "folder", "summaries.zip")
    with pytest.raises(server.WebperfError):
        server.get_audit_file(1, "../etc", "crux_summary.json")


def test_list_audits_extracts_folder():
    install(
        lambda r: json_response(
            200,
            {
                "data": [
                    {
                        "result_id": 5,
                        "title": "Audit",
                        "format": "audit_v2",
                        "content": (
                            "{'folder': '/static/results/exempel.se_20260804-summary_reports/'}"
                        ),
                        "result_files": {"crux_summary.json": "https://x/y"},
                    }
                ]
            },
        )
    )
    out = server.list_audits(3843)
    assert out["audits"][0]["folder"] == "exempel.se_20260804-summary_reports"
    assert out["audits"][0]["files"] == ["crux_summary.json"]


# --- open data ------------------------------------------------------------


def test_search_public_sites_maps_category():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return json_response(200, {"sites": [], "pagination": {"total": 0}})

    install(handler)
    server.search_public_sites("ale", category="municipality", limit=500)
    assert seen["params"]["category"] == "2"
    assert seen["params"]["limit"] == "100"


def test_get_public_site_explains_non_public():
    install(lambda r: json_response(403, {"error": "Site not publicly accessible"}))
    with pytest.raises(server.WebperfError) as exc:
        server.get_public_site(3843)
    assert "get_site_details" in str(exc.value)


# --- check_api_key --------------------------------------------------------


def test_check_api_key_no_key(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "")
    out = server.check_api_key()
    assert out["status"] == "no_key_configured"
    assert out["ok"] is False


def test_check_api_key_bad_format(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "short")
    assert server.check_api_key()["status"] == "bad_format"


def test_check_api_key_ok_via_whoami():
    install(
        lambda r: json_response(
            200,
            {
                "user_id": 7,
                "display_name": "Anna",
                "role": "user",
                "site_count": 3,
                "unrestricted": False,
            },
        )
    )
    out = server.check_api_key()
    assert out["status"] == "ok"
    assert out["site_count"] == 3
    assert out["account"]["display_name"] == "Anna"
    assert GOOD_KEY not in json.dumps(out)


def test_check_api_key_no_sites():
    install(lambda r: json_response(200, {"user_id": 7, "role": "user", "site_count": 0}))
    out = server.check_api_key()
    assert out["status"] == "no_sites"
    assert out["ok"] is True


def test_check_api_key_falls_back_to_site_list_on_old_api():
    def handler(request):
        if request.url.path == "/0.1/whoami/":
            return json_response(404, {"error": "Not Found"})
        return json_response(200, {"sites": [{"site-ID": "1"}, {"site-ID": "2"}]})

    install(handler)
    out = server.check_api_key()
    assert out["status"] == "ok"
    assert out["site_count"] == 2


def test_check_api_key_rejected():
    install(lambda r: json_response(401, {"message": "Invalid API key"}))
    out = server.check_api_key()
    assert out["status"] == "rejected"
    assert "account page" in out["next_step"]


def test_check_api_key_unreachable():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    install(handler)
    assert server.check_api_key()["status"] == "unreachable"


# --- base URL -------------------------------------------------------------


def test_api_base_rejects_plain_http(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_BASE", "http://evil.example/")
    base, err = server._resolve_api_base()
    assert err and "https" in err


def test_api_base_allows_localhost_http(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_BASE", "http://localhost:5000")
    base, err = server._resolve_api_base()
    assert err is None
    assert base == "http://localhost:5000"


def test_api_base_default(monkeypatch):
    monkeypatch.delenv("WEBPERF_API_BASE", raising=False)
    assert server._resolve_api_base() == ("https://api.webperf.se", None)


def test_config_error_blocks_requests(monkeypatch):
    monkeypatch.setattr(server, "_CONFIG_ERROR", "bad base")
    install(lambda r: json_response(200, {}))
    with pytest.raises(server.WebperfError) as exc:
        server.get_public_stats()
    assert exc.value.kind == "config"


# --- numeric env vars ------------------------------------------------------


@pytest.mark.parametrize("raw", ["abc", "", "   ", "-5"])
def test_bad_numeric_env_falls_back_to_the_default(raw):
    """A typo in the client config must not stop the server from booting.

    WEBPERF_API_BASE is already handled this way; the numeric settings were
    parsed with a bare float() and took the whole process down at import.
    """
    assert server._float_env("WEBPERF_HTTP_TIMEOUT", 30.0, {"WEBPERF_HTTP_TIMEOUT": raw}) == 30.0


def test_good_numeric_env_is_used():
    assert server._float_env("WEBPERF_HTTP_TIMEOUT", 30.0, {"WEBPERF_HTTP_TIMEOUT": "5"}) == 5.0


def test_cache_ttl_of_zero_is_honoured_not_replaced_by_the_default():
    assert server._float_env("WEBPERF_CACHE_TTL", 60.0, {"WEBPERF_CACHE_TTL": "0"}) == 0.0
