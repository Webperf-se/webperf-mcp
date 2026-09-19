"""
Webperf MCP server (stdio).

A thin, read-only client over the public api.webperf.se HTTP API. It adds no
privileges of its own: keyed requests carry the user's personal `api-key`
header, and all authorization (which sites a key may see, via `users_access`)
is enforced server-side by api.webperf.se. The key never leaves the user's
machine except as that header, over HTTPS, to the configured API base.

Two groups of tools:

* Open data, no key needed: public-sector sites and their ratings, site
  search, aggregate statistics, and the test-type catalogue.
* Your own sites, key needed: the sites a key reaches, latest and historical
  results, raw audit data, quota, categories, private tests and audits.

Run it over stdio from any MCP client (Claude Desktop, etc.). No hosting needed.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# The .mcpb bundle runs this file as a bare script (`uv run src/webperf_mcp/server.py`),
# so make the package importable for the version constant either way.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from webperf_mcp import __version__  # noqa: E402

DEFAULT_API_BASE = "https://api.webperf.se"
TRUSTED_HOST_SUFFIXES = (".webperf.se", ".webperf.cloud")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Mirrors api.webperf.se's own format check (10-100 alphanumeric chars).
_API_KEY_RE = re.compile(r"^[a-zA-Z0-9]{10,100}$")

# Report JSON files an audit can expose. Mirrors the API's allowlist, minus the
# zip bundle, which is not useful as tool output.
AUDIT_JSON_FILES = (
    "accessibility_summary.json",
    "performance_summary.json",
    "seo_summary.json",
    "best-practices_summary.json",
    "crux_summary.json",
    "privacy_summary.json",
)

TEST_TYPE_LANGS = ("sv", "en", "da", "no", "fi", "is")

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

INSTRUCTIONS = """\
Read-only access to api.webperf.se, the Swedish website-quality service.

Without an API key you can use the open-data tools: list_public_sector_ratings,
search_public_sites, get_public_site, get_public_stats and list_test_types.
Public data covers Swedish municipalities and regions only.

With an API key (Webperf Cloud Standard or Agency) you can also read the user's
own sites. Typical flow: check_api_key once if anything fails, list_my_sites to
find site ids, get_latest_results for scores and readable reports, get_test_history
for trends, list_test_types to turn numeric type_of_test ids into names. Only call
get_raw_check_data when the user explicitly wants the raw machine data behind a
score; it is large.

Ratings are on a 1-5 scale where 5 is best. -1 means not rated.
Nothing here can start a test, change a setting or delete anything.
"""


class WebperfError(Exception):
    """Raised with a human-readable message that is safe to show the user.

    `kind` classifies the failure so tools like check_api_key can branch on it:
    config, auth, forbidden, not_found, bad_request, rate_limited, unreachable,
    server, invalid_response.
    """

    def __init__(self, message: str, kind: str = "error", status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _resolve_api_base() -> tuple[str, str | None]:
    """Return (base_url, config_error).

    The key is sent as a header to whatever base is configured, so refuse
    plain http for anything but localhost. A non-Webperf https host is
    allowed (staging, proxies) but flagged on stderr so it is never silent.
    """
    raw = (os.environ.get("WEBPERF_API_BASE") or DEFAULT_API_BASE).strip().rstrip("/")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if not parsed.scheme or not host:
        return raw, f"WEBPERF_API_BASE is not a valid URL: {raw!r}"
    if host not in LOCAL_HOSTS and parsed.scheme != "https":
        return raw, (
            f"WEBPERF_API_BASE must use https (got {raw!r}); the API key would "
            "otherwise be sent in clear text."
        )
    trusted = host == "webperf.se" or host.endswith(TRUSTED_HOST_SUFFIXES) or host in LOCAL_HOSTS
    if not trusted:
        print(
            f"webperf-mcp: WARNING: sending the API key to non-Webperf host {host!r} "
            "because WEBPERF_API_BASE is set.",
            file=sys.stderr,
        )
    return raw, None


def _float_env(name: str, default: float, env: dict[str, str] | None = None) -> float:
    """Read a non-negative float from the environment, or fall back to `default`.

    A typo in an MCP client config must not stop the server from starting:
    an unparseable or negative value is reported on stderr and ignored, the
    same way an invalid WEBPERF_API_BASE is.
    """
    raw = (env if env is not None else os.environ).get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if value < 0:
        print(
            f"webperf-mcp: ignoring {name}={raw!r} (expected a non-negative number); "
            f"using {default:g}.",
            file=sys.stderr,
        )
        return default
    return value


API_BASE, _CONFIG_ERROR = _resolve_api_base()
HTTP_TIMEOUT = _float_env("WEBPERF_HTTP_TIMEOUT", 30.0)
CACHE_TTL = _float_env("WEBPERF_CACHE_TTL", 60.0)
USER_AGENT = f"webperf-mcp/{__version__} (+https://github.com/Webperf-se/webperf-mcp)"

# The cache lives for the whole life of a stdio process, so it needs a ceiling:
# a single raw-audit response can be hundreds of KB.
CACHE_MAX_ENTRIES = 128

# /0.1/stats/{id} applies LIMIT 30 server-side and says nothing about it in
# the body, so a full page means the list may be incomplete.
API_MAX_TEST_ROWS = 30

_CLIENT: httpx.Client | None = None
_cache: dict[str, tuple[float, Any]] = {}


def _make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        transport=transport,
    )


def _http() -> httpx.Client:
    """One shared client: connection reuse and a stable User-Agent."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _make_client()
    return _CLIENT


def _get_api_key() -> str:
    key = os.environ.get("WEBPERF_API_KEY", "").strip()
    if not key:
        raise WebperfError(
            "No API key configured. This tool needs a Webperf Cloud API key: set "
            "WEBPERF_API_KEY in your MCP client config, or enter it in the extension "
            "settings. The open-data tools work without one.",
            kind="auth",
        )
    if not _API_KEY_RE.match(key):
        raise WebperfError(
            "WEBPERF_API_KEY has an invalid format (expected 10-100 alphanumeric "
            "characters). Copy the key again from your Webperf Cloud account page.",
            kind="auth",
        )
    return key


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _message_from(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict):
        for field in ("message", "error"):
            if isinstance(body.get(field), str):
                return body[field]
    return resp.text[:300]


def _translate_status(resp: httpx.Response) -> WebperfError | None:
    """Map an HTTP failure to a friendly error, or None for success."""
    code = resp.status_code
    if code < 400:
        return None
    msg = _message_from(resp)
    lowered = msg.lower()

    # The older list endpoints answer 400, not 401, for a missing/invalid key.
    if code == 401 or (code == 400 and "api key" in lowered):
        return WebperfError(
            "API key rejected by api.webperf.se. The key is missing, mistyped, revoked "
            "or has API access disabled. Run check_api_key for details.",
            kind="auth",
            status=code,
        )
    if code == 400:
        return WebperfError(f"Bad request (400): {msg}", kind="bad_request", status=code)
    if code == 403:
        return WebperfError(
            "Forbidden (403): your API key does not have access to this site.",
            kind="forbidden",
            status=code,
        )
    if code == 404:
        return WebperfError(
            f"Not found (404): {msg or 'no such site or resource'}", kind="not_found", status=code
        )
    if code == 429:
        retry_after = resp.headers.get("Retry-After")
        limit = resp.headers.get("X-RateLimit-Limit")
        parts = ["Rate limited by api.webperf.se (429)."]
        if limit:
            parts.append(f"This endpoint allows {limit} requests per window.")
        if retry_after:
            parts.append(f"Retry after {retry_after} seconds.")
        else:
            parts.append("Wait a minute and try again.")
        return WebperfError(" ".join(parts), kind="rate_limited", status=code)
    if code >= 500:
        return WebperfError(
            f"api.webperf.se returned a server error ({code}). Try again shortly.",
            kind="server",
            status=code,
        )
    return WebperfError(f"API error {code}: {msg}", kind="error", status=code)


def _request(
    method: str,
    path: str,
    *,
    auth: bool = True,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    cache: bool = True,
) -> Any:
    """Make an HTTP call and translate failures into friendly messages.

    GET responses are cached in-process for CACHE_TTL seconds, keyed on the
    full request, so a follow-up question about the same site does not hit
    the API (and its rate limit) twice.
    """
    if _CONFIG_ERROR:
        raise WebperfError(_CONFIG_ERROR, kind="config")

    hdrs = dict(headers or {})
    if auth:
        hdrs["api-key"] = _get_api_key()

    url = f"{API_BASE}{path}"
    cache_key = json.dumps([method, url, params or {}, sorted(hdrs.items())], sort_keys=True)
    cacheable = cache and method == "GET" and CACHE_TTL > 0
    if cacheable:
        hit = _cache.get(cache_key)
        if hit and hit[0] > time.monotonic():
            return hit[1]

    try:
        resp = _http().request(method, url, headers=hdrs, params=params)
    except httpx.TimeoutException as exc:
        raise WebperfError(
            f"Timed out after {HTTP_TIMEOUT:.0f}s waiting for {API_BASE}.", kind="unreachable"
        ) from exc
    except httpx.RequestError as exc:
        raise WebperfError(
            f"Could not reach {API_BASE}: {exc.__class__.__name__}.", kind="unreachable"
        ) from exc

    err = _translate_status(resp)
    if err:
        raise err

    try:
        data = resp.json()
    except ValueError as exc:
        raise WebperfError("API returned a non-JSON response.", kind="invalid_response") from exc

    if cacheable:
        _evict()
        _cache[cache_key] = (time.monotonic() + CACHE_TTL, data)
    return data


def _evict() -> None:
    """Drop lapsed entries, then the oldest ones if still over the ceiling."""
    now = time.monotonic()
    for key in [k for k, (expires, _) in _cache.items() if expires <= now]:
        del _cache[key]
    # dicts keep insertion order, so the head is the least recently stored.
    while len(_cache) >= CACHE_MAX_ENTRIES:
        del _cache[next(iter(_cache))]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unescape(text: Any) -> Any:
    """The API HTML-escapes every report field; give the model plain text."""
    if isinstance(text, str):
        return html.unescape(text)
    return text


def _parse_check_data(raw: Any) -> Any:
    """Turn the stored json_check_data into clean, parsed JSON.

    The API stores this field as an HTML-escaped JSON string. Unescape it and
    parse it back to real JSON so the model sees structured data. If it is not
    a parseable string, return it unescaped.
    """
    if not isinstance(raw, str):
        return raw
    unescaped = html.unescape(raw)
    try:
        return json.loads(unescaped)
    except ValueError:
        return unescaped


def _site_id(site_id: Any) -> int:
    try:
        sid = int(site_id)
    except (TypeError, ValueError):
        raise WebperfError("site_id must be an integer.", kind="bad_request") from None
    if sid <= 0:
        raise WebperfError("site_id must be a positive integer.", kind="bad_request")
    return sid


def _rating(value: Any) -> float | None:
    """API ratings arrive as strings; -1 and 0 mean 'not rated'."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def _site_headers(site_id: int) -> dict[str, str]:
    return {"site-id": str(site_id)}


def _shape_test(t: dict) -> dict:
    return {
        "test_date": t.get("test_date"),
        "type_of_test": t.get("type_of_test"),
        "rating": _rating(t.get("rating")),
        "report_overall": _unescape(t.get("check_report")),
        "report_a11y": _unescape(t.get("check_report_a11y")),
        "report_performance": _unescape(t.get("check_report_perf")),
        "report_standards": _unescape(t.get("check_report_stand")),
        "report_security": _unescape(t.get("check_report_sec")),
    }


def _shape_public_site(s: dict) -> dict:
    return {
        "site_id": s.get("id"),
        "title": s.get("title") or s.get("name"),
        "website": s.get("website"),
        "category": s.get("category_name"),
        "wikidata": s.get("same_as") or None,
        "ratings": s.get("ratings"),
    }


def _audit_folder(content: Any) -> str | None:
    """Folder name from an audit's content blob (JSON or a Python dict string)."""
    if not isinstance(content, str) or not content:
        return None
    match = re.search(r"['\"]folder['\"]\s*:\s*['\"]([^'\"]+)['\"]", content)
    if not match:
        return None
    name = match.group(1).strip("/").rsplit("/", 1)[-1]
    return name or None


mcp = FastMCP("webperf", instructions=INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Open data: no key needed
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def list_test_types(lang: str = "sv", active_only: bool = True) -> dict:
    """List Webperf Core test types to interpret numeric test ids.

    Open data, no API key needed. Use it to translate the `type_of_test`
    values returned by get_latest_results into readable names.

    Args:
        lang: Language for names: sv, en, da, no, fi, or is. Default sv.
        active_only: If true, only currently-active tests. Default true.
    """
    lang = (lang or "sv").lower()
    if lang not in TEST_TYPE_LANGS:
        raise WebperfError(
            f"lang must be one of: {', '.join(TEST_TYPE_LANGS)}.", kind="bad_request"
        )
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


@mcp.tool(annotations=READ_ONLY)
def list_public_sector_ratings(kind: str = "municipalities") -> dict:
    """List every Swedish municipality or region with its current ratings.

    Open data, no API key needed. Ratings are 1-5 (5 is best) for overall,
    accessibility, performance, standards and security; null means not rated.
    Good for ranking, comparing and benchmarking questions.

    Args:
        kind: "municipalities" (default) or "regions".
    """
    kind = (kind or "municipalities").lower()
    if kind not in ("municipalities", "regions"):
        raise WebperfError('kind must be "municipalities" or "regions".', kind="bad_request")
    data = _request("GET", f"/v1/public/{kind}", auth=False)
    sites = [_shape_public_site(s) for s in data.get(kind, [])]
    return {"kind": kind, "count": len(sites), "license": "CC-BY", "sites": sites}


@mcp.tool(annotations=READ_ONLY)
def search_public_sites(
    query: str = "",
    category: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict:
    """Search the public-sector sites (municipalities and regions) by name or URL.

    Open data, no API key needed. Returns ids you can pass to get_public_site.

    Args:
        query: Text to match against the site title or website. Empty lists all.
        category: Optional "municipality" or "region" to narrow the results.
        limit: Page size, 1-100. Default 25.
        offset: Number of results to skip, for paging. Default 0.
    """
    params: dict[str, Any] = {
        "query": (query or "").strip(),
        "limit": max(1, min(int(limit), 100)),
        "offset": max(0, int(offset)),
    }
    if category:
        cat = category.lower().rstrip("s").replace("ie", "y")  # municipalities -> municipality
        code = {"municipality": 2, "region": 3}.get(cat)
        if code is None:
            raise WebperfError('category must be "municipality" or "region".', kind="bad_request")
        params["category"] = code
    data = _request("GET", "/v1/sites", auth=False, params=params)
    sites = [
        {
            "site_id": s.get("id"),
            "title": s.get("title"),
            "website": s.get("website"),
            "category": s.get("category_name"),
            "wikidata": s.get("same_as") or None,
        }
        for s in data.get("sites", [])
    ]
    return {"count": len(sites), "pagination": data.get("pagination"), "sites": sites}


@mcp.tool(annotations=READ_ONLY)
def get_public_site(site_id: int) -> dict:
    """Get a public-sector site's current ratings and basic facts.

    Open data, no API key needed, but only municipalities and regions are
    public. For any other site use get_site_details with an API key.

    Args:
        site_id: Numeric id from search_public_sites or list_public_sector_ratings.
    """
    sid = _site_id(site_id)
    try:
        data = _request("GET", f"/v1/sites/{sid}", auth=False)
    except WebperfError as exc:
        if exc.kind == "forbidden":
            raise WebperfError(
                f"Site {sid} is not a public-sector site. With an API key that reaches it, "
                "use get_site_details or get_latest_results instead.",
                kind="forbidden",
            ) from None
        raise
    shaped = _shape_public_site(data)
    shaped.update(
        {
            "date_added": data.get("date_added"),
            "last_updated": data.get("last_updated"),
            "recent_test_count": data.get("recent_tests"),
        }
    )
    return shaped


@mcp.tool(annotations=READ_ONLY)
def get_public_stats() -> dict:
    """Aggregate statistics for the public sector: site counts and average ratings.

    Open data, no API key needed.
    """
    data = _request("GET", "/v1/public/stats", auth=False)
    return data.get("statistics", data)


# ---------------------------------------------------------------------------
# Your own sites: key needed
# ---------------------------------------------------------------------------


def _key_failure(exc: WebperfError) -> dict:
    status = {
        "auth": "rejected",
        "rate_limited": "rate_limited",
        "unreachable": "unreachable",
        "config": "misconfigured",
    }.get(exc.kind, "error")
    next_step = {
        "rejected": (
            "Copy the key again from your Webperf Cloud account page and update it in "
            "your MCP client (extension users: Settings > Extensions > Webperf). If it "
            "was recently revoked or regenerated, only the new key works."
        ),
        "rate_limited": "Wait a minute, then run check_api_key again.",
        "unreachable": (
            "api.webperf.se could not be reached. Check your network or proxy; if "
            "WEBPERF_API_BASE is set, make sure it points at a reachable host."
        ),
        "misconfigured": "Fix WEBPERF_API_BASE in your MCP client config.",
        "error": "Try again shortly. If it persists, contact support@webperf.se.",
    }[status]
    return {"ok": False, "status": status, "detail": str(exc), "next_step": next_step}


@mcp.tool(annotations=READ_ONLY)
def check_api_key() -> dict:
    """Check whether the configured Webperf API key works, and what it reaches.

    Safe to call any time: one cheap request, no credits used, and the key
    itself is never included in the result. Returns a status of ok,
    no_sites, no_key_configured, bad_format, rejected, rate_limited,
    unreachable or misconfigured, plus a plain-language next_step.

    Call this first when a keyed tool fails, or when the user asks whether
    their key is set up correctly.
    """
    key = os.environ.get("WEBPERF_API_KEY", "").strip()
    if not key:
        return {
            "ok": False,
            "status": "no_key_configured",
            "next_step": (
                "No key is configured. The open-data tools still work. To read your own "
                "sites, add your Webperf Cloud API key: extension users enter it under "
                "Settings > Extensions > Webperf; manual installs set WEBPERF_API_KEY "
                "in the MCP client config."
            ),
        }
    if not _API_KEY_RE.match(key):
        return {
            "ok": False,
            "status": "bad_format",
            "next_step": (
                "The configured key does not look like a Webperf key (10-100 letters and "
                "digits). Copy it again from your Webperf Cloud account page, watching for "
                "stray spaces or quotes."
            ),
        }

    try:
        who = _request("GET", "/0.1/whoami/", cache=False)
    except WebperfError as exc:
        if exc.kind != "not_found":
            return _key_failure(exc)
        # Older API without /whoami/: the site list is the documented fallback.
        try:
            listing = _request("GET", "/0.1/stats/", cache=False)
        except WebperfError as exc2:
            return _key_failure(exc2)
        count = len(listing.get("sites", []))
        who = {"site_count": count, "unrestricted": False}

    count = int(who.get("site_count") or 0)
    unrestricted = bool(who.get("unrestricted"))
    account = {
        k: who.get(k) for k in ("user_id", "display_name", "role", "unrestricted") if k in who
    }
    if count == 0 and not unrestricted:
        return {
            "ok": True,
            "status": "no_sites",
            "account": account or None,
            "site_count": 0,
            "next_step": (
                "The key is valid but is not granted any sites yet. On a Starter plan, "
                "API access comes with Standard or Agency. Otherwise ask your account "
                "administrator in Webperf Cloud to grant this user access to the sites."
            ),
        }
    return {
        "ok": True,
        "status": "ok",
        "account": account or None,
        "site_count": count,
        "next_step": (
            "Call list_my_sites to see the sites, then get_latest_results for one of them."
        ),
    }


@mcp.tool(annotations=READ_ONLY)
def list_my_sites() -> dict:
    """List the websites your API key can access.

    Returns each site's numeric id, and its title and address where the API
    provides them. Access is scoped server-side to exactly the sites granted
    to your key.
    """
    data = _request("GET", "/0.1/stats/")
    sites = []
    for s in data.get("sites", []):
        site = {"site_id": s.get("site-ID")}
        try:
            site["site_id"] = int(site["site_id"])
        except (TypeError, ValueError):
            pass
        if s.get("title"):
            site["title"] = s.get("title")
        if s.get("website"):
            site["website"] = s.get("website")
        sites.append(site)
    note = None
    if sites and "title" not in sites[0]:
        note = "This API version lists ids only; use get_site_details(site_id) for names."
    result = {"count": len(sites), "sites": sites}
    if note:
        result["note"] = note
    return result


@mcp.tool(annotations=READ_ONLY)
def get_site_details(site_id: int) -> dict:
    """Get a site's title, address, category and current 1-5 ratings.

    Works for any site your API key reaches, not only public-sector ones.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
    """
    sid = _site_id(site_id)
    data = _request("GET", f"/item/{sid}")
    site = data.get("data") or {}
    scores = site.get("scores") or {}
    return {
        "site_id": sid,
        "title": site.get("title"),
        "website": site.get("url"),
        "category": site.get("category"),
        "wikidata": site.get("sameAs") or None,
        "ratings": {
            "overall": _rating(scores.get("overall")),
            "accessibility": _rating(scores.get("a11y")),
            "performance": _rating(scores.get("pagespeed")),
            "standards": _rating(scores.get("webstandard")),
            "security": _rating(scores.get("infosec")),
        }
        if scores
        else None,
    }


@mcp.tool(annotations=READ_ONLY)
def get_latest_results(site_id: int, type_of_test: int | None = None) -> dict:
    """Get the most recent test results for a site you have access to.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
        type_of_test: Optional numeric test type to return just one test's
            results (see list_test_types). Omit to return all test types.

    Returns the latest run per test type, with a 1-5 rating and readable
    reports per area (accessibility, performance, standards, security). Use
    list_test_types() to map the numeric `type_of_test` to a name.

    This intentionally omits the large raw machine data (`json_check_data`).
    If the user explicitly asks for it, call get_raw_check_data().
    """
    sid = _site_id(site_id)
    params: dict[str, Any] = {"include_raw_data": "false"}
    want = str(type_of_test) if type_of_test is not None else None
    if want is not None:
        params["type_of_test"] = want
    data = _request("GET", f"/0.1/stats/{sid}", params=params)
    rows = data.get("data", [])
    results = []
    for t in rows:
        # Older API versions ignore the filter; compare as strings either way.
        if want is not None and str(t.get("type_of_test")) != want:
            continue
        results.append(_shape_test(t))
    # Judge truncation on what the API returned, not on what the filter kept.
    truncated = len(rows) >= API_MAX_TEST_ROWS
    note = (
        "Raw audit data is omitted to keep responses small. Use "
        "get_raw_check_data(site_id, type_of_test) to fetch it for one test."
    )
    if truncated:
        note += (
            f" The API returns at most {API_MAX_TEST_ROWS} rows per site and does not"
            " paginate, so older runs may be missing; pass type_of_test to narrow it."
        )
    return {
        "site_id": sid,
        "result_count": len(results),
        "truncated": truncated,
        "results": results,
        "note": note,
    }


@mcp.tool(annotations=READ_ONLY)
def get_raw_check_data(site_id: int, type_of_test: int) -> dict:
    """Get the raw underlying audit data for ONE test on a site.

    Only call this when the user explicitly asks for the raw/detailed machine
    data behind a score; it can be hundreds of KB. get_latest_results() is
    the right tool for normal questions about scores and reports.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
        type_of_test: Numeric test type to fetch raw data for (see
            list_test_types and the `type_of_test` field of get_latest_results).

    Returns the parsed `json_check_data` for the most recent run of that test
    type, or raw_check_data=null if that test has none.
    """
    sid = _site_id(site_id)
    want = str(int(type_of_test))
    data = _request("GET", f"/0.1/stats/{sid}", params={"type_of_test": want})
    for t in data.get("data", []):
        if str(t.get("type_of_test")) != want:
            continue
        return {
            "site_id": sid,
            "type_of_test": int(want),
            "test_date": t.get("test_date"),
            "raw_check_data": _parse_check_data(t.get("json_check_data")) or None,
        }
    raise WebperfError(
        f"No test of type {want} found for site {sid}. "
        "Use get_latest_results() to see which test types are available.",
        kind="not_found",
    )


@mcp.tool(annotations=READ_ONLY)
def get_test_history(site_id: int) -> dict:
    """Get historical monthly scores for a site you have access to.

    Args:
        site_id: Numeric id of the site (from list_my_sites).

    Returns a time series of scores per score type and month, for trends.
    """
    sid = _site_id(site_id)
    data = _request("GET", f"/0.1/stats_per_month/{sid}")
    history = [
        {
            "type_of_score": s.get("type_of_score"),
            "score": _rating(s.get("score")),
            "timeperiod": s.get("timeperiod"),
        }
        for s in data.get("data", [])
    ]
    return {"site_id": sid, "result_count": len(history), "history": history}


@mcp.tool(annotations=READ_ONLY)
def get_quota(site_id: int) -> dict:
    """Get a site's test credits: monthly cap, used in the last 30 days, and available.

    Check this before suggesting a retest or audit; ordering is done in
    Webperf Cloud, not through this server.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
    """
    sid = _site_id(site_id)
    data = _request("GET", "/0.1/quota/", headers=_site_headers(sid), cache=False)
    return {
        "site_id": sid,
        "monthly_quota": data.get("monthly_quota"),
        "used_last_30_days": data.get("used"),
        "available": data.get("available"),
        "period_days": data.get("period_days"),
    }


@mcp.tool(annotations=READ_ONLY)
def list_categories() -> dict:
    """List the site categories on webperf.se with their average 1-5 ratings.

    Useful as a benchmark: compare one of your sites against its category.
    """
    data = _request("GET", "/0.1/categories/")
    cats = [
        {
            "category_id": c.get("cat_id"),
            "title": c.get("title"),
            "ratings": {
                "overall": _rating(c.get("rating_overall")),
                "accessibility": _rating(c.get("rating_a11y")),
                "performance": _rating(c.get("rating_pagespeed")),
                "standards": _rating(c.get("rating_webstandard")),
                "security": _rating(c.get("rating_infosec")),
            },
        }
        for c in data.get("categories", [])
    ]
    return {"count": len(cats), "categories": cats}


@mcp.tool(annotations=READ_ONLY)
def list_private_tests(site_id: int) -> dict:
    """List the private (non-public) test pages ordered for a site.

    Private tests are one-off tests of individual addresses. Use
    get_private_test_result to read one of them.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
    """
    sid = _site_id(site_id)
    data = _request("GET", f"/0.1/private_stats/{sid}")
    tests = []
    for t in data.get("data", []) or []:
        uri = str(t.get("uri") or "")
        test_id = uri.rstrip("/").rsplit("/", 1)[-1]
        tests.append(
            {
                "private_test_id": int(test_id) if test_id.isdigit() else test_id,
                "url": t.get("url"),
                "date_added": t.get("date_added"),
            }
        )
    return {"site_id": sid, "count": len(tests), "private_tests": tests}


@mcp.tool(annotations=READ_ONLY)
def get_private_test_result(site_id: int, private_test_id: int) -> dict:
    """Get the latest result of one private test.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
        private_test_id: Id from list_private_tests.
    """
    sid = _site_id(site_id)
    tid = _site_id(private_test_id)
    data = _request("GET", f"/0.1/private_stats/{sid}/{tid}")
    results = [
        {
            "test_date": t.get("test_date"),
            "type_of_test": t.get("type_of_test"),
            "rating": _rating(t.get("rating")),
            "report": _unescape(t.get("check_report")),
        }
        for t in data.get("data", []) or []
    ]
    return {
        "site_id": sid,
        "private_test_id": tid,
        "result_count": len(results),
        "results": results,
    }


@mcp.tool(annotations=READ_ONLY)
def list_audits(site_id: int) -> dict:
    """List completed audits (Lighthouse, CrUX, privacy summaries) for a site.

    Each audit lists the report files it has; read one with get_audit_file.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
    """
    sid = _site_id(site_id)
    # A read-only listing that the API happens to expose as POST.
    data = _request("POST", "/0.1/audits/", headers=_site_headers(sid), cache=False)
    audits = []
    for a in data.get("data", []) or []:
        files = a.get("result_files") or {}
        audits.append(
            {
                "audit_id": a.get("result_id"),
                "title": a.get("title"),
                "format": a.get("format"),
                "date_modified": a.get("date_modified"),
                "folder": _audit_folder(a.get("content")),
                "files": sorted(files.keys()) if isinstance(files, dict) else files,
            }
        )
    return {"site_id": sid, "count": len(audits), "audits": audits}


@mcp.tool(annotations=READ_ONLY)
def get_audit_file(site_id: int, folder: str, file: str) -> dict:
    """Read one JSON summary file from an audit.

    Args:
        site_id: Numeric id of the site the audit belongs to.
        folder: The audit's folder name, from list_audits.
        file: One of accessibility_summary.json, performance_summary.json,
            seo_summary.json, best-practices_summary.json, crux_summary.json,
            privacy_summary.json.
    """
    sid = _site_id(site_id)
    folder = (folder or "").strip().strip("/")
    file = (file or "").strip()
    if not folder or "/" in folder or folder in (".", ".."):
        raise WebperfError(
            "folder must be a single folder name from list_audits.", kind="bad_request"
        )
    if file not in AUDIT_JSON_FILES:
        raise WebperfError(
            f"file must be one of: {', '.join(AUDIT_JSON_FILES)}.", kind="bad_request"
        )
    data = _request(
        "GET",
        "/0.1/audit_file/",
        headers=_site_headers(sid),
        params={"folder": folder, "file": file},
    )
    return {"site_id": sid, "folder": folder, "file": file, "content": data}


def main() -> None:
    """Console entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
