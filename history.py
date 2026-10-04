"""Search history of the signed-in user."""
import time

from fastapi import APIRouter, Depends

from ..db import db
from .deps import current_user
from .schemas import HistoryReq

router = APIRouter()


@router.get("/me/history")
def get_history(user: dict = Depends(current_user)):
    with db() as con:
        rows = con.execute(
            "SELECT query FROM history WHERE user_id=? ORDER BY ts DESC LIMIT 200", (user["sub"],)
        ).fetchall()
    return {"items": [r["query"] for r in rows]}


@router.post("/me/history")
def add_history(req: HistoryReq, user: dict = Depends(current_user)):
    with db() as con:
        con.execute(
            """INSERT INTO history(user_id, query, ts) VALUES(?,?,?)
               ON CONFLICT(user_id, query) DO UPDATE SET ts=excluded.ts""",
            (user["sub"], req.query.strip(), time.time()),
        )
    return {"ok": True}


@router.delete("/me/history")
def delete_history(q: str | None = None, user: dict = Depends(current_user)):
    with db() as con:
        if q:
            con.execute("DELETE FROM history WHERE user_id=? AND query=?", (user["sub"], q))
        else:
            con.execute("DELETE FROM history WHERE user_id=?", (user["sub"],))
    return {"ok": True}

