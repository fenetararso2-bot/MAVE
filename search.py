"""Search endpoints: /search (own index + web provider, fused) and /suggest."""
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from ..core.cache import TTLCache
from ..core.errors import ProviderError
from ..db import db
from ..search import suggest as sg
from ..search.engine import search_local
from ..search.oromoo import normalize_query_lang
from ..search.query import analyze
from ..search.ranking import rrf_fuse
from ..search.web import BRAVE_PATHS, web_search

router = APIRouter()

_logged = TTLCache(ttl=600, maxsize=5000)


def should_log(request: Request, q: str) -> bool:
    ip = request.client.host if request.client else "unknown"
    key = (ip, q.lower())
    if _logged.get(key):
        return False
    _logged.set(key, True)
    return True


def local_results(q: str, page: int, size: int, log: bool = False, query_lang: str | None = None):
    with db() as con:
        if log:
            sg.log_query(con, q)
        items = search_local(con, q, limit=size, offset=(page - 1) * size, query_lang=query_lang)
        corrected = sg.spell_correct(con, q) if page == 1 else None
        n_docs = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    for it in items:
        it["source"] = "mave"
    return items, corrected, n_docs


@router.get("/search")
async def search(
    request: Request,
    q: str = Query(min_length=1, max_length=300),
    type: str = "web",
    page: int = Query(default=1, ge=1, le=20),
    lang: str | None = Query(default=None, max_length=8),  # UI language hint ("om"/"en"); unknown values are ignored
):
    if type not in BRAVE_PATHS:
        raise HTTPException(400, "Invalid type")
    q = " ".join(q.split())
    size = 20
    corrected = None
    lists, weights = [], []
    if type in ("web", "tech"):
        log = page == 1 and should_log(request, q)
        local, corrected, _ = await run_in_threadpool(local_results, q, page, size, log, normalize_query_lang(lang))
        if local:
            lists.append(local)
            weights.append(1.3)  # own index gets a boost: it is the product's differentiator
    try:
        web = await web_search(q, type, count=size, offset=(page - 1) * size)
        lists.append(web)
        weights.append(1.0)
    except ProviderError:
        if not lists:  # no local results either -> surface the error
            raise
    results = rrf_fuse(lists, weights) if len(lists) > 1 else (lists[0] if lists else [])
    for r in results:
        r.pop("score", None)
        r.pop("domain", None)
    info = analyze(q, lang)
    # Advisory only (never changes the results): on the first page of a plain web search, name a better-fitting tab.
    hint = info.intent if (type == "web" and page == 1) else None
    return {"results": results, "corrected": corrected, "page": page, "intent": hint, "query_lang": info.lang}


@router.get("/suggest")
def suggest(q: str = Query(min_length=1, max_length=100)):
    with db() as con:
        return {"suggestions": sg.suggest(con, q)}

