# webperf-mcp

A small [MCP](https://modelcontextprotocol.io) server that gives premium users of
**api.webperf.se** read-only access to their sites and test results from any MCP
client (e.g. Claude Desktop).

It runs **locally over stdio** — there is nothing to host. The server is a thin
client over the existing public HTTP API: it adds no privileges of its own, and
all authorization stays on the server.

## Security model

This is the important part.

- **The MCP server is just another API client.** It only calls the same public
  endpoints (`/0.1/stats/…`, `/v1/tests`) that already exist. It has no database
  access and no special powers.
- **Authorization stays server-side.** api.webperf.se already scopes every
  premium request to the sites your key is granted via `users_access` and returns
  `403` otherwise. The MCP cannot bypass that — it can never show data your key
  couldn't already fetch directly.
- **Read-only.** v1 exposes only listing and reading. No retest, audit, or other
  mutating endpoints are wired up.
- **Your key stays on your machine.** It is read from the `WEBPERF_API_KEY`
  environment variable by the local process and sent only as the `api-key`
  header to `https://api.webperf.se` over HTTPS. It is never written to disk by
  this tool and never sent anywhere else.
- **Revocation is immediate.** Because the key is checked on every request, if
  you rotate or revoke it in api.webperf.se, MCP access stops at once.

## Tools

| Tool | What it does | Endpoint |
| --- | --- | --- |
| `list_my_sites` | Lists the sites your key can access | `GET /0.1/stats/` |
| `get_latest_results(site_id)` | Latest test results for a site | `GET /0.1/stats/{site_id}` |
| `get_test_history(site_id)` | Historical monthly scores for a site | `GET /0.1/stats_per_month/{site_id}` |
| `list_test_types(lang, active_only)` | Catalogue of test types (open data, no key) to read numeric `type_of_test` ids | `GET /v1/tests` |

## Install & run

The easiest path is [`uv`](https://docs.astral.sh/uv/) / `uvx`, which runs the
server in an isolated environment with no manual install.

Until it's published to PyPI, run straight from the repo:

```bash
uvx --from git+https://github.com/Webperf-se/webperf-mcp webperf-mcp
```

Once published:

```bash
uvx webperf-mcp
```

Or install into a virtualenv for development:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
webperf-mcp
```

## Configure your MCP client

### Claude Desktop

Add this to `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "webperf": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Webperf-se/webperf-mcp", "webperf-mcp"],
      "env": {
        "WEBPERF_API_KEY": "your-premium-api-key-here"
      }
    }
  }
}
```

After publishing to PyPI you can simplify `args` to `["webperf-mcp"]`.

Restart the client, and the `webperf` tools will appear. Ask things like
*"list my webperf sites"* or *"show the latest accessibility results for site 3843"*.

## Configuration

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `WEBPERF_API_KEY` | yes | — | Your premium api.webperf.se key |
| `WEBPERF_API_BASE` | no | `https://api.webperf.se` | API base URL (e.g. for staging) |
| `WEBPERF_HTTP_TIMEOUT` | no | `30` | Per-request timeout in seconds |

See `.env.example`. Prefer setting the key in your MCP client's `env` block
rather than committing a `.env` file.

## License

MIT
