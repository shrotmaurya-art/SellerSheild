"""
Mailbox Simulator (spec section 7.3, 6, 16)

Channels: email and whatsapp_sim.
Endpoints:
- GET /mail/inbox
- GET /mail/threads/{id}
- POST /mail/send
- POST /mail/{id}/read
(Mounted on both /mail and /api/v1/mail)

Rules:
- verified_sender is stamped from logged-in user.
- Every outbound message is marked SIMULATED.
- Inbound messages write mail.inbound into events_outbox.
- Strict ownership checks on customer access (uniform 404).
- Fault switches: mail.down (503), mail.fail_once (fails once then clears).
"""

import json
import logging
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

import db
from app.clock import Clock
from app.deps import require_auth
from app.errors import NotFoundException, UpstreamUnavailableException, ValidationErrorException
from app.faults import is_fault_enabled, set_fault
from app.idempotency import (
    check_idempotency_replay,
    compute_payload_hash,
    require_idempotency_key,
    save_idempotency_record,
)
from app.models import EventsOutbox, MailMessage
from app.schemas import (
    MailInboxResponse,
    MailMessageResponse,
    MailSendRequest,
    MailThreadResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Mailbox"])


def _check_mail_faults(session: Optional[Session] = None):
    """Enforce mail simulator fault switches."""
    if is_fault_enabled("mail.down", session=session):
        raise UpstreamUnavailableException(message="Mail service unavailable (mail.down)")
    if is_fault_enabled("mail.fail_once", session=session):
        set_fault("mail.fail_once", False, session=session)
        if session is not None:
            session.commit()
        raise UpstreamUnavailableException(
            message="Mail delivery temporarily failed, please retry (mail.fail_once)"
        )


def _to_message_response(msg: MailMessage) -> MailMessageResponse:
    return MailMessageResponse(
        message_id=msg.message_id,
        thread_id=msg.thread_id,
        direction=msg.direction,
        channel=msg.channel,
        from_addr=msg.from_addr,
        to_addr=msg.to_addr,
        user_id=msg.user_id,
        subject=msg.subject,
        body_text=msg.body_text,
        verified_sender=msg.verified_sender,
        status=msg.status,
        created_at=msg.created_at,
        read_at=msg.read_at,
        simulated=True,
    )


@router.get(
    "/mail/inbox",
    response_model=MailInboxResponse,
    summary="Get mailbox messages (customer sees own, seller sees store inbox)",
)
@router.get(
    "/api/v1/mail/inbox",
    response_model=MailInboxResponse,
    include_in_schema=False,
)
def get_mailbox_inbox(
    principal: dict[str, Any] = Depends(require_auth),
) -> MailInboxResponse:
    _check_mail_faults()
    role = principal.get("role")
    user_id = principal.get("user_id") or principal.get("sub")
    customer_id = principal.get("customer_id")
    email = principal.get("email")

    with Session(db.engine) as session:
        if role == "customer":
            # Customer only sees messages directed to or from their account
            conditions = []
            if customer_id:
                conditions.append(MailMessage.user_id == customer_id)
            if user_id:
                conditions.append(MailMessage.user_id == user_id)
            if email:
                conditions.append(MailMessage.to_addr == email)
                conditions.append(MailMessage.from_addr == email)

            if not conditions:
                messages = []
            else:
                messages = (
                    session.query(MailMessage)
                    .filter(or_(*conditions))
                    .order_by(MailMessage.created_at.desc())
                    .all()
                )
        else:
            # Seller / staff / service sees all messages (or store inbox)
            messages = session.query(MailMessage).order_by(MailMessage.created_at.desc()).all()

        resp_list = [_to_message_response(m) for m in messages]
        return MailInboxResponse(messages=resp_list, total=len(resp_list))


@router.get(
    "/mail/threads/{thread_id}",
    response_model=MailThreadResponse,
    summary="Get all messages in a mail thread",
)
@router.get(
    "/api/v1/mail/threads/{thread_id}",
    response_model=MailThreadResponse,
    include_in_schema=False,
)
def get_mail_thread(
    thread_id: str,
    principal: dict[str, Any] = Depends(require_auth),
) -> MailThreadResponse:
    _check_mail_faults()
    role = principal.get("role")
    user_id = principal.get("user_id") or principal.get("sub")
    customer_id = principal.get("customer_id")
    email = principal.get("email")

    with Session(db.engine) as session:
        messages = (
            session.query(MailMessage)
            .filter(MailMessage.thread_id == thread_id)
            .order_by(MailMessage.created_at.asc())
            .all()
        )
        if not messages:
            raise NotFoundException(message="Thread not found")

        # Ownership enforcement for customers
        if role == "customer":
            owned = any(
                (m.user_id in (customer_id, user_id))
                or (email and (m.to_addr == email or m.from_addr == email))
                for m in messages
            )
            if not owned:
                # Golden rule 3: uniform 404 for unowned resources
                raise NotFoundException(message="Thread not found")

        resp_list = [_to_message_response(m) for m in messages]
        return MailThreadResponse(thread_id=thread_id, messages=resp_list)


@router.post(
    "/mail/send",
    response_model=MailMessageResponse,
    summary="Send a simulated mail message (inbound from customer or outbound from store)",
)
@router.post(
    "/api/v1/mail/send",
    response_model=MailMessageResponse,
    include_in_schema=False,
)
async def send_mail_message(
    body: MailSendRequest,
    request: Request,
    idempotency_key: str = Depends(require_idempotency_key),
    principal: dict[str, Any] = Depends(require_auth),
) -> JSONResponse:
    if body.channel not in ["email", "whatsapp_sim"]:
        raise ValidationErrorException(message="Channel must be 'email' or 'whatsapp_sim'")

    raw_body = await request.body()
    payload_hash = compute_payload_hash(raw_body)
    actor = principal.get("sub") or principal.get("client_id") or "unknown"

    with Session(db.engine) as session:
        _check_mail_faults(session=session)

        # 1. Idempotency replay check
        replay = check_idempotency_replay(session, idempotency_key, payload_hash)
        if replay is not None:
            sc, data = replay
            return JSONResponse(status_code=sc, content=data)

        # 2. Determine sender and direction
        role = principal.get("role")
        user_id = principal.get("user_id") or principal.get("sub")
        customer_id = principal.get("customer_id")
        user_email = principal.get("email")

        if role == "customer":
            direction = "inbound"
            from_addr = user_email or f"{user_id}@customer.nestkart.test"
            record_user_id = customer_id or user_id
        else:
            direction = body.direction or "outbound"
            from_addr = user_email or "support@nestkart.test"
            record_user_id = body.thread_id.replace("THD-", "") if body.thread_id else None

        now = Clock.now()
        naive_now = now.replace(tzinfo=None) if hasattr(now, "tzinfo") else now

        thread_id = body.thread_id or f"THD-{secrets.token_hex(4).upper()}"
        message_id = f"MSG-{secrets.token_hex(4).upper()}"

        message = MailMessage(
            message_id=message_id,
            thread_id=thread_id,
            direction=direction,
            channel=body.channel,
            from_addr=from_addr,
            to_addr=body.to_addr,
            user_id=record_user_id,
            subject=body.subject,
            body_text=body.body_text,
            verified_sender=True,  # stamped from logged-in user
            status="delivered",
            created_at=naive_now,
            read_at=None,
        )
        session.add(message)

        # Inbound messages write mail.inbound into events_outbox
        if direction == "inbound":
            event_id = f"evt_mail_{message_id}"
            payload = {
                "event_id": event_id,
                "type": "mail.inbound",
                "message_id": message_id,
                "thread_id": thread_id,
                "channel": body.channel,
                "from_addr": from_addr,
                "to_addr": body.to_addr,
                "user_id": record_user_id,
                "subject": body.subject,
                "body_text": body.body_text,
                "verified_sender": True,
                "resource_version": 1,
                "timestamp": now.isoformat(),
            }
            outbox_entry = EventsOutbox(
                event_id=event_id,
                type="mail.inbound",
                payload_json=json.dumps(payload),
                created_at=naive_now,
                delivered_at=None,
                attempts=0,
                status="pending",
                next_retry_at=naive_now,
                last_error=None,
            )
            session.add(outbox_entry)
            logger.info(f"Emitted mail.inbound event {event_id} for message {message_id}")

        session.flush()

        resp_model = _to_message_response(message)
        resp_data = resp_model.model_dump(mode="json")

        save_idempotency_record(
            session=session,
            key=idempotency_key,
            payload_hash=payload_hash,
            status_code=200,
            response_data=resp_data,
            client_id=actor,
        )
        session.commit()

        return JSONResponse(status_code=200, content=resp_data)


@router.post(
    "/mail/{id}/read",
    response_model=MailMessageResponse,
    summary="Mark a mail message as read",
)
@router.post(
    "/api/v1/mail/{id}/read",
    response_model=MailMessageResponse,
    include_in_schema=False,
)
def mark_mail_as_read(
    id: str,
    principal: dict[str, Any] = Depends(require_auth),
) -> MailMessageResponse:
    _check_mail_faults()
    role = principal.get("role")
    user_id = principal.get("user_id") or principal.get("sub")
    customer_id = principal.get("customer_id")
    email = principal.get("email")

    with Session(db.engine) as session:
        message = session.get(MailMessage, id)
        if not message:
            raise NotFoundException(message="Message not found")

        if role == "customer":
            # Ownership check: must belong to the customer
            owned = message.user_id in (customer_id, user_id) or (
                email and (message.to_addr == email or message.from_addr == email)
            )
            if not owned:
                raise NotFoundException(message="Message not found")

        now = Clock.now()
        message.read_at = now.replace(tzinfo=None) if hasattr(now, "tzinfo") else now
        session.commit()
        session.refresh(message)
        return _to_message_response(message)
