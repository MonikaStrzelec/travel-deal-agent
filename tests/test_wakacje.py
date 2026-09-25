"""Injected HTTP only: these tests never contact wakacje.pl or use Playwright."""

import json
from pathlib import Path
from urllib.error import URLError

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import ProviderConfig
from travel_deal_agent.providers.http import Response
from travel_deal_agent.providers.registry import build_providers
from travel_deal_agent.providers.wakacje import (
    BASE,
    CONFIRMED_SEARCH_QUERY,
    LISTING_PATH,
    WakacjeProvider,
)

CONFIG: ProviderConfig = {"enabled": True, "interval_seconds": 3600}

PAGE_1_PATH = f"{LISTING_PATH}?{CONFIRMED_SEARCH_QUERY}&src=fromFilters"


def page_n_path(page: int) -> str:
    if page == 1:
        return PAGE_1_PATH
    return f"{LISTING_PATH}?str-{page},{CONFIRMED_SEARCH_QUERY}"


def geo(name: str, slug: str) -> dict[str, object]:
    return {"name": name, "slug": slug, "urlName": slug, "id": 1}


def raw_offer(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 211281,
        "name": "Laur Experience & Elegance",
        "urlName": "laur-experience-elegance",
        "place": {
            "country": geo("Turcja", "turcja"),
            "region": geo("Wybrzeże Egejskie", "wybrzeze-egejskie"),
            "city": geo("Didim", "didim"),
        },
        "category": 5,
        "price": 700,
        "originalCurrency": "PLN",
        "departureDate": "2026-10-13",
        "returnDate": "2026-10-20",
        "duration": 7,
        "departurePlace": "Rzeszów",
        "departurePlaceCode": "RZE",
        "departurePlaces": ["Rzeszów", "Wrocław"],
        "service": 1,
        "serviceDesc": "Ultra All Inclusive",
        "ratingValue": 8,
        "ratingReservationCount": 336,
    }
    base.update(overrides)
    return base


def listing_html(offers: list[dict[str, object]]) -> str:
    data = {
        "props": {
            "dehydratedState": {
                "queries": [
                    {
                        "queryKey": ["listingOffers", "x"],
                        "state": {"data": {"offers": {"data": offers, "count": len(offers)}}},
                    }
                ]
            }
        }
    }
    text = json.dumps(data, ensure_ascii=False)
    return f'<script id="__NEXT_DATA__" type="application/json">{text}</script>'


class FakeTransport:
    def __init__(self, responses: list[Response | Exception]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, timeout: float) -> Response:
        assert timeout > 0
        self.urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def response(text: str, status: int = 200) -> Response:
    return Response(status, text, {})


def combo_page_response() -> Response:
    """One page of the confirmed combined search query: a mix of the airports
    it targets (KTW/LCJ/WAW/WRO), matching the real live confirmation's shape
    (RECONNAISSANCE.md sec 25) -- not a single-airport page like the old
    per-airport architecture used.
    """
    return response(
        listing_html(
            [
                raw_offer(
                    id=1,
                    departurePlace="Katowice",
                    departurePlaceCode="KTW",
                    departurePlaces=["Katowice"],
                ),
                raw_offer(
                    id=2,
                    departurePlace="Łódź",
                    departurePlaceCode="LCJ",
                    departurePlaces=["Łódź"],
                ),
            ]
        )
    )


ALLOW_ROBOTS = "User-agent: *\nDisallow: /ajax/\nDisallow: /oferty/*?*\nDisallow: /*,*/"


def test_confirmed_search_query_is_exactly_the_confirmed_combo() -> None:
    # Arrange / Act / Assert: pins the exact, live-verified combined query
    # (RECONNAISSANCE.md sec 24-25) -- no token added, removed, or reordered
    # relative to the human-confirmed URL, except the robots-illegal price cap.
    assert CONFIRMED_SEARCH_QUERY == (
        "samolotem,all-inclusive,HB,ZO,FB,3-gwiazdkowe,ocena-8,"
        "z-katowic,z-lodzi,z-warszawy,z-wroclawia,tanio,za-osobe"
    )
    assert "do-1500zl" not in CONFIRMED_SEARCH_QUERY


def test_fetch_reads_robots_then_paginates_the_combined_query(settings: Settings) -> None:
    # Arrange: production config values (tests/fixtures/test_config.json,
    # mirroring config.json) -- max_pages=3, max_requests=4 -- proving the
    # request budget is exactly enough for a full scan (robots + 3 pages).
    cfg = settings.providers["wakacje.pl"]
    max_pages = cfg.get("max_pages", 2)
    assert max_pages is not None
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), *[combo_page_response() for _ in range(max_pages)]]
    )
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    # Act
    offers = provider.fetch()
    # Assert
    assert len(offers) == 2 * max_pages
    assert all(o.provider == "wakacje.pl" for o in offers)
    expected = [BASE + "/robots.txt"] + [BASE + page_n_path(p) for p in range(1, max_pages + 1)]
    assert transport.urls == expected
    assert len(transport.urls) == cfg.get("max_requests")
    assert len(transport.urls) == 4


def test_page_one_and_page_two_use_the_confirmed_shapes_exactly(settings: Settings) -> None:
    # Arrange: page 1 was live-tested with the trailing "&src=fromFilters"
    # (RECONNAISSANCE.md sec 25); page 2's shape is the site's own real,
    # on-page pagination link, which omits that suffix -- each page uses
    # exactly what was confirmed for it.
    cfg: ProviderConfig = {**settings.providers["wakacje.pl"], "max_pages": 2, "max_requests": 3}
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), combo_page_response(), combo_page_response()]
    )
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    provider.fetch()
    assert transport.urls == [
        BASE + "/robots.txt",
        BASE + f"{LISTING_PATH}?{CONFIRMED_SEARCH_QUERY}&src=fromFilters",
        BASE + f"{LISTING_PATH}?str-2,{CONFIRMED_SEARCH_QUERY}",
    ]


def test_fetch_carries_the_real_variant_href_into_the_offer(settings: Settings) -> None:
    # Arrange: the fetched page carries the real `data-test-offer-id` anchor for
    # offer 1 (KTW) only; its query string pins the exact stay variant.
    variant_href = (
        f"{BASE}/oferty/turcja/wybrzeze-egejskie/didim/laur-experience-elegance-1.html"
        "?od-2026-10-13,7-dni,all-inclusive,z-katowic"
    )
    anchor = f'<a data-test-offer-id="1" href="{variant_href}">Laur Experience</a>'
    page = response(combo_page_response().text + anchor)
    cfg: ProviderConfig = {**settings.providers["wakacje.pl"], "max_pages": 1, "max_requests": 2}
    transport = FakeTransport([response(ALLOW_ROBOTS), page])
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    by_airport = {o.departure_airport: o for o in provider.fetch()}
    # Assert: the exact href reaches the Offer untouched; the card without an
    # anchor keeps the reconstructed URL.
    assert by_airport["KTW"].url == variant_href
    assert by_airport["LCJ"].url == (
        f"{BASE}/oferty/turcja/wybrzeze-egejskie/didim/laur-experience-elegance-2.html"
    )


# --- listing only: never a detail page, never an API endpoint -----------------------


def test_never_requests_a_detail_page(settings: Settings) -> None:
    max_pages = settings.providers["wakacje.pl"].get("max_pages", 2)
    assert max_pages is not None
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), *[combo_page_response() for _ in range(max_pages)]]
    )
    provider = WakacjeProvider(
        settings.providers["wakacje.pl"], settings.filters, transport, sleep=lambda _: None
    )
    provider.fetch()
    assert all("/oferty/" not in url for url in transport.urls)


def test_never_calls_an_api_or_ajax_endpoint_directly(settings: Settings) -> None:
    max_pages = settings.providers["wakacje.pl"].get("max_pages", 2)
    assert max_pages is not None
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), *[combo_page_response() for _ in range(max_pages)]]
    )
    provider = WakacjeProvider(
        settings.providers["wakacje.pl"], settings.filters, transport, sleep=lambda _: None
    )
    provider.fetch()
    assert all("/ajax/" not in url and not url.endswith(".json") for url in transport.urls)


def test_confirmed_search_query_includes_cheapest_sort_and_all_four_airports() -> None:
    # Arrange / Act / Assert: unlike the old per-airport architecture (which
    # deliberately never queried "tanio", RECONNAISSANCE.md sec 13c), the
    # confirmed combined query DOES include it -- confirmed live to work when
    # combined with the airport filters and other business-matching filters
    # (sec 24-25), reversing the earlier constraint.
    assert "tanio" in CONFIRMED_SEARCH_QUERY
    for slug in ("z-katowic", "z-lodzi", "z-warszawy", "z-wroclawia"):
        assert slug in CONFIRMED_SEARCH_QUERY
    assert "za-osobe" in CONFIRMED_SEARCH_QUERY
    assert "do-1500zl" not in CONFIRMED_SEARCH_QUERY


def test_paginates_up_to_max_pages(settings: Settings) -> None:
    max_pages = settings.providers["wakacje.pl"].get("max_pages", 2)
    assert max_pages is not None
    assert max_pages == 3  # pins the production config.json value this test relies on
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), *[combo_page_response() for _ in range(max_pages)]]
    )
    provider = WakacjeProvider(
        settings.providers["wakacje.pl"], settings.filters, transport, sleep=lambda _: None
    )
    provider.fetch()
    assert transport.urls == [
        BASE + "/robots.txt",
        BASE + f"{LISTING_PATH}?{CONFIRMED_SEARCH_QUERY}&src=fromFilters",
        BASE + f"{LISTING_PATH}?str-2,{CONFIRMED_SEARCH_QUERY}",
        BASE + f"{LISTING_PATH}?str-3,{CONFIRMED_SEARCH_QUERY}",
    ]


def test_max_pages_one_means_a_single_page(settings: Settings) -> None:
    cfg: ProviderConfig = {**settings.providers["wakacje.pl"], "max_pages": 1, "max_requests": 2}
    transport = FakeTransport([response(ALLOW_ROBOTS), combo_page_response()])
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    provider.fetch()
    # Assert: no str-N page ever requested.
    assert transport.urls == [
        BASE + "/robots.txt",
        BASE + f"{LISTING_PATH}?{CONFIRMED_SEARCH_QUERY}&src=fromFilters",
    ]
    assert all("str-" not in url for url in transport.urls)


def test_pagination_stops_when_the_request_budget_is_reached(settings: Settings) -> None:
    # Arrange: max_pages asks for 3 pages, but the budget only allows for
    # robots + page 1 -- fetch() must stop cleanly instead of raising.
    cfg: ProviderConfig = {**settings.providers["wakacje.pl"], "max_pages": 3, "max_requests": 2}
    transport = FakeTransport([response(ALLOW_ROBOTS), combo_page_response()])
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    offers = provider.fetch()
    # Assert: only page 1 is reached; the loop breaks before requesting page 2.
    assert len(offers) == 2
    assert transport.urls == [
        BASE + "/robots.txt",
        BASE + f"{LISTING_PATH}?{CONFIRMED_SEARCH_QUERY}&src=fromFilters",
    ]


def test_full_confirmed_scan_fits_within_the_configured_cycle_seconds(settings: Settings) -> None:
    # Arrange: a full scan needs 4 requests (robots + 3 pages), spaced
    # request_gap_seconds apart. A controlled clock/sleep pair (same technique
    # as ITAKA's test_request_spacing_and_deadline) simulates that real spacing
    # without actually waiting.
    cfg = settings.providers["wakacje.pl"]
    max_pages = cfg.get("max_pages", 2)
    assert max_pages == 3
    assert cfg.get("max_requests") == 4
    gap = cfg.get("request_gap_seconds", 5)
    assert cfg["cycle_seconds"] > 3 * gap
    now = [0.0]
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), *[combo_page_response() for _ in range(max_pages)]]
    )
    provider = WakacjeProvider(
        cfg,
        settings.filters,
        transport,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    offers = provider.fetch()
    # Assert: all 4 requests complete; none are lost to a cycle-deadline abort.
    assert len(transport.urls) == 4
    assert len(offers) == 2 * max_pages


# --- transient network resilience (RECONNAISSANCE.md sec 22-23) ---------------------


@pytest.mark.parametrize(
    "transient_error", [TimeoutError("synthetic timeout"), URLError("synthetic connection failure")]
)
def test_transient_network_error_stops_pagination_but_keeps_earlier_pages(
    settings: Settings, transient_error: Exception
) -> None:
    # Arrange: page 1 succeeds, page 2 hits a transient network error -- no
    # retry, no page 3, but page 1's offers must survive.
    cfg: ProviderConfig = {**settings.providers["wakacje.pl"], "max_pages": 3, "max_requests": 4}
    transport = FakeTransport([response(ALLOW_ROBOTS), combo_page_response(), transient_error])
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    offers = provider.fetch()
    # Assert: no exception, page 3 never requested, page 1's offers survive.
    assert transport.urls == [
        BASE + "/robots.txt",
        BASE + f"{LISTING_PATH}?{CONFIRMED_SEARCH_QUERY}&src=fromFilters",
        BASE + f"{LISTING_PATH}?str-2,{CONFIRMED_SEARCH_QUERY}",
    ]
    assert len(offers) == 2


def test_transient_network_error_warning_names_the_page(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    cfg: ProviderConfig = {**settings.providers["wakacje.pl"], "max_pages": 3, "max_requests": 4}
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), combo_page_response(), TimeoutError("synthetic timeout")]
    )
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    provider.fetch()
    # Assert: enough context to know provider/page/cause, no traceback dump.
    assert "Wakacje.pl" in caplog.text
    assert "page 2" in caplog.text
    assert "Traceback" not in caplog.text


def test_robots_violation_on_page_two_still_fails_the_whole_provider(
    settings: Settings,
) -> None:
    # Arrange: page 1 succeeds; page 2's path is (synthetically) disallowed by
    # robots.txt -- this must still raise, never be treated as transient.
    transport = FakeTransport(
        [
            response(f"User-agent: *\nDisallow: /wczasy/?str-2,{CONFIRMED_SEARCH_QUERY}"),
            combo_page_response(),
        ]
    )
    provider = WakacjeProvider(
        {**settings.providers["wakacje.pl"], "max_pages": 3, "max_requests": 4},
        settings.filters,
        transport,
        sleep=lambda _: None,
    )
    # Act / Assert
    with pytest.raises(ValueError, match="robots"):
        provider.fetch()


def test_http_error_status_on_page_two_still_fails_the_whole_provider(
    settings: Settings,
) -> None:
    # Arrange: page 1 succeeds; page 2 comes back HTTP 403 -- RequestBudget
    # itself turns this into a ValueError ("no automatic retry"), which must
    # still propagate, never be swallowed as a transient network error.
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), combo_page_response(), response("", status=403)]
    )
    provider = WakacjeProvider(
        {**settings.providers["wakacje.pl"], "max_pages": 3, "max_requests": 4},
        settings.filters,
        transport,
        sleep=lambda _: None,
    )
    # Act / Assert
    with pytest.raises(ValueError, match="HTTP"):
        provider.fetch()


def test_schema_change_on_page_two_still_fails_the_whole_provider(
    settings: Settings,
) -> None:
    # Arrange: page 1 succeeds; page 2 comes back 200 but with an unreadable
    # body (as a CAPTCHA/challenge page or a real schema change would) --
    # parse_listing()/decode_next_data() must still raise, never be treated as
    # a transient network error since the request itself succeeded.
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), combo_page_response(), response("<html>not json</html>")]
    )
    provider = WakacjeProvider(
        {**settings.providers["wakacje.pl"], "max_pages": 3, "max_requests": 4},
        settings.filters,
        transport,
        sleep=lambda _: None,
    )
    # Act / Assert
    with pytest.raises(ValueError):
        provider.fetch()


def test_module_never_imports_playwright() -> None:
    # A defensive regression: unlike Rainbow/TUI, this provider must stay HTTP-only.
    source = Path(
        Path(__file__).parent.parent / "travel_deal_agent" / "providers" / "wakacje.py"
    ).read_text(encoding="utf-8")
    assert "import playwright" not in source.lower()
    assert "from playwright" not in source.lower()


# --- robots.txt --------------------------------------------------------------------


def test_disallowed_listing_path_is_never_fetched(settings: Settings) -> None:
    transport = FakeTransport([response(f"User-agent: *\nDisallow: {LISTING_PATH}")])
    with pytest.raises(ValueError, match="robots"):
        WakacjeProvider(
            CONFIG,
            settings.filters,
            transport,
            sleep=lambda _: None,
        ).fetch()
    assert transport.urls == [BASE + "/robots.txt"]


@pytest.mark.parametrize(
    "robots",
    ["<html>CAPTCHA</html>", "User-agent: Other\nDisallow: /"],
)
def test_robots_fail_closed(robots: str, settings: Settings) -> None:
    transport = FakeTransport([response(robots)])
    with pytest.raises(ValueError):
        WakacjeProvider(
            CONFIG,
            settings.filters,
            transport,
            sleep=lambda _: None,
        ).fetch()
    assert len(transport.urls) == 1


@pytest.mark.parametrize("status", [301, 403, 429, 503])
def test_no_retry_or_redirect_on_robots_http_failure(status: int, settings: Settings) -> None:
    transport = FakeTransport([response("", status)])
    with pytest.raises(ValueError, match="HTTP"):
        WakacjeProvider(
            CONFIG,
            settings.filters,
            transport,
            sleep=lambda _: None,
        ).fetch()
    assert len(transport.urls) == 1


def test_crawl_delay_is_honored_before_the_first_page_fetch(settings: Settings) -> None:
    # Arrange: a controlled (non-advancing) clock so the resulting sleep is exact,
    # and a zero configured gap so the confirmed Crawl-delay is what drives it.
    transport = FakeTransport([response(ALLOW_ROBOTS + "\nCrawl-delay: 3"), combo_page_response()])
    waits: list[float] = []
    cfg: ProviderConfig = {**CONFIG, "request_gap_seconds": 0, "max_pages": 1}
    provider = WakacjeProvider(
        cfg,
        settings.filters,
        transport,
        clock=lambda: 0.0,
        sleep=waits.append,
    )
    provider.fetch()
    # Assert: the first request (robots.txt) never waits; the second honors the
    # confirmed Crawl-delay of 3.
    assert waits == [0, 3]


# --- schema-change / malformed body --------------------------------------------------


def test_schema_change_in_the_listing_body_fails_closed(settings: Settings) -> None:
    transport = FakeTransport([response(ALLOW_ROBOTS), response("<html>not json</html>")])
    with pytest.raises(ValueError):
        WakacjeProvider(
            CONFIG,
            settings.filters,
            transport,
            sleep=lambda _: None,
        ).fetch()


# --- registry wiring -----------------------------------------------------------------


def test_registry_builds_a_disabled_wakacje_provider_without_network_access(
    settings: Settings,
) -> None:
    # Arrange: explicitly disabled, independent of config.json's current
    # `enabled` value -- matching the ITAKA/Rainbow/TUI pattern.
    disabled: ProviderConfig = {**settings.providers["wakacje.pl"], "enabled": False}
    config: dict[str, ProviderConfig] = {"wakacje.pl": disabled}
    # Act: construction performs no network access regardless.
    sources = build_providers(config, filters=settings.filters)
    assert sources == []


def test_registry_builds_an_enabled_wakacje_provider(settings: Settings) -> None:
    enabled: ProviderConfig = {**settings.providers["wakacje.pl"], "enabled": True}
    # Act: construction validates filters but performs no network access.
    sources = build_providers({"wakacje.pl": enabled}, filters=settings.filters)
    assert [s.name for s in sources] == ["wakacje.pl"]
    assert isinstance(sources[0], WakacjeProvider)


def test_registry_requires_shared_filters() -> None:
    enabled: ProviderConfig = {**CONFIG, "enabled": True}
    with pytest.raises(ValueError, match="shared business filters"):
        build_providers({"wakacje.pl": enabled})


def test_production_request_budget_is_unchanged() -> None:
    # Arrange: the real config.json, not the frozen test fixture -- widening the
    # scan is a separate, explicit decision (robots + 3 pages of the one
    # confirmed combined query, replacing the old robots + baseline + 4
    # airports x 3 pages = 14 formula).
    production = json.loads(
        (Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8")
    )
    cfg = production["providers"]["wakacje.pl"]
    assert (cfg["max_pages"], cfg["max_requests"]) == (3, 4)
    assert cfg["max_requests"] == 1 + cfg["max_pages"]
