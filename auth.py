"""Account endpoints: register, login, token refresh, logout, password reset, e-mail verification."""
from fastapi import APIRouter, BackgroundTasks, Depends, Header

from .. import auth, mailer  # service layers (app/auth.py, app/mailer.py), not this module
from ..db import db
from .deps import access_claims, current_user
from .schemas import AuthReq, ForgotReq, LogoutReq, RefreshReq, ResetReq, VerifyReq

router = APIRouter()


def _mail(to: str, content: tuple[str, str]) -> None:
    mailer.send(to, *content)  # runs as a background task; mailer never raises


@router.post("/auth/register")
def register(req: AuthReq, bg: BackgroundTasks):
    with db() as con:
        pair, verify_token_raw = auth.register(con, req.email, req.password)
    bg.add_task(_mail, pair["email"], mailer.verification_email(verify_token_raw))
    return pair


@router.post("/auth/login")
def login(req: AuthReq):
    with db() as con:
        return auth.login(con, req.email, req.password)


@router.post("/auth/refresh")
def refresh(req: RefreshReq):
    with db() as con:
        return auth.refresh(con, req.refresh_token)


@router.post("/auth/logout")
def logout(req: LogoutReq, authorization: str | None = Header(default=None)):
    claims = access_claims(authorization)
    with db() as con:
        if req.all:
            auth.logout(con, user_id=claims["sub"] if claims else None, everywhere=True)
        if claims:
            auth.logout(con, sid=claims["sid"])
        if req.refresh_token:
            auth.logout(con, refresh_token=req.refresh_token)
    return {"ok": True}


@router.post("/auth/forgot-password")
def forgot_password(req: ForgotReq, bg: BackgroundTasks):
    with db() as con:
        found = auth.request_password_reset(con, req.email)
    if found:
        bg.add_task(_mail, found[0], mailer.reset_email(found[1]))
    return {"ok": True}  # identical answer whether or not the account exists


@router.post("/auth/reset-password")
def reset_password(req: ResetReq):
    with db() as con:
        auth.reset_password(con, req.token, req.password)
    return {"ok": True}


@router.post("/auth/verify-email")
def verify_email(req: VerifyReq):
    with db() as con:
        email = auth.verify_email(con, req.token)
    return {"ok": True, "email": email}


@router.post("/auth/resend-verification")
def resend_verification(bg: BackgroundTasks, user: dict = Depends(current_user)):
    with db() as con:
        found = auth.request_verification(con, user["sub"])
    if found:
        bg.add_task(_mail, found[0], mailer.verification_email(found[1]))
    return {"ok": True}

