"""Saved results of the signed-in user."""
import time

from fastapi import APIRouter, Depends, HTTPException

from ..db import db
from .deps import current_user
from .schemas import SavedReq

router = APIRouter()


@router.get("/me/saved")
def get_saved(user: dict = Depends(current_user)):
    with db() as con:
        rows = con.execute(
            "SELECT title, url, snippet, thumbnail FROM saved WHERE user_id=? ORDER BY ts DESC LIMIT 500",
            (user["sub"],),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/me/saved")
def add_saved(req: SavedReq, user: dict = Depends(current_user)):
    if not req.url.lower().startswith(("http://", "https://")) or len(req.url.encode("utf-8", "ignore")) > 2000:
        raise HTTPException(400, "Invalid url")
    with db() as con:
        con.execute(
            """INSERT INTO saved(user_id, url, title, snippet, thumbnail, ts) VALUES(?,?,?,?,?,?)
               ON CONFLICT(user_id, url) DO UPDATE SET title=excluded.title, ts=excluded.ts""",
            (user["sub"], req.url, req.title, req.snippet, req.thumbnail, time.time()),
        )
    return {"ok": True}


@router.delete("/me/saved")
def delete_saved(url: str, user: dict = Depends(current_user)):
    with db() as con:
        con.execute("DELETE FROM saved WHERE user_id=? AND url=?", (user["sub"], url))
    return {"ok": True}

