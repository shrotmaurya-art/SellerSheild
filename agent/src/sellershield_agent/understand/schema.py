"""Understanding schema for customer message classification (T1.7)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Intent(StrEnum):
    """Customer intents recognized by the understanding stage."""

    ORDER_STATUS = "order_status"
    SHIPPING_STATUS = "shipping_status"
    RETURN_REQUEST = "return_request"
    REFUND_STATUS = "refund_status"
    REFUND_REQUEST = "refund_request"
    CANCELLATION = "cancellation"
    DAMAGED_ITEM = "damaged_item"
    WRONG_ITEM = "wrong_item"
    HUMAN_REQUEST = "human_request"
    POLICY_QUESTION = "policy_question"
    SMALL_TALK = "small_talk"
    OFF_TOPIC = "off_topic"
    ABUSE = "abuse"


class ReasonCode(StrEnum):
    """Reason category for return, refund, or issue report."""

    DEFECTIVE = "defective"
    DAMAGED = "damaged"
    WRONG_ITEM = "wrong_item"
    NOT_AS_DESCRIBED = "not_as_described"
    CHANGED_MIND = "changed_mind"
    LATE = "late"
    OTHER = "other"


class Language(StrEnum):
    """Detected language of the customer message."""

    EN = "en"
    HI = "hi"
    HINGLISH = "hinglish"
    OTHER = "other"


class Sentiment(StrEnum):
    """Customer sentiment detected in the message."""

    CALM = "calm"
    FRUSTRATED = "frustrated"
    ANGRY = "angry"


class ClarificationSlot(StrEnum):
    """Slot requiring clarification when needs_clarification is True."""

    ORDER_ID = "order_id"
    ITEMS = "items"
    REASON = "reason"
    REFUND_METHOD = "refund_method"
    NONE = "none"


class SafetyFlag(StrEnum):
    """Safety annotations for customer messages."""

    INJECTION_ATTEMPT = "injection_attempt"
    ABUSIVE = "abusive"
    OFF_TOPIC = "off_topic"
    PII_REQUEST = "pii_request"


class Understanding(BaseModel):
    """Flat Pydantic model representing structured customer understanding.

    Spec §4.2:
    intent · confidence · order_ids · item_mentions · reason_code ·
    reason_text · language · sentiment · asks_for_human · secondary_intents ·
    needs_clarification · clarification_slot · safety_flags.
    """

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    order_ids: list[str] = Field(default_factory=list)
    item_mentions: list[str] = Field(default_factory=list)
    reason_code: ReasonCode = ReasonCode.OTHER
    reason_text: str = Field(default="", max_length=200)
    language: Language = Language.EN
    sentiment: Sentiment = Sentiment.CALM
    asks_for_human: bool = False
    secondary_intents: list[Intent] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_slot: ClarificationSlot = ClarificationSlot.NONE
    safety_flags: list[SafetyFlag] = Field(default_factory=list)
