# Security policy

## Reporting a vulnerability

Email **support@webperf.se** with "webperf-mcp" in the subject. Please do not
open a public issue for anything exploitable.

Include what you did, what happened, and what you expected. If it involves the
API rather than this client, say so — they are maintained separately.

We aim to acknowledge within a few working days.

## Supported versions

Only the latest release. This is a small client with no long-term support
branches; fixes land in a new tag.

## Scope

This repository is a **read-only HTTP client by default**. It holds no data,
has no database access and grants no privileges of its own. Every keyed request
carries the user's personal `api-key` header and all authorization is enforced
by api.webperf.se. The single write, `order_retest`, is registered only when
the install sets `WEBPERF_ALLOW_ORDERING=true`; it spends the user's own
credits and nothing else.

In scope:

- Leaking the configured API key (into tool output, error text, logs, URLs or
  the packed `.mcpb` bundle).
- Sending the key anywhere other than the configured, https, API base.
- Reaching an endpoint that mutates state without ordering enabled, or any
  mutation other than `POST /0.1/retest/` with it enabled. Every other tool
  must be a read.
- `order_retest` being registered, or callable, when `WEBPERF_ALLOW_ORDERING`
  is unset or false.
- Path or parameter handling in `get_audit_file` that escapes the documented
  allowlist.
- Anything in the release pipeline that could put unreviewed code into a
  published bundle.

Out of scope, and better reported to api.webperf.se directly:

- Authorization decisions about which sites a key may read. This client cannot
  widen them; it can only ask.
- Rate limiting, caching headers and error semantics of the upstream API.

## Known design limits

**Report text is attacker-influenceable.** Tool output quotes content from the
websites Webperf tested, unescaped. A tested site can therefore place text in
the model's context. This server can act on none of it, but a client that also
holds shell or filesystem tools should treat it as untrusted data. See the
README's security model section.

**Manual installs store the key in plaintext.** That is how MCP client configs
work. The `.mcpb` extension path marks the key `sensitive`, which puts it in
the operating system keychain instead.
