"""Confirm a listing variant and its operator booking price from public detail HTML."""

import re
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from html import unescape

from pydantic import Field, field_validator

from ..boards import normalized_text
from ..models import LocalMandatoryCost, Offer, OperatorFee
from .boundary import Boundary, mapping
from .itaka_data import AIRPORTS, Named, Rate, Segment
from .itaka_rsc import FlightData


class Money(Boundary):
    amount: Decimal
    currency: str

    @field_validator("amount", mode="before")
    @classmethod
    def monetary_amount(cls, value: object) -> Decimal:
        if type(value) is not int and not isinstance(value, Decimal):
            raise ValueError("Expected a numeric currency amount")
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
            raise ValueError("Invalid monetary amount")
        return amount


class Fee(Boundary):
    type: str
    amount: Money


class AdultPrice(Boundary):
    type: str
    price: Money
    additionalPayments: list[Fee]


class BookingPrice(Boundary):
    actualPrice: Money
    actualWithAdditionalPayments: Money
    additionalPayments: list[Fee]
    participants: list[AdultPrice]
    priceCatalogCode: str


class Party(Boundary):
    adults: int
    children: int
    infants: int


class Stay(Boundary):
    days: int = Field(ge=1)
    nights: int = Field(ge=1)


class Journey(Boundary):
    type: str
    id: str
    title: str
    beginDateTime: str
    isDirect: bool


class TransportPair(Boundary):
    outbound: Journey
    inbound: Journey


class Variant(Boundary):
    id: str
    supplierObjectId: str
    rateType: str
    saleStatus: str
    beginDate: str
    endDate: str
    duration: Stay
    meal: Named
    room: Named
    price: BookingPrice
    participants: Party
    transport: TransportPair


class Place(Boundary):
    title: str
    code: str = Field(min_length=1)


class FlightPoint(Boundary):
    place: Place
    dateTime: str
    carrierName: str = Field(min_length=1)
    carrierFlightNumber: str = Field(min_length=1)
    carrierCode: str = Field(min_length=1)


def unique_object(evidence: FlightData, candidates: list[dict[str, object]]) -> dict[str, object]:
    resolved = [mapping(evidence.resolve(candidate)) for candidate in candidates]
    if not resolved or any(item != resolved[0] for item in resolved[1:]):
        raise ValueError("Missing or conflicting detail evidence")
    return resolved[0]


def map_multiroom_fields(
    evidence: FlightData, expected_id: str, selected: dict[str, object]
) -> None:
    """Map observed aliases and single-group context for the already selected rate.

    Do not infer rateType or complete flights from a departure or a mixed summary.
    Operator totals are copied as evidence, never calculated here.
    """

    def put(target: dict[str, object], field: str, value: object) -> None:
        if field in target and target[field] != value:
            raise ValueError(
                f"Conflicting detail evidence for rate_id={expected_id!r}; "
                f"{field}: first={target[field]!r}, other={value!r}"
            )
        target[field] = value

    for obj in evidence.objects():
        if obj.get("rateId") == expected_id and "supplierObjectId" in obj:
            put(selected, "supplierObjectId", evidence.resolve(obj["supplierObjectId"]))
        if obj.get("id") == expected_id:
            for source, field in (("mainRoom", "room"), ("mainMeal", "meal")):
                if source not in obj:
                    continue
                named = mapping(evidence.resolve(obj[source]))
                if "roomId" in named and "id" in named and named["roomId"] != named["id"]:
                    raise ValueError(f"Conflicting room aliases for rate_id={expected_id!r}")
                value = {"id": named.get("id", named.get("roomId")), "title": named.get("title")}
                put(selected, field, value)
        groups = obj.get("participantGroups")
        if not isinstance(groups, list) or len(groups) != 1:
            continue
        group = mapping(evidence.resolve(groups[0]))
        if group.get("id") != expected_id:
            continue
        for field in ("beginDate", "endDate", "duration"):
            if field in obj:
                put(selected, field, evidence.resolve(obj[field]))
        totals: list[object] = []
        if "priceWithAdditionalPayments" in obj:
            totals.append(evidence.resolve(obj["priceWithAdditionalPayments"]))
        if "price" in obj:
            price = mapping(evidence.resolve(obj["price"]))
            if "totalWithAdditionalPayments" in price:
                totals.append(price["totalWithAdditionalPayments"])
        for total in totals:
            price = mapping(selected.get("price", {}))
            put(price, "actualWithAdditionalPayments", total)
            selected["price"] = price


def select_variant(evidence: FlightData, expected_id: str) -> Variant:
    """Select only evidence for the requested rate, including participant groups.

    Same-rate fragments may supply missing fields, never conflicting values.
    Incomplete evidence still fails the existing Variant boundary validation.
    """
    selected: dict[str, object] = {}
    found: set[str] = set()
    for obj in evidence.objects():
        if not any(key in obj for key in ("saleStatus", "transport", "price", "participants")):
            continue
        identity = evidence.resolve(obj.get("id", obj.get("rateId")))
        if not isinstance(identity, str):
            continue
        found.add(identity)
        if identity != expected_id:
            continue
        candidate = mapping(evidence.resolve(obj))
        candidate["id"] = identity
        for field in Variant.model_fields:
            if field not in candidate:
                continue
            value = candidate[field]
            if field in selected and selected[field] != value:
                raise ValueError(
                    f"Conflicting detail evidence for rate_id={expected_id!r}; "
                    f"{field}: first={selected[field]!r}, other={value!r}"
                )
            selected[field] = value
    if not selected:
        detail: object = next(iter(found)) if len(found) == 1 else sorted(found)
        raise ValueError(
            f"Detail variant does not match listing; rate_id: "
            f"listing={expected_id!r}, detail={detail!r}"
        )
    map_multiroom_fields(evidence, expected_id, selected)
    try:
        return Variant.model_validate(selected)
    except ValueError as exc:
        raise ValueError(
            f"Incomplete or invalid detail variant for rate_id={expected_id!r}: {exc}"
        ) from exc


def fee_totals(fees: list[Fee], currency: str) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for fee in fees:
        kind = fee.type.upper()
        # New types require observed semantics and tests, not speculative inclusion.
        if kind not in {"TFG", "TFP"} or kind in result or fee.amount.currency != currency:
            raise ValueError("Unknown, duplicated or inconsistent operator fee")
        result[kind] = fee.amount.amount
    return result


def local_costs(evidence: FlightData, url: str | None) -> list[LocalMandatoryCost]:
    """Preserve source descriptions and conditions; never infer zero or convert currencies."""
    result: list[LocalMandatoryCost] = []
    seen: set[str] = set()
    for obj in evidence.objects():
        if "descriptionShort" not in obj or "title" not in obj:
            continue
        title = obj["title"]
        if not isinstance(title, str):
            continue
        try:
            short = evidence.resolve(obj["descriptionShort"])
            long = evidence.resolve(obj.get("descriptionLong"))
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        parts = [title, *(v for v in (short, long) if isinstance(v, str))]
        text = " ".join(parts)
        text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
        text = " ".join(unescape(re.sub(r"<[^>]+>", " ", text)).split())
        lowered = text.casefold()
        if not (
            ("obowiązk" in lowered and any(w in lowered for w in ("podatek", "opłat", "koszt")))
            or ("wiz" in lowered and any(w in lowered for w in ("koszt", "płat", "usd", "eur")))
        ):
            continue
        if text not in seen:
            result.append(
                LocalMandatoryCost(
                    description=text,
                    certainty="approximate" if "ok." in lowered else "unknown",
                    source_url=url,
                )
            )
            seen.add(text)
    return result


class Differences:
    """Remember the first listing/detail disagreement for the fail-closed error message."""

    def __init__(self) -> None:
        self.first = ""

    def found(
        self, failed: bool, field: str, listing: object, detail: object, rule: str = ""
    ) -> bool:
        if failed and not self.first:
            self.first = f"{field}: listing={listing!r}, detail={detail!r}" + (
                f"; rule={rule}" if rule else ""
            )
        return failed


def confirm_detail(offer: Offer, raw: dict[str, object], html: str) -> Offer:
    """Keep the exact legacy identity; enrich only after all booking checks pass."""
    listing = Rate.model_validate(raw)
    evidence = FlightData(html)
    rate_id = listing.participantGroups[0].rateId
    variant = select_variant(evidence, rate_id)
    costs = local_costs(evidence, offer.url)
    require_variant_matches_listing(listing, variant, rate_id)
    if variant.saleStatus != "available":
        return replace(
            offer,
            sale_status=variant.saleStatus,
            local_mandatory_costs=costs,
            price_verification_reason="Variant is not available",
        )
    summary = booking_summary(evidence, listing, rate_id)
    require_flights_match_listing(listing, variant, summary, raw)
    package_price, fees, total = reconciled_booking_price(listing, variant, summary)
    return replace(
        offer,
        package_price=package_price,
        operator_mandatory_fees=[
            OperatorFee(kind, amount, listing.currency) for kind, amount in sorted(fees.items())
        ],
        booking_total_price=total,
        total_price=total,
        price_per_person=total / 2,
        price_is_complete=True,
        variant_verified=True,
        sale_status="available",
        local_mandatory_costs=costs,
        price_notes="Confirmed operator booking total; local costs are separate",
        price_verification_reason="Listing and available detail variant agree; operator fees reconciled",
    )


def require_variant_matches_listing(listing: Rate, variant: Variant, rate_id: str) -> None:
    """Check identity, party, dates, room and board; the first failed check is reported."""
    party = variant.participants
    hotel = listing.segments[1]
    room = hotel.participantGroups[0]
    differences = Differences()
    mismatch = differences.found
    if (
        mismatch(variant.id != rate_id, "rate_id", rate_id, variant.id)
        or mismatch(
            variant.supplierObjectId != listing.supplierObjectId,
            "hotel",
            listing.supplierObjectId,
            variant.supplierObjectId,
        )
        or mismatch(
            variant.rateType != "holidays",
            "rate_type",
            listing.rateType,
            variant.rateType,
            "detail must be holidays",
        )
        or mismatch(
            (party.adults, party.children, party.infants) != (2, 0, 0),
            "participants",
            [p.type for p in listing.participantGroups[0].participants],
            party.model_dump(),
            "detail must contain 2 adults, 0 children, 0 infants",
        )
        or mismatch(
            len(listing.participantGroups) != 1,
            "participant_groups",
            len(listing.participantGroups),
            party.model_dump(),
            "listing must contain exactly 1 participant group",
        )
        or mismatch(
            variant.beginDate != hotel.beginDate, "dates.begin", hotel.beginDate, variant.beginDate
        )
        or mismatch(variant.endDate != hotel.endDate, "dates.end", hotel.endDate, variant.endDate)
        or mismatch(
            variant.duration.days != listing.duration.days,
            "duration.days",
            listing.duration.days,
            variant.duration.days,
        )
        or mismatch(
            variant.duration.nights != stay_nights(hotel),
            "duration.nights",
            stay_nights(hotel),
            variant.duration.nights,
        )
        or mismatch(variant.room.id != room.room.id, "room.id", room.room.id, variant.room.id)
        or mismatch(
            not variant.room.id,
            "room.id",
            room.room.id,
            variant.room.id,
            "detail ID must be nonempty",
        )
        or mismatch(
            normalized_text(variant.room.title) != normalized_text(room.room.title),
            "room.title",
            room.room.title,
            variant.room.title,
            "normalized titles must match",
        )
        or mismatch(variant.meal.id != room.meal.id, "board.id", room.meal.id, variant.meal.id)
        or mismatch(
            not variant.meal.id,
            "board.id",
            room.meal.id,
            variant.meal.id,
            "detail ID must be nonempty",
        )
        or mismatch(
            normalized_text(variant.meal.title) != normalized_text(room.meal.title),
            "board.title",
            room.meal.title,
            variant.meal.title,
            "normalized titles must match",
        )
    ):
        raise ValueError("Detail variant does not match listing; " + differences.first)


def stay_nights(hotel: Segment) -> int:
    return (date.fromisoformat(hotel.endDate) - date.fromisoformat(hotel.beginDate)).days


def booking_summary(evidence: FlightData, listing: Rate, rate_id: str) -> dict[str, object]:
    """Return the single booking summary for this rate, room and one-room party."""
    hotel = listing.segments[1]
    room_id = hotel.participantGroups[0].room.id
    candidates = [
        obj
        for obj in evidence.objects()
        if "transportDetails" in obj
        and "rooms" in obj
        and mapping(evidence.resolve(obj["rooms"])).get("offerIds") == [rate_id]
    ]
    summary = unique_object(evidence, candidates)
    rooms = mapping(summary["rooms"])
    differences = Differences()
    mismatch = differences.found
    if (
        mismatch(
            rooms.get("offerIds") != [rate_id], "room.offer_ids", [rate_id], rooms.get("offerIds")
        )
        or mismatch(rooms.get("ids") != [room_id], "room.ids", [room_id], rooms.get("ids"))
        or mismatch(
            summary.get("roomCount") != 1,
            "room.count",
            len(hotel.participantGroups),
            summary.get("roomCount"),
            "detail room count must be 1",
        )
    ):
        raise ValueError("Conflicting room or offer identity; " + differences.first)
    return summary


def require_flights_match_listing(
    listing: Rate, variant: Variant, summary: dict[str, object], raw: dict[str, object]
) -> None:
    flights = mapping(summary["transportDetails"])
    raw_segments = raw["segments"]
    if not isinstance(raw_segments, list):
        raise ValueError("Missing listing flight evidence")
    outbound, _, inbound = listing.segments
    require_leg_matches_segment(
        "outbound", outbound, variant.transport.outbound, mapping(raw_segments[0]), flights
    )
    require_leg_matches_segment(
        "return", inbound, variant.transport.inbound, mapping(raw_segments[2]), flights
    )


def require_leg_matches_segment(
    key: str,
    segment: Segment,
    journey: Journey,
    raw_segment: dict[str, object],
    flights: dict[str, object],
) -> None:
    """A leg must be one direct flight whose times, places and carrier agree everywhere."""
    flight = mapping(flights[key])
    start = FlightPoint.model_validate(flight["from"])
    end = FlightPoint.model_validate(flight["to"])
    detail = {"journey": journey.model_dump(), "from": start.model_dump(), "to": end.model_dump()}
    differences = Differences()

    def mismatch(failed: bool, condition: str) -> bool:
        return differences.found(failed, f"{key}_flight.{condition}", raw_segment, detail)

    if segment.beginTime is None or segment.endTime is None:
        mismatch(True, "listing_times_present")
        raise ValueError("Missing listing flight times; " + differences.first)
    departure = datetime.fromisoformat(segment.beginDate + "T" + segment.beginTime)
    arrival = datetime.fromisoformat(segment.endDate + "T" + segment.endTime)
    if (
        mismatch(journey.type != "flight", "type_is_flight")
        or mismatch(not journey.isDirect, "is_direct")
        or mismatch(departure.tzinfo is None, "departure_timezone_present")
        or mismatch(arrival.tzinfo is None, "arrival_timezone_present")
        or mismatch(arrival <= departure, "arrival_after_departure")
        or mismatch(journey.id != start.place.code, "journey_id_matches_departure_code")
        or mismatch(
            datetime.fromisoformat(journey.beginDateTime) != departure, "journey_departure_time"
        )
        or mismatch(
            datetime.fromisoformat(start.dateTime) != departure.replace(tzinfo=None),
            "departure_time",
        )
        or mismatch(
            datetime.fromisoformat(end.dateTime) != arrival.replace(tzinfo=None), "arrival_time"
        )
        or mismatch(segment.departure is None, "listing_departure_present")
        or mismatch(segment.destination is None, "listing_destination_present")
        or mismatch(
            normalized_text(start.place.title)
            != normalized_text(segment.departure.title if segment.departure else ""),
            "departure_title",
        )
        or mismatch(
            normalized_text(end.place.title)
            != normalized_text(segment.destination.title if segment.destination else ""),
            "destination_title",
        )
        or mismatch(
            start.carrierFlightNumber != end.carrierFlightNumber, "detail_flight_numbers_agree"
        )
        or mismatch(start.carrierCode != end.carrierCode, "detail_carrier_codes_agree")
        or mismatch(
            any(
                raw_segment[field] != start.carrierFlightNumber
                for field in ("flightNumber", "carrierFlightNumber")
                if raw_segment.get(field) is not None
            ),
            "listing_flight_number",
        )
        or mismatch(
            raw_segment.get("carrierCode") is not None
            and raw_segment["carrierCode"] != start.carrierCode,
            "listing_carrier_code",
        )
        or mismatch(
            start.place.title in AIRPORTS and AIRPORTS[start.place.title] != start.place.code,
            "departure_airport_mapping",
        )
        or mismatch(
            end.place.title in AIRPORTS and AIRPORTS[end.place.title] != end.place.code,
            "destination_airport_mapping",
        )
    ):
        raise ValueError("Conflicting or unsupported flights; " + differences.first)


def reconciled_booking_price(
    listing: Rate, variant: Variant, summary: dict[str, object]
) -> tuple[Decimal, dict[str, Decimal], Decimal]:
    """Return (package price, operator fees, booking total) once every source agrees."""
    price = variant.price
    currency = listing.currency
    group = listing.participantGroups[0]
    if len(price.participants) != 2 or any(p.type != "adult" for p in price.participants):
        raise ValueError("Expected two adult prices")
    amounts = [
        price.actualPrice,
        price.actualWithAdditionalPayments,
        *(p.price for p in price.participants),
    ]
    if any(m.currency != currency for m in amounts):
        raise ValueError("Inconsistent booking currencies")
    package_price = sum((p.price.amount for p in price.participants), Decimal(0))
    if (
        package_price <= 0
        or package_price != price.actualPrice.amount
        or package_price != Decimal(group.price) / 100
    ):
        raise ValueError("Conflicting package price")
    fees = fee_totals(price.additionalPayments, currency)
    participant_fees: dict[str, Decimal] = {}
    for adult, listed in zip(price.participants, group.participants, strict=True):
        if (
            listed.type != "adult"
            or adult.price.amount != Decimal(listed.price) / 100
            or listed.additionalPayments is None
        ):
            raise ValueError("Conflicting participant price")
        detail_fees = fee_totals(adult.additionalPayments, currency)
        listed_fees = fee_totals(
            [
                Fee(type=p.type, amount=Money(amount=Decimal(p.amount) / 100, currency=currency))
                for p in listed.additionalPayments
            ],
            currency,
        )
        if detail_fees != listed_fees:
            raise ValueError("Listing and detail fees disagree")
        for kind, amount in detail_fees.items():
            participant_fees[kind] = participant_fees.get(kind, Decimal(0)) + amount
    if participant_fees != fees:
        raise ValueError("Group and participant fees disagree")
    total = package_price + sum(fees.values(), Decimal(0))
    if total != price.actualWithAdditionalPayments.amount or summary.get("totalPrice") != total:
        raise ValueError("Booking total does not reconcile")
    return package_price, fees, total
