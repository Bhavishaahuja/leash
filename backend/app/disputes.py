# Disputes: find PayPal disputes on our captured orders, build the evidence packet from Leash's
# own records, and file it with PayPal. Merchant only, all of it.
#
#   GET  /disputes                         the dashboard list
#   POST /disputes/sync                    look up disputes by capture id and save them
#   GET  /disputes/{id}/evidence           the packet plus a Claude-drafted summary. no PayPal write
#   GET  /disputes/{id}/evidence.pdf       the PDF submit sends, to download. no PayPal write
#   POST /disputes/{id}/submit-evidence    PDF of the packet + the summary as notes, once only
#
# PayPal rules, from the Disputes v1 schema, the usage guide and spike 3 (spikes/README.md):
#   - evidence is only accepted while status is WAITING_FOR_SELLER_RESPONSE, and only when show
#     dispute details has a provide-evidence link. no link, no evidence, whatever the status says
#   - send the evidence_type PayPal asked for (evidences[] with source REQUESTED_FROM_SELLER),
#     anything else gets EVIDENCE_TYPE_IS_NOT_ALLOWED. nothing asked for means OTHER
#   - PROOF_OF_FULFILLMENT needs carrier + tracking number, PROOF_OF_REFUND needs refund ids.
#     Leash has neither, and making them up would be fake evidence, so those are refused
#   - notes are 1 to 2000 characters
#
# The packet only holds what Leash itself recorded. The summary is a draft for the merchant to
# read first: GET .../evidence saves it, and submit sends exactly that saved text.

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.app import evidence_pdf
from backend.app.auth import require_merchant
from backend.app.config import STORE_NAME
from backend.app.intent.rules import as_utc
from backend.app.models import Dispute, Order, OrderStatus, ReviewState
from backend.app.orders import (
    CLAIM_EXPIRES_AFTER,
    PAYPAL_FAILURES,
    Claude,
    DbSession,
    PayPal,
    paypal_error_response,
    utc_now,
)
from backend.app.prompts import dispute as dispute_prompt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/disputes", tags=["disputes"], dependencies=[Depends(require_merchant)])

WAITING_FOR_SELLER = "WAITING_FOR_SELLER_RESPONSE"
# the schema's prose calls it provide-evidence, spike 3 looked for provide_evidence. PayPal's
# literal rel wasn't in anything we could read, so either spelling counts
PROVIDE_EVIDENCE_RELS = ["provide-evidence", "provide_evidence"]
REQUESTED_FROM_SELLER = "REQUESTED_FROM_SELLER"
FALLBACK_EVIDENCE_TYPE = "OTHER"

# types PayPal won't take without evidence_info we don't have. never invent these
NEEDS_INFO_WE_DONT_HAVE = {
    "PROOF_OF_FULFILLMENT": "a carrier and tracking number",
    "PROOF_OF_REFUND": "a refund ID",
    "PROOF_OF_RETURN": "return tracking",
}

NOTES_MAX_CHARS = 2000
# room left over for the OTHER explanation in front of it
SUMMARY_MAX_CHARS = 1500
OTHER_EXPLANATION = (
    "PayPal didn't request a specific evidence type for this dispute, so this is filed as "
    "OTHER: the buyer's mandate and the merchant's order records are attached. "
)

# a summary is a few sentences
SUMMARY_MAX_TOKENS = 2000
SUMMARY_TIMEOUT_SECONDS = 30
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class SummaryFailed(Exception):
    pass


# ---- shapes ----


class DisputeOut(BaseModel):
    id: int
    paypal_dispute_id: str
    order_id: int
    reason: str | None
    status: str | None
    evidence_submitted: bool
    evidence_submitted_at: datetime | None
    evidence_type: str | None
    # a summary has been drafted, so submit can go
    has_summary: bool
    # as of the last sync. the dashboard counts down to it
    seller_response_due_at: datetime | None
    created_at: datetime


class SyncError(BaseModel):
    order_id: int
    message: str


class SyncResult(BaseModel):
    orders_checked: int
    disputes_found: int
    created: int
    updated: int
    # PayPal failing on one order doesn't stop the rest
    errors: list[SyncError]


class EvidenceOut(BaseModel):
    dispute: DisputeOut
    packet: dict
    summary: str


def dispute_out(dispute):
    return DisputeOut(
        id=dispute.id,
        paypal_dispute_id=dispute.paypal_dispute_id,
        order_id=dispute.order_id,
        reason=dispute.reason,
        status=dispute.status,
        evidence_submitted=dispute.evidence_submitted_at is not None,
        evidence_submitted_at=dispute.evidence_submitted_at,
        evidence_type=dispute.evidence_type,
        has_summary=bool(dispute.evidence_summary),
        seller_response_due_at=dispute.seller_response_due_at,
        created_at=dispute.created_at,
    )


# ---- PayPal's side ----


def has_provide_evidence_link(details):
    for link in details.get("links") or []:
        if link.get("rel") in PROVIDE_EVIDENCE_RELS:
            return True
    return False


def evidence_blocker(details):
    # why PayPal won't take evidence right now, or None if it will. both have to hold
    problems = []
    status = details.get("status")
    if status != WAITING_FOR_SELLER:
        problems.append(
            "dispute status is " + str(status) + ", evidence needs " + WAITING_FOR_SELLER
        )
    if not has_provide_evidence_link(details):
        problems.append("PayPal's dispute details have no provide-evidence link")
    if not problems:
        return None
    return "PayPal won't take evidence on this dispute right now: " + "; ".join(problems)


def requested_evidence_types(details):
    requested = []
    for evidence in details.get("evidences") or []:
        if evidence.get("source") != REQUESTED_FROM_SELLER:
            continue
        evidence_type = evidence.get("evidence_type")
        if evidence_type and evidence_type not in requested:
            requested.append(evidence_type)
    return requested


def choose_evidence_type(details):
    # returns (evidence_type, why_not). evidence_type is None when every requested type needs
    # something Leash doesn't have
    requested = requested_evidence_types(details)
    if not requested:
        return FALLBACK_EVIDENCE_TYPE, None

    for evidence_type in requested:
        if evidence_type not in NEEDS_INFO_WE_DONT_HAVE:
            return evidence_type, None

    needs = []
    for evidence_type in requested:
        needs.append(evidence_type + " needs " + NEEDS_INFO_WE_DONT_HAVE[evidence_type])
    return None, (
        "PayPal only requested evidence Leash doesn't hold (" + "; ".join(needs)
        + "). Answer this one in the PayPal Resolution Center instead"
    )


def dispute_matches_capture(details, capture_id):
    # the list filter's target isn't spelled out in the schema, so check the details. spike 3
    # confirmed seller_transaction_id is our capture id (the buyer side has a different id)
    for transaction in details.get("disputed_transactions") or []:
        if transaction.get("seller_transaction_id") == capture_id:
            return True
    return False


# ---- the packet ----


def iso(moment):
    if moment is None:
        return None
    return as_utc(moment).isoformat()


def money(value):
    if value is None:
        return None
    return str(value)


def decided_approval(order):
    # the human's tap, newest first. an approval with no action was only opened
    for approval in reversed(order.approvals):
        if approval.action is not None:
            return approval
    return None


def decision_record(order, check):
    if order.review_state == ReviewState.auto_approved:
        return {
            "released_by": "automatic capture",
            "review_state": order.review_state.value,
            "action": "capture",
            "decision_ms": None,
            # Leash captures right after the check passes, so the check's time is the decision's.
            # PayPal's own capture record has the exact second
            "decided_at": iso(check.created_at) if check else None,
        }

    approval = decided_approval(order)
    review_state = order.review_state.value if order.review_state else None
    if approval is None:
        return {
            "released_by": None,
            "review_state": review_state,
            "action": None,
            "decision_ms": None,
            "decided_at": None,
        }
    return {
        "released_by": "human review",
        "review_state": review_state,
        "action": approval.action.value,
        # the server's own clock, review page opened to tap
        "decision_ms": approval.decision_ms,
        "opened_at": iso(approval.opened_at),
        "decided_at": iso(approval.decided_at),
    }


def build_evidence_packet(dispute):
    # only Leash's own records, so every line in it can be shown to come from somewhere.
    # buyer_label stays out: it's our nickname for the shopper, not evidence
    order = dispute.order
    mandate = order.mandate
    check = order.intent_check

    intent_check = None
    if check is not None:
        intent_check = {
            "verdict": check.verdict.value,
            "risk_score": check.risk_score,
            "reason": check.reason,
            "mismatched_fields": check.mismatched_fields,
            "checked_by": check.checked_by.value,
            "checked_at": iso(check.created_at),
            "watch_mode": order.watch_mode.value,
        }

    return {
        "dispute": {
            "paypal_dispute_id": dispute.paypal_dispute_id,
            "reason": dispute.reason,
            "status": dispute.status,
        },
        "mandate": {
            "id": mandate.id,
            "raw_request": mandate.raw_request,
            "rules": mandate.rules,
            "max_price": money(mandate.max_price),
            "max_price_inclusive": mandate.max_price_inclusive,
            "merchant": mandate.merchant,
            "expires_at": iso(mandate.expires_at),
            "created_at": iso(mandate.created_at),
        },
        "order": {
            "id": order.id,
            # null means our own store, see Order.merchant
            "store": order.merchant or STORE_NAME,
            "items": order.items,
            "total": money(order.total),
            "shipping_amount": money(order.shipping_amount),
            "currency": order.currency,
            "status": order.status.value,
            "paypal_order_id": order.paypal_order_id,
            "capture_id": order.capture_id,
            "created_at": iso(order.created_at),
        },
        "intent_check": intent_check,
        "decision": decision_record(order, check),
    }


def packet_lines(packet):
    # the PDF's text. plain "label: value" lines, in the same order as the summary
    lines = [
        "Leash proof of intent: " + STORE_NAME,
        "Dispute " + str(packet["dispute"]["paypal_dispute_id"]) + ", reason "
        + str(packet["dispute"]["reason"]),
        "",
        "1. What the buyer authorized (the mandate, set before the order)",
    ]
    mandate = packet["mandate"]
    lines.append("  Buyer's words: \"" + str(mandate["raw_request"]) + "\"")
    lines.append("  Price cap: " + str(mandate["max_price"])
                 + (" (up to)" if mandate["max_price_inclusive"] else " (under)"))
    lines.append("  Store: " + str(mandate["merchant"]))
    lines.append("  Expires: " + str(mandate["expires_at"]))
    lines.append("  Created: " + str(mandate["created_at"]))
    lines.append("  Parsed rules:")
    for name, value in (mandate["rules"] or {}).items():
        lines.append("    " + str(name) + ": " + str(value))

    order = packet["order"]
    lines.append("")
    lines.append("2. What was ordered")
    lines.append("  Order " + str(order["id"]) + " at " + str(order["store"]) + ", placed "
                 + str(order["created_at"]))
    for item in order["items"] or []:
        parts = [str(item.get("name"))]
        if item.get("color"):
            parts.append(str(item["color"]))
        if item.get("size"):
            parts.append("size " + str(item["size"]))
        lines.append("  " + str(item.get("quantity")) + " x " + ", ".join(parts) + " at "
                     + str(item.get("unit_price")))
    lines.append("  Shipping: " + str(order["shipping_amount"]))
    lines.append("  Total: " + str(order["total"]) + " " + str(order["currency"]))
    lines.append("  PayPal order " + str(order["paypal_order_id"]) + ", capture "
                 + str(order["capture_id"]))

    check = packet["intent_check"]
    lines.append("")
    lines.append("3. Check against the mandate (before any money was taken)")
    if check is None:
        lines.append("  No check result on record")
    else:
        lines.append("  Verdict: " + str(check["verdict"]) + ", risk score "
                     + str(check["risk_score"]) + " of 100, by " + str(check["checked_by"]))
        lines.append("  Reason: " + str(check["reason"]))
        lines.append("  Rubber-stamp watch: " + str(check["watch_mode"]))
        lines.append("  Checked at: " + str(check["checked_at"]))

    decision = packet["decision"]
    lines.append("")
    lines.append("4. Who released the money")
    lines.append("  " + str(decision["released_by"]) + ", review state "
                 + str(decision["review_state"]))
    if decision.get("decision_ms") is not None:
        lines.append("  Time on the review page: " + str(decision["decision_ms"]) + " ms")
    if decision.get("opened_at"):
        lines.append("  Review page opened: " + str(decision["opened_at"]))
    lines.append("  Decided at: " + str(decision["decided_at"]))
    lines.append("")
    lines.append("All times UTC. Generated by Leash from the merchant's own records.")
    return lines


# ---- the summary ----


def draft_summary(client, packet):
    # one Claude call. same call shape as the intent check, see intent/ai_check.py
    limited_client = client.with_options(timeout=SUMMARY_TIMEOUT_SECONDS, max_retries=0)
    try:
        response = limited_client.beta.messages.create(
            model=dispute_prompt.MODEL,
            max_tokens=SUMMARY_MAX_TOKENS,
            system=dispute_prompt.SYSTEM_PROMPT,
            output_config={"effort": dispute_prompt.EFFORT},
            betas=[FALLBACK_BETA],
            fallbacks="default",
            messages=[{"role": "user", "content": dispute_prompt.build_user_message(packet)}],
        )
    except Exception as error:
        # a missing key is a TypeError, not an APIError. either way: no summary, try again
        raise SummaryFailed("Claude call failed") from error

    if response.stop_reason == "refusal":
        raise SummaryFailed("Claude declined to write the summary")
    if response.stop_reason == "max_tokens":
        raise SummaryFailed("Summary was cut off")

    pieces = []
    for block in response.content:
        if block.type == "text":
            pieces.append(block.text)
    summary = clean_summary(" ".join(pieces))

    # the prompt says to translate our field names, this makes sure. a reviewer can't do anything
    # with "watch_mode", so a draft that leaks one is a failed draft, the merchant just retries
    leaked = leaked_field_names(summary, packet)
    if leaked:
        raise SummaryFailed("Summary used internal field names: " + ", ".join(leaked))
    return summary


def packet_field_names(value, found=None):
    # every snake_case key in the packet. one-word keys (verdict, reason, total) read fine as
    # English, so only the ones with an underscore count
    if found is None:
        found = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            if "_" in key:
                found.add(key)
            packet_field_names(inner, found)
    elif isinstance(value, list):
        for inner in value:
            packet_field_names(inner, found)
    return found


def leaked_field_names(summary, packet):
    lowered = summary.lower()
    leaked = []
    for name in sorted(packet_field_names(packet)):
        if name in lowered:
            leaked.append(name)
    return leaked


def clean_summary(text):
    # one paragraph, no em dashes (it's copy a person reads), and inside the notes limit
    text = " ".join(text.split())
    text = text.replace(" \N{EM DASH} ", ", ").replace("\N{EM DASH}", ", ")
    if not text:
        raise SummaryFailed("Summary came back empty")
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."
    return text


def build_notes(summary, evidence_type, requested_types):
    notes = summary
    if evidence_type == FALLBACK_EVIDENCE_TYPE and not requested_types:
        notes = OTHER_EXPLANATION + summary
    # can't happen with the caps above, PayPal would 400 if it did
    return notes[:NOTES_MAX_CHARS]


# ---- rows ----


def load_dispute(session, dispute_id, lock=False):
    query = select(Dispute).where(Dispute.id == dispute_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    dispute = session.scalars(query).first()
    if dispute is None:
        raise HTTPException(status_code=404, detail="Dispute " + str(dispute_id) + " not found")
    return dispute


def conflict(session, message, **extra):
    detail = {"message": message}
    for name, value in extra.items():
        detail[name] = value
    session.rollback()
    return HTTPException(status_code=409, detail=detail)


def claim_for_submit(session, dispute_id):
    # lock, check, mark it taken, commit, like claim_order. the claim is what stops a second
    # submit while the first is still talking to PayPal
    dispute = load_dispute(session, dispute_id, lock=True)
    if dispute.evidence_submitted_at is not None:
        raise conflict(
            session,
            "Evidence was already submitted for this dispute",
            evidence_submitted_at=iso(dispute.evidence_submitted_at),
        )
    if dispute.evidence_claimed_at is not None:
        if utc_now() - as_utc(dispute.evidence_claimed_at) < CLAIM_EXPIRES_AFTER:
            raise conflict(session, "A submit for this dispute is already in progress")
    if not dispute.evidence_summary:
        raise conflict(
            session,
            "Draft the summary first: GET /disputes/" + str(dispute_id) + "/evidence",
        )
    dispute.evidence_claimed_at = utc_now()
    session.commit()
    return dispute


def release_claim(session, dispute_id, status=None):
    try:
        dispute = load_dispute(session, dispute_id, lock=True)
        dispute.evidence_claimed_at = None
        if status:
            dispute.status = status
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        logger.exception("Couldn't release the evidence claim on dispute %s", dispute_id)


# ---- endpoints ----


@router.get("", response_model=list[DisputeOut])
def list_disputes(session: DbSession, limit: int = 50):
    limit = max(1, min(limit, 200))
    query = select(Dispute).order_by(Dispute.created_at.desc(), Dispute.id.desc()).limit(limit)
    disputes = []
    for dispute in session.scalars(query).all():
        disputes.append(dispute_out(dispute))
    return disputes


@router.post("/sync", response_model=SyncResult)
def sync_disputes(session: DbSession, paypal: PayPal):
    query = (
        select(Order)
        .where(Order.status == OrderStatus.captured, Order.capture_id.is_not(None))
        .order_by(Order.id)
    )
    captured_orders = session.scalars(query).all()

    result = {"orders_checked": 0, "disputes_found": 0, "created": 0, "updated": 0, "errors": []}
    for order in captured_orders:
        result["orders_checked"] += 1
        try:
            found = (paypal.list_disputes(order.capture_id) or {}).get("items") or []
            for summary in found:
                details = paypal.get_dispute(summary["dispute_id"])
                if not dispute_matches_capture(details, order.capture_id):
                    logger.warning(
                        "Dispute %s came back for capture %s but doesn't name it, skipping",
                        summary["dispute_id"], order.capture_id,
                    )
                    continue
                result["disputes_found"] += 1
                if save_dispute(session, order, details):
                    result["created"] += 1
                else:
                    result["updated"] += 1
        except PAYPAL_FAILURES as error:
            logger.exception("Dispute lookup failed for order %s", order.id)
            result["errors"].append({"order_id": order.id, "message": str(error)})

    session.commit()
    return result


def save_dispute(session, order, details):
    # returns True if it's new
    paypal_dispute_id = details["dispute_id"]
    existing = session.scalars(
        select(Dispute).where(Dispute.paypal_dispute_id == paypal_dispute_id)
    ).first()
    due_at = parse_paypal_time(details.get("seller_response_due_date"))
    if existing is not None:
        existing.reason = details.get("reason")
        existing.status = details.get("status")
        # copied every time, null included: PayPal drops the date once it's not waiting on us,
        # and a stale deadline on the dashboard would be wrong
        existing.seller_response_due_at = due_at
        return False
    session.add(Dispute(
        paypal_dispute_id=paypal_dispute_id,
        order_id=order.id,
        reason=details.get("reason"),
        status=details.get("status"),
        seller_response_due_at=due_at,
    ))
    session.flush()
    return True


def parse_paypal_time(value):
    # PayPal sends e.g. "2026-11-17T04:59:59.000Z". a missing or odd value just means no deadline
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("Couldn't read PayPal time %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@router.get("/{dispute_id}/evidence", response_model=EvidenceOut)
def get_evidence(dispute_id: int, session: DbSession, claude: Claude):
    dispute = load_dispute(session, dispute_id)
    packet = build_evidence_packet(dispute)

    # a saved draft is what submit will send, so that's what gets shown. looking at the page
    # again must not change the text, a new one only comes from POST /redraft. after a submit
    # this is also exactly what PayPal has
    if dispute.evidence_summary or dispute.evidence_submitted_at is not None:
        return EvidenceOut(
            dispute=dispute_out(dispute), packet=packet, summary=dispute.evidence_summary or ""
        )

    # first look: nothing drafted yet, so draft one
    summary = draft_or_502(claude, packet, dispute_id)
    session.rollback()
    dispute = load_dispute(session, dispute_id, lock=True)
    if dispute.evidence_summary:
        # two first looks at once: keep the one already saved, so both see the same text
        summary = dispute.evidence_summary
    else:
        dispute.evidence_summary = summary
    session.commit()
    return EvidenceOut(dispute=dispute_out(dispute), packet=packet, summary=summary)


def evidence_file(dispute):
    # (file name, PDF bytes). submit and the download both build it here, so the file the
    # merchant downloads is byte for byte the one PayPal gets
    packet = build_evidence_packet(dispute)
    file_name = evidence_pdf.evidence_file_name(dispute.paypal_dispute_id)
    return file_name, evidence_pdf.build_pdf(packet_lines(packet))


@router.get("/{dispute_id}/evidence.pdf")
def download_evidence_pdf(dispute_id: int, session: DbSession):
    # read only: no PayPal call, no Claude call, works before and after filing. the PDF is just
    # Leash's own records, the summary goes to PayPal separately as the notes
    dispute = load_dispute(session, dispute_id)
    file_name, pdf_bytes = evidence_file(dispute)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            # evidence_file_name only allows letters, digits, - and _, so no quoting trouble
            "Content-Disposition": 'attachment; filename="' + file_name + '"',
            # it's someone's order history, don't let a shared cache keep it
            "Cache-Control": "no-store",
        },
    )


@router.post("/{dispute_id}/redraft", response_model=EvidenceOut)
def redraft_evidence(dispute_id: int, session: DbSession, claude: Claude):
    # replaces the saved draft with a new one, on purpose
    dispute = load_dispute(session, dispute_id)
    blocker = redraft_blocker(dispute)
    if blocker:
        raise conflict(session, blocker)
    packet = build_evidence_packet(dispute)

    # no row lock while Claude writes (that's seconds), so check again before saving: a submit
    # may have started meanwhile, and it's sending the old text
    summary = draft_or_502(claude, packet, dispute_id)
    session.rollback()
    dispute = load_dispute(session, dispute_id, lock=True)
    blocker = redraft_blocker(dispute)
    if blocker:
        raise conflict(session, blocker)
    dispute.evidence_summary = summary
    session.commit()
    return EvidenceOut(dispute=dispute_out(dispute), packet=packet, summary=summary)


def redraft_blocker(dispute):
    # why the summary can't change right now, or None
    if dispute.evidence_submitted_at is not None:
        return "Evidence was already submitted, the summary is what PayPal has now"
    if dispute.evidence_claimed_at is not None:
        if utc_now() - as_utc(dispute.evidence_claimed_at) < CLAIM_EXPIRES_AFTER:
            return "A submit for this dispute is in progress"
    return None


def draft_or_502(claude, packet, dispute_id):
    try:
        return draft_summary(claude, packet)
    except SummaryFailed as error:
        logger.exception("Dispute summary failed for dispute %s", dispute_id)
        raise HTTPException(
            status_code=502, detail={"message": "Couldn't draft the summary, try again"}
        ) from error


@router.post("/{dispute_id}/submit-evidence", response_model=DisputeOut)
def submit_evidence(dispute_id: int, session: DbSession, paypal: PayPal):
    dispute = claim_for_submit(session, dispute_id)
    paypal_dispute_id = dispute.paypal_dispute_id

    try:
        details = paypal.get_dispute(paypal_dispute_id)
    except PAYPAL_FAILURES as error:
        logger.exception("Couldn't read dispute %s from PayPal", paypal_dispute_id)
        release_claim(session, dispute_id)
        raise paypal_error_response("dispute lookup", error) from error

    # both checks, on PayPal's answer from just now
    blocker = evidence_blocker(details)
    if blocker is not None:
        release_claim(session, dispute_id, status=details.get("status"))
        raise HTTPException(status_code=409, detail={
            "message": blocker,
            "status": details.get("status"),
            "provide_evidence_link": has_provide_evidence_link(details),
        })

    evidence_type, why_not = choose_evidence_type(details)
    if evidence_type is None:
        release_claim(session, dispute_id, status=details.get("status"))
        raise HTTPException(status_code=409, detail={
            "message": why_not,
            "requested_types": requested_evidence_types(details),
        })

    file_name, pdf_bytes = evidence_file(dispute)
    notes = build_notes(dispute.evidence_summary, evidence_type, requested_evidence_types(details))
    evidence_input = {"evidences": [{
        "evidence_type": evidence_type,
        "notes": notes,
        "documents": [{"name": file_name}],
    }]}

    try:
        paypal.provide_evidence(paypal_dispute_id, evidence_input, file_name, pdf_bytes)
    except PAYPAL_FAILURES as error:
        # e.g. 400 EVIDENCE_TYPE_IS_NOT_ALLOWED. nothing was filed, so it can be tried again
        logger.exception("PayPal provide-evidence failed for dispute %s", paypal_dispute_id)
        release_claim(session, dispute_id)
        raise paypal_error_response("provide evidence", error) from error

    try:
        dispute = load_dispute(session, dispute_id, lock=True)
        dispute.evidence_submitted_at = utc_now()
        dispute.evidence_type = evidence_type
        dispute.evidence_claimed_at = None
        dispute.status = details.get("status")
        session.commit()
    except SQLAlchemyError as error:
        # PayPal has it. the claim stays on the row, so for the next couple of minutes nobody can
        # file a second copy. after that only the status gate stands in the way, which holds if
        # PayPal has moved the dispute on by then. PayPal's dispute details show what landed
        session.rollback()
        logger.exception("Evidence went to PayPal for %s but saving that failed", paypal_dispute_id)
        raise HTTPException(status_code=502, detail={
            "message": "PayPal took the evidence but the dispute couldn't be updated here. "
            "Don't submit again, check the dispute in PayPal",
        }) from error
    return dispute_out(dispute)
