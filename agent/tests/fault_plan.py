"""Test-only fault injection plan for FixtureStoreClient (TASK T2.1).

Inert unless attached to FixtureStoreClient; production code never imports it.
Supports:
- fail_once(step): raises StoreUnavailable on first invocation of step, then clears.
- timeout_after_commit(step): change is committed, but caller receives StoreTimeout.
- unavailable(step): raises StoreUnavailable.
- conflict(step): raises StoreConflict.
- slow(step, seconds): delays step execution by specified seconds.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sellershield_agent.store.client import (
    StoreConflict,
    StoreTimeout,
    StoreUnavailable,
)


class FaultPlan:
    """Test fault plan controlling simulated faults on FixtureStoreClient steps."""

    def __init__(self) -> None:
        self._fail_once_steps: set[str] = set()
        self._timeout_after_commit_steps: set[str] = set()
        self._unavailable_steps: set[str] = set()
        self._conflict_steps: set[str] = set()
        self._slow_steps: dict[str, float] = {}

    def fail_once(self, step: str) -> FaultPlan:
        """Inject a transient failure that fires once on the given step."""
        self._fail_once_steps.add(step)
        return self

    def timeout_after_commit(self, step: str) -> FaultPlan:
        """Commit the mutation, but raise StoreTimeout to the caller."""
        self._timeout_after_commit_steps.add(step)
        return self

    def unavailable(self, step: str) -> FaultPlan:
        """Raise StoreUnavailable when the given step is invoked."""
        self._unavailable_steps.add(step)
        return self

    def conflict(self, step: str) -> FaultPlan:
        """Raise StoreConflict when the given step is invoked."""
        self._conflict_steps.add(step)
        return self

    def slow(self, step: str, seconds: float) -> FaultPlan:
        """Delay execution of the given step by seconds."""
        self._slow_steps[step] = seconds
        return self

    async def execute_before(self, step: str) -> None:
        """Execute pre-commit fault triggers."""
        if step in self._slow_steps:
            await asyncio.sleep(self._slow_steps[step])
        if step in self._unavailable_steps:
            raise StoreUnavailable(f"Fault injection: {step} unavailable")
        if step in self._conflict_steps:
            raise StoreConflict(f"Fault injection: {step} conflict")
        if step in self._fail_once_steps:
            self._fail_once_steps.remove(step)
            raise StoreUnavailable(f"Fault injection: {step} fail_once")

    def execute_after_commit(self, step: str) -> None:
        """Execute post-commit fault triggers (e.g. timeout after state change)."""
        if step in self._timeout_after_commit_steps:
            raise StoreTimeout(f"Fault injection: {step} timed out after commit")

