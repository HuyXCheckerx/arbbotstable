"""Thread-safe, revision-aware token balance observations.

The tracker is intentionally independent of Solana and WebSocket libraries.  A
monitor can inherit from it or hold one as a component, seed its initial RPC
balances, and call :meth:`update` for each WebSocket notification.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from threading import Condition, Event, RLock
import time
from types import MappingProxyType
from typing import Optional


@dataclass(frozen=True)
class BalanceSnapshot(Mapping[str, int]):
    """An immutable, atomic view of a tracker's balances and metadata.

    The snapshot is mapping-like for concise predicates: ``snapshot[key]``
    returns the raw balance.  Revision, slot, and source-timestamp metadata are
    available through the corresponding mappings.
    """

    balances: Mapping[str, int]
    revisions: Mapping[str, int]
    slots: Mapping[str, Optional[int]]
    timestamps: Mapping[str, Optional[float]]

    def __getitem__(self, key: str) -> int:
        return self.balances[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.balances)

    def __len__(self) -> int:
        return len(self.balances)


@dataclass(frozen=True)
class BalanceConfirmation:
    """Result of a WebSocket-first confirmation with one RPC fallback."""

    confirmed: bool
    balances: Mapping[str, int]
    source: str
    error: Optional[str] = None


class BalanceTracker:
    """Store balance notifications with per-account revision cursors.

    Revisions start at zero and advance once for every :meth:`update`, even if
    the reported balance is unchanged.  Initial :meth:`seed` values do not
    advance a revision, signal waiters, or count as a balance increase.
    """

    def __init__(
        self,
        keys: Iterable[str] = (),
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = RLock()
        self.condition = Condition(self._lock)
        self.update_event = Event()
        self._monotonic = monotonic

        initial_keys = tuple(dict.fromkeys(keys))
        self._balances = {key: 0 for key in initial_keys}
        self._revisions = {key: 0 for key in initial_keys}
        self._slots: dict[str, Optional[int]] = {
            key: None for key in initial_keys
        }
        self._timestamps: dict[str, Optional[float]] = {
            key: None for key in initial_keys
        }
        self._observed_balances = {key: 0 for key in initial_keys}
        self._last_increase_at: dict[str, float] = {}

    @property
    def balances(self) -> dict[str, int]:
        """Return a thread-safe copy for legacy read-only consumers."""

        with self._lock:
            return dict(self._balances)

    @property
    def revisions(self) -> dict[str, int]:
        """Return a thread-safe copy of the current revision cursors."""

        with self._lock:
            return dict(self._revisions)

    def seed(
        self,
        key: str,
        balance: int,
        *,
        slot: Optional[int] = None,
        timestamp: Optional[float] = None,
    ) -> bool:
        """Set an initial value without advancing its revision.

        A live value (revision greater than zero) is never overwritten by a
        later seed.  This makes an initial RPC/WebSocket race safe.  ``True``
        indicates that the seed was stored.
        """

        with self.condition:
            revision = self._revisions.setdefault(key, 0)
            if revision > 0:
                return False
            self._balances[key] = int(balance)
            self._slots[key] = slot
            self._timestamps[key] = timestamp
            self._observed_balances[key] = int(balance)
            return True

    def update(
        self,
        key: str,
        balance: int,
        *,
        slot: Optional[int] = None,
        timestamp: Optional[float] = None,
    ) -> int:
        """Record one notification and return its resulting per-key revision.

        When both observations have Solana slots, a notification from an older
        slot is ignored.  Equal-slot updates remain valid because an account
        can be written more than once within one slot.
        """

        balance = int(balance)
        with self.condition:
            current_revision = self._revisions.get(key, 0)
            current_slot = self._slots.get(key)
            if slot is not None and current_slot is not None and slot < current_slot:
                return current_revision

            previous = self._balances.get(key)
            observed_previous = self._observed_balances.get(key, previous)
            revision = current_revision + 1

            self._balances[key] = balance
            self._revisions[key] = revision
            # A slot-less observation must not erase the high-water mark; if
            # it did, an older numbered notification could be accepted later.
            self._slots[key] = current_slot if slot is None else slot
            self._timestamps[key] = timestamp
            if observed_previous is not None and balance > observed_previous:
                self._last_increase_at[key] = self._monotonic()
            self._observed_balances[key] = balance

            # The condition is the lossless per-revision mechanism.  The event
            # remains for existing global "anything changed" consumers.
            self.update_event.set()
            self.condition.notify_all()
            return revision

    def observe_balance(self, key: str, balance: int) -> bool:
        """Track an RPC-observed increase without creating a WS revision.

        This is used only for settlement windows.  It lets a refill first seen
        during a WebSocket outage start the same backend-indexing grace period,
        while ensuring that the RPC observation cannot confirm a transaction.
        """

        balance = int(balance)
        with self._lock:
            previous = self._observed_balances.get(key)
            increased = previous is not None and balance > previous
            if increased:
                self._last_increase_at[key] = self._monotonic()
            self._observed_balances[key] = balance
            return increased

    def get(self, key: str) -> int:
        """Return one balance under the tracker lock."""

        with self._lock:
            return self._balances[key]

    def revision(self, key: str) -> int:
        """Return one key's current revision under the tracker lock."""

        with self._lock:
            return self._revisions[key]

    def snapshot(self, keys: Optional[Iterable[str]] = None) -> BalanceSnapshot:
        """Return an immutable atomic snapshot for ``keys`` (or every key)."""

        with self._lock:
            selected = tuple(self._balances if keys is None else keys)
            return self._snapshot_unlocked(selected)

    def _snapshot_unlocked(self, keys: Iterable[str]) -> BalanceSnapshot:
        selected = tuple(keys)
        balances = {key: self._balances[key] for key in selected}
        revisions = {key: self._revisions[key] for key in selected}
        slots = {key: self._slots.get(key) for key in selected}
        timestamps = {key: self._timestamps.get(key) for key in selected}
        return BalanceSnapshot(
            balances=MappingProxyType(balances),
            revisions=MappingProxyType(revisions),
            slots=MappingProxyType(slots),
            timestamps=MappingProxyType(timestamps),
        )

    def wait_for(
        self,
        predicate: Callable[[BalanceSnapshot], bool],
        after_revisions: Mapping[str, int],
        timeout: Optional[float],
    ) -> Optional[BalanceSnapshot]:
        """Wait for a fresh atomic state satisfying ``predicate``.

        Every key in ``after_revisions`` must advance beyond its supplied
        cursor before the predicate can succeed.  The state is checked before
        sleeping, so an update that arrived after cursor capture but before
        this method was called is accepted.  Conversely, a matching balance
        from at or before the cursor can never be accepted.
        """

        cursors = {key: int(revision) for key, revision in after_revisions.items()}
        if timeout is not None:
            timeout = max(0.0, float(timeout))
            deadline = self._monotonic() + timeout
        else:
            deadline = None

        with self.condition:
            while True:
                keys = tuple(self._balances)
                snapshot = self._snapshot_unlocked(keys)
                is_fresh = all(
                    snapshot.revisions.get(key, 0) > revision
                    for key, revision in cursors.items()
                )
                if is_fresh and predicate(snapshot):
                    return snapshot

                if deadline is None:
                    self.condition.wait()
                    continue

                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(remaining)

    def seconds_until_increase_settled(self, key: str, delay: float) -> float:
        """Return time remaining in a post-increase settlement window."""

        delay = max(0.0, float(delay))
        if delay == 0:
            return 0.0
        with self._lock:
            increased_at = self._last_increase_at.get(key)
            if increased_at is None:
                return 0.0
            return max(0.0, increased_at + delay - self._monotonic())


def coherent_ws_match(
    snapshot: BalanceSnapshot,
    predicates: Mapping[str, Callable[[int], bool]],
) -> bool:
    """Require matching balances from one known Solana context slot."""

    slots = [snapshot.slots.get(key) for key in predicates]
    return (
        bool(slots)
        and all(slot is not None for slot in slots)
        and len(set(slots)) == 1
        and all(
            predicate(snapshot[key])
            for key, predicate in predicates.items()
        )
    )


def confirm_balances_ws_first(
    tracker: BalanceTracker,
    predicates: Mapping[str, Callable[[int], bool]],
    after_revisions: Mapping[str, int],
    timeout: float,
    *,
    rpc_reader: Callable[[str], int],
) -> BalanceConfirmation:
    """Wait for fresh WebSocket revisions, then read each account once.

    The bounded WS wait is retained even when the socket has just disconnected:
    reconnect notifications may still arrive, and an immediate RPC read after
    submit would commonly precede ``confirmed`` settlement.
    """

    watched_keys = tuple(predicates)
    snapshot = tracker.wait_for(
        lambda current: coherent_ws_match(current, predicates),
        {key: after_revisions[key] for key in watched_keys},
        timeout=max(0.0, float(timeout)),
    )
    if snapshot is not None:
        balances = MappingProxyType(
            {key: snapshot[key] for key in watched_keys}
        )
        return BalanceConfirmation(True, balances, "ws")

    balances = {}
    try:
        for key in watched_keys:
            balances[key] = int(rpc_reader(key))
    except Exception as exc:
        return BalanceConfirmation(
            False,
            MappingProxyType(dict(balances)),
            "rpc_error",
            str(exc),
        )

    confirmed = all(
        predicate(balances[key])
        for key, predicate in predicates.items()
    )
    return BalanceConfirmation(
        confirmed,
        MappingProxyType(dict(balances)),
        "rpc",
    )


__all__ = [
    "BalanceConfirmation",
    "BalanceSnapshot",
    "BalanceTracker",
    "coherent_ws_match",
    "confirm_balances_ws_first",
]
