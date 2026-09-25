"""Offline TUI parser tests: synthetic/reduced fixtures, never contact tui.pl."""

import copy
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from travel_deal_agent.config import Settings
from travel_deal_agent.filtering import matches
from travel_deal_agent.providers.tui_data import (
    extract_search_response_offers,
    normalize_offer,
    parse_search_response,
)

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
SEARCH_OFFERS_FIXTURE = Path(__file__).parent / "fixtures" / "tui" / "search_offers_response.json"
SEARCH_OFFERS_BODY = SEARCH_OFFERS_FIXTURE.read_text(encoding="utf-8")

# A real offer captured during reconnaissance (public listing data; no personal
# information), with `duration` in the search-results convention (days == duration).
BASE_OFFER: dict[str, Any] = {
    "hotelCode": "DLM10079",
    "hotelName": "TUI SUNEO Costa Mare Suites",
    "hotelStandard": 4,
    "offerCode": "KTWDLM20261012111020261012202610192240L07DLM10079DZX4AA02ROADZX4A02FCYY",
    "duration": 8,
    "durationText": "noclegów",
    "offerUrl": (
        "/wypoczynek/turcja/turcja-egejska/dalaman/tui-suneo-costa-mare-suites-dlm10079"
        "/OfferCodeWS/KTWDLM20261012111020261012202610192240L07DLM10079DZX4AA02ROADZX4A02FCYY"
    ),
    "breadcrumbs": [
        {"label": "Turcja", "url": "/wypoczynek/turcja"},
        {"label": "Turcja Egejska", "url": "/wypoczynek/turcja/turcja-egejska"},
        {"label": "Dalaman", "url": "/wypoczynek/turcja/turcja-egejska/dalaman"},
        {"label": "Marmaris"},
    ],
    "discountFullPrice": "4146",
    "originalFullPrice": "4146",
    "discountPerPersonPrice": "2073",
    "originalPerPersonPrice": "2073",
    "departureDate": "12.10.2026",
    "returnDate": "20.10.2026",
    "departureTime": "11:10",
    "departureAirport": "Katowice",
    "boardType": "All Inclusive",
    "boardCode": "GT06-AI",
    "tripAdvisorRating": 4.3,
    "tripAdvisorReviewsNo": 1328,
    "tripAdvisorReviewsText": "opinii",
    "participants": "2 Dorosłych + 0 Dzieci",
    "currency": "PLN",
    "priceCheckingAvailable": False,
    "soldOut": False,
    "promoted": False,
}


def offer(**overrides: object) -> dict[str, Any]:
    result = copy.deepcopy(BASE_OFFER)
    result.update(overrides)
    return result


# --- one valid offer, fully normalized -------------------------------------------


def test_malta_country_is_mapped() -> None:
    # Arrange: confirmed live, 2026-09-22 reconnaissance (Bella Vista MLA13036,
    # QAWRA Palace Resort & Spa MLA13004) -- breadcrumb label "Malta", slug
    # "/wypoczynek/malta". Previously an unmapped, unknown-real country.
    result = normalize_offer(
        offer(breadcrumbs=[{"label": "Malta", "url": "/wypoczynek/malta"}]), NOW
    )
    assert result.country == "MT"


def test_one_valid_offer_normalizes_fully() -> None:
    # Act
    result = normalize_offer(offer(), NOW)
    # Assert
    assert result.provider == "tui"
    assert result.offer_id == BASE_OFFER["offerCode"]
    assert result.hotel_name == "TUI SUNEO Costa Mare Suites"
    assert result.country == "TR"
    assert result.destination == "Turcja / Turcja Egejska / Dalaman / Marmaris"
    assert result.departure_airport == "KTW"
    assert result.departure_date == date(2026, 10, 12)
    assert result.return_date == date(2026, 10, 20)
    assert result.number_of_days == 8
    assert result.number_of_people == 2
    assert result.price_per_person == Decimal("2073")
    assert result.total_price == Decimal("4146")
    assert result.currency == "PLN"
    assert result.hotel_stars == 4.0
    assert result.rating == 4.3
    assert result.number_of_reviews == 1328
    assert result.provider_rating_max == 5.0
    assert result.board_type == "AI"
    assert result.url == (
        "https://www.tui.pl/wypoczynek/turcja/turcja-egejska/dalaman/"
        "tui-suneo-costa-mare-suites-dlm10079/OfferCodeWS/"
        "KTWDLM20261012111020261012202610192240L07DLM10079DZX4AA02ROADZX4A02FCYY"
    )
    assert not result.price_is_complete
    assert not result.variant_verified
    assert "11:10" in (result.price_notes or "")


# --- price / Decimal ---------------------------------------------------------------


def test_price_is_decimal_type() -> None:
    result = normalize_offer(offer(), NOW)
    assert isinstance(result.price_per_person, Decimal)
    assert isinstance(result.total_price, Decimal)


@pytest.mark.parametrize("bad", ["-100", "0", "abc", "1 234", "1,50", "1.234"])
def test_malformed_price_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="[Pp]rice"):
        normalize_offer(offer(discountPerPersonPrice=bad), NOW)


def test_total_price_still_derived_when_full_and_per_person_disagree() -> None:
    # Arrange: 4147 != 2073 * 2 -- a supported 2-adult party, just a rounding-sized
    # disagreement. discountFullPrice remains the authoritative party total; this
    # must never be mistaken for (or reported as) an unsupported party.
    result = normalize_offer(offer(discountFullPrice="4147"), NOW)
    assert result.price_per_person == Decimal("2073")
    assert result.total_price == Decimal("4147")
    assert result.number_of_people == 2
    assert "disagrees" in (result.price_notes or "")
    assert "unsupported party" not in (result.price_notes or "")


def test_total_price_derived_despite_per_person_rounding(settings: Settings) -> None:
    # Arrange: the real, live-captured case (ALC06081, 2026-09-22 reconnaissance):
    # 2575 / 2 = 1287.50, rounded up to 1288 for the per-person listing figure.
    # This used to be misreported as an "unsupported party" and silently dropped
    # total_price, even though 2 adults / 0 children is exactly the supported party.
    # Act
    result = normalize_offer(offer(discountFullPrice="2575", discountPerPersonPrice="1288"), NOW)
    # Assert
    assert result.total_price == Decimal("2575")
    assert result.price_per_person == Decimal("1288")
    assert result.number_of_people == 2
    assert "disagrees" in (result.price_notes or "")
    assert "unsupported party" not in (result.price_notes or "")
    # The shared eligibility filter now sees a real total for this offer instead of
    # None -- it still correctly rejects it, but only via price_is_complete (item 8
    # of the TUI recon), never a phantom "no total" gap caused by rounding.
    assert not matches(
        replace(result, departure_airport="LCJ", found_at=NOW, last_seen=NOW),
        settings.filters,
        NOW.date(),
    )


def test_total_price_not_derived_for_unsupported_party() -> None:
    result = normalize_offer(offer(participants="3 Dorosłych + 0 Dzieci"), NOW)
    assert result.total_price is None
    assert result.number_of_people == 3


def test_unrecognized_participants_text_leaves_party_unknown() -> None:
    result = normalize_offer(offer(participants="2 Adults"), NOW)
    assert result.number_of_people is None
    assert result.total_price is None
    assert "Unrecognized" in (result.price_notes or "")


def test_original_price_preserved_in_notes_when_discounted() -> None:
    result = normalize_offer(offer(originalPerPersonPrice="2200"), NOW)
    assert "2200" in (result.price_notes or "")
    assert result.price_per_person == Decimal("2073")


def test_malformed_original_price_rejects_the_offer() -> None:
    with pytest.raises(ValueError):
        normalize_offer(offer(originalPerPersonPrice="bad"), NOW)


# --- stars ---------------------------------------------------------------------


@pytest.mark.parametrize("stars", [0, 1, 3, 4, 5])
def test_hotel_stars_within_scale(stars: int) -> None:
    result = normalize_offer(offer(hotelStandard=stars), NOW)
    assert result.hotel_stars == float(stars)


@pytest.mark.parametrize("stars", [-1, 6])
def test_hotel_stars_outside_scale_is_rejected(stars: int) -> None:
    with pytest.raises(ValidationError):
        normalize_offer(offer(hotelStandard=stars), NOW)


def test_hotel_stars_accepts_a_genuine_half_star_float() -> None:
    # Arrange: search results report hotelStandard as a float, including 3.5.
    # Never rounded.
    result = normalize_offer(offer(hotelStandard=3.5), NOW)
    assert result.hotel_stars == 3.5


def test_hotel_stars_float_type_is_not_coerced_or_truncated() -> None:
    result = normalize_offer(offer(hotelStandard=4.0), NOW)
    assert isinstance(result.hotel_stars, float)
    assert result.hotel_stars == 4.0


# --- TripAdvisor rating and review count ----------------------------------------


def test_tripadvisor_rating_and_reviews_are_preserved() -> None:
    result = normalize_offer(offer(tripAdvisorRating=3.9, tripAdvisorReviewsNo=42), NOW)
    assert result.rating == 3.9
    assert result.number_of_reviews == 42
    assert result.provider_rating_max == 5.0


@pytest.mark.parametrize("rating", [0.5, 5.1])
def test_out_of_scale_rating_is_dropped_not_rejected(rating: float) -> None:
    result = normalize_offer(offer(tripAdvisorRating=rating), NOW)
    # Assert: an implausible rating does not invalidate the rest of the offer.
    assert result.rating is None
    assert result.hotel_name == "TUI SUNEO Costa Mare Suites"


def test_missing_rating_keeps_review_count() -> None:
    result = normalize_offer(offer(tripAdvisorRating=None), NOW)
    assert result.rating is None
    assert result.number_of_reviews == 1328


def test_negative_review_count_is_rejected() -> None:
    with pytest.raises(ValidationError):
        normalize_offer(offer(tripAdvisorReviewsNo=-1), NOW)


# --- board / meal type -----------------------------------------------------------


@pytest.mark.parametrize(
    "code,title,expected",
    [
        ("GT06-AI", "All Inclusive", "AI"),
        ("GT06-XX", "All Inclusive", "AI"),
        ("GT06-FB", "Trzy posiłki", "FB"),
        ("GT06-HB", "Dwa posiłki", "HB"),
        # Confirmed live, 2026-09-22 reconnaissance (QAWRA Palace Resort & Spa, Malta):
        # a genuine, previously-missing HB title alias, already correctly code-mapped.
        ("GT06-HBP", "Dwa posiłki plus", "HB"),
        ("GT06-BB", "Śniadanie", "BB"),
        ("GT06-AO", "Bez wyżywienia", "RO"),
    ],
)
def test_board_codes_normalize(code: str, title: str, expected: str) -> None:
    result = normalize_offer(offer(boardCode=code, boardType=title), NOW)
    assert result.board_type == expected


def test_unknown_board_code_is_unknown_not_guessed() -> None:
    result = normalize_offer(offer(boardCode="GT06-UNKNOWN", boardType="Nowa opcja"), NOW)
    assert result.board_type is None


def test_conflicting_board_code_and_title_is_unknown() -> None:
    # Arrange: code says AI, title says half board.
    result = normalize_offer(offer(boardCode="GT06-AI", boardType="Dwa posiłki"), NOW)
    assert result.board_type is None


# --- dates and duration ----------------------------------------------------------


def test_duration_disagreeing_with_dates_is_rejected() -> None:
    with pytest.raises(ValueError, match="nights disagree"):
        normalize_offer(offer(duration=6), NOW)


def test_search_xhr_duration_rule_requires_days_equal_duration() -> None:
    # Arrange: 12.10-20.10 is 8 days; duration=7 is the category-page convention
    # (days == duration + 1), which search results never use.
    with pytest.raises(ValueError, match="nights disagree.*search_xhr"):
        normalize_offer(offer(duration=7), NOW)


def test_search_xhr_duration_matches_when_equal_to_date_diff() -> None:
    # Arrange: a search_xhr-shaped record (date_diff == duration, per the real
    # MLA13036 observation: 04.12-10.12, duration=6).
    result = normalize_offer(
        offer(departureDate="04.12.2026", returnDate="10.12.2026", duration=6), NOW
    )
    assert result.number_of_days == 6


def test_return_before_departure_is_rejected() -> None:
    with pytest.raises(ValueError, match="return date"):
        normalize_offer(offer(departureDate="20.10.2026", returnDate="12.10.2026"), NOW)


@pytest.mark.parametrize("bad", ["2026-10-12", "12/10/2026", "32.10.2026", ""])
def test_malformed_dates_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_offer(offer(departureDate=bad), NOW)


# --- airport and departure time ---------------------------------------------------


@pytest.mark.parametrize(
    "label,code",
    [("Łódź", "LCJ"), ("Katowice", "KTW"), ("Warszawa-Chopina", "WAW"), ("Wrocław", "WRO")],
)
def test_known_departure_airports(label: str, code: str) -> None:
    result = normalize_offer(offer(departureAirport=label), NOW)
    assert result.departure_airport == code


def test_unknown_departure_airport_is_unknown() -> None:
    result = normalize_offer(offer(departureAirport="Nieznane Lotnisko"), NOW)
    assert result.departure_airport is None


def test_departure_time_is_recorded_in_notes() -> None:
    result = normalize_offer(offer(departureTime="06:05"), NOW)
    assert "06:05" in (result.price_notes or "")


@pytest.mark.parametrize("bad", ["6:05", "0605", "25:00", ""])
def test_malformed_departure_time_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="departure time"):
        normalize_offer(offer(departureTime=bad), NOW)


# --- URL ---------------------------------------------------------------------------


def test_unexpected_offer_url_host_is_rejected() -> None:
    with pytest.raises(ValueError, match="URL"):
        normalize_offer(offer(offerUrl="https://evil.example/wypoczynek/x"), NOW)


def test_offer_url_outside_wypoczynek_is_rejected() -> None:
    with pytest.raises(ValueError, match="URL"):
        normalize_offer(offer(offerUrl="/api/services/tui-search/api/search/offers"), NOW)


# --- offerCode / identity ----------------------------------------------------------


def test_offer_id_is_the_raw_offer_code() -> None:
    result = normalize_offer(offer(), NOW)
    assert result.offer_id == BASE_OFFER["offerCode"]
    assert result.variant_identity is None


def test_empty_offer_code_is_rejected() -> None:
    with pytest.raises(ValidationError):
        normalize_offer(offer(offerCode=""), NOW)


# --- one card = one variant; no combinations ---------------------------------------


def test_each_offer_object_yields_exactly_one_offer() -> None:
    # Arrange: two distinct real-shaped offers on one page.
    other = offer(
        hotelCode="AYT43209",
        offerCode="OTHERCODE123",
        boardCode="GT06-AO",
        boardType="Bez wyżywienia",
    )
    body = json.dumps({"offers": [offer(), other]}, ensure_ascii=False)
    result = parse_search_response(body, NOW)
    # Assert: exactly two offers, never a combination of their fields.
    assert len(result) == 2
    assert {o.offer_id for o in result} == {BASE_OFFER["offerCode"], "OTHERCODE123"}


# --- missing / conflicting data -----------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "hotelCode",
        "hotelName",
        "offerCode",
        "offerUrl",
        "discountFullPrice",
        "discountPerPersonPrice",
        "departureDate",
        "returnDate",
        "departureAirport",
        "boardType",
        "boardCode",
        "participants",
        "currency",
        "soldOut",
    ],
)
def test_missing_required_field_is_rejected(field: str) -> None:
    broken = offer()
    del broken[field]
    with pytest.raises(ValidationError):
        normalize_offer(broken, NOW)


def test_sold_out_offer_is_rejected() -> None:
    with pytest.raises(ValueError, match="sold out"):
        normalize_offer(offer(soldOut=True), NOW)


# --- search_xhr pipeline: parse_search_response / extract_search_response_offers --


def test_search_response_fixture_parses_both_real_offers() -> None:
    # Arrange: two real offers captured from one passive Playwright POC run of
    # .../api/services/tui-search/api/search/offers (public listing data).
    # Act
    result = parse_search_response(SEARCH_OFFERS_BODY, NOW)
    # Assert
    assert len(result) == 2
    by_hotel = {o.hotel_name: o for o in result}
    assert by_hotel["Bella Vista"].hotel_stars == 3.0
    assert by_hotel["Bella Vista"].board_type == "FB"
    assert by_hotel["Bella Vista"].departure_airport == "KTW"
    assert by_hotel["Bella Vista"].number_of_days == 6
    assert by_hotel["Hotel Koral"].hotel_stars == 3.5
    assert by_hotel["Hotel Koral"].board_type == "HB"
    assert by_hotel["Hotel Koral"].departure_airport == "WAW"
    assert by_hotel["Hotel Koral"].number_of_days == 7
    assert by_hotel["Hotel Koral"].country == "BG"


def test_extract_search_response_offers_returns_raw_records() -> None:
    raw = extract_search_response_offers(SEARCH_OFFERS_BODY)
    assert len(raw) == 2
    assert {r["hotelCode"] for r in raw} == {"MLA13036", "VAR11019"}


def test_search_response_missing_offers_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="offers list"):
        extract_search_response_offers(json.dumps({"pagination": {}}))


def test_search_response_wrong_type_offers_is_rejected() -> None:
    with pytest.raises(ValueError, match="offers list"):
        extract_search_response_offers(json.dumps({"offers": {}}))


def test_search_response_invalid_json_is_rejected() -> None:
    with pytest.raises(ValueError, match="JSON"):
        extract_search_response_offers("{not json")


def test_search_response_oversized_is_rejected() -> None:
    with pytest.raises(ValueError, match="size limit"):
        extract_search_response_offers("x" * 4_000_001)


def test_search_response_one_malformed_record_does_not_fail_the_whole_batch() -> None:
    good = extract_search_response_offers(SEARCH_OFFERS_BODY)[0]
    broken = offer()
    del broken["hotelName"]
    body = json.dumps({"offers": [good, broken]})
    result = parse_search_response(body, NOW)
    assert len(result) == 1
    assert result[0].offer_id == good["offerCode"]


def test_search_response_all_malformed_records_raise_a_schema_change_error() -> None:
    broken = offer()
    del broken["hotelName"]
    body = json.dumps({"offers": [broken]})
    with pytest.raises(ValueError, match="No readable"):
        parse_search_response(body, NOW)


def test_search_response_empty_offers_list_is_not_an_error() -> None:
    result = parse_search_response(json.dumps({"offers": []}), NOW)
    assert result == []


# --- final eligibility: shared filtering.matches() is the sole 7-9 day authority --


def test_final_filtering_rejects_the_real_offer_outside_7_9_days(settings: Settings) -> None:
    # Arrange: real captured offers -- Bella Vista is 6 nights, Hotel Koral is 7.
    # The confirmed upstream dF:6:dT:14 range would let both through; only the
    # shared filtering.matches() enforces the configured night window. The frozen
    # test config uses 6-8 nights (both inside), so the 7-9 window is set
    # explicitly here. The TUI price is never complete (item 8), which alone would
    # reject both offers, so the duration checks use a complete-price copy to make
    # the night window the only criterion that can differ.
    result = parse_search_response(SEARCH_OFFERS_BODY, NOW)
    by_hotel = {o.hotel_name: o for o in result}
    bella_vista, koral = by_hotel["Bella Vista"], by_hotel["Hotel Koral"]
    seven_to_nine = settings.filters.copy()
    seven_to_nine["min_nights"] = 7
    seven_to_nine["max_nights"] = 9
    # Act / Assert: duration alone decides between the two otherwise eligible offers.
    assert bella_vista.number_of_days == 6
    assert koral.number_of_days == 7
    assert matches(replace(bella_vista, price_is_complete=True), settings.filters, NOW.date())
    assert not matches(replace(bella_vista, price_is_complete=True), seven_to_nine, NOW.date())
    assert matches(replace(koral, price_is_complete=True), seven_to_nine, NOW.date())
    # Act / Assert: the real, incomplete TUI price keeps even the in-window offer out.
    assert not matches(koral, seven_to_nine, NOW.date())
