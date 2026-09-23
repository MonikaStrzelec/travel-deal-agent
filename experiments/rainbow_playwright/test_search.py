"""Offline checks for automatic filter updates; no browser or network access."""

from collections.abc import Callable

import pytest

from experiments.rainbow_playwright.search import (
    ListingState,
    change_filter,
    matches_query,
    wait_for_verified_listing,
)

OLD_URL = "https://r.pl/szukaj?sortowanie=cena-asc"
NEW_URL = OLD_URL + "&cena.do=1500"


class FakePage:
    def __init__(self, states: list[ListingState]) -> None:
        self.states = iter(states)
        self.current = ListingState(OLD_URL, "old", True, True)
        self.events: list[str] = []

    def state(self) -> ListingState:
        return self.current

    def wait_until(self, condition: Callable[[], bool], timeout_ms: int) -> None:
        self.events.append("wait")
        if condition():
            return
        for state in self.states:
            self.current = state
            if condition():
                return
        raise TimeoutError("Listing did not finish updating")

    def change(self) -> None:
        self.events.append("change")


def test_filter_waits_past_url_change_and_loading_before_returning() -> None:
    # Arrange: URL changes before the listing, followed by an incomplete render.
    page = FakePage(
        [
            ListingState(NEW_URL, "old", True, True),
            ListingState(NEW_URL, "new", False, True),
            ListingState(NEW_URL, "new", True, True),
        ]
    )

    # Act.
    change_filter(page, page.change, {}, {"cena.do": ("1500",)})

    # Assert: caller can only proceed after the completed revision.
    assert page.current.revision == "new"
    assert page.current.ready
    assert page.events == ["wait", "change", "wait"]


@pytest.mark.parametrize(
    "state",
    [
        ListingState(NEW_URL, "old", True, True),
        ListingState(NEW_URL, "new", False, True),
        ListingState(NEW_URL, "new", True, False),
        ListingState(NEW_URL.replace("cena-asc", "cena-desc"), "new", True, True),
        ListingState(OLD_URL, "new", True, True),
    ],
)
def test_incomplete_or_conflicting_update_stops_flow(state: ListingState) -> None:
    # Arrange.
    page = FakePage([state])

    # Act / assert.
    with pytest.raises(TimeoutError):
        change_filter(page, page.change, {}, {"cena.do": ("1500",)})


def test_verified_default_sort_and_unchanged_filter_require_no_interaction() -> None:
    # Arrange.
    page = FakePage([])

    # Act.
    wait_for_verified_listing(page, {})

    # Assert.
    assert page.events == ["wait"]


def test_query_checks_all_values_including_previously_selected_airports() -> None:
    # Arrange.
    expected = {"wybraneSkad": ("LCJ", "WAW")}

    # Act / assert.
    assert matches_query(OLD_URL + "&wybraneSkad=WAW&wybraneSkad=LCJ", expected)
    assert not matches_query(OLD_URL + "&wybraneSkad=LCJ", expected)
    assert not matches_query(OLD_URL + "&wybraneSkad=LCJ&wybraneSkad=KTW", expected)
