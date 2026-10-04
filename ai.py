"""AI answers: retrieve sources first (own index + web provider), then answer only from them, with citations."""
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from ..ai import answer
from ..core.errors import ProviderError
from ..search.oromoo import normalize_query_lang
from ..search.ranking import rrf_fuse
from ..search.web import web_search
from .schemas import AiReq
from .search import local_results, should_log

router = APIRouter()


@router.post("/ai/answer")
async def ai_answer(req: AiReq, request: Request):
    q = " ".join(req.query.split())
    log = should_log(request, q)
    local, _, _ = await run_in_threadpool(local_results, q, 1, 5, log, normalize_query_lang(req.lang))
    try:
        web = await web_search(q, "web", count=8)
    except ProviderError:
        web = []
    sources = rrf_fuse([local, web], [1.3, 1.0])[:6] if local and web else (local or web)[:6]
    for s in sources:
        s.pop("score", None)
        s.pop("domain", None)
    return await answer.grounded_answer(q, sources, req.lang)  # {answer, sources, citations, grounded, insufficient}

