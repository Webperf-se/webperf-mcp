"""Unit tests for the tool functions, with the HTTP layer mocked.

The MCPServer tool decorator returns the original function, so tools are called
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


def test_retest_status_lists_rows():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["headers"] = dict(request.headers)
        return json_response(200, {
            "site_id": 3843, "queue_length": 7,
            "retests": [{"queue_id": 4711, "test_num": 21, "status": "queued", "position": 3,
                         "server": None, "created": "2026-10-13 20:00:00+02:00", "started": None,
                         "finished": None, "refunded": False, "result": None}],
        })

    install(handler)
    out = server.get_retest_status(3843)
    assert seen["path"] == "/0.1/retest/"
    assert seen["headers"]["site-id"] == "3843"
    assert out["queue_length"] == 7
    assert out["retests"][0]["status"] == "queued" and out["retests"][0]["position"] == 3
    assert "server" not in out["retests"][0]


def test_retest_status_single_row():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return json_response(200, {
            "site_id": 3843, "queue_length": 0,
            "retest": {"queue_id": 4711, "test_num": 21, "status": "done", "position": None,
                       "created": "x", "started": "y", "finished": "z", "refunded": False,
                       "result": {"id": 99, "type_of_test": 21, "test_date": "z"}},
        })

    install(handler)
    out = server.get_retest_status(3843, queue_id=4711)
    assert seen["path"] == "/0.1/retest/4711"
    assert out["retests"][0]["result"]["id"] == 99


def _catalogue():
    def entry(tid, name, retestable, public_only=False):
        return {"id": tid, "name": name, "active": True,
                "retestable": retestable, "public_sector_only": public_only}

    return {"tests": [
        entry(2, "404", True),
        entry(21, "HTTP", True),
        entry(26, "A11y statement", True, public_only=True),
        entry(20, "Webbkoll", False),
    ]}


def test_list_test_types_passes_ordering_flags_through():
    install(lambda r: json_response(200, _catalogue()))
    out = server.list_test_types(lang="en")
    by_id = {t["id"]: t for t in out["tests"]}
    assert by_id[26]["retestable"] is True and by_id[26]["public_sector_only"] is True
    assert by_id[20]["retestable"] is False


def test_check_api_key_reports_ordering_flag(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "")
    out = server.check_api_key()
    assert out["ordering_enabled"] is False


def _ordering_api(seen, available=10, post_status=200, post_body=None):
    def handler(request):
        path = request.url.path
        if path == "/v1/tests":
            return json_response(200, _catalogue())
        if path == "/0.1/quota/":
            return json_response(200, {"monthly_quota": 500, "used": 500 - available,
                                       "available": available, "period_days": 30})
        if path == "/0.1/retest/" and request.method == "POST":
            seen["headers"] = dict(request.headers)
            return json_response(post_status, post_body or {
                "message": "ok", "queued": [21], "already_queued": [2],
                "credits_charged": 1, "queue_ids": {"21": 4711}})
        raise AssertionError(f"unexpected {request.method} {path}")
    return handler


def test_order_retest_posts_explicit_tests_and_reports_charge(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "abcdefghij1234567890")
    seen = {}
    install(_ordering_api(seen))
    out = server.order_retest(3843, [21, 2, 21])
    assert seen["headers"]["site-id"] == "3843"
    assert seen["headers"]["testnums"] == "21,2"
    assert (out["queued"], out["already_queued"], out["credits_charged"]) == ([21], [2], 1)
    assert out["queue_ids"] == {"21": 4711}
    assert out["credits_remaining"] == 9
    assert "get_retest_status" in out["next_step"]


def test_order_retest_requires_explicit_tests(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "abcdefghij1234567890")
    seen = {}
    install(_ordering_api(seen))
    with pytest.raises(server.WebperfError):
        server.order_retest(3843, [])
    assert seen == {}


def test_order_retest_refuses_non_retestable_before_posting(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "abcdefghij1234567890")
    seen = {}
    install(_ordering_api(seen))
    with pytest.raises(server.WebperfError) as exc:
        server.order_retest(3843, [21, 20])
    assert "[20]" in str(exc.value)
    assert seen == {}


def test_order_retest_refuses_when_credits_are_short(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "abcdefghij1234567890")
    seen = {}
    install(_ordering_api(seen, available=1))
    with pytest.raises(server.WebperfError) as exc:
        server.order_retest(3843, [21, 2])
    assert "Not enough credits" in str(exc.value)
    assert seen == {}


def test_order_retest_surfaces_api_refusal_for_test_26(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "abcdefghij1234567890")
    seen = {}
    install(_ordering_api(seen, post_status=400, post_body={
        "message": "Tests [26] can only be ordered for public-sector websites"}))
    with pytest.raises(server.WebperfError) as exc:
        server.order_retest(3843, [26])
    assert "public-sector" in str(exc.value)


def test_order_retest_is_not_registered_by_default():
    assert not server.ORDERING_ENABLED
    assert "order_retest" not in {t.name for t in server.mcp._tool_manager.list_tools()}


def _private_api(seen, available=100, recent_url="https://www.example.se/gammal", recent_hours=1):
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=2))
    stamp = "%Y-%m-%d %H:%M:%S+02:00"
    recent_at = (datetime.now(tz) - timedelta(hours=recent_hours)).strftime(stamp)
    old_at = (datetime.now(tz) - timedelta(days=3)).strftime(stamp)

    def handler(request):
        path = request.url.path
        if path == "/item/3843":
            return json_response(200, {"data": {"url": "https://www.example.se/", "title": "Ex"}})
        if path == "/0.1/private_stats/3843":
            return json_response(200, {"data": [
                {"uri": "https://api.webperf.se/0.1/private_stats/3843/900", "url": recent_url,
                 "date_added": recent_at},
                {"uri": "https://api.webperf.se/0.1/private_stats/3843/800",
                 "url": "https://www.example.se/aldre", "date_added": old_at},
            ]})
        if path == "/0.1/quota/":
            return json_response(200, {"monthly_quota": 500, "used": 500 - available,
                                       "available": available, "period_days": 30})
        if path == "/v1/tests":
            return json_response(200, _catalogue())
        if path == "/0.1/test_once/" and request.method == "POST":
            seen["headers"] = dict(request.headers)
            seen["body"] = request.content.decode()
            n = seen["body"].count("%2C+") + 1 if seen["body"] else 0
            return json_response(200, {"message": "ok", "endpoints": {
                str(1000 + i): f"https://api.webperf.se/private_stats/3843/{1000 + i}"
                for i in range(n)}})
        raise AssertionError(f"unexpected {request.method} {path}")
    return handler


def test_order_private_test_posts_new_pages_and_skips_recent(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "abcdefghij1234567890")
    seen = {}
    install(_private_api(seen))
    out = server.order_private_test(3843, [
        "https://www.example.se/kontakt", "https://www.example.se/gammal/",
        "https://www.example.se/aldre"])
    assert seen["headers"]["site-id"] == "3843"
    expected_body = (
        "urls=https%3A%2F%2Fwww.example.se%2Fkontakt%2C+https%3A%2F%2Fwww.example.se%2Faldre"
    )
    assert expected_body in seen["body"]
    assert [o["url"] for o in out["ordered"]] == [
        "https://www.example.se/kontakt", "https://www.example.se/aldre"]
    assert [o["private_test_id"] for o in out["ordered"]] == [1000, 1001]
    assert out["skipped_recent"][0]["private_test_id"] == 900
    assert (out["credits_charged"], out["credits_remaining"]) == (30, 70)


def test_order_private_test_everything_recent_charges_nothing(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "abcdefghij1234567890")
    seen = {}
    install(_private_api(seen))
    out = server.order_private_test(3843, ["http://www.example.se/gammal"])
    assert out["ordered"] == [] and out["credits_charged"] == 0
    assert "body" not in seen


def test_order_private_test_refuses_other_domains_before_posting(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "abcdefghij1234567890")
    seen = {}
    install(_private_api(seen))
    with pytest.raises(server.WebperfError) as exc:
        server.order_private_test(3843, ["https://www.example.se/ok", "https://example.se/annan"])
    assert "example.se/annan" in str(exc.value)
    assert "body" not in seen


def test_order_private_test_caps_at_ten(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "abcdefghij1234567890")
    install(_private_api({}))
    with pytest.raises(server.WebperfError):
        server.order_private_test(3843, [f"https://www.example.se/s{i}" for i in range(11)])


def test_order_private_test_needs_confirmation_above_five(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "abcdefghij1234567890")
    seen = {}
    install(_private_api(seen, available=200))
    pages = [f"https://www.example.se/s{i}" for i in range(6)]
    out = server.order_private_test(3843, pages)
    assert out["needs_confirmation"] is True and out["cost"] == 90 and out["ordered"] == []
    assert "body" not in seen
    out = server.order_private_test(3843, pages, confirm_large_order=True)
    assert len(out["ordered"]) == 6 and out["credits_charged"] == 90


def test_order_private_test_refuses_when_credits_are_short(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "abcdefghij1234567890")
    seen = {}
    install(_private_api(seen, available=20))
    with pytest.raises(server.WebperfError) as exc:
        server.order_private_test(3843, ["https://www.example.se/a", "https://www.example.se/b"])
    assert "Not enough credits" in str(exc.value)
    assert "body" not in seen


def test_private_result_reports_progress(monkeypatch):
    def handler(request):
        if request.url.path == "/v1/tests":
            return json_response(200, _catalogue())
        return json_response(200, {"data": [
            {"test_date": "x", "type_of_test": 2, "rating": 4.0, "check_report": "r"},
            {"test_date": "x", "type_of_test": 21, "rating": 3.5, "check_report": "r"},
        ]})

    install(handler)
    out = server.get_private_test_result(3843, 1000)
    assert (out["tests_done"], out["tests_expected"], out["complete"]) == (2, 2, True)
    assert out["pending_types"] == []


def test_private_raw_data_requests_the_flag_and_parses(monkeypatch):
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return json_response(200, {"data": [
            {"test_date": "x", "type_of_test": 21, "rating": 3.0, "check_report": "r",
             "json_check_data": "{\"a\": 1}"}]})

    install(handler)
    out = server.get_private_raw_check_data(3843, 1000, 21)
    assert seen["params"] == {"type_of_test": "21", "include_raw_data": "true"}
    assert out["raw_check_data"] == {"a": 1}
    with pytest.raises(server.WebperfError):
        server.get_private_raw_check_data(3843, 1000, 15)


def test_order_private_test_is_not_registered_by_default():
    assert "order_private_test" not in {t.name for t in server.mcp._tool_manager.list_tools()}


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


# --- CrUX ------------------------------------------------------------------


def _crux_body(rows, **extra):
    return {"premium_active": True, "latest_period_end": "2026-10-09",
            "result_count": len(rows), "data": rows, **extra}


def test_crux_weekly_defaults_to_phone_and_core_web_vitals():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        row = {"metric": "largest_contentful_paint", "p75": 2900.0}
        return json_response(200, _crux_body([row]))

    install(handler)
    result = server.get_crux(3843)
    assert seen["path"] == "/0.1/crux/3843"
    assert seen["params"] == {
        "form_factor": "phone",
        "metric": "largest_contentful_paint,cumulative_layout_shift,interaction_to_next_paint",
    }
    assert result["granularity"] == "weekly"
    assert result["result_count"] == 1
    assert result["latest_period_end"] == "2026-10-09"


def test_crux_daily_defaults_to_all_devices_and_passes_filters():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return json_response(200, _crux_body([]))

    install(handler)
    server.get_crux(7317, granularity="daily", metric="first_contentful_paint",
                    from_date="2026-10-01")
    assert seen["path"] == "/0.1/crux_daily/7317"
    assert seen["params"] == {"form_factor": "all", "metric": "first_contentful_paint",
                              "from": "2026-10-01"}


def test_crux_empty_daily_explains_why():
    install(lambda request: json_response(200, _crux_body([], premium_active=False)))
    result = server.get_crux(1, granularity="daily")
    assert result["premium_active"] is False
    assert "Webperf Cloud is not active" in result["note"]


@pytest.mark.parametrize("kwargs", [
    {"granularity": "monthly"},
    {"granularity": "weekly", "form_factor": "all"},
    {"granularity": "daily", "form_factor": "tablet"},
])
def test_crux_rejects_bad_arguments_without_calling_the_api(kwargs):
    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("API should not be called")

    install(handler)
    with pytest.raises(server.WebperfError) as exc:
        server.get_crux(1, **kwargs)
    assert exc.value.kind == "bad_request"
