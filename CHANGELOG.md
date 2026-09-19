# Changelog

## 0.2.0 (unreleased)

### Fixed

- Fresh installs crashed on import since mcp 2.0 (2026-07-28) renamed
  `FastMCP`. The dependency is now pinned to mcp 1.x and a `uv.lock` is
  committed and shipped inside the bundle, which `uv run --frozen` honours.
- Report text (`check_report*`) reached the model HTML-escaped. All report
  fields are now unescaped, not only the raw audit data.
- A rejected key on the site-list endpoint surfaced as a raw "API error 400".
  It is now the same friendly rejection as a 401.
- The 429 message claimed a fixed 200 requests/hour. It now reports the
  endpoint's actual limit and Retry-After from the response headers.
- `__init__.__version__` had drifted from `pyproject.toml` and
  `manifest.json`. The package version is now the single source, and CI
  checks the manifest and release tag against it.
- No `LICENSE` file despite the MIT claim.
- The response cache never evicted anything, so a long-lived stdio process
  grew for as long as it ran — raw audit responses included. Lapsed entries
  are now dropped and the cache is capped at 128 entries.
- A malformed `WEBPERF_HTTP_TIMEOUT` or `WEBPERF_CACHE_TTL` killed the server
  at import with a `ValueError`. Both now warn on stderr and fall back to the
  default, the way an invalid `WEBPERF_API_BASE` already did.
- `get_latest_results` presented a truncated list as if it were complete.
  `/0.1/stats/{id}` caps at 30 rows server-side and does not paginate, so the
  response now carries `truncated` and says so in its `note`.

### Added

- Open-data tools that work without a key: `list_public_sector_ratings`,
  `search_public_sites`, `get_public_site`, `get_public_stats`. The API key
  is now optional in the extension settings.
- `check_api_key`: one cheap request that says whether the configured key
  works, what account it belongs to and how many sites it reaches, with a
  plain-language next step. Never includes the key.
- Keyed tools for the rest of the read-only API: `get_site_details`,
  `get_quota`, `list_categories`, `list_private_tests`,
  `get_private_test_result`, `list_audits`, `get_audit_file`.
- Server instructions and `readOnlyHint` annotations on every tool.
- A `User-Agent` header so API logs can tell MCP traffic apart.
- In-process 60 s response cache (`WEBPERF_CACHE_TTL`), so a follow-up
  question does not hit the API and its rate limit twice.
- `WEBPERF_API_BASE` must be https (localhost excepted); non-Webperf hosts
  are flagged on stderr.
- Uses the API's `type_of_test` and `include_raw_data` parameters so
  `get_latest_results` no longer downloads every test's raw data, and still
  filters client-side on API versions without them.
- `list_my_sites` shows title and website when the API provides them.
- Tests (unit with a mocked API, plus a stdio boot test), CI on push, PR and
  a weekly schedule, a tag-driven release workflow that packs and attaches
  the `.mcpb`, and Dependabot.
- `SECURITY.md`, and a README section on treating report text as untrusted
  input: reports quote the websites Webperf tested, and this server
  deliberately unescapes them.

### Security

- Workflows now pin every GitHub Action to a commit SHA and the `.mcpb`
  bundler to `@anthropic-ai/mcpb@2.1.2`, so a released bundle can't be packed
  by an unreviewed upstream version. CI is explicitly `contents: read`.

## 0.1.1 (2026-07-16)

- Stop dumping raw `json_check_data` by default; add `get_raw_check_data`.

## 0.1.0 (2026-07-15)

- First release: `list_my_sites`, `get_latest_results`, `get_test_history`,
  `list_test_types`, Claude Desktop `.mcpb` bundle.
