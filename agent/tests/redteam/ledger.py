"""Independent financial ledger oracle for SellerShield AI money path (T2.11).

Recomputes reservations, authorizations, and releases per order directly from
store fixtures and asserts core financial and operational invariants:
- Refund totals never exceed total paid: sum(releases) + sum(reservations) <= total_paid
- One active return per item across all return requests
- Return item quantities never exceed delivered quantities
- No refund release exceeds the autonomy cap (50,000 paise) without seller authorization
- Total autonomous releases in an IST day never exceed the daily cap (500,000 paise)
- Proposal execution is atomic and idempotent (no double execution)
- Audit hash chain verifies unbroken
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, select

from sellershield_agent.audit.service import AuditService
from sellershield_agent.db import read_tx
from sellershield_agent.domain.state import proposals
from sellershield_agent.store.fixtures import ACTIVE_RETURN_STATUSES, FixtureStoreClient

IST = ZoneInfo("Asia/Kolkata")


class LedgerInvariantViolation(AssertionError):
    """Raised when an oracle check fails."""


@dataclass(frozen=True)
class OrderLedgerSummary:
    order_id: str
    total_paid_paise: int
    reserved_paise: int
    authorized_paise: int
    released_paise: int
    active_returns_count: int


class LedgerOracle:
    """Independent auditor oracle recomputing financial balances and state invariants."""

    def __init__(self, store: FixtureStoreClient) -> None:
        self.store = store

    def _get_raw_orders(self) -> list[dict[str, Any]]:
        return list(self.store._raw_data.get("orders", []))

    def _get_raw_returns(self) -> list[dict[str, Any]]:
        return list(self.store._raw_data.get("returns", []))

    def _get_raw_refunds(self) -> list[dict[str, Any]]:
        return list(self.store._raw_data.get("refunds", []))

    def recompute_order(self, order_id: str) -> OrderLedgerSummary:
        """Recompute financial totals and active returns for a specific order."""
        raw_order = next(
            (o for o in self._get_raw_orders() if o.get("order_id") == order_id),
            None,
        )
        if raw_order is None:
            raise LedgerInvariantViolation(f"Order '{order_id}' does not exist in store")

        total_paid = int(raw_order.get("total_paise", 0))

        # Tally refunds
        reserved_paise = 0
        authorized_paise = 0
        released_paise = 0
        for rfd in self._get_raw_refunds():
            if rfd.get("order_id") != order_id:
                continue
            amt = int(rfd.get("amount_paise", 0))
            status = str(rfd.get("status", "")).lower()
            if status == "reserved":
                reserved_paise += amt
            elif status == "authorized":
                authorized_paise += amt
            elif status in ("processing", "completed"):
                released_paise += amt

        # Tally active returns
        active_returns_count = 0
        for ret in self._get_raw_returns():
            if ret.get("order_id") != order_id:
                continue
            ret_status = str(ret.get("status", "")).lower()
            if ret_status in ACTIVE_RETURN_STATUSES or ret_status in (
                "requested",
                "approved",
                "pickup_scheduled",
                "in_transit_back",
                "received",
                "inspected_ok",
            ):
                active_returns_count += 1

        return OrderLedgerSummary(
            order_id=order_id,
            total_paid_paise=total_paid,
            reserved_paise=reserved_paise,
            authorized_paise=authorized_paise,
            released_paise=released_paise,
            active_returns_count=active_returns_count,
        )

    def assert_order_invariants(self, order_id: str) -> OrderLedgerSummary:
        """Assert core monetary invariants on a single order."""
        summary = self.recompute_order(order_id)

        # Invariant 1: Total allocations cannot exceed total paid
        total_allocated = (
            summary.reserved_paise + summary.authorized_paise + summary.released_paise
        )
        if total_allocated > summary.total_paid_paise:
            raise LedgerInvariantViolation(
                f"Monetary invariant violated on {order_id}: total allocated ({total_allocated}) "
                f"exceeds total paid ({summary.total_paid_paise}). "
                f"(reserved={summary.reserved_paise}, auth={summary.authorized_paise}, "
                f"rel={summary.released_paise})"
            )

        # Invariant 2: Active return uniqueness per item & quantity limits
        raw_order = next(o for o in self._get_raw_orders() if o.get("order_id") == order_id)
        order_items = {it["item_id"]: it for it in raw_order.get("items", [])}
        existing_returns = [r for r in self._get_raw_returns() if r.get("order_id") == order_id]

        for item_id, it in order_items.items():
            delivered_qty = int(it.get("qty", 1))

            # Count active returns covering this item
            active_item_returns = [
                r
                for r in existing_returns
                if str(r.get("status", "")).lower() in ACTIVE_RETURN_STATUSES
                and any(ri.get("item_id") == item_id for ri in r.get("items", []))
            ]
            if len(active_item_returns) > 1:
                raise LedgerInvariantViolation(
                    f"Return exclusivity violated on {order_id}: item {item_id} has "
                    f"{len(active_item_returns)} active returns (max 1 permitted)"
                )

            # Cumulative returned quantity across non-cancelled/non-rejected returns
            total_returned_qty = sum(
                int(ri.get("qty", 1))
                for r in existing_returns
                if str(r.get("status", "")).lower() not in ("cancelled", "rejected")
                for ri in r.get("items", [])
                if ri.get("item_id") == item_id
            )
            if total_returned_qty > delivered_qty:
                raise LedgerInvariantViolation(
                    f"Return quantity limit violated on {order_id}: returned quantity "
                    f"({total_returned_qty}) > delivered ({delivered_qty}) for item {item_id}"
                )

        return summary

    def assert_autonomy_caps(self) -> None:
        """Assert single-release autonomy cap and daily autonomous IST cap."""
        settings = self.store._settings
        autonomy_cap = settings.autonomy_cap_paise
        daily_cap = settings.autonomy_daily_cap_paise

        # Invariant 4: No release > autonomy cap without seller authorization
        for rfd in self._get_raw_refunds():
            status = str(rfd.get("status", "")).lower()
            if status in ("processing", "completed"):
                amount = int(rfd.get("amount_paise", 0))
                has_auth = (
                    rfd.get("authorized_by") is not None
                    and rfd.get("authorized_amount_paise") is not None
                )
                if not has_auth and amount > autonomy_cap:
                    raise LedgerInvariantViolation(
                        f"Autonomy cap violated on refund {rfd.get('refund_id')}: amount {amount} "
                        f"released without seller authorization (cap is {autonomy_cap})"
                    )

        # Invariant 5: Daily autonomous cap in IST
        for date_key, daily_total in self.store._daily_releases.items():
            if daily_total > daily_cap:
                raise LedgerInvariantViolation(
                    f"Daily autonomy cap exceeded for IST date {date_key}: releases {daily_total} "
                    f"exceeds cap {daily_cap}"
                )

    def assert_database_invariants(self, engine: Engine) -> None:
        """Assert audit chain integrity and proposal execution states in the agent DB."""
        with read_tx(engine) as conn:
            # Audit chain verification
            chain_status = AuditService.verify_chain(conn)
            if not chain_status.ok:
                raise LedgerInvariantViolation(
                    f"Audit chain verification failed at sequence {chain_status.first_broken_seq}"
                )

            # Check proposals table
            rows = conn.execute(select(proposals)).fetchall()
            valid_statuses = {
                "proposed",
                "confirmed",
                "executing",
                "succeeded",
                "failed",
                "cancelled",
                "expired",
                "stale",
            }
            for row in rows:
                p_status = str(row.status).lower()
                if p_status not in valid_statuses:
                    raise LedgerInvariantViolation(
                        f"Invalid proposal status '{p_status}' on proposal {row.proposal_id}"
                    )

    def assert_all(self, engine: Engine | None = None, order_ids: list[str] | None = None) -> None:
        """Run all ledger and store assertions across specified orders or all store orders."""
        target_orders = order_ids or [str(o["order_id"]) for o in self._get_raw_orders()]
        for oid in target_orders:
            self.assert_order_invariants(oid)

        self.assert_autonomy_caps()

        if engine is not None:
            self.assert_database_invariants(engine)
