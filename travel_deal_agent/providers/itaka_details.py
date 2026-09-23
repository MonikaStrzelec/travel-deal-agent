"""Confirm a listing variant and its operator booking price from public detail HTML."""

import re
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from html import unescape

from pydantic import Field, field_validator

from ..boards import normalized_text
from ..models import LocalMandatoryCost, Offer, OperatorFee
from .itaka_data import AIRPORTS, Boundary, Named, Rate, mapping
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


def unique_object(data: FlightData, candidates: list[dict[str, object]]) -> dict[str, object]:
    resolved = [mapping(data.resolve(candidate)) for candidate in candidates]
    if not resolved or any(item != resolved[0] for item in resolved[1:]):
        raise ValueError("Missing or conflicting detail evidence")
    return resolved[0]


def map_multiroom_fields(data: FlightData, expected_id: str, selected: dict[str, object]) -> None:
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

    for obj in data.objects():
        if obj.get("rateId") == expected_id and "supplierObjectId" in obj:
            put(selected, "supplierObjectId", data.resolve(obj["supplierObjectId"]))
        if obj.get("id") == expected_id:
            for source, field in (("mainRoom", "room"), ("mainMeal", "meal")):
                if source not in obj:
                    continue
                named = mapping(data.resolve(obj[source]))
                if "roomId" in named and "id" in named and named["roomId"] != named["id"]:
                    raise ValueError(f"Conflicting room aliases for rate_id={expected_id!r}")
                value = {"id": named.get("id", named.get("roomId")), "title": named.get("title")}
                put(selected, field, value)
        groups = obj.get("participantGroups")
        if not isinstance(groups, list) or len(groups) != 1:
            continue
        group = mapping(data.resolve(groups[0]))
        if group.get("id") != expected_id:
            continue
        for field in ("beginDate", "endDate", "duration"):
            if field in obj:
                put(selected, field, data.resolve(obj[field]))
        totals: list[object] = []
        if "priceWithAdditionalPayments" in obj:
            totals.append(data.resolve(obj["priceWithAdditionalPayments"]))
        if "price" in obj:
            price = mapping(data.resolve(obj["price"]))
            if "totalWithAdditionalPayments" in price:
                totals.append(price["totalWithAdditionalPayments"])
        for total in totals:
            price = mapping(selected.get("price", {}))
            put(price, "actualWithAdditionalPayments", total)
            selected["price"] = price


def select_variant(data: FlightData, expected_id: str) -> Variant:
    """Select only evidence for the requested rate, including participant groups.

    Same-rate fragments may supply missing fields, never conflicting values.
    Incomplete evidence still fails the existing Variant boundary validation.
    """
    selected: dict[str, object] = {}
    found: set[str] = set()
    for obj in data.objects():
        if not any(key in obj for key in ("saleStatus", "transport", "price", "participants")):
            continue
        identity = data.resolve(obj.get("id", obj.get("rateId")))
        if not isinstance(identity, str):
            continue
        found.add(identity)
        if identity != expected_id:
            continue
        candidate = mapping(data.resolve(obj))
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
    map_multiroom_fields(data, expected_id, selected)
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


def local_costs(data: FlightData, url: str | None) -> list[LocalMandatoryCost]:
    """Preserve source descriptions and conditions; never infer zero or convert currencies."""
    result: list[LocalMandatoryCost] = []
    seen: set[str] = set()
    for obj in data.objects():
        if "descriptionShort" not in obj or "title" not in obj:
            continue
        title = obj["title"]
        if not isinstance(title, str):
            continue
        try:
            short = data.resolve(obj["descriptionShort"])
            long = data.resolve(obj.get("descriptionLong"))
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


def confirm_detail(offer: Offer, raw: dict[str, object], html: str) -> Offer:
    """Keep the exact legacy identity; enrich only after all booking checks pass."""
    listing = Rate.model_validate(raw)
    data = FlightData(html)
    objects = data.objects()
    expected_id = listing.participantGroups[0].rateId
    variant = select_variant(data, expected_id)
    costs = local_costs(data, offer.url)
    party = variant.participants
    outbound, hotel, inbound = listing.segments
    room = hotel.participantGroups[0]
    differences: list[str] = []

    def mismatch(failed: bool, field: str, left: object, right: object, rule: str = "") -> bool:
        if failed:
            differences.append(
                f"{field}: listing={left!r}, detail={right!r}" + (f"; rule={rule}" if rule else "")
            )
        return failed

    # Preserve short-circuit order and report the first failed condition.
    if (
        mismatch(variant.id != expected_id, "rate_id", expected_id, variant.id)
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
            variant.beginDate != hotel.beginDate,
            "dates.begin",
            hotel.beginDate,
            variant.beginDate,
        )
        or mismatch(
            variant.endDate != hotel.endDate,
            "dates.end",
            hotel.endDate,
            variant.endDate,
        )
        or mismatch(
            variant.duration.days != listing.duration.days,
            "duration.days",
            listing.duration.days,
            variant.duration.days,
        )
        or mismatch(
            variant.duration.nights
            != (date.fromisoformat(hotel.endDate) - date.fromisoformat(hotel.beginDate)).days,
            "duration.nights",
            (date.fromisoformat(hotel.endDate) - date.fromisoformat(hotel.beginDate)).days,
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
        raise ValueError("Detail variant does not match listing; " + differences[0])
    if variant.saleStatus != "available":
        return replace(
            offer,
            sale_status=variant.saleStatus,
            local_mandatory_costs=costs,
            price_verification_reason="Variant is not available",
        )

    summaries = [obj for obj in objects if "transportDetails" in obj and "rooms" in obj]
    matching_summaries = [
        obj
        for obj in summaries
        if mapping(data.resolve(obj["rooms"])).get("offerIds") == [expected_id]
    ]
    summary = unique_object(data, matching_summaries)
    rooms = mapping(summary["rooms"])
    if (
        mismatch(
            rooms.get("offerIds") != [expected_id],
            "room.offer_ids",
            [expected_id],
            rooms.get("offerIds"),
        )
        or mismatch(
            rooms.get("ids") != [room.room.id], "room.ids", [room.room.id], rooms.get("ids")
        )
        or mismatch(
            summary.get("roomCount") != 1,
            "room.count",
            len(hotel.participantGroups),
            summary.get("roomCount"),
            "detail room count must be 1",
        )
    ):
        raise ValueError("Conflicting room or offer identity; " + differences[0])
    flights = mapping(summary["transportDetails"])
    raw_segments = raw["segments"]
    if not isinstance(raw_segments, list):
        raise ValueError("Missing listing flight evidence")
    for segment, journey, key, raw_segment in (
        (outbound, variant.transport.outbound, "outbound", mapping(raw_segments[0])),
        (inbound, variant.transport.inbound, "return", mapping(raw_segments[2])),
    ):
        flight = mapping(flights[key])
        start = FlightPoint.model_validate(flight["from"])
        end = FlightPoint.model_validate(flight["to"])

        def flight_mismatch(
            failed: bool,
            condition: str,
            *,
            key: str = key,
            raw_segment: dict[str, object] = raw_segment,
            journey: Journey = journey,
            start: FlightPoint = start,
            end: FlightPoint = end,
        ) -> bool:
            if not failed:
                return False
            return mismatch(
                True,
                f"{key}_flight.{condition}",
                raw_segment,
                {
                    "journey": journey.model_dump(),
                    "from": start.model_dump(),
                    "to": end.model_dump(),
                },
            )

        if segment.beginTime is None or segment.endTime is None:
            flight_mismatch(True, "listing_times_present")
            raise ValueError("Missing listing flight times; " + differences[0])
        departure = datetime.fromisoformat(segment.beginDate + "T" + segment.beginTime)
        arrival = datetime.fromisoformat(segment.endDate + "T" + segment.endTime)
        if (
            flight_mismatch(journey.type != "flight", "type_is_flight")
            or flight_mismatch(not journey.isDirect, "is_direct")
            or flight_mismatch(departure.tzinfo is None, "departure_timezone_present")
            or flight_mismatch(arrival.tzinfo is None, "arrival_timezone_present")
            or flight_mismatch(arrival <= departure, "arrival_after_departure")
            or flight_mismatch(journey.id != start.place.code, "journey_id_matches_departure_code")
            or flight_mismatch(
                datetime.fromisoformat(journey.beginDateTime) != departure, "journey_departure_time"
            )
            or flight_mismatch(
                datetime.fromisoformat(start.dateTime) != departure.replace(tzinfo=None),
                "departure_time",
            )
            or flight_mismatch(
                datetime.fromisoformat(end.dateTime) != arrival.replace(tzinfo=None), "arrival_time"
            )
            or flight_mismatch(segment.departure is None, "listing_departure_present")
            or flight_mismatch(segment.destination is None, "listing_destination_present")
            or flight_mismatch(
                normalized_text(start.place.title)
                != normalized_text(segment.departure.title if segment.departure else ""),
                "departure_title",
            )
            or flight_mismatch(
                normalized_text(end.place.title)
                != normalized_text(segment.destination.title if segment.destination else ""),
                "destination_title",
            )
            or flight_mismatch(
                start.carrierFlightNumber != end.carrierFlightNumber, "detail_flight_numbers_agree"
            )
            or flight_mismatch(start.carrierCode != end.carrierCode, "detail_carrier_codes_agree")
            or flight_mismatch(
                any(
                    raw_segment[field] != start.carrierFlightNumber
                    for field in ("flightNumber", "carrierFlightNumber")
                    if raw_segment.get(field) is not None
                ),
                "listing_flight_number",
            )
            or flight_mismatch(
                raw_segment.get("carrierCode") is not None
                and raw_segment["carrierCode"] != start.carrierCode,
                "listing_carrier_code",
            )
            or flight_mismatch(
                start.place.title in AIRPORTS and AIRPORTS[start.place.title] != start.place.code,
                "departure_airport_mapping",
            )
            or flight_mismatch(
                end.place.title in AIRPORTS and AIRPORTS[end.place.title] != end.place.code,
                "destination_airport_mapping",
            )
        ):
            raise ValueError("Conflicting or unsupported flights; " + differences[0])

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
    base = sum((p.price.amount for p in price.participants), Decimal(0))
    if base <= 0 or base != price.actualPrice.amount or base != Decimal(group.price) / 100:
        raise ValueError("Conflicting package price")
    fees = fee_totals(price.additionalPayments, currency)
    participant_fees: dict[str, Decimal] = {}
    for adult, original in zip(price.participants, group.participants, strict=True):
        if (
            original.type != "adult"
            or adult.price.amount != Decimal(original.price) / 100
            or original.additionalPayments is None
        ):
            raise ValueError("Conflicting participant price")
        current = fee_totals(adult.additionalPayments, currency)
        original_fees = fee_totals(
            [
                Fee(type=p.type, amount=Money(amount=Decimal(p.amount) / 100, currency=currency))
                for p in original.additionalPayments
            ],
            currency,
        )
        if current != original_fees:
            raise ValueError("Listing and detail fees disagree")
        for kind, amount in current.items():
            participant_fees[kind] = participant_fees.get(kind, Decimal(0)) + amount
    if participant_fees != fees:
        raise ValueError("Group and participant fees disagree")
    total = base + sum(fees.values(), Decimal(0))
    if total != price.actualWithAdditionalPayments.amount or summary.get("totalPrice") != total:
        raise ValueError("Booking total does not reconcile")
    return replace(
        offer,
        package_price=base,
        operator_mandatory_fees=[
            OperatorFee(kind, amount, currency) for kind, amount in sorted(fees.items())
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
