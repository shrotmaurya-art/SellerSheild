"""Conversation Orchestrator Pipeline implementing Stages 0 to 6 (Spec §4)."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import string
import uuid
from collections.abc import AsyncGenerator, Callable
from typing import Any

from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth.models import Principal
from sellershield_agent.clock import Clock
from sellershield_agent.compose.factsheet import build_factsheet
from sellershield_agent.compose.templates import (
    cancellation_denied as template_cancellation_denied,
)
from sellershield_agent.compose.templates import (
    guided_mode_menu,
    not_found,
)
from sellershield_agent.compose.templates import (
    order_status as template_order_status,
)
from sellershield_agent.compose.templates import (
    orders_list as template_orders_list,
)
from sellershield_agent.compose.templates import (
    policy_summary as template_policy_summary,
)
from sellershield_agent.compose.templates import (
    proposal_confirmation as template_proposal_confirmation,
)
from sellershield_agent.compose.templates import (
    refund_status as template_refund_status,
)
from sellershield_agent.compose.templates import (
    return_eligibility_preview as template_return_preview,
)
from sellershield_agent.compose.templates import (
    return_status as template_return_status,
)
from sellershield_agent.compose.templates import (
    shipping_status as template_shipping_status,
)
from sellershield_agent.compose.verifier import verify
from sellershield_agent.config import Settings, get_settings
from sellershield_agent.db import write_tx
from sellershield_agent.domain.session import (
    ChatMessageInput,
    messages,
    sessions,
)
from sellershield_agent.llm.chain import LLMChain, LLMUnavailable
from sellershield_agent.logging import get_logger
from sellershield_agent.orchestrator.cards import (
    build_case_created_card,
    build_chips_card,
    build_confirmation_card,
    build_order_summary_card,
    build_return_preview_card,
    build_shipment_timeline_card,
)
from sellershield_agent.orchestrator.escalation import (
    DEFAULT_ESCALATION_KEYWORDS,
    evaluate_escalation_triggers,
)
from sellershield_agent.orchestrator.resolver import ResolverLoop
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.store.client import RequestContext, StoreClient
from sellershield_agent.tools.base import ToolErrorCode
from sellershield_agent.tools.gateway import ToolGateway
from sellershield_agent.understand.injection import detect_injection_heuristics
from sellershield_agent.understand.router import (
    DEVANAGARI_REGEX,
    DeterministicRouter,
    extract_order_ids,
)
from sellershield_agent.understand.schema import Intent, Language
from sellershield_agent.understand.service import UnderstandContext, understand

logger = get_logger("sellershield_agent.orchestrator.pipeline")

# Test hooks for monkeypatching in unit tests
mock_draft_for_test: str | None = None
call_llm_for_resolution: Any = None
get_llm_client: Any = None

ORD_REGEX = re.compile(r"\bORD-\d+\b", re.IGNORECASE)


def sanitize_message_text(text: str) -> str:
    """Strip control characters, collapse whitespace, keep within 1000 chars."""
    # Remove control characters except newline and tab
    clean = "".join(ch for ch in text if ch in ("\n", "\t") or (ord(ch) >= 32 and ord(ch) != 127))
    # Collapse excess whitespace
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r"\n\s*\n+", "\n\n", clean)
    return clean.strip()


def _generate_msg_id() -> str:
    """Generate alphabetic message id to avoid accidental token collisions."""
    return "msg_" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(16))


class PipelineEvent:
    def __init__(self, event_type: str, data: dict[str, Any]) -> None:
        self.event_type = event_type
        self.data = data


class ConversationPipeline:
    """Stages 0-6 conversational pipeline for SellerShield AI."""

    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        store_client: StoreClient,
        settings: Settings | None = None,
        gateway: ToolGateway | None = None,
        policy: Any = None,
    ) -> None:
        self.engine = engine
        self.clock = clock
        self.store_client = store_client
        self.settings = settings or get_settings()
        self.policy = policy if policy is not None else load_policy()
        self.gateway = (
            gateway
            if gateway is not None
            else ToolGateway(
                store_client=store_client,
                engine=engine,
                clock=clock,
                policy=self.policy,
            )
        )
        self.router = DeterministicRouter()
        self.llm_chain = LLMChain(clock=clock)
        self.resolver = ResolverLoop(gateway=self.gateway)

    async def execute_stream(
        self,
        session_id: str,
        principal: Principal,
        token: str,
        input_data: ChatMessageInput,
        request_id: str,
        is_disconnected: Callable[[], bool] | None = None,
    ) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
        """Run stages 0 to 6 and yield SSE tuples of (event_type, payload)."""
        global mock_draft_for_test, call_llm_for_resolution, get_llm_client

        # ── Stage 0: Ingress and guard ────────────────────────────────────────
        customer_id = principal.customer_id or principal.user_id
        raw_text = input_data.text or ""
        sanitized_text = sanitize_message_text(raw_text) if raw_text else ""
        early_is_hi = bool(sanitized_text and DEVANAGARI_REGEX.search(sanitized_text))
        early_lang = "hi" if early_is_hi else "en"
        client_msg_id = input_data.client_msg_id
        action = input_data.action

        now_str = self.clock.now().isoformat()
        ctx = RequestContext(
            principal=principal,
            request_id=request_id,
            token=SecretStr(token) if token else None,
        )

        # Check idempotency: does client_msg_id already exist in this session?
        with self.engine.connect() as conn:
            stmt = select(messages).where(
                messages.c.session_id == session_id,
                messages.c.client_msg_id.in_([client_msg_id, f"{client_msg_id}_reply"]),
                messages.c.role == "agent",
            )
            existing_agent_msg = conn.execute(stmt).fetchone()

        if existing_agent_msg is not None:
            # Replay stored message with zero new tool calls
            msg_id = existing_agent_msg.id
            full_text = existing_agent_msg.text
            cards_raw = existing_agent_msg.cards_json

            # Replay tool status events if stored
            if cards_raw:
                try:
                    parsed_cards = json.loads(cards_raw)
                    if isinstance(parsed_cards, dict):
                        for ts in parsed_cards.get("tool_status", []):
                            yield ("tool.status", ts)
                except Exception:
                    pass

            # Emit deltas
            words = full_text.split()
            chunk_size = 3
            for i in range(0, len(words), chunk_size):
                chunk = " ".join(words[i : i + chunk_size])
                if i + chunk_size < len(words):
                    chunk += " "
                yield ("message.delta", {"delta": chunk})

            # Emit cards if any
            if cards_raw:
                try:
                    parsed_cards = json.loads(cards_raw)
                    cards_list = (
                        parsed_cards.get("cards", [])
                        if isinstance(parsed_cards, dict)
                        else parsed_cards
                    )
                    for c in cards_list:
                        yield ("card", c)
                except Exception:
                    pass

            yield ("final", {"message_id": msg_id, "full_text": full_text})
            return

        # Advisory prompt injection heuristics
        if sanitized_text:
            injection_flags = detect_injection_heuristics(sanitized_text)
            if injection_flags:
                with write_tx(self.engine) as conn:
                    AuditService.append(
                        conn,
                        AuditEvent(
                            event_id=f"evt_{uuid.uuid4().hex[:12]}",
                            ts_utc=now_str,
                            request_id=request_id,
                            session_id=session_id,
                            actor_type=ActorType.CUSTOMER,
                            actor_id=customer_id,
                            event_type=EventType.INJECTION_FLAG,
                            action="detect_injection",
                            outcome=Outcome.SUCCESS,
                            details={"phrases": [str(f) for f in injection_flags]},
                        ),
                    )

        # Money manipulation: invalid or negative quantities rejected
        if sanitized_text and re.search(
            r"(?:(?<![a-zA-Z0-9])-[0-9]+\s*(?:items?|quantity|qty|pieces?|units?)|\bnegative\s+quantity|\b0\s*(?:items?|quantity|qty|pieces?)\b)",
            sanitized_text,
            re.IGNORECASE,
        ):
            verified_text = (
                "अमान्य मात्रा अनुरोधित। कृपया वापस करने के लिए सही सकारात्मक संख्या बताएं।"
                if early_is_hi
                else (
                    "Invalid quantity requested. Please specify a positive quantity "
                    "of items to return."
                )
            )
            for chunk in verified_text.split():
                yield ("message.delta", {"delta": chunk + " "})
            agent_msg_id = _generate_msg_id()
            yield ("final", {"message_id": agent_msg_id, "full_text": verified_text})
            self._persist_turn(
                session_id=session_id,
                customer_id=customer_id,
                client_msg_id=client_msg_id,
                user_text=sanitized_text or (action.name if action else ""),
                agent_msg_id=agent_msg_id,
                agent_text=verified_text,
                cards=[],
                request_id=request_id,
            )
            return

        # Check session handler: if human, agent stays silent and only says
        # "A team member is handling this" plus read-only status and never calls propose tools.
        session_handler = "agent"
        try:
            with self.engine.connect() as conn:
                s_row = conn.execute(
                    select(sessions.c.handler).where(sessions.c.id == session_id)
                ).fetchone()
                if s_row is not None:
                    session_handler = str(s_row[0] or "agent")
        except Exception:
            pass

        if session_handler == "human":
            verified_text = (
                "इस मामले को हमारी सहायता टीम का सदस्य देख रहा है। हम आपसे सीधे संपर्क करेंगे।"
                if early_is_hi
                else "A team member is handling this case. We will follow up with you directly."
            )
            for chunk in verified_text.split():
                yield ("message.delta", {"delta": chunk + " "})
            agent_msg_id = _generate_msg_id()
            yield ("final", {"message_id": agent_msg_id, "full_text": verified_text})
            self._persist_turn(
                session_id=session_id,
                customer_id=customer_id,
                client_msg_id=client_msg_id,
                user_text=sanitized_text or (action.name if action else ""),
                agent_msg_id=agent_msg_id,
                agent_text=verified_text,
                cards=[],
                request_id=request_id,
            )
            return

        # ── Stage 1: Understanding ───────────────────────────────────────────
        is_guided_action = action is not None
        llm_down = False

        # Dev fault injection check
        import os

        from sellershield_agent.dev.faults import is_fault_active

        env_dev_fault = os.getenv("DEV_FAULT_LLM_DOWN", "false").lower() in ("true", "1")
        env_dev_mode = bool(getattr(self.settings, "DEV_MODE", False)) or (
            os.getenv("DEV_MODE", "false").lower() in ("true", "1")
        )
        dev_fault = (
            bool(getattr(self.settings, "DEV_FAULT_LLM_DOWN", False))
            or bool(getattr(self.settings, "dev_fault_llm_down", False))
            or env_dev_fault
            or is_fault_active("llm_down")
        ) and env_dev_mode

        intent_val = Intent.ORDER_STATUS
        extracted_order_id = action.order_id if action and action.order_id else None
        extracted_return_id: str | None = None
        extracted_refund_id: str | None = None
        extracted_case_id: str | None = None
        is_return_status_query = False
        is_refund_status_query = False
        is_case_status_query = False
        is_chip_status_query = False
        understanding_res: Any = None

        if sanitized_text:
            found_orders_pre = extract_order_ids(sanitized_text)
            if not extracted_order_id and found_orders_pre:
                extracted_order_id = found_orders_pre[0].upper()
            found_returns = re.findall(r"\bRET-[0-9A-Za-z]+\b", sanitized_text, re.IGNORECASE)
            if found_returns:
                extracted_return_id = found_returns[0].upper()
                is_return_status_query = True
            found_refunds = re.findall(r"\bRFD-[0-9A-Za-z]+\b", sanitized_text, re.IGNORECASE)
            if found_refunds:
                extracted_refund_id = found_refunds[0].upper()
                is_refund_status_query = True
            found_cases = re.findall(r"\bCASE-[0-9A-Za-z]+\b", sanitized_text, re.IGNORECASE)
            if found_cases:
                extracted_case_id = f"CASE-{found_cases[0].split('-', 1)[1]}"
            stripped_lower = sanitized_text.strip().lower()
            is_case_status_query = stripped_lower.startswith("check case ") and (
                extracted_case_id is not None
            )
            is_chip_status_query = (
                (stripped_lower.startswith("check return ") and extracted_return_id is not None)
                or (stripped_lower.startswith("check refund ") and extracted_refund_id is not None)
                or is_case_status_query
            )

        if is_guided_action:
            action_name = action.name if action else "track_order"
            if action_name == "track_order":
                intent_val = Intent.SHIPPING_STATUS
            elif action_name == "return_status":
                intent_val = Intent.RETURN_REQUEST
                is_return_status_query = True
            elif action_name == "refund_status":
                intent_val = Intent.REFUND_STATUS
            elif action_name == "policy_info":
                intent_val = Intent.POLICY_QUESTION
            elif action_name == "start_return":
                intent_val = Intent.RETURN_REQUEST
            elif action_name == "talk_to_human":
                intent_val = Intent.HUMAN_REQUEST
        elif is_chip_status_query:
            if extracted_return_id:
                intent_val = Intent.RETURN_REQUEST
                is_return_status_query = True
            elif extracted_refund_id:
                intent_val = Intent.REFUND_STATUS
                is_refund_status_query = True
        else:
            if dev_fault:
                llm_down = True
            else:
                # Robust extract candidate order IDs from text
                found_orders = extract_order_ids(sanitized_text)
                if found_orders:
                    extracted_order_id = found_orders[0].upper()

                u_ctx = UnderstandContext(
                    session_id=session_id,
                    customer_id=customer_id,
                    request_id=request_id,
                )
                try:
                    understanding = await understand(
                        message=sanitized_text,
                        context=u_ctx,
                        llm_chain=self.llm_chain,
                        router=self.router,
                    )
                    understanding_res = understanding
                    intent_val = understanding.intent
                    if not extracted_order_id and understanding.order_ids:
                        extracted_order_id = understanding.order_ids[0]
                except (LLMUnavailable, Exception) as e:
                    logger.warning(
                        "LLM understanding unavailable, entering guided mode",
                        error=str(e),
                    )
                    llm_down = True

                if not extracted_order_id and sanitized_text:
                    is_listing_query = bool(
                        re.search(
                            r"\b(?:(?:my|all|list|show)\s+(?:all\s+|my\s+)?orders?|"
                            r"(?:mere|sabhi|saare|sare|sab|apne)\s+(?:saare\s+|sare\s+|sabhi\s+)?orders?(?:\s+(?:dikhao|dikhaye|batao|bataiye|list))?|"
                            r"orders?\s+(?:dikhao|dikhaye|batao|bataiye|list))\b",
                            sanitized_text,
                            re.IGNORECASE,
                        )
                    ) or any(
                        p in sanitized_text
                        for p in (
                            "मेरे ऑर्डर",
                            "सभी ऑर्डर",
                            "सारे ऑर्डर",
                            "मेरे सभी ऑर्डर",
                            "मेरे सारे ऑर्डर",
                        )
                    )
                    has_explicit_malformed_ord = bool(
                        re.search(
                            r"\bORD-[A-Za-z0-9_-]+\b",
                            sanitized_text.translate(str.maketrans("०१२३४५६७८९", "0123456789")),
                            re.IGNORECASE,
                        )
                    )
                    is_unspecific_where = bool(
                        re.search(
                            r"^\s*(?:where\s+is\s+my\s+(?:order|refund|return|parcel|money)|"
                            r"what\s+happened\s+to\s+my\s+(?:order|refund|return)|"
                            r"(?:check\s+)?(?:my\s+)?(?:refund|return|order)\s+status|"
                            r"(?:(?:mera|meri|mere)\s+)?(?:order|refund|return|parcel|paisa|paise)\s+"
                            r"(?:kahan|kaha|kidhar|kab).*)\s*\??\s*$",
                            sanitized_text,
                            re.IGNORECASE,
                        )
                    ) or any(
                        p in sanitized_text
                        for p in (
                            "मेरा ऑर्डर कहाँ",
                            "मेरा रिफंड कहाँ",
                            "मेरा रिफंड कब",
                            "मेरे पैसे कब",
                            "पैसे कब",
                            "रिफंड कहाँ",
                            "मेरा रिटर्न कहाँ",
                        )
                    )
                    if (
                        not is_listing_query
                        and not is_unspecific_where
                        and not has_explicit_malformed_ord
                    ):
                        try:
                            with self.engine.connect() as ctx_conn:
                                prev_rows = ctx_conn.execute(
                                    select(messages.c.text)
                                    .where(messages.c.session_id == session_id)
                                    .order_by(messages.c.created_at.desc())
                                    .limit(6)
                                ).fetchall()
                                for (p_text,) in prev_rows:
                                    p_orders = extract_order_ids(p_text or "")
                                    if p_orders:
                                        extracted_order_id = p_orders[0].upper()
                                        break
                        except Exception:
                            pass

                if is_return_status_query:
                    intent_val = Intent.RETURN_REQUEST
                elif is_refund_status_query:
                    intent_val = Intent.REFUND_STATUS
                elif (
                    intent_val == Intent.RETURN_REQUEST
                    and sanitized_text
                    and re.search(
                        r"\b(?:paisa|paise|refund)\b.*\b(?:kab|kahan|kaha|aayenge|aayega|milega|milenge|status)\b",
                        sanitized_text,
                        re.IGNORECASE,
                    )
                    and not re.search(
                        r"\b(?:return|replace|exchange|samaan|saman|kurta|shirt|item)\b",
                        sanitized_text,
                        re.IGNORECASE,
                    )
                ):
                    intent_val = Intent.REFUND_STATUS
                    is_refund_status_query = True
                elif (
                    intent_val == Intent.RETURN_REQUEST
                    and sanitized_text
                    and re.search(
                        r"\b(?:return\s+status|status\s+of\s+(?:my\s+)?return|"
                        r"where\s+is\s+my\s+return|(?:mera\s+)?return\s+(?:kahan|kaha|kab|status)|"
                        r"return\s+ka\s+status)\b",
                        sanitized_text,
                        re.IGNORECASE,
                    )
                    and not re.search(
                        r"\b(?:want\s+to\s+return|return\s+it|return\s+this|return\s+karna|wapas|replace)\b",
                        sanitized_text,
                        re.IGNORECASE,
                    )
                ):
                    is_return_status_query = True

        # ── Stage 2: Workflow resolution ──────────────────────────────────────
        trace: dict[str, Any] = {"request_id": request_id, "intent": intent_val}
        cards_to_emit: list[dict[str, Any]] = []

        with write_tx(self.engine) as p_conn:
            AuditService.append(
                p_conn,
                AuditEvent(
                    event_id=f"AUD-{secrets.token_hex(8)}",
                    ts_utc=self.clock.now().isoformat(),
                    request_id=request_id,
                    session_id=session_id,
                    actor_type=ActorType.AGENT,
                    actor_id=customer_id,
                    event_type=EventType.MESSAGE_RECEIVED,
                    resource_type="pipeline",
                    resource_id="plan",
                    action="plan",
                    outcome=Outcome.SUCCESS,
                    policy_version=getattr(self.policy, "version", "v3"),
                    details={"stage": "plan", "intent": str(intent_val)},
                ),
            )

        if llm_down:
            # Fall back to guided mode menu with chips
            cards_to_emit.append(
                build_chips_card(
                    [
                        {"label": "Track Order", "action": "track_order"},
                        {"label": "Return Item", "action": "start_return"},
                        {"label": "Refund Status", "action": "refund_status"},
                        {"label": "Store Policy", "action": "policy_info"},
                        {"label": "Talk to Human", "action": "talk_to_human"},
                    ]
                )
            )
            verified_text = (
                "मैं अभी गाइडेड मोड में चल रहा हूँ। आज मैं आपकी किस प्रकार सहायता कर सकता हूँ? "
                "कृपया नीचे दिए गए विकल्पों में से एक चुनें:"
                if early_is_hi
                else (
                    "I'm currently running in guided mode. How can I help you today? "
                    "Please choose one of the options below:"
                )
            )
            # Yield message chunks and cards
            for chunk in verified_text.split():
                yield ("message.delta", {"delta": chunk + " "})
            for c in cards_to_emit:
                yield ("card", c)
            agent_msg_id = _generate_msg_id()
            yield ("final", {"message_id": agent_msg_id, "full_text": verified_text})

            # Persist messages
            self._persist_turn(
                session_id=session_id,
                customer_id=customer_id,
                client_msg_id=client_msg_id,
                user_text=sanitized_text or (action.name if action else ""),
                agent_msg_id=agent_msg_id,
                agent_text=verified_text,
                cards=cards_to_emit,
            )
            return

        if is_case_status_query and extracted_case_id:
            from sellershield_agent.cases.api import get_customer_case_view
            from sellershield_agent.errors import AppHTTPException
            from sellershield_agent.tools.base import ToolResult

            case_tool_res: ToolResult
            case_status_text = ""
            try:
                with self.engine.connect() as conn:
                    case_view = get_customer_case_view(conn, extracted_case_id, customer_id)
                case_tool_res = ToolResult(
                    ok=True,
                    data=case_view,
                    fact_ids=[f"case:{extracted_case_id}"],
                )
                case_status_text = str(case_view.get("status") or "open")
            except AppHTTPException:
                case_tool_res = ToolResult(
                    ok=False,
                    error_code=ToolErrorCode.not_found,
                )

            sheet = build_factsheet([case_tool_res], language="en", clock=self.clock)
            draft_text = (
                f"Case {extracted_case_id} status is {case_status_text}."
                if case_tool_res.ok
                else not_found(sheet, "en")
            )
            ver_res = verify(draft_text, sheet, "en")
            verified_text = draft_text if ver_res.ok else not_found(sheet, "en")

            for chunk in verified_text.split():
                yield ("message.delta", {"delta": chunk + " "})
            agent_msg_id = _generate_msg_id()
            yield ("final", {"message_id": agent_msg_id, "full_text": verified_text})
            self._persist_turn(
                session_id=session_id,
                customer_id=customer_id,
                client_msg_id=client_msg_id,
                user_text=sanitized_text or (action.name if action else ""),
                agent_msg_id=agent_msg_id,
                agent_text=verified_text,
                cards=[],
                request_id=request_id,
            )
            return

        # ── Escalation Triggers: Sentiment, Keywords, Human Request ─────────
        # Retrieve previous message sentiment from database
        prev_sentiment = "calm"
        try:
            with self.engine.connect() as conn:
                last_msg_row = conn.execute(
                    select(messages.c.text)
                    .where(messages.c.session_id == session_id, messages.c.role == "customer")
                    .order_by(messages.c.created_at.desc())
                ).fetchone()
                if last_msg_row and last_msg_row[0]:
                    if re.search(
                        r"\b(hate|terrible|furious|angry|awful|horrible|worst)\b",
                        str(last_msg_row[0]),
                        re.IGNORECASE,
                    ):
                        prev_sentiment = "angry"
        except Exception:
            pass

        curr_sentiment = str(getattr(understanding_res, "sentiment", "calm") or "calm").lower()
        if (
            curr_sentiment != "angry"
            and sanitized_text
            and re.search(
                r"\b(hate|terrible|furious|angry|awful|horrible|worst)\b",
                sanitized_text,
                re.IGNORECASE,
            )
        ):
            curr_sentiment = "angry"

        # Check for angry twice in a row: offer chip, no case
        if curr_sentiment == "angry" and prev_sentiment == "angry":
            with write_tx(self.engine) as tx:
                esc_chip_res = evaluate_escalation_triggers(
                    tx,
                    customer_id=customer_id,
                    message_text=sanitized_text,
                    session_id=session_id,
                    sentiment=curr_sentiment,
                    previous_sentiment=prev_sentiment,
                    clock=self.clock,
                    policy=self.policy,
                )
            if esc_chip_res.offer_human_chip:
                chip_card = build_chips_card(
                    [{"label": "Talk to Human", "action": "talk_to_human"}]
                )
                cards_to_emit.append(chip_card)
                verified_text = (
                    "मैं समझता हूँ कि आप परेशान हैं। यदि आप हमारी टीम से सीधे बात करना "
                    "चाहते हैं, तो कृपया नीचे टैप करें:"
                    if early_is_hi
                    else (
                        "I understand you are frustrated. If you would like to speak directly "
                        "with our team, please tap below:"
                    )
                )
                for chunk in verified_text.split():
                    yield ("message.delta", {"delta": chunk + " "})
                for c in cards_to_emit:
                    yield ("card", c)
                agent_msg_id = _generate_msg_id()
                yield ("final", {"message_id": agent_msg_id, "full_text": verified_text})
                self._persist_turn(
                    session_id=session_id,
                    customer_id=customer_id,
                    client_msg_id=client_msg_id,
                    user_text=sanitized_text or (action.name if action else ""),
                    agent_msg_id=agent_msg_id,
                    agent_text=verified_text,
                    cards=cards_to_emit,
                    request_id=request_id,
                )
                return

        # Check legal or threat words
        legal_word_match = None
        keywords = getattr(self.policy, "escalation_keywords", None) or DEFAULT_ESCALATION_KEYWORDS
        if sanitized_text:
            for kw in keywords:
                if re.search(rf"\b{re.escape(kw)}\b", sanitized_text, re.IGNORECASE):
                    legal_word_match = kw
                    break

        if intent_val == Intent.HUMAN_REQUEST or legal_word_match is not None:
            esc_res = None
            try:
                with write_tx(self.engine) as tx:
                    esc_res = evaluate_escalation_triggers(
                        tx,
                        customer_id=customer_id,
                        message_text=sanitized_text,
                        category="legal_threat" if legal_word_match else "human_request",
                        order_id=extracted_order_id,
                        session_id=session_id,
                        clock=self.clock,
                        policy=self.policy,
                    )
            except Exception as e:
                logger.error("escalation_case_creation_failed", error=str(e))
                esc_res = None

            if esc_res is not None and esc_res.case_id:
                sla_text = "within 2 hours" if esc_res.priority == "high" else "within 24 hours"
                sla_hi = "2 घंटे के भीतर" if esc_res.priority == "high" else "24 घंटे के भीतर"
                verified_text = (
                    f"हमने आपके लिए केस {esc_res.case_id} खोल दिया है। हमारी सहायता टीम का सदस्य "
                    f"आपके अनुरोध की समीक्षा करके {sla_hi} आपसे संपर्क करेगा।"
                    if early_is_hi
                    else (
                        f"We have opened case {esc_res.case_id} for you. A support team member "
                        f"will review your request and follow up {sla_text}."
                    )
                )
                case_card = build_case_created_card(
                    case_id=esc_res.case_id,
                    priority=esc_res.priority,
                    sla=sla_text,
                )
                cards_to_emit.append(case_card)
            else:
                verified_text = (
                    "हम अभी सहायता केस नहीं बना सके। कृपया थोड़ी देर बाद पुनः प्रयास करें।"
                    if early_is_hi
                    else (
                        "We could not create a support case at this moment. "
                        "Please try again later."
                    )
                )

            for chunk in verified_text.split():
                yield ("message.delta", {"delta": chunk + " "})
            for c in cards_to_emit:
                yield ("card", c)
            agent_msg_id = _generate_msg_id()
            yield ("final", {"message_id": agent_msg_id, "full_text": verified_text})

            self._persist_turn(
                session_id=session_id,
                customer_id=customer_id,
                client_msg_id=client_msg_id,
                user_text=sanitized_text or (action.name if action else ""),
                agent_msg_id=agent_msg_id,
                agent_text=verified_text,
                cards=cards_to_emit,
                request_id=request_id,
            )
            return

        # Check for non-supported / honest scopes
        if intent_val in (
            Intent.DAMAGED_ITEM,
            Intent.WRONG_ITEM,
        ):
            # Honest not-available template; record escalation trigger in audit
            with write_tx(self.engine) as conn:
                AuditService.append(
                    conn,
                    AuditEvent(
                        event_id=f"evt_{uuid.uuid4().hex[:12]}",
                        ts_utc=now_str,
                        request_id=request_id,
                        session_id=session_id,
                        actor_type=ActorType.AGENT,
                        event_type=EventType.TOOL_CALL,
                        action="escalation_trigger",
                        outcome=Outcome.DENIED,
                        details={
                            "intent": intent_val,
                            "note": "Feature not available in this build",
                        },
                    ),
                )

            verified_text = (
                "क्षतिग्रस्त या गलत आइटम के लिए केस बनाना इस संस्करण में उपलब्ध नहीं है।"
                if early_is_hi
                else (
                    "Reporting damaged or wrong items with case creation "
                    "is not available in this build."
                )
            )

            for chunk in verified_text.split():
                yield ("message.delta", {"delta": chunk + " "})
            agent_msg_id = _generate_msg_id()
            yield ("final", {"message_id": agent_msg_id, "full_text": verified_text})

            self._persist_turn(
                session_id=session_id,
                customer_id=customer_id,
                client_msg_id=client_msg_id,
                user_text=sanitized_text or (action.name if action else ""),
                agent_msg_id=agent_msg_id,
                agent_text=verified_text,
                cards=[],
                request_id=request_id,
            )
            return

        # Allowed tools per intent
        allowed_tools: list[str] = []
        if intent_val in (Intent.ORDER_STATUS, Intent.SHIPPING_STATUS):
            allowed_tools = ["get_order", "get_shipping_status", "list_my_orders"]
        elif intent_val in (Intent.REFUND_STATUS, Intent.REFUND_REQUEST):
            allowed_tools = ["get_refund_status", "get_order", "list_my_orders"]
        elif intent_val == Intent.RETURN_REQUEST:
            if is_return_status_query:
                allowed_tools = ["get_return_status", "get_order", "list_my_orders"]
            else:
                allowed_tools = [
                    "check_return_eligibility",
                    "get_order",
                    "list_my_orders",
                    "propose_return",
                ]
        elif intent_val == Intent.CANCELLATION:
            allowed_tools = ["get_order", "propose_cancellation", "list_my_orders"]
        elif intent_val == Intent.POLICY_QUESTION:
            allowed_tools = ["get_policy_summary"]

        # ── Stage 3: Resolver loop ────────────────────────────────────────────
        tool_status_events: list[dict[str, Any]] = []

        async def status_callback(tool_name: str, state: str) -> None:
            tool_status_events.append({"tool_name": tool_name, "status": state})

        u_res = locals().get("understanding_res")
        u_lang = getattr(u_res, "language", None)
        is_hi = bool(u_lang == Language.HI or early_is_hi)
        resp_lang = "hi" if is_hi else "en"
        slots: dict[str, Any] = {
            "order_id": extracted_order_id,
            "return_id": extracted_return_id,
            "refund_id": extracted_refund_id,
            "return_status_only": is_return_status_query,
            "user_text": sanitized_text,
            "evidence_ids": input_data.evidence_ids,
            "reason_code": getattr(u_res, "reason_code", None),
            "reason_text": getattr(u_res, "reason_text", None),
        }

        # If LLM resolution is mocked or guided action
        if is_guided_action or is_chip_status_query:
            pass  # guided action / chip tap calls tools directly via resolver
        elif call_llm_for_resolution is not None:
            await call_llm_for_resolution()

        tool_results = await self.resolver.execute(
            ctx=ctx,
            intent=str(intent_val),
            allowed_tools=allowed_tools,
            slots=slots,
            session_id=session_id,
            lang=resp_lang,
            status_callback=status_callback,
            trace=trace,
        )

        # Emit tool.status events to SSE stream
        for ts in tool_status_events:
            yield ("tool.status", ts)

        # Check client disconnection
        if is_disconnected and is_disconnected():
            logger.info("Client disconnected during tool resolution")
            return

        # Check if any tool result indicates policy ESCALATE
        escalate_tool_result = next(
            (
                r
                for r in tool_results
                if r.ok
                and isinstance(r.data, dict)
                and str(r.data.get("decision") or "").upper() == "ESCALATE"
            ),
            None,
        )
        if escalate_tool_result is not None:
            esc_res = None
            try:
                with write_tx(self.engine) as tx:
                    esc_res = evaluate_escalation_triggers(
                        tx,
                        customer_id=customer_id,
                        order_id=extracted_order_id,
                        category="policy_escalate",
                        session_id=session_id,
                        policy_decision="ESCALATE",
                        clock=self.clock,
                        policy=self.policy,
                    )
            except Exception as e:
                logger.error("escalate_tool_result_case_creation_failed", error=str(e))
                esc_res = None

            if esc_res is not None and esc_res.case_id:
                sla_text = "within 2 hours" if esc_res.priority == "high" else "within 24 hours"
                sla_hi = "2 घंटे के भीतर" if esc_res.priority == "high" else "24 घंटे के भीतर"
                verified_text = (
                    f"हमने आपके लिए केस {esc_res.case_id} खोल दिया है। हमारी सहायता टीम का सदस्य "
                    f"आपके अनुरोध की समीक्षा करके {sla_hi} आपसे संपर्क करेगा।"
                    if early_is_hi
                    else (
                        f"We have opened case {esc_res.case_id} for you. A support team member "
                        f"will review your request and follow up {sla_text}."
                    )
                )
                case_card = build_case_created_card(
                    case_id=esc_res.case_id,
                    priority=esc_res.priority,
                    sla=sla_text,
                )
                cards_to_emit.append(case_card)
            else:
                verified_text = (
                    "हम अभी सहायता केस नहीं बना सके। कृपया थोड़ी देर बाद पुनः प्रयास करें।"
                    if early_is_hi
                    else (
                        "We could not create a support case at this moment. "
                        "Please try again later."
                    )
                )

            for chunk in verified_text.split():
                yield ("message.delta", {"delta": chunk + " "})
            for c in cards_to_emit:
                yield ("card", c)
            agent_msg_id = _generate_msg_id()
            yield ("final", {"message_id": agent_msg_id, "full_text": verified_text})

            self._persist_turn(
                session_id=session_id,
                customer_id=customer_id,
                client_msg_id=client_msg_id,
                user_text=sanitized_text or (action.name if action else ""),
                agent_msg_id=agent_msg_id,
                agent_text=verified_text,
                cards=cards_to_emit,
                request_id=request_id,
            )
            return

        # ── Stage 4 & 5: FactSheet, Composition, and Verification ─────────────
        # Check if an order not found / not owned occurred
        order_not_found_detected = any(
            (not r.ok and r.error_code in (ToolErrorCode.not_found, ToolErrorCode.not_permitted))
            or (r.ok and isinstance(r.data, dict) and r.data.get("status") == "not_found")
            for r in tool_results
        )

        # Typos or malformed IDs in text: treat as not found rather than guessing orders
        sanitized_ascii_digits = sanitized_text.translate(
            str.maketrans("०१२३४५६७८९", "0123456789")
        )
        candidate_ord_tokens = re.findall(
            r"\bORD-[A-Za-z0-9_-]+\b", sanitized_ascii_digits, re.IGNORECASE
        )
        if any(
            not re.match(r"^ORD-[0-9]{3,6}$", tok, re.IGNORECASE) for tok in candidate_ord_tokens
        ):
            order_not_found_detected = True

        sheet = build_factsheet(tool_results, language=resp_lang, clock=self.clock)
        has_confirmation = any(
            r.ok and isinstance(r.data, dict) and r.data.get("type") == "confirmation"
            for r in tool_results
        )

        # Build candidate draft
        draft_text = ""
        if order_not_found_detected:
            draft_text = not_found(sheet, resp_lang)
        elif mock_draft_for_test is not None:
            # Test injection hook for verifying hallucination defense
            draft_text = mock_draft_for_test
        elif has_confirmation:
            draft_text = template_proposal_confirmation(sheet, resp_lang)
        else:
            # Deterministic template matching the intent
            if intent_val == Intent.SHIPPING_STATUS:
                if not extracted_order_id and sheet.orders:
                    draft_text = template_orders_list(sheet, resp_lang)
                else:
                    draft_text = template_shipping_status(sheet, resp_lang)
            elif intent_val == Intent.ORDER_STATUS:
                if sheet.orders and not extracted_order_id:
                    draft_text = template_orders_list(sheet, resp_lang)
                # Prefer shipping info with ETA if available
                elif sheet.eta_at_ist or sheet.carrier or "in transit" in (sheet.status or ""):
                    draft_text = template_shipping_status(sheet, resp_lang)
                else:
                    draft_text = template_order_status(sheet, resp_lang)
            elif intent_val in (Intent.REFUND_STATUS, Intent.REFUND_REQUEST):
                draft_text = template_refund_status(sheet, resp_lang)
            elif intent_val == Intent.CANCELLATION:
                if not extracted_order_id and sheet.orders:
                    draft_text = template_orders_list(sheet, resp_lang)
                else:
                    draft_text = template_cancellation_denied(sheet, resp_lang)
            elif intent_val == Intent.RETURN_REQUEST:
                if is_return_status_query:
                    draft_text = template_return_status(sheet, resp_lang)
                else:
                    missing_slot_result = next(
                        (
                            r
                            for r in tool_results
                            if isinstance(r.data, dict)
                            and ("missing_slot" in r.data or "missing_slots" in r.data)
                        ),
                        None,
                    )
                    if missing_slot_result:
                        ms = missing_slot_result.data.get("missing_slot") or (
                            missing_slot_result.data.get("missing_slots") or [None]
                        )[0]
                        oid = extracted_order_id or ("आपका ऑर्डर" if is_hi else "your order")
                        if ms == "evidence":
                            draft_text = (
                                f"ऑर्डर {oid} के लिए, क्षतिग्रस्त आइटम की समीक्षा हेतु कृपया "
                                "फोटो प्रमाण अपलोड करें।"
                                if is_hi
                                else (
                                    f"For order {oid}, damaged items require photo evidence upload "
                                    f"so our team can review your case."
                                )
                            )
                        elif ms == "refund_method":
                            draft_text = (
                                f"ऑर्डर {oid} के लिए, कैश ऑन डिलीवरी रिटर्न हेतु कृपया रिफंड माध्यम "
                                "चुनें (स्टोर क्रेडिट या UPI)।"
                                if is_hi
                                else (
                                    f"For order {oid}, cash on delivery returns require a "
                                    f"refund payout method choice (store credit or UPI transfer)."
                                )
                            )
                        else:
                            draft_text = template_return_preview(sheet, resp_lang)
                    else:
                        draft_text = template_return_preview(sheet, resp_lang)
            elif intent_val == Intent.POLICY_QUESTION:
                draft_text = template_policy_summary(sheet, resp_lang)
            else:
                draft_text = guided_mode_menu(sheet, resp_lang)

        # Verify draft against FactSheet
        verification_result = verify(draft_text, sheet, resp_lang)
        if not verification_result.ok:
            logger.warning(
                "Verifier violation in draft, falling back to template",
                violations=verification_result.violations,
            )
            # Log verifier failure in audit
            with write_tx(self.engine) as conn:
                AuditService.append(
                    conn,
                    AuditEvent(
                        event_id=f"evt_{uuid.uuid4().hex[:12]}",
                        ts_utc=now_str,
                        request_id=request_id,
                        session_id=session_id,
                        actor_type=ActorType.AGENT,
                        event_type=EventType.VERIFIER_FAILURE,
                        action="verify_draft",
                        outcome=Outcome.FAILED,
                        details={"violations": verification_result.violations},
                    ),
                )

            # Fall back directly to safe template
            if order_not_found_detected:
                verified_text = not_found(sheet, resp_lang)
            elif has_confirmation:
                verified_text = template_proposal_confirmation(sheet, resp_lang)
            elif intent_val in (Intent.ORDER_STATUS, Intent.SHIPPING_STATUS):
                if not extracted_order_id and sheet.orders:
                    verified_text = template_orders_list(sheet, resp_lang)
                else:
                    verified_text = template_shipping_status(sheet, resp_lang)
            elif intent_val in (Intent.REFUND_STATUS, Intent.REFUND_REQUEST):
                verified_text = template_refund_status(sheet, resp_lang)
            elif intent_val == Intent.CANCELLATION:
                if not extracted_order_id and sheet.orders:
                    verified_text = template_orders_list(sheet, resp_lang)
                else:
                    verified_text = template_cancellation_denied(sheet, resp_lang)
            elif intent_val == Intent.RETURN_REQUEST and is_return_status_query:
                verified_text = template_return_status(sheet, resp_lang)
            else:
                verified_text = template_policy_summary(sheet, resp_lang)
        else:
            verified_text = draft_text

        is_verified = (
            verification_result is None or bool(getattr(verification_result, "valid", True))
        )
        with write_tx(self.engine) as v_conn:
            AuditService.append(
                v_conn,
                AuditEvent(
                    event_id=f"AUD-{secrets.token_hex(8)}",
                    ts_utc=self.clock.now().isoformat(),
                    request_id=request_id,
                    session_id=session_id,
                    actor_type=ActorType.AGENT,
                    actor_id=customer_id,
                    event_type=EventType.MESSAGE_RECEIVED,
                    resource_type="verifier",
                    resource_id="outcome",
                    action="verifier_outcome",
                    outcome=Outcome.SUCCESS if is_verified else Outcome.FAILED,
                    policy_version=getattr(self.policy, "version", "v3"),
                    details={
                        "stage": "verifier_outcome",
                        "valid": is_verified,
                    },
                ),
            )

        # ── Stage 6: Stream and Persist ───────────────────────────────────────
        # Emit message.delta in word chunks
        words = verified_text.split()
        chunk_size = 3
        for i in range(0, len(words), chunk_size):
            chunk = " ".join(words[i : i + chunk_size])
            if i + chunk_size < len(words):
                chunk += " "
            yield ("message.delta", {"delta": chunk})
            await asyncio.sleep(0.01)

        suppress_generic_orders = bool(sheet.refunds or sheet.returns)
        # Build cards
        for r in tool_results:
            if r.ok and isinstance(r.data, dict):
                if r.data.get("type") == "confirmation":
                    cards_to_emit.append(build_confirmation_card(r.data))
                elif (
                    not has_confirmation
                    and "decision" in r.data
                    and "rule_results" in r.data
                    and "total_refund_paise" in r.data
                ):
                    cards_to_emit.append(build_return_preview_card(r.data))
                elif "carrier" in r.data and ("timeline" in r.data or "events" in r.data):
                    cards_to_emit.append(build_shipment_timeline_card(r.data))
                elif "total_paise" in r.data and "items" in r.data:
                    cards_to_emit.append(build_order_summary_card(r.data))
                elif (
                    "orders" in r.data
                    and isinstance(r.data["orders"], list)
                    and not suppress_generic_orders
                ):
                    for o in r.data["orders"][:3]:
                        if isinstance(o, dict) and "order_id" in o:
                            cards_to_emit.append(build_order_summary_card(o))

        if not extracted_order_id and sheet.orders and not suppress_generic_orders:
            order_chips = [
                {
                    "label": str(o.get("order_id", "")),
                    "action": "track_order",
                    "order_id": str(o.get("order_id", "")),
                }
                for o in sheet.orders[:6]
                if o.get("order_id")
            ]
            if order_chips:
                cards_to_emit.append(build_chips_card(order_chips))

        for c in cards_to_emit:
            yield ("card", c)

        agent_msg_id = _generate_msg_id()
        yield ("final", {"message_id": agent_msg_id, "full_text": verified_text})

        # Persist conversation turn
        self._persist_turn(
            session_id=session_id,
            customer_id=customer_id,
            client_msg_id=client_msg_id,
            user_text=sanitized_text or (action.name if action else ""),
            agent_msg_id=agent_msg_id,
            agent_text=verified_text,
            cards=cards_to_emit,
            tool_status=tool_status_events,
            request_id=request_id,
        )

    def _persist_turn(
        self,
        session_id: str,
        customer_id: str,
        client_msg_id: str,
        user_text: str,
        agent_msg_id: str,
        agent_text: str,
        cards: list[dict[str, Any]],
        tool_status: list[dict[str, Any]] | None = None,
        request_id: str | None = None,
    ) -> None:
        """Persist user message, agent message, and update session activity."""
        now_str = self.clock.now().isoformat()
        with write_tx(self.engine) as conn:
            # 1. User message (if not already recorded)
            existing_user = conn.execute(
                select(messages.c.id).where(
                    messages.c.session_id == session_id,
                    messages.c.client_msg_id == client_msg_id,
                )
            ).fetchone()
            if existing_user is None:
                user_msg_id = _generate_msg_id()
                conn.execute(
                    messages.insert().values(
                        id=user_msg_id,
                        session_id=session_id,
                        role="customer",
                        text=user_text,
                        cards_json=None,
                        client_msg_id=client_msg_id,
                        created_at=now_str,
                    )
                )

            # 2. Agent message (if not already recorded)
            agent_msg_client_id = f"{client_msg_id}_reply"
            existing_agent = conn.execute(
                select(messages.c.id).where(
                    messages.c.session_id == session_id,
                    messages.c.client_msg_id == agent_msg_client_id,
                )
            ).fetchone()
            if existing_agent is None:
                stored_payload = {"cards": cards, "tool_status": tool_status or []}
                cards_json = json.dumps(stored_payload)
                conn.execute(
                    messages.insert().values(
                        id=agent_msg_id,
                        session_id=session_id,
                        role="agent",
                        text=agent_text,
                        cards_json=cards_json,
                        client_msg_id=agent_msg_client_id,
                        created_at=now_str,
                    )
                )

            # 3. Update session
            conn.execute(
                update(sessions).where(sessions.c.id == session_id).values(last_active_at=now_str)
            )

            # 4. Audit final reply
            AuditService.append(
                conn,
                AuditEvent(
                    event_id=f"AUD-{secrets.token_hex(8)}",
                    ts_utc=now_str,
                    request_id=request_id,
                    session_id=session_id,
                    actor_type=ActorType.AGENT,
                    actor_id=customer_id,
                    event_type=EventType.MESSAGE_RECEIVED,
                    resource_type="messages",
                    resource_id=agent_msg_id,
                    action="final_reply",
                    outcome=Outcome.SUCCESS,
                    policy_version=None,
                    details={"stage": "final_reply", "client_msg_id": client_msg_id},
                ),
            )
