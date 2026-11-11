"""Outbox package for notifications queue and worker processing."""

from sellershield_agent.outbox.enqueue import enqueue_notification

__all__ = ["enqueue_notification"]

