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
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

# The .mcpb bundle runs this file as a bare script (`uv run src/webperf_mcp/server.py`),
# so make the package importable for the version constant either way.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from webperf_mcp import __version__  # noqa: E402
from webperf_mcp import campaign as tagging  # noqa: E402

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

# order_retest spends the user's credits and queues work on Webperf's servers.
# No readOnlyHint, and destructiveHint so that clients ask before each call.
ORDERING = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)


def _bool_env(name: str, env: dict[str, str] | None = None) -> bool:
    raw = (env if env is not None else os.environ).get(name, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# Ordering is opt-in per install (WEBPERF_ALLOW_ORDERING=true). Off, the server
# is strictly read-only and order_retest is not registered at all.
ORDERING_ENABLED = _bool_env("WEBPERF_ALLOW_ORDERING")

# Saving the campaign convention is a separate opt-in. It spends nothing, but
# it changes what everyone in the organisation is allowed to tag with.
CONVENTION_EDITS_ENABLED = _bool_env("WEBPERF_ALLOW_CONVENTION_EDITS")

# save_campaign_convention replaces the whole convention: clients should ask.
CONVENTION_REPLACE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)

# add_campaign_value only adds to it.
CONVENTION_ADD = ToolAnnotations(
    readOnlyHint=False,
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
for trends, get_crux for real-visitor Core Web Vitals (LCP, CLS, INP) from Chrome,
list_test_types to turn numeric type_of_test ids into names. Only call
get_raw_check_data when the user explicitly wants the raw machine data behind a
score; it is large. The same goes for get_private_raw_check_data.

Ratings are on a 1-5 scale where 5 is best. -1 means not rated.

Campaign tagging (UTM/MTM): never write a tagged URL yourself. Use
build_campaign_url, which only accepts values from the organisation's naming
convention and reports every value it rewrote; pass those notes on. When a
value is rejected, offer the suggested or allowed values. Adding a new value
to the convention is the user's decision, not yours. check_campaign_urls
audits links people already use. With an API key the convention lives in
Webperf Cloud per site (pass site_id); without one, in the local JSON file
named by WEBPERF_CAMPAIGN_CONVENTION. If no convention exists yet, follow the
setup_campaign_convention prompt with the user before tagging anything.
"""

CONVENTION_EDITS_ON = """\
Editing the campaign convention is enabled. save_campaign_convention replaces
the whole convention and add_campaign_value adds one value. Only call them
after the user has agreed to the exact change in the conversation.
"""

CONVENTION_EDITS_OFF = """\
Editing the campaign convention is off for this install. To save one, the user
turns on "Allow editing the campaign convention" in the extension settings
(WEBPERF_ALLOW_CONVENTION_EDITS=true).
"""

INSTRUCTIONS_ORDERING = """\
Ordering is enabled for this install. order_retest queues new tests for one of
the user's sites and spends credits (1 per test), so only call it when the user
explicitly asks for a new test, never to refresh data on your own. Before
ordering, check get_retest_status: a test that is queued or running must not be
ordered again. After ordering, follow up with get_retest_status at most once a
minute and say that results normally take a few minutes, longer when the queue
is long.

order_private_test runs the whole test suite on individual pages of the user's
site and costs 15 credits per page, so state the cost before calling it. Pages
ordered in the last 24 hours are skipped automatically. More than five pages
require confirm_large_order=true, and you may only set it after the user has
confirmed the number of pages and the cost in the conversation, never on your
own. Results arrive test type by test type over 15 to 30 minutes; read them with
get_private_test_result, which says how many test types are done.
"""


def _instructions(ordering: bool, edits: bool) -> str:
    text = INSTRUCTIONS + (INSTRUCTIONS_ORDERING if ordering else "")
    text += CONVENTION_EDITS_ON if edits else CONVENTION_EDITS_OFF
    if not ordering and not edits:
        return text + "Nothing here can start a test, change a setting or delete anything.\n"
    if not ordering:
        return text + "Nothing here can start a test or delete anything.\n"
    return text + "Nothing here deletes anything.\n"


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
                errors = body.get("errors")
                if isinstance(errors, list) and errors:
                    return f"{body[field]}: " + "; ".join(str(e) for e in errors)
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
    if code == 409:
        return WebperfError(f"Conflict (409): {msg}", kind="conflict", status=code)
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
    data: dict[str, str] | None = None,
    json_body: Any = None,
) -> Any:
    """Make an HTTP call and translate failures into friendly messages.

    `data` is sent as a form body (the legacy order endpoints read request.form),
    `json_body` as JSON (the context endpoints).

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
        resp = _http().request(method, url, headers=hdrs, params=params, data=data, json=json_body)
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


mcp = MCPServer("webperf", instructions=_instructions(ORDERING_ENABLED, CONVENTION_EDITS_ENABLED))


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
            # retestable: the test servers run it, so it can be ordered as a
            # retest. public_sector_only: orderable only for public-sector sites.
            "retestable": t.get("retestable"),
            "public_sector_only": t.get("public_sector_only"),
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
    unreachable or misconfigured, plus a plain-language next_step,
    ordering_enabled, which says whether this install may order tests, and
    convention_edits_enabled, which says whether it may save the campaign
    convention.

    Call this first when a keyed tool fails, or when the user asks whether
    their key is set up correctly.
    """
    result = _check_api_key()
    result["ordering_enabled"] = ORDERING_ENABLED
    result["convention_edits_enabled"] = CONVENTION_EDITS_ENABLED
    return result


def _check_api_key() -> dict:
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


CRUX_GRANULARITIES = {
    # granularity: (endpoint, allowed form factors, default form factor)
    "weekly": ("/0.1/crux/{sid}", ("phone", "desktop", "tablet"), "phone"),
    "daily": ("/0.1/crux_daily/{sid}", ("all", "phone", "desktop"), "all"),
}
# Core Web Vitals in the order Webperf always shows them.
CRUX_DEFAULT_METRICS = "largest_contentful_paint,cumulative_layout_shift,interaction_to_next_paint"


@mcp.tool(annotations=READ_ONLY)
def get_crux(
    site_id: int,
    granularity: str = "weekly",
    form_factor: str | None = None,
    metric: str | None = None,
    from_date: str | None = None,
) -> dict:
    """Get Chrome UX Report (CrUX) field data: how real Chrome visitors experience a site.

    Each row summarises a 28-day window. Consecutive windows overlap almost
    entirely, so compare a window with the one ending 28 days earlier, not
    with the previous day or week.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
        granularity: "weekly" (windows ending on Saturdays, history going back
            further than Google keeps it, any site you can access) or "daily"
            (one window per day for the last 90 days, only for sites with an
            active Webperf Cloud). Default weekly.
        form_factor: Comma-separated device types. Weekly: phone, desktop,
            tablet (default phone, which Google uses to assess Core Web
            Vitals). Daily: all, phone, desktop (default all, which small
            sites most often have data for).
        metric: Comma-separated Google metric names. Default LCP, CLS and INP
            (largest_contentful_paint, cumulative_layout_shift,
            interaction_to_next_paint). Others: first_contentful_paint,
            experimental_time_to_first_byte, round_trip_time and the
            largest_contentful_paint_image_* breakdown.
        from_date: Only windows ending on this date or later (YYYY-MM-DD).

    p75 is in milliseconds except for cumulative_layout_shift, which has no
    unit. Google only publishes data for sites with enough Chrome visits, so
    days, device types or single metrics can be missing on smaller sites.
    """
    sid = _site_id(site_id)
    key = (granularity or "weekly").strip().lower()
    if key not in CRUX_GRANULARITIES:
        raise WebperfError('granularity must be "weekly" or "daily".', kind="bad_request")
    path, allowed, default_ff = CRUX_GRANULARITIES[key]

    form_factors = (form_factor or default_ff).strip().lower()
    unknown = [f for f in form_factors.split(",") if f.strip() and f.strip() not in allowed]
    if unknown:
        raise WebperfError(
            f"form_factor for {key} data must be one or more of: {', '.join(allowed)}.",
            kind="bad_request",
        )
    params: dict[str, Any] = {
        "form_factor": form_factors,
        "metric": (metric or CRUX_DEFAULT_METRICS).strip(),
    }
    if from_date:
        params["from"] = from_date.strip()

    data = _request("GET", path.format(sid=sid), params=params)
    rows = data.get("data", [])
    note = (
        "Each row is a 28-day window. Compare with the window ending 28 days "
        "earlier; adjacent windows overlap and their differences are mostly noise."
    )
    if not rows and key == "daily":
        note += (
            " No daily data: either Webperf Cloud is not active for this site"
            " (see premium_active) or Google has too few Chrome visits to report."
        )
    elif not rows:
        note += " No data: Google has too few Chrome visits for this site to report."
    return {
        "site_id": sid,
        "granularity": key,
        "premium_active": data.get("premium_active"),
        "latest_period_end": data.get("latest_period_end"),
        "result_count": len(rows),
        "rows": rows,
        "note": note,
    }


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
def get_retest_status(site_id: int, queue_id: int | None = None) -> dict:
    """Where a site's ordered retests are in the queue: queued (with position),
    running, done (with a pointer to the result) or failed (credit refunded).

    Use this instead of ordering again when the user asks whether a retest is
    finished. Ordering itself is done in Webperf Cloud, not through this server.
    Poll at most once a minute.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
        queue_id: Optional queue id from an order, to look at one retest only.
    """
    sid = _site_id(site_id)
    path = "/0.1/retest/" if queue_id is None else f"/0.1/retest/{int(queue_id)}"
    data = _request("GET", path, headers=_site_headers(sid), cache=False)
    retests = data.get("retests") if queue_id is None else [data.get("retest")]
    return {
        "site_id": sid,
        "queue_length": data.get("queue_length"),
        "retests": [
            {
                "queue_id": r.get("queue_id"),
                "test_num": r.get("test_num"),
                "status": r.get("status"),
                "position": r.get("position"),
                "created": r.get("created"),
                "started": r.get("started"),
                "finished": r.get("finished"),
                "refunded": r.get("refunded"),
                "result": r.get("result"),
            }
            for r in retests
            if r
        ],
    }


def _catalogue_flags() -> tuple[set[int], set[int]]:
    """(retestable ids, public-sector-only ids) from the test catalogue."""
    data = _request("GET", "/v1/tests", auth=False, params={"lang": "en", "active": "true"})
    retestable = {int(t["id"]) for t in data.get("tests", []) if t.get("retestable")}
    public_only = {int(t["id"]) for t in data.get("tests", []) if t.get("public_sector_only")}
    return retestable, public_only


def order_retest(site_id: int, test_numbers: list[int]) -> dict:
    """Order new tests for one of the user's sites. Spends 1 credit per test.

    Only call this when the user explicitly asks for a fresh test. Tests that
    are already queued or running are skipped by the API and not charged, so
    the result may be cheaper than requested; it says exactly what was queued.
    Follow up with get_retest_status (at most once a minute), never by ordering
    again. Results normally arrive within minutes, longer when the queue is long.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
        test_numbers: Test ids to run, e.g. [21, 15]. Required and explicit;
            see list_test_types for which ids are retestable. Test 26 can only
            be ordered for public-sector sites.
    """
    sid = _site_id(site_id)
    try:
        wanted = list(dict.fromkeys(int(n) for n in (test_numbers or [])))
    except (TypeError, ValueError) as exc:
        raise WebperfError("test_numbers must be a list of integers.", kind="bad_request") from exc
    if not wanted:
        raise WebperfError(
            "test_numbers is required: say which tests to run, e.g. [21]. "
            "Use list_test_types to see which ids are retestable.",
            kind="bad_request",
        )

    retestable, public_only = _catalogue_flags()
    not_orderable = [n for n in wanted if n not in retestable]
    if not_orderable:
        raise WebperfError(
            f"Tests {not_orderable} cannot be ordered as a retest. Nothing was ordered. "
            f"Retestable ids: {sorted(retestable)}.",
            kind="bad_request",
        )

    quota = _request("GET", "/0.1/quota/", headers=_site_headers(sid), cache=False)
    available = quota.get("available")
    if isinstance(available, int) and len(wanted) > available:
        raise WebperfError(
            f"Not enough credits: this order needs up to {len(wanted)} but {available} "
            f"remain for the current 30-day period. Nothing was ordered.",
            kind="bad_request",
        )

    headers = {**_site_headers(sid), "testnums": ",".join(str(n) for n in wanted)}
    data = _request("POST", "/0.1/retest/", headers=headers, cache=False)
    charged = int(data.get("credits_charged") or 0)
    return {
        "site_id": sid,
        "queued": data.get("queued", []),
        "already_queued": data.get("already_queued", []),
        "credits_charged": charged,
        "credits_remaining": (available - charged) if isinstance(available, int) else None,
        "queue_ids": data.get("queue_ids", {}),
        "public_sector_only_requested": [n for n in wanted if n in public_only],
        "next_step": (
            "Call get_retest_status(site_id) to follow the order, at most once a minute. "
            "Do not order again while a test is queued or running."
        ),
    }


PRIVATE_TEST_CREDITS = 15          # per page, same as /privata-tester/ in Webperf Cloud
PRIVATE_TEST_MAX_PER_ORDER = 10    # the API's own cap per call
PRIVATE_TEST_CONFIRM_ABOVE = 5     # more pages than this need confirm_large_order
PRIVATE_TEST_DEDUP_HOURS = 24


def _normalise_page_url(url: str) -> str:
    """Compare pages loosely: scheme and host lowercased, no fragment, no
    trailing slash. http and https count as the same page."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit(("https", parts.netloc.lower(), path, parts.query, ""))


def _parse_api_timestamp(value: Any):
    """The legacy API writes '2026-10-12 23:24:32+02:00'."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def order_private_test(site_id: int, urls: list[str], confirm_large_order: bool = False) -> dict:
    """Run the whole test suite on individual pages of the user's site.

    Costs 15 credits per page, so state the cost before calling. Pages must
    be on the site's own domain, at most 10 per call. Pages ordered in the
    last 24 hours are skipped and not charged. More than 5 new pages require
    confirm_large_order=true, which you may set only after the user confirmed
    the page count and cost in the conversation. Results arrive over 15 to
    30 minutes; read them with get_private_test_result.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
        urls: Full page addresses on the site's domain, e.g.
            ["https://www.example.se/kontakt"].
        confirm_large_order: Set true only after the user confirmed an order
            of more than 5 pages.
    """
    from datetime import datetime, timedelta, timezone
    from urllib.parse import urlsplit

    sid = _site_id(site_id)
    wanted = list(dict.fromkeys(
        u.strip() for u in (urls or []) if isinstance(u, str) and u.strip()
    ))
    if not wanted:
        raise WebperfError("urls is required: give the full addresses of the pages to test.",
                           kind="bad_request")
    if len(wanted) > PRIVATE_TEST_MAX_PER_ORDER:
        raise WebperfError(
            f"At most {PRIVATE_TEST_MAX_PER_ORDER} pages per order (you gave {len(wanted)}). "
            "Nothing was ordered.",
            kind="bad_request",
        )

    site = _request("GET", f"/item/{sid}").get("data") or {}
    site_host = urlsplit(str(site.get("url") or "")).netloc.lower()
    if not site_host:
        raise WebperfError(f"Could not read the address of site {sid}.", kind="invalid_response")
    off_domain = [
        u for u in wanted
        if urlsplit(u).scheme not in ("http", "https") or urlsplit(u).netloc.lower() != site_host
    ]
    if off_domain:
        raise WebperfError(
            f"These pages are not on {site_host}: {off_domain}. Private tests must be on the "
            "site's own domain, written exactly as the site is registered. Nothing was ordered.",
            kind="bad_request",
        )

    # Dedup against pages ordered recently, so a repeated request does not
    # cost another 15 credits per page.
    recent: dict[str, dict] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=PRIVATE_TEST_DEDUP_HOURS)
    listing = _request("GET", f"/0.1/private_stats/{sid}", cache=False)
    for t in listing.get("data", []) or []:
        when = _parse_api_timestamp(t.get("date_added"))
        if when is None or when.tzinfo is None or when < cutoff:
            continue
        uri = str(t.get("uri") or "").rstrip("/").rsplit("/", 1)[-1]
        recent[_normalise_page_url(str(t.get("url") or ""))] = {
            "url": t.get("url"),
            "date_added": t.get("date_added"),
            "private_test_id": int(uri) if uri.isdigit() else None,
        }
    skipped = [recent[_normalise_page_url(u)] for u in wanted if _normalise_page_url(u) in recent]
    to_order = [u for u in wanted if _normalise_page_url(u) not in recent]

    quota = _request("GET", "/0.1/quota/", headers=_site_headers(sid), cache=False)
    available = quota.get("available")
    cost = PRIVATE_TEST_CREDITS * len(to_order)

    if not to_order:
        return {
            "site_id": sid,
            "ordered": [],
            "skipped_recent": skipped,
            "credits_charged": 0,
            "credits_remaining": available,
            "message": (
                f"All {len(skipped)} page(s) were already ordered in the last "
                f"{PRIVATE_TEST_DEDUP_HOURS} hours. Nothing was charged; read them with "
                "get_private_test_result."
            ),
        }

    if isinstance(available, int) and cost > available:
        raise WebperfError(
            f"Not enough credits: {len(to_order)} page(s) cost {cost} but {available} remain "
            "for the current 30-day period. Nothing was ordered.",
            kind="bad_request",
        )

    if len(to_order) > PRIVATE_TEST_CONFIRM_ABOVE and not confirm_large_order:
        return {
            "site_id": sid,
            "needs_confirmation": True,
            "ordered": [],
            "pages": to_order,
            "skipped_recent": skipped,
            "cost": cost,
            "credits_available": available,
            "message": (
                f"This would test {len(to_order)} pages for {cost} credits"
                + (f", leaving {available - cost}." if isinstance(available, int) else ".")
                + " Nothing was ordered. Ask the user to confirm the page count and cost, then "
                "call again with confirm_large_order=true."
            ),
        }

    # The API splits on ", " (comma + space).
    data = _request("POST", "/0.1/test_once/", headers=_site_headers(sid), cache=False,
                    data={"urls": ", ".join(to_order)})
    endpoints = data.get("endpoints") or {}
    ids = sorted(int(k) for k in endpoints if str(k).isdigit())
    failed = list(data.get("failed") or [])
    created_urls = [u for u in to_order if u not in failed]
    ordered = [
        {"private_test_id": pid, "url": created_urls[i] if i < len(created_urls) else None}
        for i, pid in enumerate(ids)
    ]
    charged = PRIVATE_TEST_CREDITS * len(ids)
    _cache.clear()  # list_private_tests must see the new pages at once
    return {
        "site_id": sid,
        "ordered": ordered,
        "skipped_recent": skipped,
        "failed": failed,
        "credits_charged": charged,
        "credits_remaining": (available - charged) if isinstance(available, int) else None,
        "next_step": (
            "Call get_private_test_result(site_id, private_test_id) to read results; they "
            "arrive test type by test type over 15 to 30 minutes. Poll at most once a minute."
        ),
    }


if ORDERING_ENABLED:
    mcp.tool(annotations=ORDERING)(order_retest)
    mcp.tool(annotations=ORDERING)(order_private_test)


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
    # A one-off page gets the whole suite, one test type at a time, so progress
    # is "types with a result" against the types the fleet runs. Public-sector-
    # only types are left out of the expectation since most pages never get them.
    retestable, public_only = _catalogue_flags()
    expected = sorted(retestable - public_only)
    done_types = {int(r["type_of_test"]) for r in results if r.get("type_of_test") is not None}
    pending = [t for t in expected if t not in done_types]
    return {
        "site_id": sid,
        "private_test_id": tid,
        "result_count": len(results),
        "tests_done": len(done_types),
        "tests_expected": len(expected),
        "pending_types": pending,
        "complete": not pending,
        "results": results,
    }


@mcp.tool(annotations=READ_ONLY)
def get_private_raw_check_data(site_id: int, private_test_id: int, type_of_test: int) -> dict:
    """Get the raw underlying audit data for ONE test type of a private test.

    Only call this when the user explicitly asks for the raw/detailed machine
    data; it can be hundreds of KB. get_private_test_result is the right tool
    for scores and reports. Needs api.webperf.se 1.2.1 or later.

    Args:
        site_id: Numeric id of the site (from list_my_sites).
        private_test_id: Id from list_private_tests or order_private_test.
        type_of_test: Numeric test type (see list_test_types).
    """
    sid = _site_id(site_id)
    tid = _site_id(private_test_id)
    want = str(int(type_of_test))
    data = _request(
        "GET",
        f"/0.1/private_stats/{sid}/{tid}",
        params={"type_of_test": want, "include_raw_data": "true"},
    )
    for t in data.get("data", []) or []:
        if str(t.get("type_of_test")) != want:
            continue
        return {
            "site_id": sid,
            "private_test_id": tid,
            "type_of_test": int(want),
            "test_date": t.get("test_date"),
            "raw_check_data": _parse_check_data(t.get("json_check_data")) or None,
        }
    raise WebperfError(
        f"No result of type {want} for private test {tid} yet. "
        "Use get_private_test_result to see which test types are done.",
        kind="not_found",
    )


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


# ---------------------------------------------------------------------------
# Campaign tagging
# ---------------------------------------------------------------------------

CONTEXT_PATH = "/0.1/context/campaign_convention"
MAX_URLS_TO_CHECK = 50


def _convention_path() -> Path | None:
    raw = os.environ.get("WEBPERF_CAMPAIGN_CONVENTION", "").strip()
    return Path(raw).expanduser() if raw else None


def _has_key() -> bool:
    return bool(os.environ.get("WEBPERF_API_KEY", "").strip())


def _load_convention(site_id: int | None) -> dict:
    """Where the convention lives and what it says.

    With site_id the cloud copy wins, even if a local file is configured: a
    silent fallback to a stale local copy would defeat the point of sharing
    one. Without site_id, the local file. A key without site_id and without a
    local file is an error, since the user has a cloud convention we cannot
    pick a site for.
    """
    path = _convention_path()
    if site_id is not None:
        sid = _site_id(site_id)
        data = _request("GET", CONTEXT_PATH, headers=_site_headers(sid), cache=False)
        notes = []
        if path:
            notes.append(f"Using the convention in Webperf Cloud; the local file {path} is "
                         "ignored when site_id is given.")
        return {
            "stored_in": "webperf_cloud",
            "site_id": data.get("site_id", sid),
            "revision": int(data.get("revision") or 0),
            "convention": data.get("value"),
            "date_modified": data.get("date_modified"),
            "modified_by": data.get("modified_by"),
            "notes": notes,
        }
    if path:
        if not path.exists():
            return {"stored_in": "local_file", "path": str(path), "convention": None, "notes": [
                f"{path} does not exist yet; saving creates it."]}
        try:
            conv = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WebperfError(f"Could not read {path}: {exc}", kind="config") from None
        errors = tagging.validate_convention(conv)
        if errors:
            raise WebperfError(f"The convention in {path} is not valid: " + "; ".join(errors),
                               kind="config")
        notes = []
        if _has_key():
            notes.append("Pass site_id to use your organisation's convention in Webperf Cloud "
                         "instead of this local file.")
        return {"stored_in": "local_file", "path": str(path), "convention": conv, "notes": notes}
    if _has_key():
        raise WebperfError(
            "Pass site_id (from list_my_sites) to use the campaign convention stored for that "
            "site in Webperf Cloud, or set WEBPERF_CAMPAIGN_CONVENTION to a local JSON file.",
            kind="bad_request",
        )
    return {"stored_in": None, "convention": None, "notes": [
        "No convention is configured. Without an API key it lives in a local JSON file: set "
        "WEBPERF_CAMPAIGN_CONVENTION to its path in the MCP client config."]}


def _require_convention(site_id: int | None) -> dict:
    loaded = _load_convention(site_id)
    if loaded["convention"] is None:
        raise WebperfError(
            "No campaign convention is saved yet, so there is nothing to tag against. Call "
            "get_campaign_convention for a starter and set one up with the user first.",
            kind="not_found",
        )
    return loaded


def _save_convention(conv: dict, site_id: int | None, base_revision: int | None) -> dict:
    errors = tagging.validate_convention(conv)
    if errors:
        raise WebperfError("The convention is not valid: " + "; ".join(errors), kind="bad_request")
    if site_id is not None:
        sid = _site_id(site_id)
        if base_revision is None or int(base_revision) < 0:
            raise WebperfError(
                "base_revision is required: call get_campaign_convention first and pass its "
                "revision (0 when nothing is saved).",
                kind="bad_request",
            )
        try:
            data = _request("PUT", CONTEXT_PATH, headers=_site_headers(sid), cache=False,
                            json_body={"value": conv, "base_revision": int(base_revision)})
        except WebperfError as exc:
            if exc.kind == "conflict":
                raise WebperfError(
                    f"Someone saved the convention after revision {base_revision}. Call "
                    "get_campaign_convention, apply the user's change to the current version "
                    "and save again.",
                    kind="conflict",
                    status=409,
                ) from None
            raise
        return {"saved": True, "stored_in": "webperf_cloud", "site_id": data.get("site_id", sid),
                "revision": data.get("revision")}
    path = _convention_path()
    if path is None:
        raise WebperfError(
            "Pass site_id to save in Webperf Cloud, or set WEBPERF_CAMPAIGN_CONVENTION to a "
            "local JSON file." if _has_key() else
            "Set WEBPERF_CAMPAIGN_CONVENTION to the path of a local JSON file to save the "
            "convention in.",
            kind="config",
        )
    if not path.parent.is_dir():
        raise WebperfError(f"The folder {path.parent} does not exist.", kind="config")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(conv, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return {"saved": True, "stored_in": "local_file", "path": str(path)}


@mcp.tool(annotations=READ_ONLY)
def get_campaign_convention(site_id: int | None = None) -> dict:
    """The organisation's naming convention for campaign (UTM/MTM) tagging.

    With an API key, pass site_id to read the convention stored for that site
    in Webperf Cloud; everyone with access to the site shares it. Without a
    key it is read from the local file in WEBPERF_CAMPAIGN_CONVENTION.

    When nothing is saved yet, the result has convention null and a starter
    to set one up from with the user. Keep revision: saving needs it.

    Args:
        site_id: Numeric id of one of the user's sites, from list_my_sites.
    """
    loaded = _load_convention(site_id)
    loaded["editing_enabled"] = CONVENTION_EDITS_ENABLED
    if loaded["convention"] is None:
        loaded["starter"] = tagging.STARTER_CONVENTION
        loaded["next_step"] = (
            "No convention is saved yet. Go through the setup_campaign_convention questions "
            "with the user, starting from the starter, and save the result with "
            "save_campaign_convention once they agree."
            if CONVENTION_EDITS_ENABLED else
            "No convention is saved yet. Saving one needs \"Allow editing the campaign "
            "convention\" turned on in the extension settings (WEBPERF_ALLOW_CONVENTION_EDITS)."
        )
    return loaded


@mcp.tool(annotations=READ_ONLY)
def build_campaign_url(
    url: str,
    source: str,
    medium: str,
    campaign: str,
    content: str | None = None,
    term: str | None = None,
    site_id: int | None = None,
    placement_url: str | None = None,
) -> dict:
    """Tag a URL with campaign parameters that follow the organisation's convention.

    Always use this instead of writing a tagged URL yourself. Values are
    matched against the convention: known misspellings are mapped (FB ->
    facebook), letters are folded to lowercase ASCII (höst -> host), and
    values the convention does not allow are refused with the closest allowed
    ones. Every rewrite is listed in changes; tell the user about them.
    Existing utm_/mtm_ tags on the URL are replaced, not duplicated.

    Returns ok true with the tagged url, or ok false with errors and
    suggestions. On ok false, ask the user to choose; do not pick for them
    and do not add values to the convention unless they ask.

    Args:
        url: The full landing page address.
        source: Where the link is published, e.g. facebook, nyhetsbrev.
        medium: The kind of traffic, e.g. social, email, cpc.
        campaign: The campaign name.
        content: What tells links in the same campaign apart (placement,
            variant, format). Leave out when there is only one link.
        term: Paid search keyword. Only with paid search.
        site_id: The user's site, to use its convention in Webperf Cloud.
        placement_url: Where the link will be placed, if known. Used to warn
            about tagging internal links.
    """
    loaded = _require_convention(site_id)
    conv = loaded["convention"]
    problem = tagging.check_target(url)
    if problem:
        raise WebperfError(problem, kind="bad_request")

    result = tagging.evaluate(conv, {"source": source, "medium": medium, "campaign": campaign,
                                     "content": content, "term": term})
    warnings = result["warnings"]
    if placement_url and tagging.same_site(url, placement_url):
        warnings.append(
            "The link is placed on the same site it points to. Tagging internal links starts "
            "a new session in GA4 and credits the visit to the campaign instead of where the "
            "visitor came from. Leave internal links untagged."
        )
    if result["errors"]:
        return {
            "ok": False,
            "errors": result["errors"],
            "suggestions": result["suggestions"],
            "changes": result["changes"],
            "warnings": warnings,
            "next_step": "Ask the user to choose one of the suggested or allowed values. Adding "
                         "a new value to the convention is their decision.",
        }

    tagged, removed = tagging.tag_url(url, conv["format"], result["values"])
    changes = result["changes"]
    if removed:
        changes.append("replaced existing tags: " + ", ".join(removed))
    return {
        "ok": True,
        "url": tagged,
        "format": conv["format"],
        "values": result["values"],
        "changes": changes,
        "warnings": warnings,
        "convention_from": loaded["stored_in"],
    }


@mcp.tool(annotations=READ_ONLY)
def check_campaign_urls(urls: list[str], site_id: int | None = None) -> dict:
    """Audit already tagged URLs against the organisation's convention.

    Each URL gets a status: compliant; fixable (every value maps onto the
    convention, and fixed_url is the corrected link); non_compliant (some
    value has no match, see issues and suggestions); untagged; or invalid.
    Up to 50 URLs per call.

    Args:
        urls: Tagged URLs, e.g. a column pasted from a campaign spreadsheet.
        site_id: The user's site, to use its convention in Webperf Cloud.
    """
    if not urls:
        raise WebperfError("Give at least one URL.", kind="bad_request")
    if len(urls) > MAX_URLS_TO_CHECK:
        raise WebperfError(f"At most {MAX_URLS_TO_CHECK} URLs per call.", kind="bad_request")
    loaded = _require_convention(site_id)
    conv = loaded["convention"]
    expected = conv["format"]

    results = []
    for url in urls:
        problem = tagging.check_target(url)
        if problem:
            results.append({"url": url, "status": "invalid", "issues": [problem]})
            continue
        fmt, values, problems = tagging.read_tags(url)
        if not values:
            results.append({"url": url, "status": "untagged"})
            continue
        result = tagging.evaluate(conv, values)
        entry: dict[str, Any] = {"url": url, "values": values, "warnings": result["warnings"]}
        if result["errors"]:
            entry.update(status="non_compliant", issues=problems + result["errors"],
                         suggestions=result["suggestions"])
        else:
            changes = list(result["changes"])
            if fmt != expected:
                changes.append(f"format: {fmt} -> {expected}")
            changes += problems
            fixed, _ = tagging.tag_url(url, expected, result["values"])
            if changes or fixed != url.strip():
                entry.update(status="fixable", changes=changes, fixed_url=fixed)
            else:
                entry["status"] = "compliant"
        results.append(entry)

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"convention_from": loaded["stored_in"], "summary": counts, "results": results}


def save_campaign_convention(
    convention: dict, site_id: int | None = None, base_revision: int | None = None
) -> dict:
    """Save the whole campaign convention, replacing the current one.

    Only call this after showing the user the complete convention and getting
    their yes. With site_id it is saved in Webperf Cloud for everyone with
    access to the site, and base_revision must be the revision from
    get_campaign_convention (0 when nothing was saved). If a colleague saved
    in between, this fails with a conflict: read again, reapply the change
    and save again. Without site_id it is written to the local file.

    Every value must be lowercase ASCII (a-z, 0-9, _ and -). format is utm
    unless the user only uses Matomo and prefers mtm; utm works in Matomo too.

    Args:
        convention: The complete convention object.
        site_id: The user's site, to save in Webperf Cloud.
        base_revision: The revision the change was based on.
    """
    return _save_convention(convention, site_id, base_revision)


def add_campaign_value(
    parameter: str, value: str, site_id: int | None = None, aliases: list[str] | None = None
) -> dict:
    """Add one allowed value, and optionally its aliases, to the convention.

    Only when the user has decided the value belongs in the convention, never
    to make a rejected value pass. The value is folded to lowercase ASCII
    first, and the result says so. Retries once if a colleague saved at the
    same time.

    Args:
        parameter: source, medium, campaign, content or term.
        value: The new allowed value.
        site_id: The user's site, to change its convention in Webperf Cloud.
        aliases: Misspellings or variants that should map to the value.
    """
    if parameter not in tagging.PARAMS:
        raise WebperfError(f"parameter must be one of {', '.join(tagging.PARAMS)}.",
                           kind="bad_request")
    for attempt in range(2):
        loaded = _require_convention(site_id)
        conv = json.loads(json.dumps(loaded["convention"]))
        clean = tagging.normalise(value, conv.get("separator", "_"))
        if not clean:
            raise WebperfError(f"{value!r} has no usable characters (a-z, 0-9, _ and -).",
                               kind="bad_request")
        rules = conv.setdefault(parameter, {})
        values = rules.setdefault("values", [])
        known = rules.setdefault("aliases", {})
        new_aliases = []
        for alias in aliases or []:
            if known.get(alias) not in (None, clean):
                raise WebperfError(f"{alias!r} is already an alias for {known[alias]!r}.",
                                   kind="bad_request")
            if alias != clean and alias not in known:
                known[alias] = clean
                new_aliases.append(alias)
        if not known:
            del rules["aliases"]
        if clean in values and not new_aliases:
            return {"added": False, "value": clean, "reason": f"{clean!r} is already allowed."}
        if clean not in values:
            values.append(clean)
        try:
            saved = _save_convention(conv, site_id, loaded.get("revision"))
        except WebperfError as exc:
            if exc.kind == "conflict" and attempt == 0:
                continue
            raise
        saved.update(added=True, parameter=parameter, value=clean, aliases_added=new_aliases)
        if clean != value:
            saved["changed_from"] = value
        return saved
    raise WebperfError("The convention kept changing; try again.", kind="conflict")


if CONVENTION_EDITS_ENABLED:
    mcp.tool(annotations=CONVENTION_REPLACE)(save_campaign_convention)
    mcp.tool(annotations=CONVENTION_ADD)(add_campaign_value)


@mcp.prompt(
    name="setup_campaign_convention",
    title="Set up a campaign tagging convention",
    description="Agree on the organisation's UTM/MTM naming convention, step by step.",
)
def setup_campaign_convention(site_id: str = "") -> str:
    where = (f"Use site_id {site_id} so it is stored in Webperf Cloud for everyone with access "
             "to that site." if site_id.strip() else
             "If I have an API key, ask which of my sites the convention belongs to "
             "(list_my_sites); otherwise it is saved in the local file.")
    return f"""\
Help me set up our naming convention for campaign tagging. {where}

Start with get_campaign_convention. If a convention already exists, show it and
ask what should change. Otherwise start from the starter it returns. Then ask me
one question at a time:

1. Do we use Google Analytics 4, Matomo or both? Use UTM tags unless we only use
   Matomo and want MTM. If we use Matomo, tell me UTM tags are enough for basic
   tracking: Matomo reads utm_campaign and utm_term out of the box, and the free
   Marketing Campaigns Reporting plugin reads source, medium and content too.
2. Where do we actually publish links? Those become the allowed sources.
3. What kinds of traffic are they? Those become the allowed media. Prefer values
   GA4 groups on their own (email, social, cpc, display, affiliate, referral);
   others land in Unassigned in GA4 unless the source is a known site.
4. What must a campaign name contain, and in which order: an organisation
   prefix, year, quarter, product? That becomes the campaign template.
5. Do we run paid search? If so, term is used only with medium cpc.
6. Which misspellings and variants do people use? Those become aliases.

Every value is lowercase ASCII: a-z, digits, _ and -, so å and ä become a and
ö becomes o. Keep the lists to what we really use. Show me the complete
convention before saving, and save it with save_campaign_convention only after
I say yes.
"""


def main() -> None:
    """Console entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
