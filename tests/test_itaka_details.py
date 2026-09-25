"""Offline booking verification using minimized observations from public ITAKA HTML."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypedDict

from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import FilterConfig, ProviderConfig
from travel_deal_agent.filtering import matches
from travel_deal_agent.models import LocalMandatoryCost, Offer, duplicate_key
from travel_deal_agent.notification_content import NotificationMessage
from travel_deal_agent.pipeline import OfferPipeline
from travel_deal_agent.providers.boundary import mapping
from travel_deal_agent.providers.http import Response
from travel_deal_agent.providers.itaka import ItakaProvider
from travel_deal_agent.providers.itaka_data import normalize_rate
from travel_deal_agent.providers.itaka_details import confirm_detail, map_multiroom_fields
from travel_deal_agent.providers.itaka_rsc import FlightData
from travel_deal_agent.storage import Store


class Evidence(TypedDict):
    listing: dict[str, object]
    variant: dict[str, object]
    summary: dict[str, object]
    local_information: list[dict[str, object]]
    legacy_offer_id: str


def permissive_filters(settings: Settings) -> FilterConfig:
    """A filters override that makes the real captured fixtures' listing price
    (well above the real PLN 1500 business cap) eligible for a detail request.

    Used only by tests below that exercise `confirm_detail` mechanics (mismatch
    diagnostics, budget/failure behavior) through `ItakaProvider.fetch()`'s
    shortlist, which now skips a candidate ineligible under `settings.filters`
    (see `itaka.py`). Business price/rating thresholds are covered separately
    (tests/test_business_rules.py, tests/test_itaka.py's shortlist tests) --
    this override exists so those thresholds don't gate unrelated detail tests.
    """
    filters: FilterConfig = deepcopy(settings.filters)
    filters["max_price"] = "10000"
    filters["board_price_bands"] = [
        {"min_price": "0", "max_price": "10000", "min_board": "BB", "max_inclusive": True}
    ]
    filters["provider_ratings"]["itaka"] = {
        "enabled": True,
        "scale": {"min": 1, "max": 6},
        "min_rating": 1,
        "price_bands": [],
    }
    return filters


def evidence(code: str = "RMFTULR") -> Evidence:
    return TypeAdapter(Evidence).validate_json(
        (Path(__file__).parent / "fixtures" / "itaka" / f"{code}.json").read_text(encoding="utf-8")
    )


def detail_html(fixture: Evidence) -> str:
    variant = deepcopy(fixture["variant"])
    price = variant["price"]
    variant["price"] = "$a"
    local = deepcopy(fixture["local_information"])
    text_records = ""
    if local:
        text = str(local[0]["descriptionShort"])
        local[0]["descriptionShort"] = "$b"
        text_records = f"b:T{len(text.encode('utf-8')):x},{text}"
    stream = (
        text_records
        + ':HC["/",""]\n'
        + "".join(
            key + ":" + json.dumps(value, ensure_ascii=False) + "\n"
            for key, value in [
                ("1", variant),
                ("2", fixture["summary"]),
                ("3", local),
                ("a", price),
            ]
        )
    )
    # Network chunks may split a JSON record, a reference or UTF-8 text arbitrarily.
    middle = len(stream) // 2
    return "".join(
        "<script>self.__next_f.push(" + json.dumps([1, chunk]) + ")</script>"
        for chunk in [stream[:middle], stream[middle:]]
    )


def variants_html(objects: list[object]) -> str:
    stream = "".join(
        f"{index:x}:" + json.dumps(obj, ensure_ascii=True) + "\n"
        for index, obj in enumerate(objects, 1)
    )
    return "<script>self.__next_f.push(" + json.dumps([1, stream]) + ")</script>"


def mapping_evidence() -> dict[str, object]:
    return mapping(
        json.loads(
            (Path(__file__).parent / "fixtures" / "itaka" / "FUERIOC_mapping.json").read_text(
                encoding="utf-8"
            )
        )
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_captured_mapping_leaves_only_unavailable_rate_type_and_transport(reverse: bool) -> None:
    # Arrange
    fixture = mapping_evidence()
    raw = mapping(fixture["listing"])
    objects = fixture["objects"]
    assert isinstance(objects, list)
    if reverse:
        objects.reverse()
    # Act
    with pytest.raises(ValueError) as error:
        confirm_detail(normalize_rate(raw, []), raw, variants_html(objects))
    # Assert: mixed summary flights and the default B variant must not fill A's gaps.
    cause = error.value.__cause__
    assert isinstance(cause, ValidationError)
    assert {(item["loc"], item["type"]) for item in cause.errors()} == {
        (("rateType",), "missing"),
        (("transport",), "missing"),
    }


def test_captured_field_sources_map_exact_values_without_inventing_flights() -> None:
    fixture = mapping_evidence()
    objects = fixture["objects"]
    assert isinstance(objects, list)
    groups = mapping(fixture["listing"])["participantGroups"]
    assert isinstance(groups, list)
    expected = mapping(groups[0])["rateId"]
    assert isinstance(expected, str)
    selected: dict[str, object] = {}
    map_multiroom_fields(FlightData(variants_html(objects)), expected, selected)
    assert selected == {
        "supplierObjectId": "FUERIOC",
        "beginDate": "2026-10-05",
        "endDate": "2026-10-12",
        "duration": {"days": 8, "nights": 7},
        "room": {"id": "DBL", "title": "Pokój 2 os."},
        "meal": {"id": "H", "title": "2 posiłki"},
        "price": {"actualWithAdditionalPayments": {"amount": 8058, "currency": "PLN"}},
    }


def aliased_evidence(fixture: Evidence) -> list[object]:
    """Synthetic reshaping of complete offline evidence into observed multiroom aliases."""
    variant = deepcopy(fixture["variant"])
    expected = variant["id"]
    metadata = {"rateId": expected, "supplierObjectId": variant.pop("supplierObjectId")}
    group = {"id": expected, "mainRoom": variant.pop("room"), "mainMeal": variant.pop("meal")}
    parent = {key: variant.pop(key) for key in ("beginDate", "endDate", "duration")}
    parent["participantGroups"] = [group]
    price = mapping(variant["price"])
    parent["price"] = {"totalWithAdditionalPayments": price.pop("actualWithAdditionalPayments")}
    variant["price"] = price
    return [variant, metadata, parent, fixture["summary"]]


def test_alias_mapping_passes_unchanged_validation_when_evidence_is_complete() -> None:
    fixture = evidence("FUERIOC")
    listing = normalize_rate(fixture["listing"], [])
    html = variants_html(aliased_evidence(fixture))
    confirmed = confirm_detail(listing, fixture["listing"], html)
    assert confirmed.variant_verified and confirmed.price_is_complete
    assert confirmed.booking_total_price == Decimal("7058")
    assert confirmed.offer_id == confirmed.variant_identity == listing.offer_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("beginDate", "2026-09-18"),
        ("duration", {"days": 6, "nights": 4}),
        ("price", {"totalWithAdditionalPayments": {"amount": 1, "currency": "PLN"}}),
    ],
)
def test_conflicting_alias_context_fails_closed(field: str, value: object) -> None:
    fixture = evidence("FUERIOC")
    objects = aliased_evidence(fixture)
    conflicting = deepcopy(mapping(objects[2]))
    conflicting[field] = value
    objects.append(conflicting)
    with pytest.raises(ValueError, match="Conflicting detail evidence"):
        confirm_detail(
            normalize_rate(fixture["listing"], []), fixture["listing"], variants_html(objects)
        )


@pytest.mark.parametrize("mode", ["other_rate", "multiple_groups"])
def test_context_for_other_or_multiple_groups_cannot_supply_fields(mode: str) -> None:
    # Arrange
    fixture = evidence("FUERIOC")
    objects = aliased_evidence(fixture)
    parent = mapping(objects[2])
    groups = parent["participantGroups"]
    assert isinstance(groups, list)
    if mode == "other_rate":
        alter(groups, (0, "id"), "rate-B")
    else:
        groups.append({"id": "rate-B"})
    # Act / Assert
    with pytest.raises(ValueError, match="Incomplete or invalid detail variant"):
        confirm_detail(
            normalize_rate(fixture["listing"], []), fixture["listing"], variants_html(objects)
        )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "container", ["variant", "initialParticipantGroups", "current", "reference"]
)
def test_expected_variant_selected_independently_of_order(reverse: bool, container: str) -> None:
    # Arrange: B is a different booking, not evidence for A's price or flights.
    fixture = evidence("FUERIOC")
    listing = normalize_rate(fixture["listing"], [])
    other = deepcopy(fixture["variant"])
    other["id"] = "rate-B"
    other["supplierObjectId"] = "OTHER"
    other_summary = deepcopy(fixture["summary"])
    alter(other_summary, ("rooms", "offerIds"), ["rate-B"])
    alter(other_summary, ("totalPrice",), 99999)
    variant = fixture["variant"]
    selected: object = {
        "variant": {"variant": variant},
        "initialParticipantGroups": {"initialParticipantGroups": [variant]},
        "current": {"current": {"participantGroups": [variant]}},
        "reference": {"current": {"participantGroups": ["$a"]}},
    }[container]
    objects: list[object] = [selected, {"variant": other}, fixture["summary"], other_summary]
    if reverse:
        objects.reverse()
    html = variants_html(objects)
    if container == "reference":
        html += (
            "<script>self.__next_f.push("
            + json.dumps([1, "a:" + json.dumps(variant) + "\n"])
            + ")</script>"
        )
    confirmed = confirm_detail(listing, fixture["listing"], html)
    assert confirmed.variant_verified and confirmed.price_is_complete
    assert confirmed.booking_total_price == Decimal("7058")
    assert confirmed.offer_id == confirmed.variant_identity == listing.offer_id
    assert confirmed.url == listing.url


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_expected_variants_never_confirm(reverse: bool) -> None:
    fixture = evidence("FUERIOC")
    conflicting = deepcopy(fixture["variant"])
    conflicting["beginDate"] = "2026-09-18"
    objects: list[object] = [fixture["variant"], conflicting, fixture["summary"]]
    if reverse:
        objects.reverse()
    with pytest.raises(ValueError, match="Conflicting detail evidence for rate_id=.*beginDate"):
        confirm_detail(
            normalize_rate(fixture["listing"], []), fixture["listing"], variants_html(objects)
        )


def test_incomplete_expected_group_never_borrows_other_variant_fields() -> None:
    fixture = evidence("FUERIOC")
    partial = {
        key: fixture["variant"][key] for key in ("id", "saleStatus", "participants", "price")
    }
    other = deepcopy(fixture["variant"])
    other["id"] = "rate-B"
    html = variants_html([{"initialParticipantGroups": [partial]}, other, fixture["summary"]])
    with pytest.raises(ValueError, match="Incomplete or invalid detail variant for rate_id="):
        confirm_detail(normalize_rate(fixture["listing"], []), fixture["listing"], html)


def test_same_rate_fragments_and_duplicates_can_agree() -> None:
    fixture = evidence("FUERIOC")
    variant = fixture["variant"]
    initial = {key: variant[key] for key in ("id", "saleStatus", "participants", "price")}
    remaining = {key: value for key, value in variant.items() if key not in initial or key == "id"}
    html = variants_html(
        [
            {"initialParticipantGroups": [initial]},
            {"current": {"participantGroups": [remaining]}},
            deepcopy(initial),
            fixture["summary"],
        ]
    )
    result = confirm_detail(normalize_rate(fixture["listing"], []), fixture["listing"], html)
    assert result.price_is_complete
    assert result.booking_total_price == Decimal("7058")


@pytest.mark.parametrize("reverse", [False, True])
def test_captured_multiroom_selection_does_not_use_default_variant(reverse: bool) -> None:
    # Arrange: minimized September 20 evidence has A's groups but only B's full variant.
    fixture = mapping(
        json.loads(
            (Path(__file__).parent / "fixtures" / "itaka" / "FUERIOC_selection.json").read_text(
                encoding="utf-8"
            )
        )
    )
    listing = mapping(fixture["listing"])
    groups = listing["participantGroups"]
    assert isinstance(groups, list)
    expected = mapping(groups[0])["rateId"]
    objects: list[object] = [
        {"initialParticipantGroups": fixture["initialParticipantGroups"]},
        {"current": fixture["current"]},
        fixture["otherVariants"],
    ]
    if reverse:
        objects.reverse()
    # Act
    with pytest.raises(ValueError) as error:
        confirm_detail(normalize_rate(listing, []), listing, variants_html(objects))
    # Assert: A is selected, but missing evidence is never filled from B or the listing.
    assert str(error.value).startswith(
        f"Incomplete or invalid detail variant for rate_id={expected!r}:"
    )
    assert "supplierObjectId" in str(error.value)
    assert "transport" in str(error.value)


def alter(root: object, path: tuple[str | int, ...], value: object) -> None:
    current = root
    for key in path[:-1]:
        if isinstance(key, int):
            assert isinstance(current, list)
            current = current[key]
        else:
            assert isinstance(current, dict)
            current = current[key]
    last = path[-1]
    if isinstance(last, int):
        assert isinstance(current, list)
        current[last] = value
    else:
        assert isinstance(current, dict)
        current[last] = value


def affordable(fixture: Evidence, per_person: int = 1450) -> None:
    """Change only monetary observations, preserving the captured variant identity."""
    base = per_person - 30
    for index in range(2):
        alter(
            fixture, ("listing", "participantGroups", 0, "participants", index, "price"), base * 100
        )
        alter(fixture, ("variant", "price", "participants", index, "price", "amount"), base)
    alter(fixture, ("listing", "participantGroups", 0, "price"), base * 200)
    alter(fixture, ("variant", "price", "actualPrice", "amount"), base * 2)
    alter(fixture, ("variant", "price", "actualWithAdditionalPayments", "amount"), per_person * 2)
    alter(fixture, ("summary", "totalPrice"), per_person * 2)


@pytest.mark.parametrize(
    "code,total,cost_count",
    [
        ("FUERIOC", "7058", 0),
        ("RMFTULR", "5858", 1),
        ("SIDROYA", "9358", 2),
        ("CFUANGE", "3678", 1),
    ],
)
def test_captured_booking_prices_and_legacy_identity(
    code: str, total: str, cost_count: int
) -> None:
    # Arrange
    fixture = evidence(code)
    listing = normalize_rate(fixture["listing"], [])
    # Act
    confirmed = confirm_detail(listing, fixture["listing"], detail_html(fixture))
    # Assert
    assert confirmed.booking_total_price == confirmed.total_price == Decimal(total)
    assert confirmed.price_per_person == Decimal(total) / 2
    assert confirmed.price_is_complete and confirmed.variant_verified
    assert confirmed.sale_status == "available"
    assert confirmed.offer_id == confirmed.variant_identity == fixture["legacy_offer_id"]
    assert duplicate_key(confirmed) == duplicate_key(listing)
    assert len(confirmed.local_mandatory_costs) == cost_count
    assert all(cost.certainty == "approximate" for cost in confirmed.local_mandatory_costs)


@pytest.mark.parametrize(
    "path,value",
    [
        (("variant", "id"), "different"),
        (("variant", "supplierObjectId"), "other-hotel"),
        (("variant", "participants", "adults"), 1),
        (("variant", "participants", "children"), 1),
        (("variant", "beginDate"), "2026-09-24"),
        (("variant", "endDate"), "2026-10-03"),
        (("variant", "duration", "nights"), 8),
        (("variant", "duration", "days"), 9),
        (("variant", "room", "id"), "OTHER"),
        (("variant", "meal", "id"), "H"),
        (("variant", "transport", "outbound", "isDirect"), False),
        (("summary", "rooms", "offerIds"), ["other"]),
        (("summary", "roomCount"), 2),
        (("summary", "transportDetails", "return", "to", "dateTime"), "2026-10-03T18:00:00"),
        (("summary", "transportDetails", "outbound", "from", "place", "code"), "WAW"),
        (("summary", "transportDetails", "return", "to", "carrierFlightNumber"), "OTHER"),
        (("variant", "price", "actualPrice", "amount"), 5799),
        (("variant", "price", "actualWithAdditionalPayments", "amount"), 5859),
        (("summary", "totalPrice"), 5859),
        (("variant", "price", "actualPrice", "currency"), "EUR"),
        # Reverse direction: the LISTING's own currency disagrees with the
        # detail page's (still-PLN) amounts -- itaka_details.py's
        # `if any(m.currency != currency for m in amounts)` check, exercised
        # from the listing side rather than the detail side.
        (("listing", "currency"), "EUR"),
        (("variant", "price", "actualPrice", "amount"), True),
        (("variant", "price", "actualPrice", "amount"), "5798"),
        (("variant", "price", "additionalPayments", 0, "type"), "UNKNOWN"),
        (("variant", "price", "additionalPayments", 0, "type"), "TFP"),
        (("variant", "price", "additionalPayments", 0, "amount", "amount"), 31),
        (("variant", "price", "participants", 0, "additionalPayments"), []),
        (("variant", "price", "additionalPayments"), None),
        (("listing", "segments", 0, "flightNumber"), "CONFLICT"),
        (("listing", "segments", 2, "carrierCode"), "CONFLICT"),
        (("listing", "segments", 0, "beginTime"), None),
        (("summary", "transportDetails", "return", "to", "place", "code"), "WAW"),
    ],
)
def test_conflicting_evidence_never_confirms(path: tuple[str | int, ...], value: object) -> None:
    fixture = evidence()
    listing = normalize_rate(fixture["listing"], [])
    alter(fixture, path, value)
    with pytest.raises(ValueError):
        confirm_detail(listing, fixture["listing"], detail_html(fixture))


@pytest.mark.parametrize(
    "path,value,diagnostic",
    [
        (("supplierObjectId",), "OTHER", "hotel: listing='FUERIOC', detail='OTHER'"),
        (
            ("rateType",),
            "other",
            "rate_type: listing='holidays', detail='other'; rule=detail must be holidays",
        ),
        (("beginDate",), "2026-09-18", "dates.begin: listing='2026-09-17', detail='2026-09-18'"),
        (("endDate",), "2026-09-22", "dates.end: listing='2026-09-21', detail='2026-09-22'"),
        (("duration", "days"), 6, "duration.days: listing=5, detail=6"),
        (("duration", "nights"), 5, "duration.nights: listing=4, detail=5"),
        (("room", "id"), "OTHER", "room.id: listing='DBL', detail='OTHER'"),
        (
            ("room", "title"),
            "Other room",
            "room.title: listing='Pokój 2 os.', detail='Other room'; rule=normalized titles must match",
        ),
        (("meal", "id"), "A", "board.id: listing='H', detail='A'"),
        (
            ("meal", "title"),
            "Other board",
            "board.title: listing='2 posiłki', detail='Other board'; rule=normalized titles must match",
        ),
        (
            ("participants", "adults"),
            1,
            "participants: listing=['adult', 'adult'], detail={'adults': 1, 'children': 0, 'infants': 0}; rule=detail must contain 2 adults, 0 children, 0 infants",
        ),
        (
            ("participants", "children"),
            1,
            "participants: listing=['adult', 'adult'], detail={'adults': 2, 'children': 1, 'infants': 0}; rule=detail must contain 2 adults, 0 children, 0 infants",
        ),
        (
            ("participants", "infants"),
            1,
            "participants: listing=['adult', 'adult'], detail={'adults': 2, 'children': 0, 'infants': 1}; rule=detail must contain 2 adults, 0 children, 0 infants",
        ),
    ],
)
def test_variant_mismatch_reports_values(
    path: tuple[str | int, ...], value: object, diagnostic: str
) -> None:
    fixture = evidence("FUERIOC")
    listing = normalize_rate(fixture["listing"], [])
    alter(fixture["variant"], path, value)
    with pytest.raises(ValueError) as error:
        confirm_detail(listing, fixture["listing"], detail_html(fixture))
    assert str(error.value) == "Detail variant does not match listing; " + diagnostic


def test_variant_diagnostic_preserves_first_failure_order() -> None:
    fixture = evidence("FUERIOC")
    listing = normalize_rate(fixture["listing"], [])
    original_id = fixture["variant"]["id"]
    fixture["variant"]["id"] = "different"
    fixture["variant"]["supplierObjectId"] = "OTHER"
    with pytest.raises(ValueError) as error:
        confirm_detail(listing, fixture["listing"], detail_html(fixture))
    assert str(error.value) == (
        f"Detail variant does not match listing; rate_id: listing={original_id!r}, detail='different'"
    )


@pytest.mark.parametrize("field,label", [("room", "room"), ("meal", "board")])
@pytest.mark.parametrize("value", [None, ""])
def test_matching_empty_ids_still_rejected_with_diagnostic(
    field: str, label: str, value: str | None
) -> None:
    # Arrange
    fixture = evidence("FUERIOC")
    listing = normalize_rate(fixture["listing"], [])
    alter(fixture, ("listing", "segments", 1, "participantGroups", 0, field, "id"), value)
    alter(fixture, ("variant", field, "id"), value)
    # Act
    with pytest.raises(ValueError) as error:
        confirm_detail(listing, fixture["listing"], detail_html(fixture))
    # Assert
    assert str(error.value) == (
        f"Detail variant does not match listing; {label}.id: listing={value!r}, detail={value!r}"
        "; rule=detail ID must be nonempty"
    )


def test_listing_group_count_diagnostic() -> None:
    # Arrange
    fixture = evidence("FUERIOC")
    listing = normalize_rate(fixture["listing"], [])
    groups = fixture["listing"]["participantGroups"]
    assert isinstance(groups, list)
    groups.append(deepcopy(groups[0]))
    # Act
    with pytest.raises(ValueError) as error:
        confirm_detail(listing, fixture["listing"], detail_html(fixture))
    # Assert
    assert str(error.value) == (
        "Detail variant does not match listing; participant_groups: listing=2, "
        "detail={'adults': 2, 'children': 0, 'infants': 0}; "
        "rule=listing must contain exactly 1 participant group"
    )


@pytest.mark.parametrize("key", ["outbound", "return"])
def test_flight_diagnostic_identifies_direction_condition_and_values(key: str) -> None:
    # Arrange
    fixture = evidence("FUERIOC")
    listing = normalize_rate(fixture["listing"], [])
    alter(fixture, ("summary", "transportDetails", key, "to", "carrierFlightNumber"), "OTHER")
    # Act
    with pytest.raises(ValueError) as error:
        confirm_detail(listing, fixture["listing"], detail_html(fixture))
    # Assert
    message = str(error.value)
    assert message.startswith(
        f"Conflicting or unsupported flights; {key}_flight.detail_flight_numbers_agree: listing="
    )
    assert "detail={'journey':" in message
    assert "'carrierFlightNumber': 'OTHER'" in message
    assert (
        "'carrierFlightNumber': 'AMQ448'"
        if key == "outbound"
        else "'carrierFlightNumber': 'RR5006'"
    ) in message


def test_normalized_titles_still_confirm() -> None:
    fixture = evidence("FUERIOC")
    listing = normalize_rate(fixture["listing"], [])
    alter(fixture, ("variant", "room", "title"), "  POKÓJ  2 OS. ")
    alter(fixture, ("variant", "meal", "title"), "  2 POSIŁKI ")
    result = confirm_detail(listing, fixture["listing"], detail_html(fixture))
    assert result.price_is_complete
    assert result.offer_id == listing.offer_id == result.variant_identity
    assert result.booking_total_price == Decimal("7058")


@pytest.mark.parametrize("status", ["unavailable", "onRequest", "unknown"])
def test_nonavailable_variant_remains_diagnostic(status: str) -> None:
    fixture = evidence()
    fixture["variant"]["saleStatus"] = status
    result = confirm_detail(
        normalize_rate(fixture["listing"], []), fixture["listing"], detail_html(fixture)
    )
    assert not result.price_is_complete
    assert not result.variant_verified
    assert result.booking_total_price is None
    assert result.sale_status == status


@pytest.mark.parametrize(
    "costs",
    [
        [],
        [LocalMandatoryCost("Tax", "exact", Decimal("1000"), "EUR", "person/day")],
        [LocalMandatoryCost("Tax approximately EUR 2.5/day", "approximate")],
        [LocalMandatoryCost("Visa fee cannot be calculated", "unknown")],
    ],
)
def test_local_costs_do_not_change_eligibility_or_price(
    costs: list[LocalMandatoryCost], settings: Settings
) -> None:
    # Arrange
    fixture = evidence()
    affordable(fixture)
    confirmed = confirm_detail(
        normalize_rate(fixture["listing"], []), fixture["listing"], detail_html(fixture)
    )
    # Act
    offer = replace(confirmed, local_mandatory_costs=costs)
    # Assert
    assert offer.price_per_person == Decimal("1450")
    assert offer.price_is_complete
    assert matches(offer, settings.filters, today=date(2026, 9, 12))
    message = NotificationMessage(1, "new_offer", offer, None).render()
    if not costs:
        # The confirmed booking total still renders its own "🧾" line;
        # only a per-cost line must be absent when there are no local costs.
        assert message.count("🧾") == 1
    else:
        assert costs[0].description in message
        assert message.count("🧾") == 2


@pytest.mark.parametrize(
    "price,board,expected",
    [
        (999, "BB", True),
        (1000, "BB", False),
        (1000, "HB", True),
        (1500, "HB", True),
        (1501, "HB", False),
        (999, "RO", False),
    ],
)
def test_booking_price_drives_meal_and_budget_bands(
    price: int, board: str, expected: bool, settings: Settings
) -> None:
    settings.filters["allowed_boards"] = ["BB", "HB", "FB", "AI", "UAI"]
    fixture = evidence()
    affordable(fixture, price)
    code, title = {"BB": ("B", "Breakfast"), "HB": ("H", "2 posiłki"), "RO": ("R", "Room only")}[
        board
    ]
    meal = {"id": code, "title": title}
    alter(fixture, ("listing", "segments", 1, "participantGroups", 0, "meal"), meal)
    fixture["variant"]["meal"] = meal
    confirmed = confirm_detail(
        normalize_rate(fixture["listing"], []), fixture["listing"], detail_html(fixture)
    )
    assert confirmed.price_is_complete
    assert matches(confirmed, settings.filters, today=date(2026, 9, 12)) is expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("booking_total_price", Decimal("1")),
        ("price_per_person", Decimal("1")),
        ("total_price", Decimal("1")),
        ("variant_verified", False),
        ("sale_status", "unavailable"),
    ],
)
def test_confirmed_model_rejects_inconsistent_booking_fields(field: str, value: object) -> None:
    fixture = evidence()
    confirmed = confirm_detail(
        normalize_rate(fixture["listing"], []), fixture["listing"], detail_html(fixture)
    )
    raw = mapping(json.loads(confirmed.to_json()))
    raw[field] = str(value) if isinstance(value, Decimal) else value
    with pytest.raises(ValueError, match="booking price"):
        TypeAdapter(Offer).validate_json(json.dumps(raw))


def test_confirmation_preserves_history_and_alert_baseline(
    settings: Settings, store: Store
) -> None:
    # Arrange: serialize the historical format without any new fields.
    fixture = evidence()
    affordable(fixture)
    listing = normalize_rate(fixture["listing"], [])
    old = mapping(json.loads(listing.to_json()))
    for key in (
        "package_price",
        "booking_total_price",
        "operator_mandatory_fees",
        "local_mandatory_costs",
        "variant_verified",
        "sale_status",
        "price_verification_reason",
    ):
        old.pop(key)
    legacy = TypeAdapter(Offer).validate_json(json.dumps(old))
    store.observe(legacy, False)
    before = store.get_offer("itaka", legacy.offer_id)
    confirmed = confirm_detail(listing, fixture["listing"], detail_html(fixture))
    pipeline = OfferPipeline(settings, store, today=lambda: date(2026, 9, 12))
    # Act: first eligibility alerts once; repeated confirmation does not.
    pipeline.finalize(pipeline.filter_batch("itaka", [confirmed]))
    pipeline.finalize(pipeline.filter_batch("itaka", [confirmed]))
    after = store.get_offer("itaka", legacy.offer_id)
    # Assert
    assert before is not None and after is not None
    assert after.found_at == before.found_at
    assert store.price_history("itaka", legacy.offer_id) == [Decimal("1450")]
    assert len(store.pending()) == 1
    message = NotificationMessage.from_notification(store.pending()[0]).render()
    assert "🧾" in message
    assert "30 USD" in message
    assert "2900 zł" in message
    assert after.local_mandatory_costs == confirmed.local_mandatory_costs
    # A genuine cumulative booking-price drop keeps the same variant and baseline.
    affordable(fixture, 1350)
    cheaper = confirm_detail(
        normalize_rate(fixture["listing"], []), fixture["listing"], detail_html(fixture)
    )
    pipeline.finalize(pipeline.filter_batch("itaka", [cheaper]))
    # 1350 undercuts the only prior recorded price (1450), so it is this
    # offer's new historical low, not merely a drop from the last observation.
    assert [n["kind"] for n in store.pending()] == ["new_offer", "new_low"]
    assert store.pending()[1]["previous_price"] == "1450"


class FakeHTTP:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, timeout: float) -> Response:
        assert timeout > 0
        self.urls.append(url)
        return self.responses.pop(0)


def listing_html(fixture: Evidence) -> str:
    from urllib.parse import urlencode

    query = {
        "queryKey": ["rates", {"skip": 0, "take": 15}],
        "state": {
            "data": {"main": {"multiRoomRates": {"list": [fixture["listing"]], "ratesCount": 1}}}
        },
    }
    data = {"props": {"pageProps": {"initialQueryState": {"queries": [query]}}}}
    link = "/wczasy/test,RMFTULR/?" + urlencode(
        {"id[0]": str(fixture["variant"]["id"]), "adults[0]": 2}
    )
    return (
        '<script id="__NEXT_DATA__">'
        + json.dumps(data)
        + '</script><a href="'
        + link
        + '">Offer</a>'
    )


def test_provider_preserves_mismatch_diagnostic(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    fixture = evidence()
    html = listing_html(fixture)
    alter(fixture, ("variant", "supplierObjectId"), "OTHER")
    transport = FakeHTTP(
        [
            Response(200, "User-agent: *\nDisallow: /api", {}),
            Response(200, html, {}),
            Response(200, detail_html(fixture), {}),
        ]
    )
    provider = ItakaProvider(
        settings.providers["itaka"], permissive_filters(settings), transport, sleep=lambda _: None
    )
    # Act
    offers = provider.fetch()
    # Assert
    expected = "Detail variant does not match listing; hotel: listing='RMFTULR', detail='OTHER'"
    assert len(offers) == 1
    assert not offers[0].price_is_complete
    assert not offers[0].variant_verified
    assert offers[0].price_verification_reason == expected
    assert expected in caplog.text
    assert len(transport.urls) == 3


@pytest.mark.parametrize("mode", ["confirmed", "malformed", "budget", "disabled", "robots", "http"])
def test_provider_detail_budget_and_failure_behavior(mode: str, settings: Settings) -> None:
    # Arrange
    fixture = evidence()
    cfg: ProviderConfig = {**settings.providers["itaka"]}
    if mode == "budget":
        cfg["max_requests"] = 2
    if mode == "disabled":
        cfg["max_detail_requests"] = 0
    transport = FakeHTTP(
        [
            Response(
                200, "User-agent: *\nDisallow: " + ("/wczasy/" if mode == "robots" else "/api*"), {}
            ),
            Response(200, listing_html(fixture), {}),
            Response(
                429 if mode == "http" else 200,
                "<html>challenge</html>" if mode == "malformed" else detail_html(fixture),
                {},
            ),
        ]
    )
    provider = ItakaProvider(cfg, permissive_filters(settings), transport, sleep=lambda _: None)
    # Act / Assert
    if mode in {"robots", "http"}:
        with pytest.raises(ValueError):
            provider.fetch()
        assert len(transport.urls) == (2 if mode == "robots" else 3)
    else:
        result = provider.fetch()
        assert len(result) == 1
        assert result[0].price_is_complete is (mode == "confirmed")
        assert len(transport.urls) == (2 if mode in {"budget", "disabled"} else 3)
    assert all("/api" not in url and "/booking" not in url for url in transport.urls)


def test_flight_decoder_fails_closed() -> None:
    for payload in ("1:T10,short", "1:{}", "1:{}\n1:{}\n"):
        with pytest.raises(ValueError):
            FlightData("<script>self.__next_f.push(" + json.dumps([1, payload]) + ")</script>")
    data = FlightData('<script>self.__next_f.push([1,"1:\\"$1\\"\\n"])</script>')
    with pytest.raises(ValueError, match="cyclic"):
        data.resolve("$1")
    with pytest.raises(ValueError, match="Missing"):
        data.resolve("$ffff")


def test_flight_component_property_reference() -> None:
    # Arrange: retain the observed Flight element / props path convention.
    payload = '1:["$","div",null,{"children":["$","component",null,{"value":7058}]}]\n'
    data = FlightData("<script>self.__next_f.push(" + json.dumps([1, payload]) + ")</script>")
    assert data.resolve("$1:props:children:props:value") == 7058


@pytest.mark.parametrize("kind", ["duplicate", "count_changed"])
def test_pagination_rejects_duplicate_and_changing_inventory(kind: str, settings: Settings) -> None:
    # Arrange
    fixture = evidence()
    initial = (
        listing_html(fixture)
        .replace('"ratesCount": 1', '"ratesCount": 2')
        .replace('"take": 15', '"take": 1')
    )
    if kind == "duplicate":
        initial = initial.replace(
            json.dumps([fixture["listing"]]), json.dumps([fixture["listing"], fixture["listing"]])
        ).replace('"take": 1', '"take": 2')
    second = (
        listing_html(fixture)
        .replace('"skip": 0', '"skip": 1')
        .replace('"take": 15', '"take": 1')
        .replace('"ratesCount": 1', '"ratesCount": 3')
    )
    cfg: ProviderConfig = {**settings.providers["itaka"], "max_detail_requests": 0}
    transport = FakeHTTP(
        [
            Response(200, "User-agent: *\nDisallow: /api", {}),
            Response(200, initial, {}),
            Response(200, second, {}),
        ]
    )
    # Act / Assert
    with pytest.raises(ValueError, match="repeated|count changed"):
        ItakaProvider(cfg, settings.filters, transport, sleep=lambda _: None).fetch()
