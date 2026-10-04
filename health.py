"""Liveness probe."""
from fastapi import APIRouter

from .. import __version__

router = APIRouter()


@router.get("/health")
def health():
    return {"ok": True, "version": __version__}

