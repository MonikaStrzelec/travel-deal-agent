"""Passive TUI real-time price/availability confirmation for one candidate offer.

Confirmed by live reconnaissance (2026-09-22, `data/tui-production/
realtime-recon-20260922T191702Z/realtime_price_response.json`): TUI's own offer
detail page passively triggers `.../api/services/tui-search/api/search/offers/
price?offerCode=...&mode=REALTIME`, which reports `offerStatus` and, when
`"AVAILABLE"`, a `priceDetails` block. This module never fetches that path
directly -- `tui_browser.capture_offer_price` only opens the detail page and
listens for the response the page's own JavaScript triggers.

**`priceGuaranteeFund`: confirmed as TFG+TFP for a charter-flight package tour.**
The one successful, confirmed response observed so far (Sun City Apartments &
Hotel, KTW -> Antalya, 2 adults) reported `priceDetails.priceGuaranteeFund: 60`
alongside `priceGuaranteeFundInfo` citing the Polish package-travel act -- the
same legal fund pair as ITAKA's TFG/TFP (`itaka_details.py`). Two independent,
officially confirmed rates (2026-09-2x, project owner, from TUI's own published
terms and the TFG regulation): for a package tour with **charter** air
transport, TFG = 15 PLN and TFP = 15 PLN per traveller. For 2 adults that is
`(15 + 15) * 2 = 60 PLN` -- exactly the observed `priceGuaranteeFund`, which is
therefore confirmed to be a **per-party total**, not per-person (consistent
with it sitting in `priceDetails` next to `totalPrice`, unlike every genuinely
per-person amount in the same payload, which carries an explicit "PerPerson"
name).

This rate is confirmed **only for a charter-flight package tour**. Before
trusting it, `confirm_realtime_price` requires the response to independently,
structurally confirm that classification -- never inferred from price alone:
- `"CHARTER_FLIGHT" in tags` (the same tag observed on the listing offer), and
- `analyticsData.values.flight_type == "CHART"` (a second, independent field
  agreeing with the tag), and
- `offerTravelType == "BYPLANE"` (air transport, not e.g. "own transport").

Even once classified as a charter package, `priceGuaranteeFund` must still
equal the confirmed rate exactly (`_expected_guarantee_fund`); any disagreement
(a rate change, or a different fee composition) fails closed with a
`ValueError` rather than silently trusting whatever number TUI reports. A
non-charter, or not-yet-classifiable, offer is left unconfirmed
(`price_is_complete` stays `False`, reason `CHARTER_PACKAGE_NOT_CONFIRMED`) --
its own mandatory-fee rate has not been confirmed and is not guessed here.

**Local mandatory costs: known limitation, not implemented here.** The same
reconnaissance also found a real local cost (Malta hotel tourist tax, ~1.50
EUR/day/person, capped ~22.50 EUR/person, paid at the hotel reception) -- the
same kind of fact the generic `models.LocalMandatoryCost` /
`itaka_details.local_costs()` mechanism already exists to record. ITAKA's
extractor works because ITAKA's detail stream carries *structured*
practical-information objects (`descriptionShort`/`title` fields in the RSC
stream) to scan. No structured equivalent has been found anywhere in TUI's own
JSON responses (`offerData`, this `price/REALTIME` response, `alternatives`);
the Malta tax text was only ever observed in the rendered page's free-form body
text. Scraping that would mean a fragile, ad hoc HTML/text parser matched
against arbitrary page copy, which risks silently breaking or silently
misfiring on unrelated text -- exactly what this project avoids elsewhere. This
module therefore does not populate `Offer.local_mandatory_costs` for TUI at
all; it is left as a known limitation until (or unless) a structured source is
found.
"""

import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator

from ..models import Offer, OperatorFee
from .itaka_data import Boundary, mapping

MAX_JSON_BYTES = 4_000_000

# Officially confirmed (2026-09-2x, project owner, from TUI's own published terms
# and the TFG regulation): per-traveller mandatory fund contributions for a
# package tour with charter air transport. Confirmed only for this transport
# type; a non-charter rate is not known and not guessed here.
TFG_PER_TRAVELLER_CHARTER_PLN = Decimal("15")
TFP_PER_TRAVELLER_CHARTER_PLN = Decimal("15")

_KNOWN_PRICE_DETAIL_FIELDS = frozenset(
    {
        "totalPrice",
        "totalDiscountPrice",
        "priceGuaranteeFund",
        "priceGuaranteeFundInfo",
        "priceDifference",
        "currency",
        "pricePerPerson",
        "factor",
        "discountPercentage",
    }
)


def _decimal_amount(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("Expected a numeric amount")
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Non-finite amount")
    return amount


class PriceDetails(Boundary):
    """Only the fields this module uses; `_KNOWN_PRICE_DETAIL_FIELDS` guards the rest."""

    totalPrice: Decimal
    priceGuaranteeFund: Decimal
    priceDifference: Decimal
    currency: str = Field(min_length=1)
    pricePerPerson: Decimal

    @field_validator(
        "totalPrice", "priceGuaranteeFund", "priceDifference", "pricePerPerson", mode="before"
    )
    @classmethod
    def _amount(cls, value: object) -> Decimal:
        return _decimal_amount(value)


class TravellerCount(Boundary):
    adults: int = Field(ge=0)
    children: int = Field(ge=0)


class FlightLeg(Boundary):
    departureAirportCode: str = Field(min_length=1)


class Accommodation(Boundary):
    hotelCode: str = Field(min_length=1)
    duration: int = Field(ge=1)


class AnalyticsValues(Boundary):
    flight_type: str | None = None


class AnalyticsData(Boundary):
    values: AnalyticsValues | None = None


class RealtimePriceResponse(Boundary):
    """Only the fields used for confirmation; unrecognized top-level fields are ignored.

    `offerCode` and `priceDetails` are optional at the model level because a
    `NOT_AVAILABLE` response (confirmed live) omits both -- that is a normal,
    non-error outcome, checked explicitly in `confirm_realtime_price` before any
    field below it is required.
    """

    offerStatus: str
    offerCode: str | None = None
    priceDetails: PriceDetails | None = None
    travellerCount: TravellerCount | None = None
    outboundFlight: FlightLeg | None = None
    accommodations: list[Accommodation] = Field(default_factory=list)
    startDate: str | None = None
    endDate: str | None = None
    tags: list[str] = Field(default_factory=list)
    offerTravelType: str | None = None
    analyticsData: AnalyticsData | None = None


def parse_realtime_price(body: str) -> RealtimePriceResponse:
    """Parse the passively captured REALTIME response body; never guesses new fields."""
    if len(body) > MAX_JSON_BYTES:
        raise ValueError("TUI REALTIME price response exceeds size limit")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ValueError("Invalid TUI REALTIME price JSON payload") from exc
    top = mapping(data)
    price_details = top.get("priceDetails")
    if price_details is not None:
        unknown = set(mapping(price_details)) - _KNOWN_PRICE_DETAIL_FIELDS
        if unknown:
            raise ValueError(
                f"Unknown TUI priceDetails field(s), possible new mandatory fee: {sorted(unknown)}"
            )
    return RealtimePriceResponse.model_validate(top)


def _is_confirmed_charter_air_package(response: RealtimePriceResponse) -> bool:
    """Structural confirmation only -- never inferred from price or airline name."""
    flight_type = (
        response.analyticsData.values.flight_type
        if response.analyticsData is not None and response.analyticsData.values is not None
        else None
    )
    return (
        "CHARTER_FLIGHT" in response.tags
        and flight_type == "CHART"
        and response.offerTravelType == "BYPLANE"
    )


def confirm_realtime_price(offer: Offer, body: str) -> Offer:
    """Confirm one candidate offer's real-time price/availability, fail-closed.

    Returns the offer unchanged in `price_is_complete` (still `False`) whenever the
    check is inconclusive, not available, or reports an unrecognized status --
    those are normal outcomes, not errors. Raises `ValueError` for evidence that
    actively contradicts the listing (mismatched identity, malformed payload,
    unknown fee fields, non-zero `priceDifference`, a `priceGuaranteeFund` that
    disagrees with the confirmed charter-package rate); the caller is expected to
    catch this alongside the existing parsing exceptions and keep the original,
    unconfirmed offer -- exactly like `itaka_details.confirm_detail`.
    """
    response = parse_realtime_price(body)

    if response.offerStatus != "AVAILABLE":
        return replace(
            offer,
            price_verification_reason=(
                f"TUI real-time check reported offerStatus={response.offerStatus!r}; not confirmed"
            ),
        )

    if response.offerCode != offer.offer_id:
        raise ValueError(
            f"REALTIME offerCode mismatch: expected={offer.offer_id!r}, got={response.offerCode!r}"
        )
    if response.priceDetails is None:
        raise ValueError("REALTIME response for an AVAILABLE offer is missing priceDetails")
    if response.travellerCount is None or (
        response.travellerCount.adults,
        response.travellerCount.children,
    ) != (2, 0):
        raise ValueError("REALTIME travellerCount is not exactly 2 adults, 0 children")
    if offer.number_of_people != 2:
        raise ValueError("Offer party is not 2 people; TUI real-time confirmation needs 2 adults")

    price = response.priceDetails
    if price.currency != offer.currency:
        raise ValueError(
            f"REALTIME currency {price.currency!r} disagrees with offer currency {offer.currency!r}"
        )
    if price.priceDifference != 0:
        raise ValueError(f"REALTIME priceDifference={price.priceDifference} is nonzero")
    if price.totalPrice <= 0 or price.pricePerPerson <= 0 or price.priceGuaranteeFund < 0:
        raise ValueError("REALTIME reported a non-positive or invalid price amount")

    if (
        response.outboundFlight is not None
        and offer.departure_airport is not None
        and response.outboundFlight.departureAirportCode != offer.departure_airport
    ):
        raise ValueError("REALTIME departure airport disagrees with the listing offer")
    if response.accommodations:
        accommodation = response.accommodations[0]
        if offer.offer_id and accommodation.hotelCode not in offer.offer_id:
            raise ValueError("REALTIME hotel code is not part of the offer's identity code")
        if offer.number_of_days is not None and accommodation.duration != offer.number_of_days:
            raise ValueError("REALTIME duration disagrees with the listing offer")
    if (
        response.startDate is not None
        and offer.departure_date is not None
        and datetime.fromisoformat(response.startDate).date() != offer.departure_date
    ):
        raise ValueError("REALTIME start date disagrees with the listing offer")
    if (
        response.endDate is not None
        and offer.return_date is not None
        and datetime.fromisoformat(response.endDate).date() != offer.return_date
    ):
        raise ValueError("REALTIME end date disagrees with the listing offer")

    package_price = price.totalPrice
    adults = response.travellerCount.adults

    if not _is_confirmed_charter_air_package(response):
        return replace(
            offer,
            price_verification_reason=(
                "TUI real-time check confirmed offerStatus=AVAILABLE and reconciled "
                f"priceDetails.totalPrice={package_price} {price.currency}, but this offer is "
                "not structurally confirmed as a charter-flight package tour (tags/"
                "analyticsData.flight_type/offerTravelType) -- the only confirmed TFG/TFP rate "
                "applies to charter-flight packages. price_is_complete stays False "
                "(CHARTER_PACKAGE_NOT_CONFIRMED)."
            ),
        )

    expected_fund = (TFG_PER_TRAVELLER_CHARTER_PLN + TFP_PER_TRAVELLER_CHARTER_PLN) * adults
    if price.priceGuaranteeFund != expected_fund:
        raise ValueError(
            f"REALTIME priceGuaranteeFund={price.priceGuaranteeFund} disagrees with the "
            f"confirmed charter-package TFG+TFP rate of {expected_fund} {price.currency} for "
            f"{adults} adults; possible rate change or different fee composition"
        )

    fees = [
        OperatorFee("TFG", TFG_PER_TRAVELLER_CHARTER_PLN * adults, price.currency),
        OperatorFee("TFP", TFP_PER_TRAVELLER_CHARTER_PLN * adults, price.currency),
    ]
    booking_total_price = package_price + sum((fee.amount for fee in fees), Decimal(0))

    return replace(
        offer,
        package_price=package_price,
        operator_mandatory_fees=fees,
        booking_total_price=booking_total_price,
        total_price=booking_total_price,
        price_per_person=booking_total_price / 2,
        price_is_complete=True,
        variant_verified=True,
        sale_status="available",
        price_notes="Confirmed TUI real-time booking total (TFG+TFP included); local costs are separate",
        price_verification_reason=(
            "TUI real-time check confirmed offerStatus=AVAILABLE for a charter-flight package "
            f"tour; booking total {booking_total_price} {price.currency} = totalPrice "
            f"{package_price} + confirmed TFG+TFP {expected_fund} "
            f"({TFG_PER_TRAVELLER_CHARTER_PLN}+{TFP_PER_TRAVELLER_CHARTER_PLN} PLN/traveller x "
            f"{adults})"
        ),
    )
