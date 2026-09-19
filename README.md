# webperf-mcp

A small [MCP](https://modelcontextprotocol.io) server that lets an AI
assistant (Claude Desktop, Claude Code, or any MCP client) read data from
**api.webperf.se** in plain language.

- **Without an API key** it answers from Webperf's open data: every Swedish
  municipality and region with its accessibility, performance, standards and
  security ratings, site search, aggregate statistics and the test catalogue.
- **With a Webperf Cloud API key** (Standard or Agency) it also reads your own
  sites: latest results and readable reports, monthly history, raw audit data,
  quota, private tests and audit reports.

It runs **locally over stdio**. There is nothing to host. The server is a thin
client over the existing public HTTP API: it adds no privileges of its own, and
all authorization stays on the server.

## Security model

- **The MCP server is just another API client.** It only calls the same public
  endpoints that already exist. It has no database access and no special powers.
- **Authorization stays server-side.** api.webperf.se scopes every keyed request
  to the sites your key is granted and returns `403` otherwise. The server
  cannot bypass that: it can never show data your key couldn't fetch directly.
- **Read-only.** Every tool is a read and is annotated `readOnlyHint` so clients
  can treat it that way. No retest, audit ordering, or other mutating endpoints
  are wired up.
- **Your key stays on your machine.** It is sent only as the `api-key` header
  to the configured API base, which must be `https`. The server never writes
  the key anywhere and never includes it in tool output or error text.
  Where the key is *stored* depends on how you install:
  - **Extension (recommended):** the manifest marks the key `sensitive`, so
    Claude Desktop keeps it in your operating system's keychain.
  - **Manual config:** the key sits in **plaintext** in your client's config
    file. That is normal for MCP, but worth knowing when you rotate it.
- **Revocation takes effect at once, give or take the cache.** The key is
  checked on every request, so rotating or revoking it in Webperf Cloud stops
  MCP access immediately. The one exception is the in-process response cache:
  an identical repeat of a request already made can be served from memory for
  up to `WEBPERF_CACHE_TTL` seconds (60 by default). Cache entries are keyed
  on the API key, so a different key never sees them. Set `WEBPERF_CACHE_TTL=0`
  to disable.

### Treat report text as untrusted input

This is the one risk the server cannot remove for you, and it is worth
understanding before you wire it into an agent that also has shell or file
access.

Report fields (`report_overall`, `report_a11y`, …) and `raw_check_data`
describe **websites that Webperf tested**. They quote domains, URLs, page
titles, element attributes and validator messages taken from those sites. The
server deliberately HTML-unescapes that text so the model sees it as written,
which also means a tested site controls a slice of what lands in the model's
context.

Nothing in this server can act on such text — every tool is a read, and
`get_audit_file` only serves an allowlisted set of JSON summaries. But the
*client* may hold other tools. If you are running this alongside a filesystem
or shell server, treat website-derived report content the way you would treat
any scraped page: data to summarise, not instructions to follow.

## Tools

### Open data (no key needed)

| Tool | What it does | Endpoint |
| --- | --- | --- |
| `list_public_sector_ratings(kind)` | Every municipality or region with its current 1-5 ratings | `GET /v1/public/{municipalities,regions}` |
| `search_public_sites(query, category?, limit?, offset?)` | Find public-sector sites by name or URL | `GET /v1/sites` |
| `get_public_site(site_id)` | Ratings and facts for one public-sector site | `GET /v1/sites/{id}` |
| `get_public_stats()` | Site counts and average ratings across the public sector | `GET /v1/public/stats` |
| `list_test_types(lang?, active_only?)` | The test catalogue, to read numeric `type_of_test` ids | `GET /v1/tests` |

### Your own sites (key needed)

| Tool | What it does | Endpoint |
| --- | --- | --- |
| `check_api_key()` | Does the key work, whose is it, how many sites does it reach. Never returns the key | `GET /0.1/whoami/` |
| `list_my_sites()` | The sites your key can access | `GET /0.1/stats/` |
| `get_site_details(site_id)` | Title, address, category and current ratings for one site | `GET /item/{id}` |
| `get_latest_results(site_id, type_of_test?)` | Latest results with readable reports; omits the large raw data | `GET /0.1/stats/{id}` |
| `get_raw_check_data(site_id, type_of_test)` | Opt-in: the raw audit data behind one test, parsed to JSON. Large | `GET /0.1/stats/{id}` |
| `get_test_history(site_id)` | Monthly score history, for trends | `GET /0.1/stats_per_month/{id}` |
| `get_quota(site_id)` | Test credits: cap, used, available | `GET /0.1/quota/` |
| `list_categories()` | Site categories with average ratings, for benchmarking | `GET /0.1/categories/` |
| `list_private_tests(site_id)` | Private one-off test pages for a site | `GET /0.1/private_stats/{id}` |
| `get_private_test_result(site_id, private_test_id)` | Latest result of one private test | `GET /0.1/private_stats/{id}/{test}` |
| `list_audits(site_id)` | Completed audits and their report files | `POST /0.1/audits/` |
| `get_audit_file(site_id, folder, file)` | One JSON summary file from an audit | `GET /0.1/audit_file/` |

`get_latest_results` asks the API to leave out each test's raw
`json_check_data` (which can be hundreds of KB per test). When you actually
want that detail, `get_raw_check_data` fetches it for one test.

The API returns at most 30 test rows per site and does not paginate. When a
response comes back full, `get_latest_results` sets `truncated: true` and says
so in its `note`, so a partial list is never presented as the whole history.
Pass `type_of_test` to narrow the query instead.

## Install

### Claude Desktop (recommended)

Download the `.mcpb` bundle from the
[latest release](https://github.com/Webperf-se/webperf-mcp/releases/latest) and
**double-click it**. Claude Desktop shows an install prompt and asks for your
API key. Leave it empty to use only the open data; add it later from
**Settings → Extensions → Webperf**.

No JSON to edit, no terminal, and no need to install Python or
[uv](https://docs.astral.sh/uv/) yourself. The bundle declares the `uv`
runtime and ships a lockfile, so Claude Desktop fetches the exact Python
dependencies that were tested.

Then ask things like *"which municipality in Skåne has the worst accessibility
score?"*, *"list my webperf sites"* or *"show the latest accessibility results
for site 3843"*.

### Other MCP clients

Clients that don't support MCP Bundles need the server configured by hand. The
easiest path is [`uvx`](https://docs.astral.sh/uv/), which runs it in an
isolated environment. Pin a release tag so you get a tested version:

```bash
uvx --from git+https://github.com/Webperf-se/webperf-mcp@v0.2.0 webperf-mcp
```

For Claude Code:

```bash
claude mcp add webperf --env WEBPERF_API_KEY=your-key-here \
  -- uvx --from git+https://github.com/Webperf-se/webperf-mcp@v0.2.0 webperf-mcp
```

For a client that takes JSON:

```json
{
  "mcpServers": {
    "webperf": {
      "command": "/Users/you/.local/bin/uvx",
      "args": ["--from", "git+https://github.com/Webperf-se/webperf-mcp@v0.2.0", "webperf-mcp"],
      "env": {
        "WEBPERF_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

> **Use the absolute path to `uvx`.** GUI-launched apps on macOS do not inherit
> your shell `PATH`; they get the bare launchd default, which does not include
> `~/.local/bin` where the uv installer puts `uvx`. A plain `"command": "uvx"`
> fails there with nothing but a generic "server failed" message. Run
> `which uvx` to get your path.

## Is my key set up correctly?

Ask the assistant to *check my webperf API key*. The `check_api_key` tool
makes one cheap request and reports one of:

| Status | Meaning | What to do |
| --- | --- | --- |
| `ok` | Key works; shows the account and how many sites it reaches | Nothing |
| `no_sites` | Key is valid but granted no sites | Ask your Webperf Cloud administrator to grant access, or upgrade from Starter |
| `no_key_configured` | No key set; open-data tools still work | Add the key in the extension settings or `WEBPERF_API_KEY` |
| `bad_format` | Key doesn't look like a Webperf key | Copy it again, watch for stray spaces or quotes |
| `rejected` | api.webperf.se refused the key | Copy it again from your account page; a regenerated key replaces the old one |
| `rate_limited` | Too many requests | Wait a minute |
| `unreachable` | Could not reach the API | Check network or proxy |
| `misconfigured` | `WEBPERF_API_BASE` is invalid or not https | Fix the client config |

The key itself is never included in the result, in error messages, or in logs.

## Configuration

When installed as an extension, Claude Desktop collects these for you; you only
need them when configuring a client by hand.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `WEBPERF_API_KEY` | no | — | Your Webperf Cloud API key. Without it only the open-data tools work |
| `WEBPERF_API_BASE` | no | `https://api.webperf.se` | API base URL (e.g. staging). Must be `https` unless localhost |
| `WEBPERF_HTTP_TIMEOUT` | no | `30` | Per-request timeout in seconds |
| `WEBPERF_CACHE_TTL` | no | `60` | Seconds to reuse an identical GET response in-process. `0` disables |

These are read from the process environment. The server does not read a `.env`
file.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest            # unit tests with a mocked API + a stdio boot test
webperf-mcp       # run the server on stdio
```

`tests/test_stdio.py` boots the installed package the way an MCP client does
and calls the open-data endpoint live. It is the test that catches an
upstream `mcp` release breaking a fresh install. Set `WEBPERF_TEST_OFFLINE=1`
to skip the network part.

## Releasing (maintainers)

1. Bump `__version__` in `src/webperf_mcp/__init__.py` and `version` in
   `manifest.json` (CI fails if they differ), add a `CHANGELOG.md` entry.
2. `uv lock` if dependencies changed, and commit `uv.lock`. The bundle runs
   `uv run --frozen`, so the lockfile is what users get.
3. Tag `vX.Y.Z` and push the tag. The release workflow runs the tests,
   validates and packs the `.mcpb`, checks it contains no secrets, and
   attaches it to a GitHub release. The "latest release" link above then
   resolves to that bundle.

To build locally:

```bash
npx @anthropic-ai/mcpb validate manifest.json
npx @anthropic-ai/mcpb pack . webperf-mcp.mcpb
unzip -l webperf-mcp.mcpb   # check for stray secrets before sharing
```

## License

MIT, see [LICENSE](LICENSE).
