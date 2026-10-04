"""Trending searches."""
from fastapi import APIRouter

from ..db import db
from ..search import suggest as sg

router = APIRouter()


@router.get("/trending")
def trending():
    with db() as con:
        return {"trending": sg.trending(con)}

