"""SQLite snapshots, price history and a transactional notification outbox."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import TracebackType

from pydantic import TypeAdapter
from typing_extensions import TypedDict

from .alerts import NO_MINIMUM_DROP, PriceDropThreshold, classify_alert
from .models import Offer, duplicate_key, utc_now


class Notification(TypedDict):
    id: int
    kind: str
    payload: str
    previous_price: str | None


class RunState(TypedDict):
    next_run: float
    failures: int


@dataclass(frozen=True)
class PriceStats:
    """Derived price-history facts for one stably identified (provider, offer_id).

    Computed from the existing `offers`/`price_history` tables -- no
    redundant columns. `previous_price`/`lowest_price` are `None` when there
    is not yet a prior price to compare against.
    """

    current_price: Decimal | None
    previous_price: Decimal | None
    lowest_price: Decimal | None
    first_seen: datetime
    last_seen: datetime


class Store:
    """Persist observations and outbox entries in the same SQLite transaction.

    `alert_rearm_after` is how long an alerted offer group must go without an
    eligible observation before it is announced again as new; `None` never
    re-announces an unchanged price. `price_drop_threshold` is the minimum
    drop (PLN amount or percentage) that turns a lower price into a
    `price_drop`/`new_low` notification; the default makes any drop
    significant, matching this project's original threshold-free behavior.
    """

    def __init__(
        self,
        path: Path,
        alert_rearm_after: timedelta | None = None,
        clock: Callable[[], datetime] = utc_now,
        price_drop_threshold: PriceDropThreshold = NO_MINIMUM_DROP,
    ) -> None:
        if alert_rearm_after is not None and alert_rearm_after <= timedelta(0):
            raise ValueError("Alert re-arm period must be positive")
        self.alert_rearm_after = alert_rearm_after
        self.clock = clock
        self.price_drop_threshold = price_drop_threshold
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS offers (
                provider TEXT NOT NULL, offer_id TEXT NOT NULL,
                payload TEXT NOT NULL, price TEXT, currency TEXT,
                found_at TEXT NOT NULL, last_seen TEXT NOT NULL,
                PRIMARY KEY(provider, offer_id)
            );
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY, provider TEXT NOT NULL,
                offer_id TEXT NOT NULL, price TEXT, currency TEXT, seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alert_state (
                group_key TEXT PRIMARY KEY, lowest_alert_price TEXT NOT NULL,
                last_eligible_at TEXT
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                previous_price TEXT, created_at TEXT NOT NULL, delivered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS provider_runs (
                provider TEXT PRIMARY KEY, next_run REAL NOT NULL, failures INTEGER NOT NULL
            );
        """)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(alert_state)")}
        if "last_eligible_at" not in columns:
            with self.connection:
                self.connection.execute("ALTER TABLE alert_state ADD COLUMN last_eligible_at TEXT")

    def __enter__(self) -> "Store":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release the connection, including when used as a context manager."""
        self.connection.close()

    def observe(self, offer: Offer, eligible: bool) -> list[str]:
        """Record one observation atomically and return resulting event names.

        `eligible` is trusted as-is: it must already be the result of
        `filtering.matches(offer, filters)` (see `OfferPipeline.filter_batch`/
        `finalize`), which is the single place that decides both business
        eligibility and whether an incomplete price is acceptable for this
        offer's provider (`filters["accept_incomplete_price_from"]`). Storage
        is a persistence boundary, not a policy layer: it does not re-derive
        that business decision from `offer.price_is_complete` or from
        business configuration, to avoid a second, driftable copy of the same
        rule. `pipeline.py`'s contract test pins this invariant.
        """
        if eligible and offer.price_per_person is None:
            raise ValueError("An eligible offer must have a price")
        now = self.clock()
        events = []
        with self.connection:
            previous = self.get_offer(offer.provider, offer.offer_id)
            # Read this exact offer's own prior price and historical low
            # *before* this observation is saved below -- these describe
            # what was already known, not what we are about to record.
            previous_price = (
                previous.price_per_person
                if previous is not None and previous.currency == offer.currency
                else None
            )
            prior_prices = self._price_series(offer.provider, offer.offer_id, offer.currency)
            lowest_price = min(prior_prices) if prior_prices else None
            snapshot = replace(
                offer, found_at=previous.found_at if previous else now, last_seen=now
            )
            self._save_snapshot(snapshot)
            if (
                previous is None
                or previous.price_per_person != offer.price_per_person
                or previous.currency != offer.currency
            ):
                self._save_price(snapshot)
                if previous is not None:
                    events.append("price_changed")
            if eligible:
                kind = self._enqueue_alert(snapshot, previous_price, lowest_price)
                if kind:
                    events.append(kind)
        return events

    def _save_snapshot(self, offer: Offer) -> None:
        price = str(offer.price_per_person) if offer.price_per_person is not None else None
        self.connection.execute(
            "INSERT OR REPLACE INTO offers VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                offer.provider,
                offer.offer_id,
                offer.to_json(),
                price,
                offer.currency,
                offer.found_at.isoformat(),
                offer.last_seen.isoformat(),
            ),
        )

    def _save_price(self, offer: Offer) -> None:
        self.connection.execute(
            "INSERT INTO price_history(provider,offer_id,price,currency,seen_at) VALUES (?,?,?,?,?)",
            (
                offer.provider,
                offer.offer_id,
                str(offer.price_per_person) if offer.price_per_person is not None else None,
                offer.currency,
                offer.last_seen.isoformat(),
            ),
        )

    def _enqueue_alert(
        self, offer: Offer, previous_price: Decimal | None, lowest_price: Decimal | None
    ) -> str | None:
        """Queue at most one alert per eligible observation of an offer group.

        Every eligible observation refreshes `last_eligible_at`, so a group that
        stays present in hourly scans never looks "returned"; an identical
        repeat observation therefore never produces a duplicate alert.

        `previous_price`/`lowest_price` describe this specific (provider,
        offer_id)'s own prior observations (see `observe`); `baseline` here
        is unrelated to them -- it is the group-level (`duplicate_key`)
        "has this offer group ever been alerted before" marker that
        `classify_alert` uses only for `new_offer`/`returned`.
        """
        if offer.price_per_person is None:
            raise ValueError("An alert requires a price")
        key = duplicate_key(offer)
        row = self.connection.execute(
            "SELECT * FROM alert_state WHERE group_key=?", (key,)
        ).fetchone()
        baseline = Decimal(row["lowest_alert_price"]) if row else None
        last_eligible = (
            datetime.fromisoformat(row["last_eligible_at"])
            if row and row["last_eligible_at"]
            else None
        )
        returned = (
            self.alert_rearm_after is not None
            and last_eligible is not None
            and offer.last_seen - last_eligible >= self.alert_rearm_after
        )
        kind = classify_alert(
            offer.price_per_person,
            baseline,
            returned,
            previous_price,
            lowest_price,
            self.price_drop_threshold,
        )
        seen = offer.last_seen.isoformat()
        if kind:
            self.connection.execute(
                "INSERT INTO notifications(kind,payload,previous_price,created_at) VALUES (?,?,?,?)",
                (
                    kind,
                    offer.to_json(),
                    str(previous_price) if kind in ("price_drop", "new_low") else None,
                    seen,
                ),
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO alert_state VALUES (?,?,?)",
                (key, str(offer.price_per_person), seen),
            )
        else:
            self.connection.execute(
                "UPDATE alert_state SET last_eligible_at=? WHERE group_key=?", (seen, key)
            )
        return kind

    def get_offer(self, provider: str, offer_id: str) -> Offer | None:
        """Retrieve the latest snapshot of a source-specific trip variant."""
        row = self.connection.execute(
            "SELECT payload FROM offers WHERE provider=? AND offer_id=?", (provider, offer_id)
        ).fetchone()
        return TypeAdapter(Offer).validate_json(row["payload"]) if row else None

    def price_history(self, provider: str, offer_id: str) -> list[Decimal | None]:
        """Return observed price changes in observation order."""
        rows = self.connection.execute(
            "SELECT price FROM price_history WHERE provider=? AND offer_id=? ORDER BY id",
            (provider, offer_id),
        ).fetchall()
        return [Decimal(row["price"]) if row["price"] is not None else None for row in rows]

    def _price_series(self, provider: str, offer_id: str, currency: str | None) -> list[Decimal]:
        """Same-currency observed price changes only, oldest first.

        Prices are stored as exact-decimal TEXT, so comparisons/minimums are
        always done here in Python (correct `Decimal` ordering), never via a
        SQL `MIN()`/`ORDER BY` on the TEXT column (which would sort
        lexicographically, e.g. treat "1200" as less than "950").
        """
        rows = self.connection.execute(
            "SELECT price, currency FROM price_history WHERE provider=? AND offer_id=? ORDER BY id",
            (provider, offer_id),
        ).fetchall()
        return [
            Decimal(row["price"])
            for row in rows
            if row["price"] is not None and row["currency"] == currency
        ]

    def price_stats(self, provider: str, offer_id: str) -> PriceStats | None:
        """Current/previous/lowest price and first/last-seen for one stably
        identified offer, derived from the existing snapshot/history tables
        (see `PriceStats`); `None` when this (provider, offer_id) was never observed.
        """
        offer = self.get_offer(provider, offer_id)
        if offer is None:
            return None
        history = self._price_series(provider, offer_id, offer.currency)
        return PriceStats(
            current_price=offer.price_per_person,
            previous_price=history[-2] if len(history) >= 2 else None,
            lowest_price=min(history) if history else None,
            first_seen=offer.found_at,
            last_seen=offer.last_seen,
        )

    def pending(self) -> list[Notification]:
        """Return undelivered outbox entries in creation order."""
        rows = self.connection.execute(
            "SELECT * FROM notifications WHERE delivered_at IS NULL ORDER BY id"
        ).fetchall()
        return [
            Notification(
                id=row["id"],
                kind=row["kind"],
                payload=row["payload"],
                previous_price=row["previous_price"],
            )
            for row in rows
        ]

    def mark_delivered(self, notification_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE notifications SET delivered_at=? WHERE id=?",
                (utc_now().isoformat(), notification_id),
            )

    def run_state(self, name: str) -> RunState | None:
        row = self.connection.execute(
            "SELECT * FROM provider_runs WHERE provider=?", (name,)
        ).fetchone()
        return RunState(next_run=row["next_run"], failures=row["failures"]) if row else None

    def schedule(self, name: str, next_run: float, failures: int) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO provider_runs VALUES (?,?,?)", (name, next_run, failures)
            )
