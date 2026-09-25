"""Direct, deterministic coverage for providers/http.py::RequestBudget.

RequestBudget is shared by every HTTP-based provider (ITAKA, Wakacje.pl) but
was previously exercised only indirectly through those providers' own tests
(e.g. test_itaka.py::test_request_spacing_and_deadline). These tests target
RequestBudget itself, with a fake clock/sleep/transport and no real network
access, so its request-limit, gap and deadline contract is pinned
independently of any single provider's usage of it.
"""

from collections.abc import Callable

import pytest

from travel_deal_agent.providers.http import RequestBudget, Response, Transport


class FakeClock:
    """Advances only when told to (via sleep or an explicit transport cost)."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RecordingTransport:
    """Returns queued responses; optionally advances the fake clock per call,
    simulating a request that itself takes non-zero time."""

    def __init__(
        self,
        responses: list[Response],
        clock: FakeClock,
        cost: float = 0.0,
    ) -> None:
        self.responses = responses
        self.clock = clock
        self.cost = cost
        self.calls: list[tuple[str, float]] = []

    def get(self, url: str, timeout: float) -> Response:
        self.calls.append((url, timeout))
        self.clock.now += self.cost
        return self.responses.pop(0)


class RaisingTransport:
    def __init__(self, exception: Exception) -> None:
        self.exception = exception
        self.calls = 0

    def get(self, url: str, timeout: float) -> Response:
        self.calls += 1
        raise self.exception


def ok(text: str = "") -> Response:
    return Response(200, text, {})


def recording_sleep(log: list[float], clock: FakeClock) -> Callable[[float], None]:
    def sleep(seconds: float) -> None:
        log.append(seconds)
        clock.now += seconds

    return sleep


def test_max_requests_limit_raises_without_calling_transport_again() -> None:
    # Arrange: budget for exactly one request.
    clock = FakeClock()
    transport = RecordingTransport([ok("a")], clock)
    budget = RequestBudget(transport, 1, 15, 60, 0, clock, lambda _: None)

    budget.get("https://example.invalid/1")

    # Assert: the second call is rejected before touching the transport.
    with pytest.raises(ValueError, match="budget exhausted"):
        budget.get("https://example.invalid/2")
    assert len(transport.calls) == 1


def test_request_gap_delays_the_next_call_by_exactly_the_configured_gap() -> None:
    # Arrange: no real time passes between calls except via sleep.
    clock = FakeClock()
    transport = RecordingTransport([ok("a"), ok("b")], clock)
    sleeps: list[float] = []
    budget = RequestBudget(transport, 5, 15, 60, 5, clock, recording_sleep(sleeps, clock))

    budget.get("https://example.invalid/1")
    budget.get("https://example.invalid/2")

    # Assert: first call has no gap to wait out; second waits the full gap.
    assert sleeps == [0.0, 5.0]
    assert clock.now == 5.0


def test_request_gap_shrinks_by_time_already_spent_since_the_last_call() -> None:
    # Arrange: time passes between calls outside the budget's own control
    # (e.g. the caller spent time parsing the previous response) -- the gap
    # only tops up whatever is left, it never re-waits the full configured gap.
    clock = FakeClock()
    transport = RecordingTransport([ok("a"), ok("b")], clock)
    sleeps: list[float] = []
    budget = RequestBudget(transport, 5, 15, 60, 5, clock, recording_sleep(sleeps, clock))

    budget.get("https://example.invalid/1")
    clock.now += 2.0
    budget.get("https://example.invalid/2")

    # Assert: only the remaining 3s of the 5s gap are slept.
    assert sleeps == [0.0, 3.0]


def test_aggregate_deadline_is_checked_before_sleeping_further_requests() -> None:
    # Arrange: a 9s cycle with a 5s gap allows exactly two requests.
    clock = FakeClock()
    transport = RecordingTransport([ok("a"), ok("b")], clock)
    budget = RequestBudget(transport, 3, 15, 9, 5, clock, recording_sleep([], clock))

    budget.get("https://example.invalid/1")
    budget.get("https://example.invalid/2")

    assert clock.now == 5
    with pytest.raises(ValueError, match="deadline"):
        budget.get("https://example.invalid/3")
    assert len(transport.calls) == 2


def test_request_exactly_at_the_deadline_boundary_is_rejected() -> None:
    # Arrange: a 5s cycle with a 5s gap; the second call's pre-check computes
    # clock() + delay == deadline exactly, and the boundary itself must
    # already count as exceeded (a strict `>=` check, not `>`).
    clock = FakeClock()
    transport = RecordingTransport([ok("a")], clock)
    budget = RequestBudget(transport, 5, 15, 5, 5, clock, recording_sleep([], clock))
    budget.get("https://example.invalid/1")

    with pytest.raises(ValueError, match="deadline"):
        budget.get("https://example.invalid/2")
    assert len(transport.calls) == 1


def test_request_one_tick_before_the_deadline_boundary_succeeds() -> None:
    # Arrange: same setup as above, but the gap falls just short of the
    # deadline, so the call must be allowed through.
    clock = FakeClock()
    transport = RecordingTransport([ok("a"), ok("b")], clock)
    budget = RequestBudget(transport, 5, 15, 5, 4, clock, recording_sleep([], clock))
    budget.get("https://example.invalid/1")

    response = budget.get("https://example.invalid/2")

    assert response.text == "b"
    assert clock.now == 4


def test_deadline_exceeded_while_the_request_itself_is_in_flight() -> None:
    # Arrange: the pre-check passes, but the transport call takes long enough
    # that completion lands exactly on the deadline -- the post-check must
    # still reject it even though the transport already returned 200.
    clock = FakeClock()
    transport = RecordingTransport([ok("a")], clock, cost=10.0)
    budget = RequestBudget(transport, 5, 15, 10, 0, clock, recording_sleep([], clock))

    with pytest.raises(ValueError, match="deadline"):
        budget.get("https://example.invalid/1")
    # The transport call still happened -- the deadline is only caught after.
    assert len(transport.calls) == 1


def test_non_200_status_raises_without_retry() -> None:
    clock = FakeClock()
    transport = RecordingTransport([Response(429, "", {})], clock)
    budget = RequestBudget(transport, 5, 15, 60, 0, clock, recording_sleep([], clock))

    with pytest.raises(ValueError, match="HTTP 429"):
        budget.get("https://example.invalid/1")


def test_transport_error_propagates_unmodified() -> None:
    clock = FakeClock()
    transport = RaisingTransport(TimeoutError("synthetic timeout"))
    budget = RequestBudget(transport, 5, 15, 60, 0, clock, recording_sleep([], clock))

    with pytest.raises(TimeoutError, match="synthetic timeout"):
        budget.get("https://example.invalid/1")


def test_a_failed_call_still_consumes_its_request_slot() -> None:
    # Current contract: the call counter is incremented before the transport
    # is invoked, so a transport failure still consumes budget -- there is no
    # free retry against the same budget. This pins that existing behavior
    # rather than silently relying on it.
    clock = FakeClock()
    transport = RaisingTransport(TimeoutError("synthetic timeout"))
    budget = RequestBudget(transport, 1, 15, 60, 0, clock, recording_sleep([], clock))

    with pytest.raises(TimeoutError):
        budget.get("https://example.invalid/1")

    with pytest.raises(ValueError, match="budget exhausted"):
        budget.get("https://example.invalid/2")
    assert transport.calls == 1


def test_timeout_passed_to_transport_is_capped_by_the_remaining_deadline() -> None:
    # The per-request timeout is min(configured timeout, time left on the
    # cycle deadline), so a generous timeout never lets a single request run
    # past the aggregate cycle budget.
    clock = FakeClock()
    transport = RecordingTransport([ok("a")], clock)
    budget = RequestBudget(transport, 5, 15, 4, 0, clock, recording_sleep([], clock))

    budget.get("https://example.invalid/1")

    assert transport.calls[0] == ("https://example.invalid/1", 4)


def test_transport_protocol_is_satisfied_by_the_fakes_used_here() -> None:
    # A light structural check that the fakes above actually implement the
    # Transport protocol these tests rely on.
    clock = FakeClock()
    transport: Transport = RecordingTransport([ok("a")], clock)
    assert transport.get("https://example.invalid/1", 1.0).status == 200
