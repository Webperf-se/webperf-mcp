"""Campaign tagging: the pure rules in campaign.py and the tools around them.

The cloud convention goes through httpx.MockTransport standing in for
api.webperf.se; the local one through a file in tmp_path.
"""

from __future__ import annotations

import json

import httpx
import pytest

from webperf_mcp import campaign as tagging
from webperf_mcp import server

GOOD_KEY = "abcdefghij1234567890"

CONV = {
    "format": "utm",
    "separator": "_",
    "required": ["source", "medium", "campaign"],
    "source": {"values": ["facebook", "linkedin", "nyhetsbrev"],
               "aliases": {"FB": "facebook", "ln": "linkedin"}},
    "medium": {"values": ["social", "email", "cpc", "print"], "aliases": {"epost": "email"}},
    "campaign": {"template": "acme_{yyyy}q{q}_{name}"},
    "content": {"free": True},
    "term": {"free": True, "only_when_medium": ["cpc"]},
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", "")
    monkeypatch.delenv("WEBPERF_CAMPAIGN_CONVENTION", raising=False)
    monkeypatch.setattr(server, "_CONFIG_ERROR", None)
    server._cache.clear()
    yield
    server._CLIENT = None


@pytest.fixture
def local(tmp_path, monkeypatch):
    path = tmp_path / "convention.json"
    monkeypatch.setenv("WEBPERF_CAMPAIGN_CONVENTION", str(path))
    return path


@pytest.fixture
def local_conv(local):
    local.write_text(json.dumps(CONV), encoding="utf-8")
    return local


class FakeCloud:
    """The context endpoint, with revisions, behind a MockTransport."""

    def __init__(self, value=None, revision=0):
        self.value, self.revision = value, revision
        self.requests: list[httpx.Request] = []
        self.conflict_once = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == "/0.1/context/campaign_convention"
        assert request.headers["site-id"] == "42"
        if request.method == "GET":
            return httpx.Response(200, json={
                "site_id": 42, "revision": self.revision, "value": self.value,
                "date_modified": None, "modified_by": "Anna"})
        body = json.loads(request.content)
        if self.conflict_once:
            self.conflict_once = False
            self.revision += 1
            return httpx.Response(409, json={"message": "Someone saved", "revision": self.revision})
        if body["base_revision"] != self.revision:
            return httpx.Response(409, json={"message": "Someone saved", "revision": self.revision})
        self.value, self.revision = body["value"], self.revision + 1
        return httpx.Response(200, json={"site_id": 42, "revision": self.revision})


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", GOOD_KEY)
    fake = FakeCloud(value=json.loads(json.dumps(CONV)), revision=3)
    server._CLIENT = server._make_client(transport=httpx.MockTransport(fake))
    return fake


# --- rules ----------------------------------------------------------------


def test_starter_convention_is_valid():
    assert tagging.validate_convention(tagging.STARTER_CONVENTION) == []
    assert tagging.validate_convention(CONV) == []


@pytest.mark.parametrize("raw, expected", [
    ("Höstkampanj", "hostkampanj"),
    ("Sommar Kampanj 2026", "sommar_kampanj_2026"),
    ("Blå annons!", "bla_annons"),
    ("Æble Smørrebrød", "aeble_smorrebrod"),
    ("  __x__  ", "x"),
])
def test_normalise_folds_to_lowercase_ascii(raw, expected):
    assert tagging.normalise(raw) == expected


def test_aliases_match_regardless_of_case():
    assert tagging.resolve(CONV, "source", "fb")["value"] == "facebook"
    assert tagging.resolve(CONV, "source", "LN")["value"] == "linkedin"


def test_unknown_value_is_refused_with_suggestions():
    r = tagging.resolve(CONV, "source", "facebok")
    assert r["error"] and r["suggestions"] == ["facebook"]


def test_template_is_enforced():
    assert tagging.resolve(CONV, "campaign", "acme_2026q4_host")["error"] is None
    assert tagging.resolve(CONV, "campaign", "acme_2026q5_host")["error"]
    assert tagging.resolve(CONV, "campaign", "hostkampanj")["error"]


def test_values_must_be_ascii_in_the_convention():
    bad = dict(CONV, source={"values": ["utemöbler"]})
    assert any("outside a-z" in e for e in tagging.validate_convention(bad))


def test_ga4_channels_by_medium():
    assert tagging.ga4_channel("email") == "Email"
    assert tagging.ga4_channel("social") == "Organic Social"
    assert tagging.ga4_channel("cpc").startswith("Paid")
    assert tagging.ga4_channel("print") is None
    assert tagging.ga4_channel("epost") is None


def test_tag_url_replaces_old_tags_and_keeps_the_rest():
    url, removed = tagging.tag_url(
        "https://acme.se/p?a=1&utm_source=old&utm_id=7#top", "utm",
        {"source": "facebook", "medium": "social", "campaign": "acme_2026q4_host"})
    assert url == ("https://acme.se/p?a=1&utm_id=7&utm_source=facebook&utm_medium=social"
                   "&utm_campaign=acme_2026q4_host#top")
    assert removed == ["utm_source=old"]


def test_mtm_uses_kwd_for_term():
    url, _ = tagging.tag_url("https://acme.se/", "mtm", {"source": "google", "term": "skor"})
    assert url == "https://acme.se/?mtm_source=google&mtm_kwd=skor"


# --- build_campaign_url ---------------------------------------------------


def test_build_maps_aliases_and_reports_the_change(local_conv):
    r = server.build_campaign_url("https://acme.se/kampanj", "FB", "Social", "acme_2026q4_host")
    assert r["ok"] is True
    assert r["url"] == ("https://acme.se/kampanj?utm_source=facebook&utm_medium=social"
                        "&utm_campaign=acme_2026q4_host")
    assert "source: 'FB' -> 'facebook'" in r["changes"]
    assert r["convention_from"] == "local_file"


def test_build_refuses_values_outside_the_convention(local_conv):
    r = server.build_campaign_url("https://acme.se/", "tiktok", "social", "acme_2026q4_host")
    assert r["ok"] is False and "url" not in r
    assert any("tiktok" in e for e in r["errors"])
    assert r["suggestions"]["source"]["allowed"] == ["facebook", "linkedin", "nyhetsbrev"]


def test_build_requires_the_required_parameters(local_conv):
    r = server.build_campaign_url("https://acme.se/", "facebook", "", "acme_2026q4_host")
    assert r["ok"] is False and "medium is required by the convention" in r["errors"]


def test_term_only_with_paid_search(local_conv):
    r = server.build_campaign_url("https://acme.se/", "facebook", "social", "acme_2026q4_host",
                                  term="skor")
    assert r["ok"] is False


def test_build_warns_about_unassigned_medium_and_internal_links(local_conv):
    r = server.build_campaign_url("https://acme.se/", "nyhetsbrev", "print", "acme_2026q4_host",
                                  placement_url="https://www.acme.se/start")
    assert r["ok"] is True
    assert any("Unassigned" in w for w in r["warnings"])
    assert any("internal links" in w for w in r["warnings"])


def test_build_refuses_a_relative_url(local_conv):
    with pytest.raises(server.WebperfError):
        server.build_campaign_url("acme.se/kampanj", "facebook", "social", "acme_2026q4_host")


def test_build_without_any_convention_says_how_to_start():
    with pytest.raises(server.WebperfError) as exc:
        server.build_campaign_url("https://acme.se/", "facebook", "social", "x")
    assert exc.value.kind == "not_found"


# --- check_campaign_urls --------------------------------------------------


def test_check_sorts_urls_by_status(local_conv):
    r = server.check_campaign_urls([
        "https://acme.se/?utm_source=facebook&utm_medium=social&utm_campaign=acme_2026q4_host",
        "https://acme.se/?utm_source=FB&utm_medium=epost&utm_campaign=acme_2026q4_host",
        "https://acme.se/?mtm_source=facebook&mtm_medium=social&mtm_campaign=acme_2026q4_host",
        "https://acme.se/?utm_source=tiktok&utm_medium=social&utm_campaign=acme_2026q4_host",
        "https://acme.se/",
        "not a url",
    ])
    statuses = [x["status"] for x in r["results"]]
    assert statuses == ["compliant", "fixable", "fixable", "non_compliant", "untagged", "invalid"]
    assert r["results"][1]["fixed_url"] == (
        "https://acme.se/?utm_source=facebook&utm_medium=email&utm_campaign=acme_2026q4_host")
    assert "format: mtm -> utm" in r["results"][2]["changes"]
    assert r["summary"] == {"compliant": 1, "fixable": 2, "non_compliant": 1, "untagged": 1,
                            "invalid": 1}


def test_check_is_capped(local_conv):
    with pytest.raises(server.WebperfError):
        server.check_campaign_urls(["https://acme.se/"] * 51)


# --- where the convention lives -------------------------------------------


def test_no_key_no_file_offers_the_starter():
    r = server.get_campaign_convention()
    assert r["convention"] is None and r["starter"] == tagging.STARTER_CONVENTION


def test_key_without_site_id_and_without_file_asks_for_site(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", GOOD_KEY)
    with pytest.raises(server.WebperfError) as exc:
        server.get_campaign_convention()
    assert "site_id" in str(exc.value)


def test_cloud_wins_over_local_file_when_site_id_is_given(cloud, local_conv):
    r = server.get_campaign_convention(site_id=42)
    assert r["stored_in"] == "webperf_cloud" and r["revision"] == 3
    assert any("ignored" in n for n in r["notes"])
    assert cloud.requests[0].headers["api-key"] == GOOD_KEY


def test_invalid_local_file_is_reported(local):
    local.write_text('{"format": "ga"}', encoding="utf-8")
    with pytest.raises(server.WebperfError) as exc:
        server.get_campaign_convention()
    assert exc.value.kind == "config" and "format" in str(exc.value)


# --- saving ---------------------------------------------------------------


def test_save_to_cloud_sends_base_revision(cloud):
    r = server.save_campaign_convention(CONV, site_id=42, base_revision=3)
    assert r == {"saved": True, "stored_in": "webperf_cloud", "site_id": 42, "revision": 4}
    sent = json.loads(cloud.requests[-1].content)
    assert sent == {"value": CONV, "base_revision": 3}


def test_save_to_cloud_needs_base_revision(cloud):
    with pytest.raises(server.WebperfError) as exc:
        server.save_campaign_convention(CONV, site_id=42)
    assert "base_revision" in str(exc.value)
    assert cloud.requests == []


def test_stale_save_is_a_conflict(cloud):
    with pytest.raises(server.WebperfError) as exc:
        server.save_campaign_convention(CONV, site_id=42, base_revision=1)
    assert exc.value.kind == "conflict"


def test_invalid_convention_is_not_sent(cloud):
    with pytest.raises(server.WebperfError):
        server.save_campaign_convention(dict(CONV, format="ga"), site_id=42, base_revision=3)
    assert cloud.requests == []


def test_save_to_local_file(local):
    r = server.save_campaign_convention(CONV)
    assert r["stored_in"] == "local_file"
    assert json.loads(local.read_text(encoding="utf-8")) == CONV


def test_add_value_folds_and_saves(cloud):
    r = server.add_campaign_value("source", "Instagram Stories", site_id=42, aliases=["IG"])
    assert r["added"] is True and r["value"] == "instagram_stories"
    assert r["changed_from"] == "Instagram Stories"
    assert "instagram_stories" in cloud.value["source"]["values"]
    assert cloud.value["source"]["aliases"]["IG"] == "instagram_stories"


def test_add_value_retries_once_on_conflict(cloud):
    cloud.conflict_once = True
    r = server.add_campaign_value("medium", "sms", site_id=42)
    assert r["added"] is True and r["revision"] == 5


def test_add_existing_value_changes_nothing(local_conv):
    before = local_conv.read_text(encoding="utf-8")
    r = server.add_campaign_value("source", "facebook")
    assert r["added"] is False
    assert local_conv.read_text(encoding="utf-8") == before


def test_add_alias_already_pointing_elsewhere_is_refused(local_conv):
    with pytest.raises(server.WebperfError):
        server.add_campaign_value("source", "fb_ads", aliases=["FB"])


def test_validation_errors_from_the_api_are_passed_on(monkeypatch):
    monkeypatch.setenv("WEBPERF_API_KEY", GOOD_KEY)
    server._CLIENT = server._make_client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"message": "Validation error",
                                            "errors": ["format must be one of ['utm', 'mtm']"]})))
    with pytest.raises(server.WebperfError) as exc:
        server._request("PUT", server.CONTEXT_PATH, headers={"site-id": "42"}, json_body={})
    assert "format must be one of" in str(exc.value)


def test_setup_prompt_mentions_matomo_and_ascii():
    text = server.setup_campaign_convention("42")
    assert "site_id 42" in text
    assert "Marketing Campaigns Reporting" in text and "lowercase ASCII" in text
