"""Endpoints v1: POST/GET /v1/topups y GET /v1/rewards."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import AuthContext, authenticate
from app.api.errors import ApiError
from app.api.schemas import RewardOut, TopupCreate, TopupOut
from app.db import get_session
from app.domain.topups import (
    TopupRejectedError,
    create_topup,
    find_by_external_ref,
    reward_codes,
)
from app.models import Reward, Topup, TopupStatus

router = APIRouter(prefix="/v1")

Auth = Annotated[AuthContext, Depends(authenticate)]
Db = Annotated[Session, Depends(get_session)]
_NO_EXISTE = ApiError(404, "not_found", "Recarga no encontrada")


def _out(session: Session, topup: Topup) -> TopupOut:
    return TopupOut(
        id=topup.id,
        external_ref=topup.external_ref,
        status=topup.status,
        env=topup.env,
        reward_code=reward_codes(session, {topup.reward_id})[topup.reward_id],
        amount=topup.amount,
        currency=topup.currency,
        phone=topup.phone,
        error_code=topup.pn_response_code if topup.status == TopupStatus.FAILED else None,
        created_at=topup.created_at,
        finalized_at=topup.finalized_at,
    )


def _visible(topup: Topup | None, auth: AuthContext) -> Topup:
    """Un producto solo ve sus recargas, y solo las del entorno de su llave."""
    if topup is None or topup.product_id != auth.product.id or topup.env != auth.env:
        raise _NO_EXISTE
    return topup


@router.post("/topups", response_model=TopupOut, status_code=202)
def post_topup(body: TopupCreate, auth: Auth, session: Db, response: Response) -> TopupOut:
    try:
        r = create_topup(
            session,
            product=auth.product,
            env=auth.env,
            reward_code=body.reward_code,
            phone=body.phone,
            external_ref=body.external_ref,
            operator=body.operator,
        )
    except TopupRejectedError as e:
        raise ApiError(e.http_status, e.code, e.message) from None
    if not r.created:
        response.status_code = 200
    return _out(session, r.topup)


@router.get("/topups/{topup_id}", response_model=TopupOut)
def get_topup(topup_id: str, auth: Auth, session: Db) -> TopupOut:
    return _out(session, _visible(session.get(Topup, topup_id), auth))


@router.get("/topups", response_model=TopupOut)
def get_topup_by_ref(
    external_ref: Annotated[str, Query(min_length=1, max_length=200)], auth: Auth, session: Db
) -> TopupOut:
    return _out(
        session, _visible(find_by_external_ref(session, auth.product.id, external_ref), auth)
    )


@router.get("/rewards", response_model=list[RewardOut])
def get_rewards(auth: Auth, session: Db) -> list[RewardOut]:
    recompensas = session.scalars(
        select(Reward)
        .where(Reward.product_id == auth.product.id, Reward.active)
        .order_by(Reward.code)
    )
    return [
        RewardOut(code=r.code, label=r.label, amount=r.amount, currency=r.currency)
        for r in recompensas
    ]
