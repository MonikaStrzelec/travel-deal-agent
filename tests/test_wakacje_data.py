"""Offline Wakacje.pl parser tests: sanitized fixtures only, never contact wakacje.pl."""

import copy
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from travel_deal_agent.config import Settings
from travel_deal_agent.filtering import matches
from travel_deal_agent.providers.wakacje_data import (
    RawOffer,
    decode_next_data,
    extract_offer_links,
    extract_offers,
    normalize_offer,
    parse_listing,
    variant_identity,
)

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "wakacje" / "listing.html"
FIXTURE_HTML = FIXTURE.read_text(encoding="utf-8")
WRO_FIXTURE = Path(__file__).parent / "fixtures" / "wakacje" / "listing_z_wroclawia.html"
WRO_HTML = WRO_FIXTURE.read_text(encoding="utf-8")
# Real offers from the confirmed combined search query's own live confirmation
# (RECONNAISSANCE.md sec 25) -- the query this provider now always uses, so
# unlike FIXTURE_HTML/WRO_HTML (captured before the za-osobe switch), this
# fixture's `price` values are genuinely per-person, matching current code.
COMBO_FIXTURE = Path(__file__).parent / "fixtures" / "wakacje" / "listing_combo_search.html"
COMBO_HTML = COMBO_FIXTURE.read_text(encoding="utf-8")


def geo(name: str, slug: str) -> dict[str, Any]:
    return {"name": name, "slug": slug, "urlName": slug, "id": 1}


# The exact real offer captured during reconnaissance (public listing data; no
# personal information). Kept identical to the "Laur Experience & Elegance" record
# in both tests/fixtures/wakacje/*.html files -- session 1's unfiltered listing
# (Rzeszów, 5559 PLN, 2026-10-13) and session 3's `?z-wroclawia` listing (Wrocław,
# 5938 PLN, 2026-10-22) -- so both can be used interchangeably by tests that mutate
# one field. See RECONNAISSANCE.md sec 12.1 for the real diff between these two.
BASE_OFFER: dict[str, Any] = {
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
    "priceDiscount": 0,
    "priceOld": 0,
    "originalCurrency": "PLN",
    "shownCurrency": "PLN",
    "departureDate": "2026-10-13",
    "returnDate": "2026-10-20",
    "duration": 7,
    "durationNights": 7,
    "departurePlace": "Rzeszów",
    "departurePlaceCode": "RZE",
    "departurePlaces": [
        "Rzeszów",
        "Gdańsk",
        "Wrocław",
        "Katowice",
        "Kraków",
        "Poznań",
        "Warszawa",
    ],
    "service": 1,
    "serviceDesc": "Ultra All Inclusive",
    "ratingValue": 8,
    "ratingString": "8",
    "ratingReservationCount": 336,
    "ratingRecommends": 336,
    "tourOperator": 552,
    "tourOperatorName": "Coral Travel",
}

# The same real hotel/id, but as returned by the confirmed `?z-wroclawia` filtered
# listing (RECONNAISSANCE.md sec 12.1): a different departure date and price for
# the same numeric id -- this is the confirmed evidence behind "id alone is not a
# stable variant identifier".
WRO_OFFER: dict[str, Any] = {
    **BASE_OFFER,
    "price": 5938,
    "departureDate": "2026-10-22",
    "returnDate": "2026-10-29",
    "departurePlace": "Wrocław",
    "departurePlaceCode": "WRO",
    "departurePlaces": ["Wrocław"],
}


def offer(**overrides: object) -> dict[str, Any]:
    result = copy.deepcopy(BASE_OFFER)
    result.update(overrides)
    return result


def wrap(offers: list[dict[str, Any]], *, count: int = 1) -> str:
    data = {
        "props": {
            "dehydratedState": {
                "queries": [
                    {"queryKey": ["header-content"], "state": {"data": {}}},
                    {
                        "queryKey": [
                            "listingOffers",
                            json.dumps([{"method": "search.tripsSearch", "params": {}}]),
                        ],
                        "state": {"data": {"offers": {"data": offers, "count": count}}},
                    },
                ]
            }
        }
    }
    text = json.dumps(data, ensure_ascii=False)
    return f'<script id="__NEXT_DATA__" type="application/json">{text}</script>'


# --- __NEXT_DATA__ decoding ---------------------------------------------------------


def test_decode_next_data_returns_the_json_object() -> None:
    # Act
    data = decode_next_data(FIXTURE_HTML)
    # Assert
    assert "props" in data
    offers = extract_offers(data)
    assert len(offers) == 2
    assert offers[0]["id"] == 211281


def test_missing_next_data_script_is_rejected() -> None:
    # Arrange
    html = "<html><body>no next data here</body></html>"
    # Act / Assert
    with pytest.raises(ValueError, match="Missing or ambiguous"):
        decode_next_data(html)


def test_ambiguous_duplicate_next_data_script_is_rejected() -> None:
    # Arrange
    fragment = wrap([offer()])
    html = fragment + fragment
    # Act / Assert
    with pytest.raises(ValueError, match="Missing or ambiguous"):
        decode_next_data(html)


def test_empty_next_data_script_is_rejected() -> None:
    # Arrange
    html = '<script id="__NEXT_DATA__" type="application/json"></script>'
    # Act / Assert
    with pytest.raises(ValueError, match="Empty"):
        decode_next_data(html)


def test_invalid_json_is_rejected() -> None:
    # Arrange
    html = '<script id="__NEXT_DATA__" type="application/json">not json</script>'
    # Act / Assert
    with pytest.raises(ValueError, match="JSON"):
        decode_next_data(html)


def test_oversized_html_is_rejected() -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="size limit"):
        decode_next_data("x" * 4_000_001)


def test_missing_dehydrated_queries_is_rejected() -> None:
    # Arrange
    html = '<script id="__NEXT_DATA__" type="application/json">{"props": {}}</script>'
    # Act / Assert
    with pytest.raises(ValueError, match="dehydratedState"):
        extract_offers(decode_next_data(html))


def test_missing_listing_offers_query_is_rejected() -> None:
    # Arrange: a __NEXT_DATA__ with only unrelated queries (e.g. header-content/banners).
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props": {"dehydratedState": {"queries": '
        '[{"queryKey": ["header-content"], "state": {"data": {}}}]}}}'
        "</script>"
    )
    # Act / Assert
    with pytest.raises(ValueError, match="Missing or ambiguous"):
        extract_offers(decode_next_data(html))


def test_ambiguous_multiple_listing_offers_queries_is_rejected() -> None:
    # Arrange: never guess which of two "listingOffers" queries is authoritative.
    query = {
        "queryKey": ["listingOffers", "x"],
        "state": {"data": {"offers": {"data": [offer()], "count": 1}}},
    }
    data = {"props": {"dehydratedState": {"queries": [query, query]}}}
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data, ensure_ascii=False)
        + "</script>"
    )
    # Act / Assert
    with pytest.raises(ValueError, match="Missing or ambiguous"):
        extract_offers(decode_next_data(html))


def test_missing_offers_list_is_rejected() -> None:
    # Arrange
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props": {"dehydratedState": {"queries": [{"queryKey": ["listingOffers", "x"], '
        '"state": {"data": {"offers": {}}}}]}}}'
        "</script>"
    )
    # Act / Assert
    with pytest.raises(ValueError, match="Missing Wakacje.pl offers list"):
        extract_offers(decode_next_data(html))


# --- one valid offer, fully normalized ----------------------------------------------


def test_one_valid_offer_normalizes_fully() -> None:
    # Act
    result = normalize_offer(offer(), NOW)
    # Assert
    assert result.provider == "wakacje.pl"
    assert result.hotel_name == "Laur Experience & Elegance"
    assert result.country == "TR"
    assert result.destination == "Wybrzeże Egejskie / Didim"
    assert result.departure_airport == "RZE"
    assert result.departure_date == date(2026, 10, 13)
    assert result.return_date == date(2026, 10, 20)
    assert result.number_of_days == 7
    assert result.number_of_people == 2
    assert result.currency == "PLN"
    assert result.hotel_stars == 5.0
    assert result.rating == 8.0
    assert result.provider_rating_max == 10.0
    assert result.board_type == "AI"
    assert result.url == (
        "https://www.wakacje.pl/oferty/turcja/wybrzeze-egejskie/didim/"
        "laur-experience-elegance-211281.html"
    )
    assert not result.price_is_complete
    assert not result.variant_verified


def test_parse_listing_end_to_end_matches_normalize_offer() -> None:
    # Act
    result = parse_listing(FIXTURE_HTML, NOW)
    # Assert
    assert len(result) == 2
    assert result[0] == normalize_offer(BASE_OFFER, NOW)


# --- country mapping (COUNTRIES) -----------------------------------------------------


@pytest.mark.parametrize(
    "slug,expected_iso",
    [
        ("turcja", "TR"),
        ("egipt", "EG"),
        ("tunezja", "TN"),
        ("grecja", "GR"),
        ("albania", "AL"),
        ("bulgaria", "BG"),
        ("hiszpania", "ES"),
        ("cypr", "CY"),
        ("malta", "MT"),
    ],
)
def test_confirmed_country_slugs_map_to_iso_codes(slug: str, expected_iso: str) -> None:
    # Arrange: each slug here is a real, observed Wakacje.pl place.country.slug
    # (turcja/egipt/tunezja/grecja and bulgaria/hiszpania: RECONNAISSANCE.md
    # sec 11.4; albania/hiszpania: real fetched offer detail URLs from a later
    # manual comparison session; cypr: 2 real records from the final
    # full-provider live scan, RECONNAISSANCE.md sec 23; malta: a real record
    # from the confirmed combined search query's live confirmation, sec 25 --
    # never guessed).
    candidate = offer(
        place={
            "country": geo("X", slug),
            "region": geo("Wybrzeże Egejskie", "wybrzeze-egejskie"),
            "city": geo("Didim", "didim"),
        }
    )
    # Act
    result = normalize_offer(candidate, NOW)
    # Assert
    assert result.country == expected_iso


@pytest.mark.parametrize(
    "name,slug",
    [
        ("Portugalia", "portugalia"),
        ("Włochy", "wlochy"),
        ("Czarnogóra", "czarnogora"),
        ("Nieznany Kraj", "nieznany-kraj"),
    ],
)
def test_country_outside_the_mapping_is_not_rejected(
    settings: Settings, name: str, slug: str
) -> None:
    # Arrange: an otherwise good offer from a country COUNTRIES does not map.
    candidate = copy.deepcopy(ALION_OFFER)
    candidate["place"]["country"] = geo(name, slug)
    # Act
    result = normalize_offer(candidate, NOW)
    # Assert
    assert result.country is None
    assert result.destination is not None and result.destination.startswith(f"{name} / ")
    assert matches(result, settings.filters, date(2026, 9, 22))


def test_mapped_country_destination_is_unchanged() -> None:
    # Act
    result = normalize_offer(ALION_OFFER, NOW)
    # Assert: a known code already names the country, so it is not repeated.
    assert result.country == "AL"
    assert result.destination == "Riwiera Albańska / Durrës"


# --- price / Decimal / total-vs-per-person ------------------------------------------


def test_price_is_decimal_type() -> None:
    # Act
    result = normalize_offer(offer(), NOW)
    # Assert
    assert isinstance(result.price_per_person, Decimal)
    assert isinstance(result.total_price, Decimal)


def test_price_is_per_person_not_a_total() -> None:
    # Arrange: RECONNAISSANCE.md sec 25 -- the provider's one confirmed search
    # query includes `za-osobe` ("average per person"), so `price` is now
    # confirmed to be the per-person figure directly, the reverse of the site's
    # plain default the parser originally assumed (sec 8a.1).
    # Act
    result = normalize_offer(offer(price=5559), NOW)
    # Assert
    assert result.price_per_person == Decimal("5559")


def test_total_price_is_price_per_person_times_party_size() -> None:
    # Act
    result = normalize_offer(offer(price=5559), NOW)
    # Assert: every fetch uses the site's own unmodified 2-adult default (sec 3, 5, 12.1).
    assert result.total_price == Decimal("5559") * Decimal(2)
    assert result.number_of_people == 2


@pytest.mark.parametrize("bad", [-1])
def test_negative_price_is_rejected(bad: int) -> None:
    # Act / Assert
    with pytest.raises(ValidationError):
        normalize_offer(offer(price=bad), NOW)


def test_price_is_complete_always_false() -> None:
    # Arrange: RECONNAISSANCE.md sec 12.2/12.4 -- no robots-compliant HTTP path to a
    # booking total currently exists for Wakacje.pl.
    # Act
    result = normalize_offer(offer(), NOW)
    # Assert
    assert result.price_is_complete is False


# --- stars ---------------------------------------------------------------------------


@pytest.mark.parametrize("stars", [0, 1, 3, 4, 5])
def test_hotel_stars_within_scale(stars: int) -> None:
    # Act
    result = normalize_offer(offer(category=stars), NOW)
    # Assert
    assert result.hotel_stars == float(stars)


@pytest.mark.parametrize("stars", [-1, 6])
def test_hotel_stars_outside_scale_is_rejected(stars: int) -> None:
    # Act / Assert
    with pytest.raises(ValidationError):
        normalize_offer(offer(category=stars), NOW)


def test_hotel_stars_accepts_a_genuine_half_star_float() -> None:
    # Arrange: RECONNAISSANCE.md sec 4/11 confirmed `category` as float, e.g. 4.5.
    # Act
    result = normalize_offer(offer(category=4.5), NOW)
    # Assert
    assert result.hotel_stars == 4.5
    assert isinstance(result.hotel_stars, float)


# --- rating and ratingReservationCount ------------------------------------------------


def test_rating_is_kept_on_its_native_zero_to_ten_scale() -> None:
    # Act
    result = normalize_offer(offer(ratingValue=8.7), NOW)
    # Assert
    assert result.rating == 8.7
    assert result.provider_rating_max == 10.0


@pytest.mark.parametrize("rating", [-0.1, 10.1])
def test_out_of_scale_rating_is_dropped_not_rejected(rating: float) -> None:
    # Act
    result = normalize_offer(offer(ratingValue=rating), NOW)
    # Assert: an implausible rating does not invalidate the rest of the offer.
    assert result.rating is None
    assert result.hotel_name == "Laur Experience & Elegance"


def test_missing_rating_is_none() -> None:
    # Act
    result = normalize_offer(offer(ratingValue=None), NOW)
    # Assert
    assert result.rating is None


def test_rating_reservation_count_is_never_mapped_to_number_of_reviews() -> None:
    # Arrange: RECONNAISSANCE.md sec 8b.1 -- semantics (bookings vs. reviews) are not
    # literally confirmed; never guessed into a specific meaning.
    # Act
    result = normalize_offer(offer(ratingReservationCount=336), NOW)
    # Assert
    assert result.number_of_reviews is None
    # The raw value is preserved diagnostically in price_notes, not silently dropped.
    assert "336" in (result.price_notes or "")


def test_missing_rating_reservation_count_still_normalizes() -> None:
    # Act
    result = normalize_offer(offer(ratingReservationCount=None), NOW)
    # Assert
    assert result.number_of_reviews is None


def test_negative_rating_reservation_count_is_rejected() -> None:
    # Act / Assert
    with pytest.raises(ValidationError):
        normalize_offer(offer(ratingReservationCount=-1), NOW)


# --- board / service code -------------------------------------------------------------


@pytest.mark.parametrize(
    "service,desc,expected",
    [
        (1, "All Inclusive", "AI"),
        (1, "Ultra All Inclusive", "AI"),
        (2, "Śniadania i obiadokolacje (HB)", "HB"),
        (3, "Śniadania (BB)", "BB"),
        (5, "Według programu", "ZO"),
        (6, "Trzy posiłki (FB)", "FB"),
    ],
)
def test_confirmed_service_codes_map_to_the_bucket(service: int, desc: str, expected: str) -> None:
    # Arrange: RECONNAISSANCE.md sec 8a.2/11.3 -- the numeric code is the reliable
    # bucket; "Ultra All Inclusive" and "All Inclusive" are BOTH code 1 -> AI.
    # Act
    result = normalize_offer(offer(service=service, serviceDesc=desc), NOW)
    # Assert
    assert result.board_type == expected


def test_own_catering_code_maps_to_room_only() -> None:
    # Act
    result = normalize_offer(offer(service=4, serviceDesc="Własne"), NOW)
    # Assert
    assert result.board_type == "RO"


def test_itinerary_based_board_code_maps_to_zo() -> None:
    # Arrange: code 5 ("Według programu") is a deliberate business decision to
    # accept ZO as a normal canonical board (boards.CANONICAL_BOARDS), ranked
    # below HB/FB/AI -- see boards.BOARD_ORDER.
    # Act
    result = normalize_offer(offer(service=5, serviceDesc="Według programu"), NOW)
    # Assert
    assert result.board_type == "ZO"


def test_unrecognized_service_code_is_unknown_not_guessed() -> None:
    # Act
    result = normalize_offer(offer(service=99, serviceDesc="Nowa opcja"), NOW)
    # Assert
    assert result.board_type is None


def test_service_description_text_is_kept_diagnostically() -> None:
    # Act
    result = normalize_offer(offer(serviceDesc="Ultra All Inclusive"), NOW)
    # Assert
    assert "Ultra All Inclusive" in (result.price_notes or "")


# --- departure airport / departurePlaces --------------------------------------------


def test_departure_airport_is_the_source_code_directly() -> None:
    # Act
    result = normalize_offer(offer(departurePlace="Wrocław", departurePlaceCode="WRO"), NOW)
    # Assert
    assert result.departure_airport == "WRO"


def test_unrecognized_departure_airport_code_is_rejected() -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="Unrecognized Wakacje.pl departure"):
        normalize_offer(offer(departurePlaceCode="12"), NOW)


def test_requested_departure_airport_must_match_every_returned_offer() -> None:
    # Arrange: RECONNAISSANCE.md sec 12.1 -- every offer on a `?z-wroclawia` fetch was
    # confirmed to have departurePlaceCode == "WRO"; a mismatch must fail loudly.
    # Act / Assert
    with pytest.raises(ValueError, match="unexpected departure airport"):
        normalize_offer(offer(), NOW, requested_departure_airport="WRO")


def test_requested_departure_airport_matching_is_accepted() -> None:
    # Act
    result = normalize_offer(WRO_OFFER, NOW, requested_departure_airport="WRO")
    # Assert
    assert result.departure_airport == "WRO"


def test_departure_places_never_create_additional_offers() -> None:
    # Arrange: a card listing 13 alternative departure cities, none priced.
    many_places = offer(
        departurePlaces=[
            "Rzeszów",
            "Zielona Góra",
            "Gdańsk",
            "Wrocław",
            "Bydgoszcz",
            "Katowice",
            "Łódź",
            "Lublin",
            "Kraków",
            "Poznań",
            "Szczecin",
            "Warszawa - Radom",
            "Warszawa",
        ]
    )
    # Act
    result = parse_listing(wrap([many_places]), NOW)
    # Assert: exactly one Offer, priced only for the one departurePlace shown.
    assert len(result) == 1
    assert result[0].departure_airport == "RZE"


def test_departure_places_shape_is_validated_but_not_projected() -> None:
    # Act
    raw = RawOffer.model_validate(offer())
    # Assert: parsed for shape validation only.
    assert len(raw.departurePlaces) == 7
    result = normalize_offer(offer(), NOW)
    # No field on Offer carries the other-airport list.
    assert not hasattr(result, "departure_places")


# --- variant identity -----------------------------------------------------------------


def test_variant_identity_is_stable_for_identical_input() -> None:
    # Act
    a = normalize_offer(offer(), NOW)
    b = normalize_offer(offer(), NOW)
    # Assert
    assert a.offer_id == b.offer_id
    assert a.variant_identity == a.offer_id


def test_numeric_id_alone_is_not_used_as_offer_id() -> None:
    # Act
    result = normalize_offer(offer(), NOW)
    # Assert
    assert result.offer_id != "211281"
    assert str(offer()["id"]) not in result.offer_id


def test_same_id_different_airport_and_date_yields_a_different_variant_id() -> None:
    # Arrange: the real confirmed pair from RECONNAISSANCE.md sec 12.1 -- same
    # numeric id (211281), different departure airport, date and price.
    # Act
    unfiltered = normalize_offer(BASE_OFFER, NOW)
    filtered = normalize_offer(WRO_OFFER, NOW, requested_departure_airport="WRO")
    # Assert
    assert BASE_OFFER["id"] == WRO_OFFER["id"] == 211281
    assert unfiltered.offer_id != filtered.offer_id
    assert unfiltered.variant_identity != filtered.variant_identity


def test_same_id_same_context_yields_the_same_variant_id() -> None:
    # Act
    first = variant_identity(RawOffer.model_validate(offer()), date(2026, 10, 13))
    second = variant_identity(RawOffer.model_validate(offer()), date(2026, 10, 13))
    # Assert
    assert first == second


def test_variant_id_changes_when_board_changes() -> None:
    # Act
    ai = normalize_offer(offer(service=1, serviceDesc="All Inclusive"), NOW)
    fb = normalize_offer(offer(service=6, serviceDesc="Trzy posiłki"), NOW)
    # Assert
    assert ai.offer_id != fb.offer_id


def test_variant_id_changes_when_duration_changes() -> None:
    # Act
    seven = normalize_offer(offer(duration=7, returnDate="2026-10-20"), NOW)
    eight = normalize_offer(offer(duration=8, returnDate="2026-10-21"), NOW)
    # Assert
    assert seven.offer_id != eight.offer_id


# --- dates and duration ----------------------------------------------------------------


def test_duration_consistent_with_dates() -> None:
    # Act
    result = normalize_offer(offer(), NOW)
    # Assert
    assert result.number_of_days == 7
    assert (result.return_date - result.departure_date).days == 7  # type: ignore[operator]


def test_duration_disagreeing_with_dates_is_rejected() -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="duration disagrees"):
        normalize_offer(offer(duration=6), NOW)


def test_return_before_departure_is_rejected() -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="return date"):
        normalize_offer(offer(departureDate="2026-10-20", returnDate="2026-10-13", duration=7), NOW)


@pytest.mark.parametrize("bad", ["13.10.2026", "2026/10/13", "not-a-date", ""])
def test_malformed_dates_are_rejected(bad: str) -> None:
    # Act / Assert
    with pytest.raises(ValueError):
        normalize_offer(offer(departureDate=bad), NOW)


# --- URL ----------------------------------------------------------------------------------


def test_offer_url_matches_the_confirmed_pattern() -> None:
    # Act
    result = normalize_offer(offer(), NOW)
    # Assert: RECONNAISSANCE.md sec 6/11 confirmed pattern, from JSON-LD, not guessed.
    assert result.url == (
        "https://www.wakacje.pl/oferty/turcja/wybrzeze-egejskie/didim/"
        "laur-experience-elegance-211281.html"
    )


def test_unsafe_slug_characters_leave_url_unset_not_the_whole_offer() -> None:
    # Arrange
    unsafe = offer()
    unsafe["place"]["city"]["slug"] = "didim/../evil"
    # Act
    result = normalize_offer(unsafe, NOW)
    # Assert: the rest of the offer is still usable.
    assert result.url is None
    assert result.hotel_name == "Laur Experience & Elegance"


# --- real on-page href preservation (variant fidelity) -------------------------------
#
# Regression coverage for a real bug: a user's Telegram link for hotel "Preluna"
# opened Wakacje.pl on an unrelated stay (different dates/price) because the
# reconstructed `/oferty/.../slug-id.html` URL carries no query string, and
# Wakacje.pl then defaulted to some other variant. The real on-page anchor for
# each offer card DOES carry the exact variant in its query string (confirmed
# via a real fetch, `data/wakacje-recon/wczasy-combo-recon.html`) -- these tests
# confirm it is read from the already-fetched listing HTML and passed through
# untouched, rather than being lost during URL construction.

REAL_HREF = (
    "https://www.wakacje.pl/oferty/turcja/wybrzeze-egejskie/didim/"
    "laur-experience-elegance-211281.html?od-2026-10-13,7-dni,all-inclusive,z-rzeszowa"
)


def test_real_listing_href_is_extracted_keyed_by_offer_id() -> None:
    # Arrange
    html = wrap([offer()]) + (
        f'<a data-test-offer-id="211281" href="{REAL_HREF}">Laur Experience</a>'
    )
    # Act
    links = extract_offer_links(html)
    # Assert: the query string survives extraction unchanged.
    assert links == {211281: REAL_HREF}


def test_parse_listing_prefers_the_real_href_over_the_reconstructed_url() -> None:
    # Arrange
    html = wrap([offer()]) + (
        f'<a data-test-offer-id="211281" href="{REAL_HREF}">Laur Experience</a>'
    )
    # Act
    result = parse_listing(html, NOW)
    # Assert: exact variant href passes through untouched, not the bare hotel URL.
    assert len(result) == 1
    assert result[0].url == REAL_HREF


def test_normalize_offer_falls_back_to_reconstructed_url_without_a_real_href() -> None:
    # Act: no `offer_links` given, same as every other test in this module.
    result = normalize_offer(offer(), NOW)
    # Assert: unchanged behavior when no real href is available.
    assert result.url == (
        "https://www.wakacje.pl/oferty/turcja/wybrzeze-egejskie/didim/"
        "laur-experience-elegance-211281.html"
    )


def test_real_href_for_a_different_offer_id_is_not_applied() -> None:
    # Arrange: a href on the page for some other offer must never leak onto this one.
    other_href = REAL_HREF.replace("211281", "999999")
    # Act
    result = normalize_offer(offer(), NOW, offer_links={999999: other_href})
    # Assert: falls back to the reconstructed URL, exactly as if no href existed.
    assert result.url == (
        "https://www.wakacje.pl/oferty/turcja/wybrzeze-egejskie/didim/"
        "laur-experience-elegance-211281.html"
    )


def test_off_origin_href_is_rejected_not_trusted() -> None:
    # Arrange: same id, but pointing off-site -- must never be trusted verbatim.
    evil_href = "https://evil.example/oferty/x-211281.html?od-2026-10-13"
    # Act
    result = normalize_offer(offer(), NOW, offer_links={211281: evil_href})
    # Assert
    assert result.url == (
        "https://www.wakacje.pl/oferty/turcja/wybrzeze-egejskie/didim/"
        "laur-experience-elegance-211281.html"
    )


def test_variant_identity_is_unaffected_by_the_real_href() -> None:
    # Act
    without_href = normalize_offer(offer(), NOW)
    with_href = normalize_offer(offer(), NOW, offer_links={211281: REAL_HREF})
    # Assert: the href only changes `url`, never the price-history/dedup identity.
    assert with_href.variant_identity == without_href.variant_identity
    assert with_href.offer_id == without_href.offer_id


def test_preluna_scenario_duration_renders_8_days_7_nights() -> None:
    """Regression for the reported case: 05.12.2026 -> 12.12.2026 must stay
    "8 dni / 7 nocy" (7 nights, 8 calendar days) -- confirming the duration
    calculation itself was never the bug; only the URL was.
    """
    # Arrange
    from travel_deal_agent.notification_content import _stay_length

    preluna = offer(
        id=1179536,
        name="Preluna (Sliema)",
        urlName="preluna-sliema",
        place={
            "country": geo("Malta", "malta"),
            "region": geo("Wyspa Malta", "wyspa-malta"),
            "city": geo("Sliema", "sliema"),
        },
        departureDate="2026-12-05",
        returnDate="2026-12-12",
        duration=7,
        durationNights=7,
        departurePlace="Warszawa - Modlin",
        departurePlaceCode="WMI",
        service=2,
        serviceDesc="HB",
    )
    # Act
    result = normalize_offer(preluna, NOW)
    # Assert
    assert (result.return_date - result.departure_date).days == 7  # type: ignore[operator]
    assert _stay_length(result) == "8 dni / 7 nocy"


# --- missing / conflicting data ---------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "name",
        "urlName",
        "place",
        "price",
        "originalCurrency",
        "category",
        "service",
        "serviceDesc",
        "departureDate",
        "returnDate",
        "duration",
        "departurePlace",
        "departurePlaceCode",
    ],
)
def test_missing_required_field_is_rejected(field: str) -> None:
    # Arrange
    broken = offer()
    del broken[field]
    # Act / Assert
    with pytest.raises(ValidationError):
        normalize_offer(broken, NOW)


def test_one_malformed_record_does_not_fail_the_whole_page() -> None:
    # Arrange
    broken = offer(id=999999)
    del broken["name"]
    html = wrap([offer(), broken])
    # Act
    result = parse_listing(html, NOW)
    # Assert
    assert len(result) == 1


def test_all_malformed_records_raise_a_schema_change_error() -> None:
    # Arrange
    broken = offer()
    del broken["name"]
    html = wrap([broken])
    # Act / Assert
    with pytest.raises(ValueError, match="No readable"):
        parse_listing(html, NOW)


def test_empty_offers_list_is_not_an_error() -> None:
    # Act
    result = parse_listing(wrap([]), NOW)
    # Assert
    assert result == []


# --- the filtered fixture, end to end ----------------------------------------------------


def test_wroclawia_fixture_parses_and_matches_confirmed_evidence() -> None:
    # Act: WRO_HTML predates the provider's switch to the za-osobe query (sec
    # 25), so its raw `price` (5938) is read by the current, unconditional
    # per-person parsing exactly like any other fetched page would be -- this
    # test exercises the parsing mechanics (departure airport, dates,
    # requested_departure_airport validation), not a live per-person price claim.
    result = parse_listing(WRO_HTML, NOW, requested_departure_airport="WRO")
    # Assert: RECONNAISSANCE.md sec 12.1's real captured pair.
    assert len(result) == 2
    laur = next(o for o in result if o.hotel_name == "Laur Experience & Elegance")
    assert laur.departure_airport == "WRO"
    assert laur.price_per_person == Decimal("5938")
    assert laur.departure_date == date(2026, 10, 22)


def test_combo_search_fixture_parses_multiple_airports_with_per_person_prices() -> None:
    # Act: real offers from the confirmed combined search query's own live
    # confirmation (RECONNAISSANCE.md sec 25) -- the exact query this provider
    # now always uses. No requested_departure_airport is passed: a single
    # combined-query fetch spans multiple airports, unlike the old per-airport
    # fetches.
    result = parse_listing(COMBO_HTML, NOW)
    # Assert
    assert len(result) == 2
    luna = next(o for o in result if o.hotel_name == "Luna Holiday Complex")
    meridian = next(o for o in result if o.hotel_name == "Meridian")
    assert luna.country == "MT"
    assert luna.departure_airport == "WMI"
    assert luna.price_per_person == Decimal("1080")
    assert luna.total_price == Decimal("2160")
    assert meridian.country == "BG"
    assert meridian.departure_airport == "WAW"
    assert meridian.price_per_person == Decimal("1393")
    assert meridian.total_price == Decimal("2786")


# --- final eligibility: shared filtering.matches() is the sole arbiter -------------------
#
# price_is_complete is unconditionally False for every Wakacje.pl offer (sec 12.2/12.4
# -- no robots-compliant path to a confirmed booking total exists for this source), but
# that no longer means every offer is unconditionally rejected: `filtering.matches()`
# accepts this provider's listing price via the business-decided
# `filters["accept_incomplete_price_from"]` whitelist (config.json lists "wakacje.pl").
# `price_is_complete` itself is untouched -- these tests confirm the whitelist, not a
# change to what this parser reports.


def test_final_filtering_accepts_a_wakacje_offer_via_the_incomplete_price_whitelist(
    settings: Settings,
) -> None:
    # Arrange: an offer that satisfies every other business rule (airport, stars,
    # board, days, price, rating); price_is_complete is False, same as always.
    cheap = offer(
        price=1400,  # per-person (za-osobe query), under the 1500 cap
        category=4,
        service=1,
        serviceDesc="All Inclusive",
        departurePlaceCode="WRO",
        departurePlace="Wrocław",
        departureDate="2030-01-10",
        returnDate="2030-01-18",
        duration=8,
        ratingValue=9,
    )
    result = normalize_offer(cheap, NOW)
    # Act / Assert
    assert result.price_per_person == Decimal("1400")
    assert not result.price_is_complete
    assert "wakacje.pl" in settings.filters.get("accept_incomplete_price_from", [])
    assert matches(result, settings.filters, date(2026, 9, 22))


def test_final_filtering_still_blocks_a_wakacje_offer_without_the_whitelist_entry(
    settings: Settings,
) -> None:
    # Arrange: the exact same otherwise-fully-matching offer, but with the
    # whitelist entry removed -- proves the acceptance is driven by
    # configuration, not by any change to this parser's own output.
    settings.filters["accept_incomplete_price_from"] = []
    cheap = offer(
        price=1400,
        category=4,
        service=1,
        serviceDesc="All Inclusive",
        departurePlaceCode="WRO",
        departurePlace="Wrocław",
        departureDate="2030-01-10",
        returnDate="2030-01-18",
        duration=8,
        ratingValue=9,
    )
    result = normalize_offer(cheap, NOW)
    # Act / Assert
    assert not matches(result, settings.filters, date(2026, 9, 22))


# --- real-world regression: the Alion scenario from the manual comparison session -------
#
# Every value below was recorded by hand from the live site during a manual
# comparison session, not invented for this test: hotel "Alion", Albania,
# Riwiera Albanska / Durres, departing Warszawa (WAW, confirmed via the
# "?z-warszawy" filter), 30.10.2026-06.11.2026 (8 dni / 7 nocy), Sniadania i
# obiadokolacje (HB), 4 gwiazdki, ocena 8.6. That same manual session recorded
# BOTH the total ("Cena razem" 2958 PLN) and the per-person figure (1479
# PLN/os.) for this exact offer -- `price` below uses the per-person figure
# (1479), matching what the provider's current za-osobe query would return
# (sec 25); this is not a new number, only the other already-recorded real
# value for the new query context. This offer is exactly why Albania was added
# to COUNTRIES and WAW to CONFIRMED_AIRPORT_SLUGS: before that change it was
# rejected on two independent grounds (offer.country is None; WAW never
# deliberately queried).

ALION_OFFER: dict[str, Any] = {
    "id": 1145009,
    "name": "Alion",
    "urlName": "alion",
    "place": {
        "country": geo("Albania", "albania"),
        "region": geo("Riwiera Albańska", "riwiera-albanska"),
        "city": geo("Durrës", "durres"),
    },
    "category": 4,
    "price": 1479,
    "originalCurrency": "PLN",
    "departureDate": "2026-10-30",
    "returnDate": "2026-11-06",
    "duration": 7,
    "departurePlace": "Warszawa",
    "departurePlaceCode": "WAW",
    "departurePlaces": ["Warszawa"],
    "service": 2,
    "serviceDesc": "Śniadania i obiadokolacje (HB)",
    "ratingValue": 8.6,
    "ratingReservationCount": 12,
}


def test_real_alion_scenario_normalizes_as_observed() -> None:
    # Act
    result = normalize_offer(ALION_OFFER, NOW)
    # Assert: matches exactly what was recorded manually.
    assert result.hotel_name == "Alion"
    assert result.country == "AL"
    assert result.destination == "Riwiera Albańska / Durrës"
    assert result.departure_airport == "WAW"
    assert result.departure_date == date(2026, 10, 30)
    assert result.return_date == date(2026, 11, 6)
    assert result.number_of_days == 7
    assert result.hotel_stars == 4.0
    assert result.board_type == "HB"
    assert result.rating == 8.6
    assert result.total_price == Decimal("2958")
    assert result.price_per_person == Decimal("1479")
    assert not result.price_is_complete


def test_real_alion_scenario_passes_matches_end_to_end(settings: Settings) -> None:
    # Arrange
    result = normalize_offer(ALION_OFFER, NOW)
    # Act / Assert: every configured hard filter, evaluated as of the date this
    # offer was actually observed.
    assert matches(result, settings.filters, date(2026, 9, 22))


def test_three_star_offer_from_a_former_four_star_country_passes(settings: Settings) -> None:
    # Arrange: Albania previously required 4 stars; now 3 stars suffice everywhere.
    candidate = {**ALION_OFFER, "category": 3}
    # Act
    result = normalize_offer(candidate, NOW)
    # Assert
    assert result.country == "AL"
    assert matches(result, settings.filters, date(2026, 9, 22))


@pytest.mark.parametrize(
    "price,rating,expected",
    [
        (999, 7.9, False),
        (999, 8.0, True),
        (1479, 7.9, False),
        (1479, 8.0, True),
        (999, 7.0, False),
    ],
)
def test_one_internal_rating_threshold_regardless_of_price(
    settings: Settings, price: int, rating: float, expected: bool
) -> None:
    # Arrange: `price` is per-person directly (za-osobe, sec 25) -- 999 and 1479
    # PLN per person -- the old bands used 7.0 below 1000.
    candidate = {**ALION_OFFER, "price": price, "ratingValue": rating, "service": 1}
    # Act
    result = normalize_offer(candidate, NOW)
    # Assert
    assert matches(result, settings.filters, date(2026, 9, 22)) is expected
