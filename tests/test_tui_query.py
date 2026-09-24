"""Offline tests for the deterministic TUI search-URL builder; no network access."""

import copy

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import FilterConfig
from travel_deal_agent.providers.tui_query import PATH, build_search_path


def filters(settings: Settings, **overrides: object) -> FilterConfig:
    result = copy.deepcopy(settings.filters)
    result.update(overrides)  # type: ignore[typeddict-item]
    return result


def test_path_and_static_protocol_tokens(settings: Settings) -> None:
    # Act
    url = build_search_path(settings.filters)
    # Assert
    assert url.startswith(PATH + "?q=")
    assert url.endswith("&fullPrice=false")
    q = url.split("?q=", 1)[1].split("&fullPrice=false", 1)[0]
    assert q.startswith("%3Aprice%3AbyPlane%3AT%3A")
    assert "ctAdult%3A2" in q
    assert "ctChild%3A0" in q
    assert "tripType%3AWS" in q


def test_confirmed_airports_are_included(settings: Settings) -> None:
    # Act
    url = build_search_path(settings.filters)
    # Assert
    for code in ("LCJ", "KTW", "WAW", "WRO"):
        assert f"a%3A{code}" in url


def test_wmi_is_silently_excluded_even_though_configured(settings: Settings) -> None:
    # Arrange: production filters include WMI in the shared airport list.
    assert "WMI" in settings.filters["airports"]
    # Act
    url = build_search_path(settings.filters)
    # Assert: TUI itself has WMI disabled (reconnaissance); it must never be sent.
    assert "WMI" not in url


def test_rdo_never_appears_by_default(settings: Settings) -> None:
    # Arrange: RDO was clicked in the Codegen recording but is not a business airport.
    assert "RDO" not in settings.filters["airports"]
    # Act
    url = build_search_path(settings.filters)
    # Assert
    assert "RDO" not in url


def test_unsupported_airport_code_is_rejected(settings: Settings) -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="Unsupported TUI airport"):
        build_search_path(filters(settings, airports=["ZZZ"]))


def test_all_airports_disabled_is_rejected(settings: Settings) -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="No TUI-enabled airport"):
        build_search_path(filters(settings, airports=["WMI"]))


def test_party_is_two_adults_zero_children(settings: Settings) -> None:
    # Act
    url = build_search_path(settings.filters)
    # Assert
    assert "ctAdult%3A2" in url
    assert "ctChild%3A0" in url


def test_unsupported_party_size_is_rejected(settings: Settings) -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="two adults"):
        build_search_path(filters(settings, people=3))


def test_price_ceiling_is_per_person_times_party_size(settings: Settings) -> None:
    # Arrange: business requirement is 1500 PLN/person for 2 adults, i.e. 3000 total.
    # The amountRange facet is TUI's own "total party price" filter (reconnaissance),
    # so it must never receive the per-person figure unchanged.
    assert settings.filters["max_price"] == "1500" and settings.filters["people"] == 2
    # Act
    url = build_search_path(settings.filters)
    # Assert
    assert "amountRange%3A%233000" in url
    assert "%231500" not in url


def test_price_ceiling_scales_with_configured_cap_not_hardcoded(settings: Settings) -> None:
    # Act
    url = build_search_path(filters(settings, max_price="1000"))
    # Assert: 1000/person * 2 adults = 2000, not a hardcoded 3000.
    assert "amountRange%3A%232000" in url
    assert "%233000" not in url


def test_board_facets_cover_ai_fb_hb(settings: Settings) -> None:
    # Arrange
    assert set(settings.filters["allowed_boards"]) == {"HB", "FB", "AI"}
    # Act
    url = build_search_path(settings.filters)
    # Assert: each confirmed facet group is present.
    assert "GT06-AI%20GT06-XX%20GT06-AIP" in url
    assert "GT06-FB%20GT06-FBP" in url
    assert "GT06-HB%20GT06-HBP" in url


def test_unconfirmed_board_facet_is_rejected(settings: Settings) -> None:
    # Act / Assert: none of the allowed boards have a confirmed TUI facet.
    with pytest.raises(ValueError, match="No confirmed TUI facet"):
        build_search_path(filters(settings, allowed_boards=["UAI"]))


def test_zo_is_silently_excluded_from_the_tui_query(settings: Settings) -> None:
    # Arrange: ZO is a Wakacje.pl-only board (boards.py) with no TUI facet; TUI
    # simply never returns it, so it must not block building the query for the
    # boards TUI does support -- unlike test_unconfirmed_board_facet_is_rejected,
    # where NO allowed board has a facet.
    # Act
    url = build_search_path(filters(settings, allowed_boards=["ZO", "HB", "FB", "AI"]))
    # Assert: the confirmed facets are still present; nothing ZO-shaped is added.
    assert "GT06-AI%20GT06-XX%20GT06-AIP" in url
    assert "GT06-FB%20GT06-FBP" in url
    assert "GT06-HB%20GT06-HBP" in url


@pytest.mark.parametrize("stars,code", [(3, "3s"), (4, "4s"), (5, "5s")])
def test_minimum_stars_maps_to_the_confirmed_threshold_code(
    settings: Settings, stars: int, code: str
) -> None:
    # Act
    url = build_search_path(filters(settings, min_stars=stars))
    # Assert
    assert f"minHotelCategory%3A{code}" in url


def test_unsupported_star_threshold_is_rejected(settings: Settings) -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="minHotelCategory"):
        build_search_path(filters(settings, min_stars=3.5))


def test_sort_is_ascending_by_price(settings: Settings) -> None:
    # Act
    url = build_search_path(settings.filters)
    # Assert: "price" is the one confirmed ascending-price sort token, placed first.
    q = url.split("?q=", 1)[1]
    assert q.startswith("%3Aprice%3A")


def test_duration_uses_the_one_confirmed_encoding_regardless_of_configured_range(
    settings: Settings,
) -> None:
    # Arrange: production business range at time of writing, in nights.
    assert (settings.filters["min_nights"], settings.filters["max_nights"]) == (6, 8)
    # Act
    url = build_search_path(settings.filters)
    # Assert: dF:6:dT:14 is TUI's own unmodified default and a strict superset of 6-8.
    assert "dF%3A6" in url and "dT%3A14" in url


def test_duration_with_no_configured_bounds_still_uses_the_confirmed_default(
    settings: Settings,
) -> None:
    # Arrange: the current production business decision -- no duration limit at all.
    # Act
    url = build_search_path(filters(settings, min_nights=None, max_nights=None))
    # Assert
    assert "dF%3A6" in url and "dT%3A14" in url


def test_duration_outside_the_confirmed_envelope_is_rejected(settings: Settings) -> None:
    # Act / Assert: neither bound narrower nor wider than [6, 14] can be honored.
    with pytest.raises(ValueError, match="not covered"):
        build_search_path(filters(settings, min_nights=3, max_nights=9))
    with pytest.raises(ValueError, match="not covered"):
        build_search_path(filters(settings, min_nights=7, max_nights=20))


def test_no_tripadvisor_rating_filter_by_default(settings: Settings) -> None:
    # Arrange: MVP decision -- no TUI hard rating filter; use rating/review
    # data in ranking. config.py defaults provider_ratings["tui"] to a
    # disabled rule so an unconfigured provider isn't silently rejected
    # outright, but that default must still never turn into an enabled filter.
    assert settings.filters["provider_ratings"]["tui"]["enabled"] is False
    # Act
    url = build_search_path(settings.filters)
    # Assert: the recording's tripAdvisorRating:4t must not become a silent hard filter.
    assert "tripAdvisorRating" not in url


def test_tripadvisor_rating_is_honored_only_when_explicitly_configured(
    settings: Settings,
) -> None:
    # Arrange
    rule = {
        "enabled": True,
        "scale": {"min": 1.0, "max": 5.0},
        "price_bands": [{"min_price": "0", "max_price": "1500", "min_rating": 4.0}],
    }
    ratings = {**settings.filters["provider_ratings"], "tui": rule}
    # Act
    url = build_search_path(filters(settings, provider_ratings=ratings))
    # Assert
    assert "tripAdvisorRating%3A4t" in url


def test_disabled_tripadvisor_rule_adds_no_filter(settings: Settings) -> None:
    # Arrange
    rule = {
        "enabled": False,
        "scale": {"min": 1.0, "max": 5.0},
        "price_bands": [{"min_price": "0", "max_price": "1500", "min_rating": 4.0}],
    }
    ratings = {**settings.filters["provider_ratings"], "tui": rule}
    # Act
    url = build_search_path(filters(settings, provider_ratings=ratings))
    # Assert
    assert "tripAdvisorRating" not in url


def test_unconfirmed_rating_floor_is_rejected(settings: Settings) -> None:
    # Arrange: 2.0 is below every confirmed TripAdvisor threshold code (3/3.5/4/4.5).
    rule = {
        "enabled": True,
        "scale": {"min": 1.0, "max": 5.0},
        "price_bands": [{"min_price": "0", "max_price": "1500", "min_rating": 2.0}],
    }
    ratings = {**settings.filters["provider_ratings"], "tui": rule}
    # Act / Assert
    with pytest.raises(ValueError, match="TripAdvisor threshold"):
        build_search_path(filters(settings, provider_ratings=ratings))


def test_non_pln_currency_is_rejected(settings: Settings) -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="PLN"):
        build_search_path(filters(settings, currency="EUR"))


def test_builder_is_pure_and_deterministic(settings: Settings) -> None:
    # Act / Assert
    assert build_search_path(settings.filters) == build_search_path(settings.filters)


# --- page parameter: infrastructure only, PAGINATION_LIVE_CONFIRMATION_NEEDED -----


def test_default_page_omits_the_page_parameter(settings: Settings) -> None:
    # Arrange / Act
    url = build_search_path(settings.filters)
    # Assert: page=1 is the only confirmed, always-used value; no suffix by default.
    assert "page=" not in url


def test_explicit_page_appends_the_confirmed_parameter_name(settings: Settings) -> None:
    # Act
    url = build_search_path(settings.filters, page=2)
    # Assert: this only builds the URL; whether TUI's search/offers response
    # actually reflects a second page through it is not confirmed (see the
    # function's docstring and CURRENT_STATE.md).
    assert url.endswith("&page=2")
    assert build_search_path(settings.filters, page=1) == build_search_path(settings.filters)


def test_nonpositive_page_is_rejected(settings: Settings) -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="page"):
        build_search_path(settings.filters, page=0)
