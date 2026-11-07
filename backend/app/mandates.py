# POST /mandates, GET /mandates and GET /mandates/{id}

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app import mandate_parser
from backend.app.auth import require_agent, require_merchant
from backend.app.config import get_settings
from backend.app.db import get_session
from backend.app.models import Mandate

router = APIRouter(prefix="/mandates", tags=["mandates"])


class MandateCreate(BaseModel):
    buyer_label: str = Field(min_length=1, max_length=200)
    request_text: str = Field(min_length=1, max_length=4000)
    # the store this mandate is for. wins over a store named in request_text
    merchant: str | None = Field(default=None, max_length=200)


class MandateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    buyer_label: str
    raw_request: str
    rules: dict
    max_price: Decimal | None
    max_price_inclusive: bool
    merchant: str | None
    expires_at: datetime | None
    created_at: datetime


def get_anthropic_client():
    # its own dependency so tests can swap in a fake
    return mandate_parser.make_client(get_settings().anthropic_api_key)


DbSession = Annotated[Session, Depends(get_session)]
AnthropicClient = Annotated[object, Depends(get_anthropic_client)]


@router.post(
    "", response_model=MandateOut, status_code=201, dependencies=[Depends(require_agent)]
)
def create_mandate(body: MandateCreate, session: DbSession, client: AnthropicClient):
    try:
        fields = mandate_parser.parse_mandate(client, body.request_text)
    except mandate_parser.MandateParseError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    mandate = Mandate(
        buyer_label=body.buyer_label,
        # verbatim, this is what goes to PayPal as dispute evidence
        raw_request=body.request_text,
        rules=fields,
        max_price=mandate_parser.max_price_decimal(fields),
        max_price_inclusive=fields["max_price_inclusive"],
        merchant=body.merchant or fields.get("merchant"),
        expires_at=mandate_parser.expires_at_datetime(fields),
    )
    session.add(mandate)
    session.commit()
    session.refresh(mandate)
    return mandate


@router.get("", response_model=list[MandateOut], dependencies=[Depends(require_merchant)])
def list_mandates(session: DbSession, limit: int = 50):
    limit = max(1, min(limit, 200))
    query = select(Mandate).order_by(Mandate.created_at.desc(), Mandate.id.desc()).limit(limit)
    return session.scalars(query).all()


@router.get(
    "/{mandate_id}", response_model=MandateOut, dependencies=[Depends(require_merchant)]
)
def get_mandate(mandate_id: int, session: DbSession):
    mandate = session.get(Mandate, mandate_id)
    if mandate is None:
        raise HTTPException(status_code=404, detail="Mandate not found")
    return mandate
