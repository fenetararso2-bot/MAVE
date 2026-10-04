"""The signed-in user: profile, preferences, account deletion."""
from fastapi import APIRouter, Depends

from .. import auth  # service layer (app/auth.py), not this module
from ..db import db
from .deps import current_user
from .schemas import DeleteAccountReq, PrefsReq

router = APIRouter()


@router.get("/me")
def me(user: dict = Depends(current_user)):
    with db() as con:
        return auth.get_profile(con, user["sub"])


@router.get("/me/preferences")
def get_preferences(user: dict = Depends(current_user)):
    with db() as con:
        return auth.get_preferences(con, user["sub"])


@router.put("/me/preferences")
def put_preferences(req: PrefsReq, user: dict = Depends(current_user)):
    with db() as con:
        return auth.set_preferences(con, user["sub"], lang=req.lang, theme=req.theme)


@router.post("/me/delete")  # POST, not DELETE: some proxies/CDNs drop DELETE bodies, and we need the password
def delete_account(req: DeleteAccountReq, user: dict = Depends(current_user)):
    with db() as con:
        auth.delete_account(con, user["sub"], req.password)
    return {"ok": True}

