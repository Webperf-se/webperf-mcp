"""Campaign URL tagging against an organisation's naming convention.

Pure functions, no I/O: the server loads the convention (from Webperf Cloud or
a local file) and hands it in. The point of doing this in code rather than in
the model is that the output is exact. A model asked to "follow the
convention" drifts (fb one week, facebook the next); a function that only
accepts listed values does not.

The convention format is the same one api.webperf.se validates for
/0.1/context/campaign_convention:

    {
      "format": "utm" | "mtm",
      "separator": "_" | "-",
      "required": ["source", "medium", "campaign"],
      "notes": "free text",
      "<param>": {
        "values": [...],            # allowed values
        "aliases": {"FB": "facebook"},
        "free": true,               # allow values outside `values`
        "template": "{org}_{yyyy}_{name}",
        "only_when_medium": ["cpc"]
      }
    }

where <param> is source, medium, campaign, content or term. Every value is
lowercase ASCII: a-z, 0-9, _ and -.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PARAMS = ("source", "medium", "campaign", "content", "term")
FORMATS = ("utm", "mtm")
SEPARATORS = ("_", "-")
PARAM_KEYS = {"values", "aliases", "free", "template", "only_when_medium"}
TOP_KEYS = {"format", "separator", "required", "notes"} | set(PARAMS)

TOKEN_RE = re.compile(r"^[a-z0-9_-]{1,100}$")
TEMPLATE_RE = re.compile(r"^[a-z0-9_{}-]{1,200}$")
MAX_VALUES = 500
MAX_ALIAS_LENGTH = 100
MAX_NOTES = 2000

# Letters NFKD does not split into base letter + accent.
_FOLD = str.maketrans({"æ": "ae", "ø": "o", "ß": "ss", "đ": "d", "ł": "l", "þ": "th"})

# Template placeholders with a fixed shape. Anything else in braces ({org},
# {name}) is one or more ASCII words joined by _ or -.
_PLACEHOLDERS = {
    "yyyy": r"\d{4}",
    "yy": r"\d{2}",
    "q": r"[1-4]",
    "mm": r"(?:0[1-9]|1[0-2])",
    "dd": r"(?:0[1-9]|[12]\d|3[01])",
    "ww": r"(?:0[1-9]|[1-4]\d|5[0-3])",
}
_WORDS = r"[a-z0-9]+(?:[_-][a-z0-9]+)*"

# GA4 default channel groups that can be reached on utm_medium alone, from
# Google's documentation (support.google.com/analytics/answer/9756891,
# read 2026-10-16). A medium outside these lands in Unassigned unless the
# source is one of Google's listed sites.
_GA4_PAID = re.compile(r"^(.*cp.*|ppc|retargeting|paid.*)$")
_GA4_EXACT = {
    **dict.fromkeys(("email", "e-mail", "e_mail"), "Email"),
    **dict.fromkeys(("social", "social-network", "social-media", "sm"), "Organic Social"),
    **dict.fromkeys(("display", "banner", "expandable", "interstitial", "cpm"), "Display"),
    "affiliate": "Affiliates",
    **dict.fromkeys(("referral", "app", "link"), "Referral"),
    "audio": "Audio",
    "sms": "SMS",
    "organic": "Organic Search",
    "ai-assistant": "AI Assistant",
}

STARTER_CONVENTION: dict[str, Any] = {
    "format": "utm",
    "separator": "_",
    "required": ["source", "medium", "campaign"],
    "source": {
        "values": ["facebook", "linkedin", "instagram", "google", "nyhetsbrev", "qr_code"],
        "aliases": {"fb": "facebook", "ln": "linkedin", "ig": "instagram"},
    },
    "medium": {
        "values": ["social", "cpc", "email", "display", "affiliate", "referral", "print"],
        "aliases": {"epost": "email", "e-post": "email", "mejl": "email", "ppc": "cpc"},
    },
    "campaign": {"template": "{org}_{yyyy}_{name}"},
    "content": {"free": True},
    "term": {"free": True, "only_when_medium": ["cpc"]},
    "notes": (
        "Starter convention. Replace {org} in the campaign template with your "
        "organisation's prefix, and trim source and medium to the channels you use."
    ),
}


# ---------------------------------------------------------------------------
# Convention checks
# ---------------------------------------------------------------------------


def _check_tokens(raw: Any, where: str, errors: list[str]) -> list[str]:
    if not isinstance(raw, list):
        errors.append(f"{where} must be a list")
        return []
    if len(raw) > MAX_VALUES:
        errors.append(f"{where} has {len(raw)} entries, max is {MAX_VALUES}")
    bad = [v for v in raw if not isinstance(v, str) or not TOKEN_RE.match(v)]
    if bad:
        errors.append(f"{where} has values outside a-z, 0-9, _ and -: {bad[:10]}")
    dupes = sorted({v for v in raw if isinstance(v, str) and raw.count(v) > 1})
    if dupes:
        errors.append(f"{where} lists values more than once: {dupes[:10]}")
    return [v for v in raw if isinstance(v, str)]


def validate_convention(conv: Any) -> list[str]:
    """Problems with a convention, or [] if it holds. Mirrors the API's rules."""
    if not isinstance(conv, dict):
        return ["the convention must be a JSON object"]
    errors: list[str] = []
    unknown = sorted(set(conv) - TOP_KEYS)
    if unknown:
        errors.append(f"unknown keys: {unknown}")
    if conv.get("format") not in FORMATS:
        errors.append(f"format must be one of {list(FORMATS)}")
    if "separator" in conv and conv["separator"] not in SEPARATORS:
        errors.append(f"separator must be one of {list(SEPARATORS)}")
    required = conv.get("required", [])
    if not isinstance(required, list) or any(r not in PARAMS for r in required):
        errors.append(f"required must be a list drawn from {list(PARAMS)}")
    notes = conv.get("notes", "")
    if not isinstance(notes, str) or len(notes) > MAX_NOTES:
        errors.append(f"notes must be text of at most {MAX_NOTES} characters")
    for name in PARAMS:
        if name not in conv:
            continue
        rules = conv[name]
        if not isinstance(rules, dict):
            errors.append(f"{name} must be an object")
            continue
        extra = sorted(set(rules) - PARAM_KEYS)
        if extra:
            errors.append(f"{name} has unknown keys: {extra}")
        values = []
        if "values" in rules:
            values = _check_tokens(rules["values"], f"{name}.values", errors)
        aliases = rules.get("aliases", {})
        if not isinstance(aliases, dict):
            errors.append(f"{name}.aliases must be an object")
        else:
            for alias, target in aliases.items():
                if not alias.strip() or len(alias) > MAX_ALIAS_LENGTH:
                    errors.append(
                        f"{name}.aliases has an empty or too long alias: {alias[:20]!r}"
                    )
                elif target not in values:
                    errors.append(
                        f"{name}.aliases maps {alias!r} to {target!r}, "
                        f"which is not in {name}.values"
                    )
        if "free" in rules and not isinstance(rules["free"], bool):
            errors.append(f"{name}.free must be true or false")
        template = rules.get("template")
        if template is not None and (
            not isinstance(template, str) or not TEMPLATE_RE.match(template)
        ):
            errors.append(f"{name}.template may only contain a-z, 0-9, _, - and {{placeholders}}")
        if "only_when_medium" in rules:
            _check_tokens(rules["only_when_medium"], f"{name}.only_when_medium", errors)
    return errors


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def normalise(raw: str, separator: str = "_") -> str:
    """Lowercase ASCII: å/ä -> a, ö -> o, spaces -> separator, the rest dropped."""
    text = raw.strip().lower().translate(_FOLD)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"\s+", separator, text)
    text = re.sub(r"[^a-z0-9_-]", "", text)
    text = re.sub(r"([_-])[_-]+", r"\1", text)
    return text.strip("_-")[:100]


def _template_regex(template: str) -> re.Pattern:
    parts = re.split(r"(\{[a-z0-9_-]+\})", template)
    out = []
    for part in parts:
        if part.startswith("{") and part.endswith("}"):
            out.append(_PLACEHOLDERS.get(part[1:-1], _WORDS))
        else:
            out.append(re.escape(part))
    return re.compile("^" + "".join(out) + "$")


def _alias(rules: dict, raw: str) -> str | None:
    aliases = rules.get("aliases") or {}
    if raw in aliases:
        return aliases[raw]
    folded = raw.strip().lower()
    for alias, target in aliases.items():
        if alias.strip().lower() == folded:
            return target
    return None


def resolve(conv: dict, param: str, raw: str) -> dict:
    """Turn what the user typed into the convention's value for `param`.

    Returns {"value", "changed_from", "error", "suggestions"}. `error` is None
    when the value is usable; `changed_from` is set when it was rewritten
    (alias or normalisation), so the caller can say so instead of doing it
    silently.
    """
    rules = conv.get(param) or {}
    separator = conv.get("separator", "_")
    result: dict[str, Any] = {"value": None, "changed_from": None, "error": None, "suggestions": []}

    value = _alias(rules, raw)
    if value is None:
        value = normalise(raw, separator)
        value = _alias(rules, value) or value
    if value != raw:
        result["changed_from"] = raw
    result["value"] = value

    if not value:
        result["error"] = f"{param}: {raw!r} has no usable characters (a-z, 0-9, _ and -)"
        return result

    allowed = rules.get("values") or []
    if allowed and not rules.get("free") and value not in allowed:
        result["error"] = f"{param}: {value!r} is not in the convention"
        result["suggestions"] = difflib.get_close_matches(value, allowed, n=3, cutoff=0.5)
        result["allowed"] = allowed
        return result

    template = rules.get("template")
    if template and value not in allowed and not _template_regex(template).match(value):
        result["error"] = f"{param}: {value!r} does not follow the template {template!r}"
        result["template"] = template
    return result


def ga4_channel(medium: str) -> str | None:
    """The GA4 default channel group utm_medium alone puts traffic in, or None."""
    if medium in _GA4_EXACT:
        return _GA4_EXACT[medium]
    if _GA4_PAID.match(medium):
        return "Paid Search, Paid Social or Paid Other, depending on source"
    if medium.endswith("push") or "mobile" in medium or "notification" in medium:
        return "Mobile Push Notifications"
    if "video" in medium:
        return "Organic Video"
    return None


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def _key(fmt: str, param: str) -> str:
    if param == "term" and fmt == "mtm":
        return "mtm_kwd"
    return f"{fmt}_{param}"


# The keys this module writes. Other utm_/mtm_ parameters (utm_id,
# utm_source_platform, mtm_cid, ...) are left alone: they are outside the
# convention, not wrong.
_CAMPAIGN_KEYS = {f"{fmt}_{p}" for fmt in FORMATS for p in PARAMS} - {"mtm_term"} | {"mtm_kwd"}


def _is_campaign_key(key: str) -> bool:
    return key.lower() in _CAMPAIGN_KEYS


def check_target(url: str) -> str | None:
    """Why `url` cannot be tagged, or None."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"{url!r} is not a full http(s) address"
    return None


def tag_url(url: str, fmt: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """Add campaign parameters to `url`. Returns (tagged url, removed old tags).

    Old utm_/mtm_ parameters are replaced, not duplicated. Other parameters
    and the #fragment stay where they are, with the tags before the fragment.
    """
    parts = urlsplit(url.strip())
    query = parse_qsl(parts.query, keep_blank_values=True)
    removed = [f"{k}={v}" for k, v in query if _is_campaign_key(k)]
    kept = [(k, v) for k, v in query if not _is_campaign_key(k)]
    tags = [(_key(fmt, p), values[p]) for p in PARAMS if values.get(p)]
    return urlunsplit(parts._replace(query=urlencode(kept + tags))), removed


def read_tags(url: str) -> tuple[str | None, dict[str, str], list[str]]:
    """(format, {param: value}, problems) from an already tagged URL."""
    problems: list[str] = []
    found: dict[str, dict[str, str]] = {"utm": {}, "mtm": {}}
    for key, value in parse_qsl(urlsplit(url.strip()).query, keep_blank_values=True):
        low = key.lower()
        if not _is_campaign_key(low):
            continue
        fmt, name = low[:3], low[4:]
        if name == "kwd":
            name = "term"
        if name in found[fmt]:
            problems.append(f"{key} appears more than once")
        found[fmt][name] = value
    if found["utm"] and found["mtm"]:
        problems.append("the URL mixes utm_ and mtm_ parameters")
    if not found["utm"] and not found["mtm"]:
        return None, {}, problems
    fmt = "mtm" if len(found["mtm"]) > len(found["utm"]) else "utm"
    return fmt, found[fmt], problems


def evaluate(conv: dict, values: dict[str, str | None]) -> dict:
    """Check a set of raw parameter values against the convention.

    Returns {"values", "changes", "errors", "warnings", "suggestions"}, where
    values holds the resolved values for the parameters that were given.
    """
    resolved: dict[str, str] = {}
    changes: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []
    suggestions: dict[str, Any] = {}

    for param in PARAMS:
        raw = values.get(param)
        if raw is None or not str(raw).strip():
            continue
        r = resolve(conv, param, str(raw))
        if r["error"]:
            errors.append(r["error"])
            hint = {k: r[k] for k in ("suggestions", "allowed", "template") if r.get(k)}
            if hint:
                suggestions[param] = hint
            continue
        resolved[param] = r["value"]
        if r["changed_from"] is not None:
            changes.append(f"{param}: {r['changed_from']!r} -> {r['value']!r}")

    for param in conv.get("required", []):
        if not (values.get(param) or "").strip():
            errors.append(f"{param} is required by the convention")

    medium = resolved.get("medium")
    for param in PARAMS:
        only = (conv.get(param) or {}).get("only_when_medium")
        if only and param in resolved and medium not in only:
            errors.append(f"{param} is only used with medium {only}, not {medium!r}")

    content = resolved.get("content")
    others = (resolved.get("source"), resolved.get("medium"), resolved.get("campaign"))
    if content and content in others:
        warnings.append(
            f"content {content!r} repeats another parameter. Use content for what differs "
            "between links in the same campaign: placement, variant or format."
        )

    if conv.get("format", "utm") == "utm" and medium and ga4_channel(medium) is None:
        warnings.append(
            f"GA4 does not recognise medium {medium!r} on its own. Unless the source is a "
            "site Google lists for a channel, the visits land in Unassigned in the default "
            "channel report."
        )

    return {
        "values": resolved,
        "changes": changes,
        "errors": errors,
        "warnings": warnings,
        "suggestions": suggestions,
    }


def same_site(url: str, placement_url: str) -> bool:
    a = (urlsplit(url.strip()).hostname or "").removeprefix("www.")
    b = (urlsplit(placement_url.strip()).hostname or "").removeprefix("www.")
    return bool(a) and a == b
