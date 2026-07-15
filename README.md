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
- **Your key stays on your machine.** This tool never writes your key anywhere
  itself, and sends it only as the `api-key` header to `https://api.webperf.se`
  over HTTPS. Where the key is *stored* depends on how you install:
  - **Extension (recommended):** the manifest marks the key `sensitive`, so
    Claude Desktop keeps it in your operating system's keychain.
  - **Manual config:** the key sits in **plaintext** in your client's config
    file. That is normal for MCP, but worth knowing when you rotate it.
- **Revocation is immediate.** Because the key is checked on every request, if
  you rotate or revoke it in api.webperf.se, MCP access stops at once.

## Tools

| Tool | What it does | Endpoint |
| --- | --- | --- |
| `list_my_sites` | Lists the sites your key can access | `GET /0.1/stats/` |
| `get_latest_results(site_id)` | Latest test results for a site | `GET /0.1/stats/{site_id}` |
| `get_test_history(site_id)` | Historical monthly scores for a site | `GET /0.1/stats_per_month/{site_id}` |
| `list_test_types(lang, active_only)` | Catalogue of test types (open data, no key) to read numeric `type_of_test` ids | `GET /v1/tests` |

## Install

### Claude Desktop (recommended)

Download `webperf-mcp-<version>.mcpb` from the
[latest release](https://github.com/Webperf-se/webperf-mcp/releases/latest) and
**double-click it**. Claude Desktop will show an install prompt, ask for your
premium API key, and store it in your keychain.

That's the whole setup — no JSON to edit, no terminal, and no need to install
Python or [uv](https://docs.astral.sh/uv/) yourself. The bundle declares the
`uv` runtime, so Claude Desktop fetches the right Python and dependencies on its
own.

You can also install it from **Settings → Extensions → Install extension…**, and
change your API key later from that same screen.

Then ask things like *"list my webperf sites"* or *"show the latest
accessibility results for site 3843"*.

### Other MCP clients

Clients that don't support MCP Bundles need the server configured by hand. The
easiest path is [`uvx`](https://docs.astral.sh/uv/), which runs it in an
isolated environment with no manual install:

```bash
uvx --from git+https://github.com/Webperf-se/webperf-mcp webperf-mcp
```

For Claude Code:

```bash
claude mcp add webperf --env WEBPERF_API_KEY=your-key-here \
  -- uvx --from git+https://github.com/Webperf-se/webperf-mcp webperf-mcp
```

For a client that takes JSON (this is what the `.mcpb` does for you):

```json
{
  "mcpServers": {
    "webperf": {
      "command": "/Users/you/.local/bin/uvx",
      "args": ["--from", "git+https://github.com/Webperf-se/webperf-mcp", "webperf-mcp"],
      "env": {
        "WEBPERF_API_KEY": "your-premium-api-key-here"
      }
    }
  }
}
```

> **Use the absolute path to `uvx`.** GUI-launched apps on macOS do not inherit
> your shell `PATH` — they get the bare launchd default
> (`/usr/bin:/bin:/usr/sbin:/sbin`), which does not include `~/.local/bin` where
> the standard uv installer puts `uvx`. A plain `"command": "uvx"` fails there
> with nothing but a generic "server failed" message. Run `which uvx` to get
> your path, or symlink uv somewhere already on the default `PATH`.

### Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
webperf-mcp
```

## Configuration

When installed as an extension, Claude Desktop collects these for you; you only
need them when configuring a client by hand.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `WEBPERF_API_KEY` | yes | — | Your premium api.webperf.se key |
| `WEBPERF_API_BASE` | no | `https://api.webperf.se` | API base URL (e.g. for staging) |
| `WEBPERF_HTTP_TIMEOUT` | no | `30` | Per-request timeout in seconds |

These are read from the process environment — set them in your MCP client's
`env` block. The server does not read a `.env` file.

## Building the bundle (maintainers)

[`manifest.json`](manifest.json) describes the extension: the `uv` runtime, the
four tools, and the `user_config` fields Claude Desktop prompts for.

```bash
npx @anthropic-ai/mcpb validate manifest.json
npx @anthropic-ai/mcpb pack . webperf-mcp-0.1.0.mcpb
```

Attach the resulting `.mcpb` to a GitHub release so the download link above
resolves. Keep `version` in `manifest.json` in step with `pyproject.toml`.

`.mcpbignore` keeps `.env`, virtualenvs and build artifacts out of the bundle.
**Check it before publishing** — `pack` zips the working directory, so a stray
secret would ship to every user. Verify with:

```bash
unzip -l webperf-mcp-0.1.0.mcpb
```

## License

MIT
