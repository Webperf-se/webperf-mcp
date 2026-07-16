"""
Webperf MCP server (stdio).

A thin, read-only client over the public api.webperf.se HTTP API. It adds no
privileges of its own: every request carries the user's personal premium
`api-key`, and all authorization (which sites a key may see, via `users_access`)
is enforced server-side by api.webperf.se. The key never leaves the user's
machine except as the `api-key` header to https://api.webperf.se.

Run it over stdio from any MCP client (Claude Desktop, etc.). No hosting needed.
"""

from __future__ import annotations

import html
import json
import os
import re
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# `or` rather than a .get() default: an MCP client may pass these through as
# empty strings when the user leaves an optional field blank.
API_BASE = (os.environ.get("WEBPERF_API_BASE") or "https://api.webperf.se").rstrip("/")
HTTP_TIMEOUT = float(os.environ.get("WEBPERF_HTTP_TIMEOUT") or "30")

# Mirrors api.webperf.se's own format check (10–100 alphanumeric chars).
_API_KEY_RE = re.compile(r"^[a-zA-Z0-9]{10,100}$")

mcp = FastMCP("webperf")


class WebperfError(Exception):
    """Raised with a human-readable message that is safe to show the user."""


def _get_api_key() -> str:
    key = os.environ.get("WEBPERF_API_KEY", "").strip()
    if not key:
        raise WebperfError(
            "No API key configured. Set the WEBPERF_API_KEY environment variable "
            "in your MCP client config to your premium api.webperf.se key."
        )
    if not _API_KEY_RE.match(key):
        raise WebperfError(
            "WEBPERF_API_KEY has an invalid format (expected 10–100 alphanumeric "
            "characters). Check the key you copied from api.webperf.se."
        )
    return key


def _request(method: str, path: str, *, auth: bool = True, **kwargs: Any) -> Any:
    """Make an HTTP call and translate failures into friendly messages."""
    headers = dict(kwargs.pop("headers", {}))
    if auth:
        headers["api-key"] = _get_api_key()
    headers.setdefault("Accept", "application/json")
    url = f"{API_BASE}{path}"
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.request(method, url, headers=headers, **kwargs)
    except httpx.RequestError as exc:
        raise WebperfError(f"Could not reach {API_BASE}: {exc}") from exc

    if resp.status_code == 401:
        raise WebperfError("Unauthorized (401): the API key is missing or invalid.")
    if resp.status_code == 403:
        raise WebperfError(
            "Forbidden (403): your API key does not have access to this site."
        )
    if resp.status_code == 404:
        raise WebperfError("Not found (404): no such site or resource.")
    if resp.status_code == 429:
        raise WebperfError(
            "Rate limit exceeded (429). Premium keys allow 200 requests/hour — "
            "wait a bit and try again."
        )
    if resp.status_code >= 400:
        raise WebperfError(f"API error {resp.status_code}: {resp.text[:300]}")

    try:
        return resp.json()
    except ValueError as exc:
        raise WebperfError("API returned a non-JSON response.") from exc


def _parse_check_data(raw: Any) -> Any:
    """Turn the stored json_check_data into clean, parsed JSON.

    The API stores this field as an HTML-escaped JSON string (e.g. `&quot;`
    instead of `"`). Unescape it and parse it back to real JSON so the model
    sees structured data, not escaped-string noise. If it is not a parseable
    string, return it unchanged.
    """
    if not isinstance(raw, str):
        return raw
    unescaped = html.unescape(raw)
    try:
        return json.loads(unescaped)
    except ValueError:
        return unescaped


def _site_id(site_id: int) -> int:
    """Validate a site id argument."""
    try:
        sid = int(site_id)
    except (TypeError, ValueError):
        raise WebperfError("site_id must be an integer.")
    if sid <= 0:
        raise WebperfError("site_id must be a positive integer.")
    return sid


@mcp.tool()
def list_my_sites() -> dict:
    """List the websites your premium API key can access.

    Returns each site's numeric id plus links to its current and historical
    stats. Access is scoped server-side to exactly the sites granted to your
    key, so this only ever shows sites you are entitled to see.
    """
    data = _request("GET", "/0.1/stats/")
    sites = []
    for s in data.get("sites", []):
        sites.append(
            {
                "site_id": s.get("site-ID"),
                "uri": s.get("uri"),
                "current_stats_endpoint": s.get("endpoint-current-stats"),
                "historic_stats_endpoint": s.get("endpoint-historic-stats"),
            }
        )
    return {"count": len(sites), "sites": sites}


@mcp.tool()
def get_latest_results(site_id: int, type_of_test: int | None = None) -> dict:
    """Get the most recent test results for a site you have access to.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
        type_of_test: Optional numeric test type to return just one test's
            results (see list_test_types). Omit to return all test types.

    Returns the latest run per test type, including ratings and the per-area
    reports (accessibility, performance, standards, security). Use
    list_test_types() to map the numeric `type_of_test` to a readable name.

    This intentionally omits the large raw machine data (`json_check_data`),
    which can be hundreds of KB per test. If the user explicitly asks for the
    raw underlying audit data for a specific test, call get_raw_check_data().
    """
    sid = _site_id(site_id)
    data = _request("GET", f"/0.1/stats/{sid}")
    results = []
    want = str(type_of_test) if type_of_test is not None else None
    for t in data.get("data", []):
        # The API returns type_of_test as a string; compare as strings.
        if want is not None and str(t.get("type_of_test")) != want:
            continue
        results.append(
            {
                "test_date": t.get("test_date"),
                "type_of_test": t.get("type_of_test"),
                "rating": t.get("rating"),
                "report_overall": t.get("check_report"),
                "report_a11y": t.get("check_report_a11y"),
                "report_performance": t.get("check_report_perf"),
                "report_standards": t.get("check_report_stand"),
                "report_security": t.get("check_report_sec"),
                "has_raw_check_data": bool(t.get("json_check_data")),
            }
        )
    return {
        "site_id": sid,
        "uri": data.get("uri"),
        "result_count": len(results),
        "results": results,
        "note": (
            "Raw audit data is omitted to keep responses small. Use "
            "get_raw_check_data(site_id, type_of_test) to fetch it for one test."
        ),
    }


@mcp.tool()
def get_raw_check_data(site_id: int, type_of_test: int) -> dict:
    """Get the raw underlying audit data for ONE test on a site.

    Only call this when the user explicitly asks for the raw/detailed machine
    data behind a score — it can be hundreds of KB. get_latest_results() is
    the right tool for normal questions about scores and reports.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
        type_of_test: Numeric test type to fetch raw data for (see
            list_test_types and the `type_of_test` field of get_latest_results).

    Returns the parsed `json_check_data` for the single most recent run of that
    test type. Returns raw_check_data=None if that test has no raw data.
    """
    sid = _site_id(site_id)
    data = _request("GET", f"/0.1/stats/{sid}")
    want = str(type_of_test)
    for t in data.get("data", []):
        # The API returns type_of_test as a string; compare as strings.
        if str(t.get("type_of_test")) != want:
            continue
        raw = t.get("json_check_data")
        return {
            "site_id": sid,
            "uri": data.get("uri"),
            "type_of_test": type_of_test,
            "test_date": t.get("test_date"),
            "raw_check_data": _parse_check_data(raw),
        }
    raise WebperfError(
        f"No test of type {type_of_test} found for site {sid}. "
        "Use get_latest_results() to see which test types are available."
    )


@mcp.tool()
def get_test_history(site_id: int) -> dict:
    """Get historical monthly scores for a site you have access to.

    Args:
        site_id: Numeric id of the site (from list_my_sites).

    Returns a time series of scores per score type and time period — useful for
    trend analysis over time.
    """
    sid = _site_id(site_id)
    data = _request("GET", f"/0.1/stats_per_month/{sid}")
    history = []
    for s in data.get("data", []):
        history.append(
            {
                "type_of_score": s.get("type_of_score"),
                "score": s.get("score"),
                "timeperiod": s.get("timeperiod"),
            }
        )
    return {
        "site_id": sid,
        "uri": data.get("uri"),
        "result_count": len(history),
        "history": history,
    }


@mcp.tool()
def list_test_types(lang: str = "sv", active_only: bool = True) -> dict:
    """List Webperf Core test types to interpret numeric test ids.

    This is open data and needs no API key. Use it to translate the
    `type_of_test` values returned by get_latest_results into readable names.

    Args:
        lang: Language for names: sv, en, da, no, fi, or is. Default sv.
        active_only: If true, only currently-active tests. Default true.
    """
    lang = (lang or "sv").lower()
    if lang not in {"sv", "en", "da", "no", "fi", "is"}:
        raise WebperfError("lang must be one of: sv, en, da, no, fi, is.")
    params = {"lang": lang}
    if active_only:
        params["active"] = "true"
    data = _request("GET", "/v1/tests", auth=False, params=params)
    tests = [
        {
            "id": t.get("id"),
            "name": t.get("name"),
            "active": t.get("active"),
            "description": t.get("description"),
        }
        for t in data.get("tests", [])
    ]
    return {"count": len(tests), "lang": lang, "tests": tests}


def main() -> None:
    """Console entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
