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
    CONFIRMED_AIRPORT_SLUGS,
    LISTING_PATH,
    WakacjeProvider,
    robots_policy,
)

CONFIG: ProviderConfig = {"enabled": True, "interval_seconds": 3600}
# "z-warszawy", "z-lodzi", "z-katowic" and "z-wroclawia" are now all confirmed
# and deliberately excluded from this list; "z-modlina" (WMI) has no confirmed
# slug; "z-warszawa-chopin" is the UI's own Chopin-only sub-filter, confirmed
# to exist but empirically returns 301 -> /wczasy/ as a standalone request --
# never used; "z-warszawy-radom" (RDO) is a real slug never live-verified
# standalone and not one of our configured airports.
UNCONFIRMED_SLUGS = ("z-modlina", "z-warszawa-chopin", "z-warszawy-radom")


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
        "price": 5559,
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


def wro_page_response(**overrides: object) -> Response:
    base: dict[str, object] = {
        "departurePlace": "Wrocław",
        "departurePlaceCode": "WRO",
        "departurePlaces": ["Wrocław"],
    }
    base.update(overrides)
    return response(listing_html([raw_offer(**base)]))


def waw_page_response(**overrides: object) -> Response:
    base: dict[str, object] = {
        "departurePlace": "Warszawa",
        "departurePlaceCode": "WAW",
        "departurePlaces": ["Warszawa"],
    }
    base.update(overrides)
    return response(listing_html([raw_offer(**base)]))


def lcj_page_response(**overrides: object) -> Response:
    base: dict[str, object] = {
        "departurePlace": "Łódź",
        "departurePlaceCode": "LCJ",
        "departurePlaces": ["Łódź"],
    }
    base.update(overrides)
    return response(listing_html([raw_offer(**base)]))


def ktw_page_response(**overrides: object) -> Response:
    base: dict[str, object] = {
        "departurePlace": "Katowice",
        "departurePlaceCode": "KTW",
        "departurePlaces": ["Katowice"],
    }
    base.update(overrides)
    return response(listing_html([raw_offer(**base)]))


def confirmed_airport_pages(max_pages: int) -> list[Response]:
    """Responses in fetch()'s real order: LCJ, WAW, KTW, then WRO pages.

    Mirrors self.airports' order, which follows filters["airports"]
    (LCJ, WAW, WMI, KTW, WRO in config.json) filtered down to confirmed slugs
    (WMI has no confirmed slug and is skipped).
    """
    return (
        [lcj_page_response() for _ in range(max_pages)]
        + [waw_page_response() for _ in range(max_pages)]
        + [ktw_page_response() for _ in range(max_pages)]
        + [wro_page_response() for _ in range(max_pages)]
    )


ALLOW_ROBOTS = "User-agent: *\nDisallow: /ajax/\nDisallow: /oferty/*?*\nDisallow: /*,*/"


def test_confirmed_airport_slugs_are_exactly_the_four_confirmed_airports() -> None:
    # Arrange / Act / Assert: pins the exact, live-verified mapping -- no other
    # airport (WMI/RDO) and no Chopin-only sub-filter is confirmed.
    assert CONFIRMED_AIRPORT_SLUGS == {
        "LCJ": "z-lodzi",
        "WAW": "z-warszawy",
        "KTW": "z-katowic",
        "WRO": "z-wroclawia",
    }
    assert "z-warszawa-chopin" not in CONFIRMED_AIRPORT_SLUGS.values()


def test_fetch_reads_robots_then_baseline_then_paginated_confirmed_airports(
    settings: Settings,
) -> None:
    # Arrange: four confirmed airports (LCJ, WAW, KTW, WRO), each paginated.
    # Uses the frozen test config values (tests/fixtures/test_config.json,
    # mirroring config.json) -- max_pages=3, max_requests=14 -- so this also
    # proves the request budget is exactly enough for a full scan
    # (robots + baseline + 4 airports x 3 pages = 14) with none left unused.
    cfg = settings.providers["wakacje.pl"]
    max_pages = cfg.get("max_pages", 2)
    assert max_pages is not None
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            response(listing_html([raw_offer()])),
            *confirmed_airport_pages(max_pages),
        ]
    )
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    # Act
    offers = provider.fetch()
    # Assert
    assert len(offers) == 1 + 4 * max_pages
    assert all(o.provider == "wakacje.pl" for o in offers)
    expected = [BASE + "/robots.txt", BASE + LISTING_PATH, BASE + f"{LISTING_PATH}?z-lodzi"]
    expected += [BASE + f"{LISTING_PATH}?str-{page},z-lodzi" for page in range(2, max_pages + 1)]
    expected += [BASE + f"{LISTING_PATH}?z-warszawy"]
    expected += [BASE + f"{LISTING_PATH}?str-{page},z-warszawy" for page in range(2, max_pages + 1)]
    expected += [BASE + f"{LISTING_PATH}?z-katowic"]
    expected += [BASE + f"{LISTING_PATH}?str-{page},z-katowic" for page in range(2, max_pages + 1)]
    expected += [BASE + f"{LISTING_PATH}?z-wroclawia"]
    expected += [
        BASE + f"{LISTING_PATH}?str-{page},z-wroclawia" for page in range(2, max_pages + 1)
    ]
    assert transport.urls == expected
    assert len(transport.urls) == cfg.get("max_requests")
    assert len(transport.urls) == 14


def test_only_confirmed_airport_slugs_are_queried(settings: Settings) -> None:
    # Arrange: business config wants LCJ/WAW/WMI/KTW/WRO, but only LCJ/WAW/KTW/WRO
    # have live-verified, standalone-request-confirmed slugs -- WMI must
    # never be guessed into a request, on any page.
    max_pages = settings.providers["wakacje.pl"].get("max_pages", 2)
    assert max_pages is not None
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            response(listing_html([raw_offer()])),
            *confirmed_airport_pages(max_pages),
        ]
    )
    provider = WakacjeProvider(
        settings.providers["wakacje.pl"], settings.filters, transport, sleep=lambda _: None
    )
    # Act
    provider.fetch()
    # Assert
    assert set(CONFIRMED_AIRPORT_SLUGS) == {"LCJ", "WAW", "KTW", "WRO"}
    lcj_requests = [u for u in transport.urls if "z-lodzi" in u]
    waw_requests = [u for u in transport.urls if "z-warszawy" in u]
    ktw_requests = [u for u in transport.urls if "z-katowic" in u]
    wro_requests = [u for u in transport.urls if "z-wroclawia" in u]
    assert len(lcj_requests) == max_pages
    assert len(waw_requests) == max_pages
    assert len(ktw_requests) == max_pages
    assert len(wro_requests) == max_pages
    assert all(slug not in url for url in transport.urls for slug in UNCONFIRMED_SLUGS)


def test_no_airports_confirmed_means_baseline_only(settings: Settings) -> None:
    # Arrange: exclude all four confirmed airports (LCJ, WAW, KTW, WRO); WMI is
    # the only configured airport left without a confirmed slug.
    filters = dict(settings.filters)
    filters["airports"] = ["WMI"]
    transport = FakeTransport([response(ALLOW_ROBOTS), response(listing_html([raw_offer()]))])
    provider = WakacjeProvider(
        CONFIG,
        filters,  # type: ignore[arg-type]
        transport,
        sleep=lambda _: None,
    )
    # Act
    provider.fetch()
    # Assert
    assert transport.urls == [BASE + "/robots.txt", BASE + LISTING_PATH]


# --- listing only: never a detail page, never an API endpoint -----------------------


def test_never_requests_a_detail_page(settings: Settings) -> None:
    # Arrange
    max_pages = settings.providers["wakacje.pl"].get("max_pages", 2)
    assert max_pages is not None
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            response(listing_html([raw_offer()])),
            *confirmed_airport_pages(max_pages),
        ]
    )
    provider = WakacjeProvider(
        settings.providers["wakacje.pl"], settings.filters, transport, sleep=lambda _: None
    )
    # Act
    provider.fetch()
    # Assert
    assert all("/oferty/" not in url for url in transport.urls)


def test_never_calls_an_api_or_ajax_endpoint_directly(settings: Settings) -> None:
    # Arrange
    max_pages = settings.providers["wakacje.pl"].get("max_pages", 2)
    assert max_pages is not None
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            response(listing_html([raw_offer()])),
            *confirmed_airport_pages(max_pages),
        ]
    )
    provider = WakacjeProvider(
        settings.providers["wakacje.pl"], settings.filters, transport, sleep=lambda _: None
    )
    # Act
    provider.fetch()
    # Assert
    assert all("/ajax/" not in url and not url.endswith(".json") for url in transport.urls)


def test_only_the_confirmed_pagination_shape_combines_filters(settings: Settings) -> None:
    # Arrange: robots.txt disallows comma-joined multi-*dimension* filter URLs
    # (sec 1, 8a.4); the one confirmed exception is the site's own pagination
    # shape, which pairs a page number with the *same* single airport filter --
    # never two different filter dimensions, and never a sort flag such as
    # "tanio".
    max_pages = settings.providers["wakacje.pl"].get("max_pages", 2)
    assert max_pages is not None
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            response(listing_html([raw_offer()])),
            *confirmed_airport_pages(max_pages),
        ]
    )
    provider = WakacjeProvider(
        settings.providers["wakacje.pl"], settings.filters, transport, sleep=lambda _: None
    )
    # Act
    provider.fetch()
    # Assert
    for url in transport.urls:
        assert url.count("?") <= 1
        assert "tanio" not in url
        if "," in url:
            query = url.split("?", 1)[1]
            page_part, airport_part = query.split(",")
            assert page_part.startswith("str-")
            assert airport_part in CONFIRMED_AIRPORT_SLUGS.values()


def test_paginates_each_confirmed_airport_up_to_max_pages(settings: Settings) -> None:
    # Arrange
    max_pages = settings.providers["wakacje.pl"].get("max_pages", 2)
    assert max_pages is not None
    assert max_pages == 3  # pins the production config.json value this test relies on
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            response(listing_html([raw_offer()])),
            *confirmed_airport_pages(max_pages),
        ]
    )
    provider = WakacjeProvider(
        settings.providers["wakacje.pl"], settings.filters, transport, sleep=lambda _: None
    )
    # Act
    provider.fetch()
    # Assert: page 1 is the bare confirmed slug; pages 2..max_pages use the
    # confirmed str-N pagination shape; not one request more, for each airport.
    lcj_urls = [u for u in transport.urls if "z-lodzi" in u]
    waw_urls = [u for u in transport.urls if "z-warszawy" in u]
    ktw_urls = [u for u in transport.urls if "z-katowic" in u]
    wro_urls = [u for u in transport.urls if "z-wroclawia" in u]
    assert lcj_urls == [
        BASE + f"{LISTING_PATH}?z-lodzi",
        BASE + f"{LISTING_PATH}?str-2,z-lodzi",
        BASE + f"{LISTING_PATH}?str-3,z-lodzi",
    ]
    assert waw_urls == [
        BASE + f"{LISTING_PATH}?z-warszawy",
        BASE + f"{LISTING_PATH}?str-2,z-warszawy",
        BASE + f"{LISTING_PATH}?str-3,z-warszawy",
    ]
    assert ktw_urls == [
        BASE + f"{LISTING_PATH}?z-katowic",
        BASE + f"{LISTING_PATH}?str-2,z-katowic",
        BASE + f"{LISTING_PATH}?str-3,z-katowic",
    ]
    assert wro_urls == [
        BASE + f"{LISTING_PATH}?z-wroclawia",
        BASE + f"{LISTING_PATH}?str-2,z-wroclawia",
        BASE + f"{LISTING_PATH}?str-3,z-wroclawia",
    ]


def test_max_pages_one_means_a_single_page_per_airport(settings: Settings) -> None:
    # Arrange
    cfg: ProviderConfig = {**settings.providers["wakacje.pl"], "max_pages": 1, "max_requests": 6}
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            response(listing_html([raw_offer()])),
            lcj_page_response(),
            waw_page_response(),
            ktw_page_response(),
            wro_page_response(),
        ]
    )
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    # Act
    provider.fetch()
    # Assert: no str-N page ever requested, for any airport.
    assert transport.urls == [
        BASE + "/robots.txt",
        BASE + LISTING_PATH,
        BASE + f"{LISTING_PATH}?z-lodzi",
        BASE + f"{LISTING_PATH}?z-warszawy",
        BASE + f"{LISTING_PATH}?z-katowic",
        BASE + f"{LISTING_PATH}?z-wroclawia",
    ]
    assert all("str-" not in url for url in transport.urls)


def test_pagination_stops_when_the_request_budget_is_reached(settings: Settings) -> None:
    # Arrange: max_pages asks for 3 pages per airport, but the budget only
    # allows for robots + baseline + 1 airport page -- fetch() must stop
    # cleanly instead of raising, returning whatever it already collected.
    cfg: ProviderConfig = {**settings.providers["wakacje.pl"], "max_pages": 3, "max_requests": 3}
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), response(listing_html([raw_offer()])), lcj_page_response()]
    )
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    # Act
    offers = provider.fetch()
    # Assert: only LCJ's first page is reached (LCJ is queried first, per
    # filters["airports"] order); the remaining airports' loops break
    # immediately without any further request.
    assert len(offers) == 2
    assert transport.urls == [
        BASE + "/robots.txt",
        BASE + LISTING_PATH,
        BASE + f"{LISTING_PATH}?z-lodzi",
    ]


def test_full_confirmed_scan_fits_within_the_configured_cycle_seconds(settings: Settings) -> None:
    # Arrange: a full scan needs 14 requests (robots + baseline + 4 confirmed
    # airports x 3 pages), spaced request_gap_seconds apart -- 13 mandatory
    # gaps alone. A controlled clock/sleep pair (same technique as ITAKA's
    # test_request_spacing_and_deadline) simulates that real spacing without
    # actually waiting. A live run once hit RequestBudget's own "cycle
    # deadline exceeded" here because cycle_seconds (60) was smaller than the
    # 13 mandatory gaps alone (13 x 5s = 65s) -- see RECONNAISSANCE.md sec 22.
    # cycle_seconds must stay comfortably above that dead-time floor.
    cfg = settings.providers["wakacje.pl"]
    max_pages = cfg.get("max_pages", 2)
    assert max_pages == 3
    assert cfg.get("max_requests") == 14
    gap = cfg.get("request_gap_seconds", 5)
    assert cfg["cycle_seconds"] > 13 * gap  # the dead-time floor this bug hit
    now = [0.0]
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            response(listing_html([raw_offer()])),
            *confirmed_airport_pages(max_pages),
        ]
    )
    provider = WakacjeProvider(
        cfg,
        settings.filters,
        transport,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    # Act
    offers = provider.fetch()
    # Assert: all 14 requests complete; none are lost to a cycle-deadline abort.
    assert len(transport.urls) == 14
    assert len(offers) == 1 + 4 * max_pages


# --- transient network resilience (RECONNAISSANCE.md sec 22-23) ---------------------


@pytest.mark.parametrize(
    "transient_error", [TimeoutError("synthetic timeout"), URLError("synthetic connection failure")]
)
def test_transient_network_error_on_an_airport_page_skips_its_remaining_pages(
    settings: Settings, transient_error: Exception
) -> None:
    # Arrange: LCJ page 1 succeeds, page 2 hits a transient network error -- no
    # retry, no LCJ page 3, but WAW/KTW/WRO must still be fully queried and
    # everything already parsed must survive.
    cfg = settings.providers["wakacje.pl"]
    max_pages = cfg.get("max_pages", 2)
    assert max_pages is not None
    assert max_pages == 3
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            response(listing_html([raw_offer()])),
            lcj_page_response(),
            transient_error,
            *[waw_page_response() for _ in range(max_pages)],
            *[ktw_page_response() for _ in range(max_pages)],
            *[wro_page_response() for _ in range(max_pages)],
        ]
    )
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    # Act
    offers = provider.fetch()
    # Assert: no exception, LCJ page 3 never requested, every other airport
    # fully queried, and the baseline + LCJ page 1 + WAW/KTW/WRO offers survive.
    assert transport.urls == [
        BASE + "/robots.txt",
        BASE + LISTING_PATH,
        BASE + f"{LISTING_PATH}?z-lodzi",
        BASE + f"{LISTING_PATH}?str-2,z-lodzi",
        BASE + f"{LISTING_PATH}?z-warszawy",
        BASE + f"{LISTING_PATH}?str-2,z-warszawy",
        BASE + f"{LISTING_PATH}?str-3,z-warszawy",
        BASE + f"{LISTING_PATH}?z-katowic",
        BASE + f"{LISTING_PATH}?str-2,z-katowic",
        BASE + f"{LISTING_PATH}?str-3,z-katowic",
        BASE + f"{LISTING_PATH}?z-wroclawia",
        BASE + f"{LISTING_PATH}?str-2,z-wroclawia",
        BASE + f"{LISTING_PATH}?str-3,z-wroclawia",
    ]
    assert len(offers) == 1 + 1 + 3 * max_pages  # baseline + LCJ page 1 + WAW/KTW/WRO


def test_transient_network_error_on_the_baseline_listing_is_skipped_not_fatal(
    settings: Settings,
) -> None:
    # Arrange: the baseline listing itself hits a transient network error --
    # skipped, but every confirmed airport is still fully queried.
    cfg = settings.providers["wakacje.pl"]
    max_pages = cfg.get("max_pages", 2)
    assert max_pages is not None
    assert max_pages == 3
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            TimeoutError("synthetic timeout"),
            *confirmed_airport_pages(max_pages),
        ]
    )
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    # Act
    offers = provider.fetch()
    # Assert: no baseline offer, but all 4 confirmed airports fully queried.
    assert len(offers) == 4 * max_pages
    assert transport.urls[:2] == [BASE + "/robots.txt", BASE + LISTING_PATH]
    assert len(transport.urls) == 2 + 4 * max_pages


def test_transient_network_error_warning_names_the_airport_and_page(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange: same LCJ-page-2 scenario as above; assert the warning alone
    # (without reading source) is enough to know which airport/page/provider
    # was affected and why.
    cfg = settings.providers["wakacje.pl"]
    max_pages = cfg.get("max_pages", 2)
    assert max_pages is not None
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            response(listing_html([raw_offer()])),
            lcj_page_response(),
            TimeoutError("synthetic timeout"),
            *[waw_page_response() for _ in range(max_pages)],
            *[ktw_page_response() for _ in range(max_pages)],
            *[wro_page_response() for _ in range(max_pages)],
        ]
    )
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    # Act
    provider.fetch()
    # Assert: enough context to know provider/airport/page/cause, no traceback dump.
    assert "Wakacje.pl" in caplog.text
    assert "LCJ" in caplog.text
    assert "page 2" in caplog.text
    assert "Traceback" not in caplog.text


def test_transient_network_error_baseline_warning_is_clear_and_non_fatal(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    cfg = settings.providers["wakacje.pl"]
    max_pages = cfg.get("max_pages", 2)
    assert max_pages is not None
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            TimeoutError("synthetic timeout"),
            *confirmed_airport_pages(max_pages),
        ]
    )
    provider = WakacjeProvider(cfg, settings.filters, transport, sleep=lambda _: None)
    # Act
    provider.fetch()
    # Assert
    assert "Wakacje.pl" in caplog.text
    assert "baseline" in caplog.text


def test_robots_violation_on_an_airport_page_still_fails_the_whole_provider(
    settings: Settings,
) -> None:
    # Arrange: LCJ page 1 succeeds; page 2's path is (synthetically) disallowed
    # by robots.txt -- this must still raise, never be treated as transient.
    transport = FakeTransport(
        [
            response("User-agent: *\nDisallow: /wczasy/?str-2,z-lodzi"),
            response(listing_html([raw_offer()])),
            lcj_page_response(),
        ]
    )
    filters = dict(settings.filters)
    filters["airports"] = ["LCJ"]
    provider = WakacjeProvider(
        {**settings.providers["wakacje.pl"], "max_pages": 3},
        filters,  # type: ignore[arg-type]
        transport,
        sleep=lambda _: None,
    )
    # Act / Assert
    with pytest.raises(ValueError, match="robots"):
        provider.fetch()


def test_http_error_status_on_an_airport_page_still_fails_the_whole_provider(
    settings: Settings,
) -> None:
    # Arrange: LCJ page 1 succeeds; page 2 comes back HTTP 403 -- RequestBudget
    # itself turns this into a ValueError ("no automatic retry"), which must
    # still propagate, never be swallowed as a transient network error.
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            response(listing_html([raw_offer()])),
            lcj_page_response(),
            response("", status=403),
        ]
    )
    filters = dict(settings.filters)
    filters["airports"] = ["LCJ"]
    provider = WakacjeProvider(
        {**settings.providers["wakacje.pl"], "max_pages": 3},
        filters,  # type: ignore[arg-type]
        transport,
        sleep=lambda _: None,
    )
    # Act / Assert
    with pytest.raises(ValueError, match="HTTP"):
        provider.fetch()


def test_schema_change_on_an_airport_page_still_fails_the_whole_provider(
    settings: Settings,
) -> None:
    # Arrange: LCJ page 1 succeeds; page 2 comes back 200 but with an
    # unreadable body (as a CAPTCHA/challenge page or a real schema change
    # would) -- parse_listing()/decode_next_data() must still raise, never be
    # treated as a transient network error since the request itself succeeded.
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            response(listing_html([raw_offer()])),
            lcj_page_response(),
            response("<html>not json</html>"),
        ]
    )
    filters = dict(settings.filters)
    filters["airports"] = ["LCJ"]
    provider = WakacjeProvider(
        {**settings.providers["wakacje.pl"], "max_pages": 3},
        filters,  # type: ignore[arg-type]
        transport,
        sleep=lambda _: None,
    )
    # Act / Assert
    with pytest.raises(ValueError):
        provider.fetch()


def test_never_queries_the_cheapest_first_sort(settings: Settings) -> None:
    # Arrange: the cheapest-sorted results were confirmed live to be dominated by
    # no-flight offers with no departure-airport code, and no on-page evidence of
    # a legal tanio+airport combination was ever found -- this provider must
    # never request it, alone or combined.
    max_pages = settings.providers["wakacje.pl"].get("max_pages", 2)
    assert max_pages is not None
    transport = FakeTransport(
        [
            response(ALLOW_ROBOTS),
            response(listing_html([raw_offer()])),
            *confirmed_airport_pages(max_pages),
        ]
    )
    provider = WakacjeProvider(
        settings.providers["wakacje.pl"], settings.filters, transport, sleep=lambda _: None
    )
    # Act
    provider.fetch()
    # Assert
    assert all("tanio" not in url for url in transport.urls)


def test_module_never_imports_playwright() -> None:
    # A defensive regression: unlike Rainbow/TUI, this provider must stay HTTP-only.
    source = Path(
        Path(__file__).parent.parent / "travel_deal_agent" / "providers" / "wakacje.py"
    ).read_text(encoding="utf-8")
    assert "import playwright" not in source.lower()
    assert "from playwright" not in source.lower()


# --- robots.txt --------------------------------------------------------------------


def test_disallowed_listing_path_is_never_fetched(settings: Settings) -> None:
    # Arrange
    filters = dict(settings.filters)
    filters["airports"] = ["WRO"]
    transport = FakeTransport([response(f"User-agent: *\nDisallow: {LISTING_PATH}")])
    # Act / Assert
    with pytest.raises(ValueError, match="robots"):
        WakacjeProvider(
            CONFIG,
            filters,  # type: ignore[arg-type]
            transport,
            sleep=lambda _: None,
        ).fetch()
    assert transport.urls == [BASE + "/robots.txt"]


@pytest.mark.parametrize(
    "robots",
    ["<html>CAPTCHA</html>", "User-agent: Other\nDisallow: /"],
)
def test_robots_fail_closed(robots: str, settings: Settings) -> None:
    # Arrange
    filters = dict(settings.filters)
    filters["airports"] = ["WRO"]
    transport = FakeTransport([response(robots)])
    # Act / Assert
    with pytest.raises(ValueError):
        WakacjeProvider(
            CONFIG,
            filters,  # type: ignore[arg-type]
            transport,
            sleep=lambda _: None,
        ).fetch()
    assert len(transport.urls) == 1


@pytest.mark.parametrize("status", [301, 403, 429, 503])
def test_no_retry_or_redirect_on_robots_http_failure(status: int, settings: Settings) -> None:
    # Arrange
    filters = dict(settings.filters)
    filters["airports"] = ["WRO"]
    transport = FakeTransport([response("", status)])
    # Act / Assert
    with pytest.raises(ValueError, match="HTTP"):
        WakacjeProvider(
            CONFIG,
            filters,  # type: ignore[arg-type]
            transport,
            sleep=lambda _: None,
        ).fetch()
    assert len(transport.urls) == 1


def test_crawl_delay_is_honored_before_the_baseline_fetch(settings: Settings) -> None:
    # Arrange: a controlled (non-advancing) clock so the resulting sleep is exact,
    # and a zero configured gap so the confirmed Crawl-delay is what drives it.
    filters = dict(settings.filters)
    filters["airports"] = []
    transport = FakeTransport(
        [response(ALLOW_ROBOTS + "\nCrawl-delay: 3"), response(listing_html([raw_offer()]))]
    )
    waits: list[float] = []
    cfg: ProviderConfig = {**CONFIG, "request_gap_seconds": 0}
    provider = WakacjeProvider(
        cfg,
        filters,  # type: ignore[arg-type]
        transport,
        clock=lambda: 0.0,
        sleep=waits.append,
    )
    # Act
    provider.fetch()
    # Assert: the first request (robots.txt) never waits; the second honors the
    # confirmed Crawl-delay of 3.
    assert waits == [0, 3]


def test_robots_policy_is_a_conservative_union() -> None:
    # Arrange / Act / Assert
    assert robots_policy("User-agent: *\nDisallow: /api/\nCrawl-delay: 5", "/lastminute/") == 5
    with pytest.raises(ValueError, match="forbidden"):
        robots_policy("User-agent: *\nDisallow: /lastminute/", "/lastminute/")
    with pytest.raises(ValueError, match="robots.txt"):
        robots_policy("<html>not robots</html>", "/lastminute/")


# --- schema-change / malformed body --------------------------------------------------


def test_schema_change_in_the_listing_body_fails_closed(settings: Settings) -> None:
    # Arrange
    filters = dict(settings.filters)
    filters["airports"] = []
    transport = FakeTransport([response(ALLOW_ROBOTS), response("<html>not json</html>")])
    # Act / Assert
    with pytest.raises(ValueError):
        WakacjeProvider(
            CONFIG,
            filters,  # type: ignore[arg-type]
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
    # Assert
    assert sources == []


def test_registry_builds_an_enabled_wakacje_provider(settings: Settings) -> None:
    # Arrange
    enabled: ProviderConfig = {**settings.providers["wakacje.pl"], "enabled": True}
    # Act: construction validates filters but performs no network access.
    sources = build_providers({"wakacje.pl": enabled}, filters=settings.filters)
    # Assert
    assert [s.name for s in sources] == ["wakacje.pl"]
    assert isinstance(sources[0], WakacjeProvider)


def test_registry_requires_shared_filters() -> None:
    # Arrange
    enabled: ProviderConfig = {**CONFIG, "enabled": True}
    # Act / Assert
    with pytest.raises(ValueError, match="shared business filters"):
        build_providers({"wakacje.pl": enabled})
