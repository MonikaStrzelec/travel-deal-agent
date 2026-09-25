"""Offline tests for TUI real-time price/availability confirmation.

`realtime_price_available.json` and `realtime_offer_raw.json` are the exact,
real listing offer and real-time response captured during controlled, robots-
respecting reconnaissance on 2026-09-22 (Sun City Apartments & Hotel, KTW ->
Antalya); `realtime_price_not_available.json` is the real NOT_AVAILABLE
response captured for a different, since-expired offer the same day. No test
here contacts tui.pl or uses Playwright.
"""

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from travel_deal_agent.models import Offer, OperatorFee
from travel_deal_agent.providers.tui_data import normalize_offer
from travel_deal_agent.providers.tui_price import (
    TFG_PER_TRAVELLER_CHARTER_PLN,
    TFP_PER_TRAVELLER_CHARTER_PLN,
    confirm_realtime_price,
    parse_realtime_price,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tui"
NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)

RAW_OFFER: dict[str, Any] = json.loads(
    (FIXTURES / "realtime_offer_raw.json").read_text(encoding="utf-8")
)
REALTIME_AVAILABLE = (FIXTURES / "realtime_price_available.json").read_text(encoding="utf-8")
REALTIME_NOT_AVAILABLE = (FIXTURES / "realtime_price_not_available.json").read_text(
    encoding="utf-8"
)


def raw_offer(**overrides: object) -> dict[str, Any]:
    result = deepcopy(RAW_OFFER)
    result.update(overrides)
    return result


def offer(**overrides: object) -> Offer:
    return normalize_offer(raw_offer(**overrides), NOW)


def realtime(**overrides: object) -> str:
    data = json.loads(REALTIME_AVAILABLE)
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def realtime_price(**overrides: object) -> str:
    data = json.loads(REALTIME_AVAILABLE)
    data["priceDetails"] = {**data["priceDetails"], **overrides}
    return json.dumps(data, ensure_ascii=False)


# --- happy path (real captured data): confirmed AVAILABLE AND complete ---------
# Sun City is a confirmed charter-flight package tour (tags=CHARTER_FLIGHT,
# analyticsData.flight_type=CHART, offerTravelType=BYPLANE), so the officially
# confirmed TFG+TFP rate (15+15 PLN/traveller) applies and price_is_complete
# becomes True: 2786 (totalPrice) + 60 (confirmed TFG+TFP for 2 adults) = 2846
# total, 1423/person.


def test_available_charter_package_offer_is_fully_confirmed() -> None:
    # Arrange
    base = offer()
    # Act
    result = confirm_realtime_price(base, REALTIME_AVAILABLE)
    # Assert
    assert result.price_is_complete is True
    assert result.variant_verified is True
    assert result.sale_status == "available"
    assert result.package_price == Decimal("2786")
    assert result.booking_total_price == Decimal("2846")
    assert result.total_price == Decimal("2846")
    assert result.price_per_person == Decimal("1423")
    assert result.operator_mandatory_fees == [
        OperatorFee("TFG", Decimal("30"), "PLN"),
        OperatorFee("TFP", Decimal("30"), "PLN"),
    ]
    assert "AVAILABLE" in (result.price_verification_reason or "")
    assert "TFG+TFP" in (result.price_verification_reason or "")
    # Identity is never rebuilt by confirmation.
    assert result.offer_id == base.offer_id


def test_guarantee_fund_matches_the_confirmed_charter_rate_for_two_adults() -> None:
    # Documents the external confirmation directly: (15 + 15) * 2 == 60 == the
    # real captured priceGuaranteeFund for this 2-adult offer.
    assert Decimal("60") == (TFG_PER_TRAVELLER_CHARTER_PLN + TFP_PER_TRAVELLER_CHARTER_PLN) * 2


def test_non_charter_offer_is_left_unconfirmed() -> None:
    # Arrange: same offer, but not structurally confirmed as a charter package
    # (e.g. a scheduled/"linowy" flight) -- the charter TFG/TFP rate does not
    # apply, and no other rate is confirmed, so this must stay unconfirmed.
    base = offer()
    body = json.loads(REALTIME_AVAILABLE)
    body["tags"] = ["FIRST_MINUTE"]
    body["analyticsData"]["values"]["flight_type"] = "LINE"
    result = confirm_realtime_price(base, json.dumps(body))
    assert result.price_is_complete is False
    assert "CHARTER_PACKAGE_NOT_CONFIRMED" in (result.price_verification_reason or "")


def test_missing_charter_tag_alone_is_not_enough() -> None:
    # Arrange: analyticsData/offerTravelType still say charter/by-plane, but the
    # tags list no longer includes it -- structural confirmation requires all
    # three independent signals to agree, not just two out of three.
    base = offer()
    body = json.loads(REALTIME_AVAILABLE)
    body["tags"] = ["FIRST_MINUTE"]
    result = confirm_realtime_price(base, json.dumps(body))
    assert result.price_is_complete is False
    assert "CHARTER_PACKAGE_NOT_CONFIRMED" in (result.price_verification_reason or "")


def test_guarantee_fund_disagreeing_with_the_confirmed_rate_is_rejected() -> None:
    # Arrange: still a confirmed charter package, but the fund amount does not
    # match (15+15)*2=60 -- a possible rate change, never silently trusted.
    base = offer()
    body = realtime_price(priceGuaranteeFund=99)
    with pytest.raises(ValueError, match="disagrees with the confirmed charter-package"):
        confirm_realtime_price(base, body)


def test_real_response_reports_a_mandatory_fee_not_in_the_listing_price() -> None:
    parsed = parse_realtime_price(REALTIME_AVAILABLE)
    # Assert: documents the actual discovery -- a confirmed mandatory fund fee
    # (Turystyczny Fundusz Gwarancyjny/Pomocowy) sitting outside totalPrice.
    assert parsed.offerStatus == "AVAILABLE"
    assert parsed.priceDetails is not None
    assert parsed.priceDetails.priceGuaranteeFund == Decimal("60")
    assert parsed.priceDetails.totalPrice == Decimal("2786")
    assert parsed.priceDetails.pricePerPerson == Decimal("1393")


# --- NOT_AVAILABLE / unknown status: never an error, never a new identity ------


def test_not_available_offer_is_left_unconfirmed_without_raising() -> None:
    base = offer()
    result = confirm_realtime_price(base, REALTIME_NOT_AVAILABLE)
    # Assert: a legitimate business outcome, not a parsing failure.
    assert result.price_is_complete is False
    assert "NOT_AVAILABLE" in (result.price_verification_reason or "")


def test_not_available_never_changes_identity() -> None:
    base = offer()
    result = confirm_realtime_price(base, REALTIME_NOT_AVAILABLE)
    # Assert: disappearance/expiry must not fabricate a new offer_id or identity.
    assert result.offer_id == base.offer_id
    assert result.variant_identity == base.variant_identity
    assert result.hotel_name == base.hotel_name
    assert result.departure_date == base.departure_date
    assert result.return_date == base.return_date
    assert result.departure_airport == base.departure_airport


def test_unknown_status_is_treated_like_not_available() -> None:
    # Arrange: a hypothetical future status this project has never observed.
    base = offer()
    body = realtime(offerStatus="PENDING_REVIEW", offerCode=None, priceDetails=None)
    result = confirm_realtime_price(base, body)
    # Assert: no crash, no confirmation -- exactly like NOT_AVAILABLE.
    assert result.price_is_complete is False
    assert "PENDING_REVIEW" in (result.price_verification_reason or "")


# --- fail-closed identity and structure checks (raise, caller keeps the offer) --


@pytest.mark.parametrize(
    "body,match",
    [
        pytest.param(
            realtime(offerCode="SOME-OTHER-OFFER-CODE"), "offerCode mismatch", id="offer_code"
        ),
        pytest.param(realtime(priceDetails=None), "priceDetails", id="missing_price_details"),
        pytest.param(
            realtime(travellerCount={"adults": 2, "children": 1}),
            "travellerCount",
            id="traveller_count",
        ),
        pytest.param(realtime_price(currency="EUR"), "currency", id="currency"),
    ],
)
def test_structure_mismatch_is_rejected(body: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        confirm_realtime_price(offer(), body)


# --- priceDifference is informational only, never a gate (2026-09-25 live evidence) --
#
# Confirmed by real captured evidence (see tui_price.py's module-level note):
# a nonzero priceDifference reflects the realtime totalPrice's own delta from
# the listing snapshot, not an ambiguous or incomplete price. The realtime
# totalPrice/pricePerPerson (used for `package_price`/`booking_total_price`
# below) are already the authoritative current price regardless of this
# delta; no arbitrary tolerance threshold is used or needed.


def test_realtime_price_increase_is_still_confirmed_using_realtime_amount() -> None:
    base = offer()
    body = realtime_price(
        totalPrice=2788, totalDiscountPrice=2788, pricePerPerson=1394, priceDifference=2
    )
    result = confirm_realtime_price(base, body)
    assert result.price_is_complete is True
    assert result.package_price == Decimal("2788")
    assert result.booking_total_price == Decimal("2848")  # 2788 + 60 confirmed TFG+TFP
    assert result.price_per_person == Decimal("1424")
    assert "differs from the listing snapshot by 2 PLN" in (result.price_verification_reason or "")


def test_realtime_price_decrease_is_still_confirmed_using_realtime_amount() -> None:
    base = offer()
    body = realtime_price(
        totalPrice=2784, totalDiscountPrice=2784, pricePerPerson=1392, priceDifference=-2
    )
    result = confirm_realtime_price(base, body)
    assert result.price_is_complete is True
    assert result.package_price == Decimal("2784")
    assert result.booking_total_price == Decimal("2844")  # 2784 + 60 confirmed TFG+TFP
    assert result.price_per_person == Decimal("1422")
    assert "differs from the listing snapshot by -2 PLN" in (result.price_verification_reason or "")


def test_large_realtime_price_change_is_still_confirmed_without_arbitrary_threshold() -> None:
    # No abs(priceDifference) <= X tolerance anywhere: a large delta is accepted
    # exactly like a small one, as long as every other fail-closed check
    # (identity, dates, board, party size, charter structure, fund amount)
    # still passes -- the API's own structure decides this, not a magic number.
    base = offer()
    body = realtime_price(
        totalPrice=3286, totalDiscountPrice=3286, pricePerPerson=1643, priceDifference=500
    )
    result = confirm_realtime_price(base, body)
    assert result.price_is_complete is True
    assert result.booking_total_price == Decimal("3346")  # 3286 + 60


def test_missing_guarantee_fund_field_is_rejected() -> None:
    # A missing mandatory fee field (not merely a disagreeing amount, already
    # covered by test_guarantee_fund_disagreeing_with_the_confirmed_rate_is_rejected)
    # must still fail closed rather than silently treating it as zero/absent.
    base = offer()
    data = json.loads(REALTIME_AVAILABLE)
    del data["priceDetails"]["priceGuaranteeFund"]
    with pytest.raises(ValueError):
        confirm_realtime_price(base, json.dumps(data))


@pytest.mark.parametrize("field", ["totalPrice", "pricePerPerson"])
def test_nonpositive_price_amount_is_rejected(field: str) -> None:
    base = offer()
    body = realtime_price(**{field: 0})
    with pytest.raises(ValueError, match="non-positive"):
        confirm_realtime_price(base, body)


def test_negative_guarantee_fund_is_rejected() -> None:
    base = offer()
    body = realtime_price(priceGuaranteeFund=-1)
    with pytest.raises(ValueError, match="non-positive or invalid"):
        confirm_realtime_price(base, body)


def test_unknown_price_details_field_fails_closed() -> None:
    # A defensive regression: a new, unrecognized monetary field must block
    # confirmation rather than being silently ignored (possible new operator fee).
    base = offer()
    body = realtime_price(someNewMandatoryFee=25)
    with pytest.raises(ValueError, match="[Uu]nknown"):
        confirm_realtime_price(base, body)


def test_malformed_json_is_rejected() -> None:
    base = offer()
    with pytest.raises(ValueError, match="JSON"):
        confirm_realtime_price(base, "{not json")


def test_missing_offer_status_is_rejected() -> None:
    base = offer()
    with pytest.raises(ValueError):
        confirm_realtime_price(base, json.dumps({"priceDetails": None}))


# --- identity binding: offerCode ties hotel/date/airport/party together --------


def _with_accommodation(**fields: object) -> Callable[[dict[str, Any]], None]:
    def mutate(body: dict[str, Any]) -> None:
        body["accommodations"] = [{**body["accommodations"][0], **fields}]

    return mutate


def _with_departure_airport(code: str) -> Callable[[dict[str, Any]], None]:
    def mutate(body: dict[str, Any]) -> None:
        body["outboundFlight"] = {**body["outboundFlight"], "departureAirportCode": code}

    return mutate


@pytest.mark.parametrize(
    "mutate,match",
    [
        pytest.param(_with_accommodation(hotelCode="ZZZ99999"), "hotel code", id="hotel_code"),
        pytest.param(_with_departure_airport("WAW"), "departure airport", id="departure_airport"),
        pytest.param(_with_accommodation(duration=3), "duration", id="duration"),
        pytest.param(
            lambda body: body.update(startDate="2027-01-01T00:00:00"), "start date", id="start_date"
        ),
        pytest.param(
            lambda body: body.update(endDate="2027-01-08T00:00:00"), "end date", id="end_date"
        ),
    ],
)
def test_identity_mismatch_is_rejected(
    mutate: Callable[[dict[str, Any]], None], match: str
) -> None:
    body = json.loads(REALTIME_AVAILABLE)
    mutate(body)
    with pytest.raises(ValueError, match=match):
        confirm_realtime_price(offer(), json.dumps(body))
