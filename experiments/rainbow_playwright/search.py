"""Offline orchestration contract; a verified Rainbow browser adapter is still required."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class ListingState:
    url: str
    revision: str
    ready: bool
    price_ascending_selected: bool


class SearchPage(Protocol):
    def state(self) -> ListingState:
        """Read readiness metadata, never extract offer cards here.

        Revision must identify a completed listing update, including empty results.
        Ready must be false during a pending/debounced update, not just while loading.
        """
        ...

    def wait_until(self, condition: Callable[[], bool], timeout_ms: int) -> None:
        """Poll a condition with a bounded browser wait; raise on timeout, no fixed sleep."""
        ...


def matches_query(url: str, expected: Mapping[str, tuple[str, ...]]) -> bool:
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    return all(sorted(query.get(key, [])) == sorted(values) for key, values in expected.items())


def wait_for_verified_listing(
    page: SearchPage,
    expected: Mapping[str, tuple[str, ...]],
    *,
    previous_revision: str | None = None,
    timeout_ms: int = 30_000,
) -> None:
    """Require URL, UI sorting and completed listing evidence to agree."""
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    required = dict(expected)
    required["sortowanie"] = ("cena-asc",)

    def complete() -> bool:
        current = page.state()
        return (
            current.ready
            and bool(current.revision)
            and (previous_revision is None or current.revision != previous_revision)
            and current.price_ascending_selected
            and matches_query(current.url, required)
        )

    page.wait_until(complete, timeout_ms)


def change_filter(
    page: SearchPage,
    change: Callable[[], None],
    expected_before: Mapping[str, tuple[str, ...]],
    expected_after: Mapping[str, tuple[str, ...]],
    *,
    timeout_ms: int = 30_000,
) -> None:
    """Apply exactly one real change, then block until its listing update completes.

    Expectations include all previously selected filters. Already-selected controls
    must use wait_for_verified_listing directly, because they may trigger no update.
    No submit button or sorting interaction is part of this flow.
    """
    wait_for_verified_listing(page, expected_before, timeout_ms=timeout_ms)
    previous_revision = page.state().revision
    change()
    wait_for_verified_listing(
        page, expected_after, previous_revision=previous_revision, timeout_ms=timeout_ms
    )
