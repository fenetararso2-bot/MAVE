"""Admin / ops endpoints (X-Admin-Key or an admin-role user's Bearer token): crawler control, seeds, analytics,
users and roles, audit log, metrics. Every state-changing action is audited with the actor that performed it."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from .. import __version__, admin  # service layer (app/admin.py), not this module
from ..core import metrics
from ..crawler import Crawler
from ..db import db
from ..search.engine import stats
from .deps import AdminActor, require_admin, require_admin_or_bearer
from .schemas import CrawlReq, RoleReq, SeedEnabledReq, SeedReq

router = APIRouter()


def _crawl_job(seeds: list[str], req: CrawlReq) -> None:
    Crawler(max_pages=req.max_pages, max_depth=req.max_depth, delay=req.delay, use_sitemaps=True, recrawl=True).run(seeds)


@router.post("/admin/crawl", dependencies=[Depends(require_admin)])
def admin_crawl(req: CrawlReq, bg: BackgroundTasks, actor: AdminActor = Depends(require_admin)):
    with db() as con:
        seeds = req.seeds or admin.enabled_seed_urls(con)
        if not seeds:
            raise HTTPException(400, "No seeds: pass seeds or add some in the dashboard")
        admin.audit(con, "crawl.start", f"{len(seeds)} seeds, max_pages={req.max_pages}, depth={req.max_depth}", actor=actor.label)
    bg.add_task(_crawl_job, seeds, req)
    return {"started": True, "seeds": len(seeds)}


@router.get("/admin/stats", dependencies=[Depends(require_admin)])
def admin_stats():
    with db() as con:
        return stats(con)


@router.get("/admin/health", dependencies=[Depends(require_admin)])
def admin_health():
    with db() as con:
        return admin.system_health(con, __version__)


@router.get("/admin/analytics", dependencies=[Depends(require_admin)])
def admin_analytics(days: int = Query(default=7, ge=1, le=90)):
    with db() as con:
        return {
            **admin.query_analytics(con, days=days),
            "top_domains": admin.top_domains(con),
            "languages": admin.language_breakdown(con),
        }


@router.get("/admin/crawl-errors", dependencies=[Depends(require_admin)])
def admin_crawl_errors(limit: int = Query(default=100, ge=1, le=500)):
    with db() as con:
        return {"items": admin.crawl_errors(con, limit)}


@router.get("/admin/seeds", dependencies=[Depends(require_admin)])
def admin_seeds():
    with db() as con:
        return {"items": admin.list_seeds(con)}


@router.post("/admin/seeds", dependencies=[Depends(require_admin)])
def admin_add_seed(req: SeedReq, actor: AdminActor = Depends(require_admin)):
    with db() as con:
        url = admin.add_seed(con, req.url)
        admin.audit(con, "seed.add", url, actor=actor.label)
    return {"ok": True, "url": url}


@router.put("/admin/seeds", dependencies=[Depends(require_admin)])
def admin_toggle_seed(req: SeedEnabledReq, actor: AdminActor = Depends(require_admin)):
    with db() as con:
        admin.set_seed_enabled(con, req.url, req.enabled)
        admin.audit(con, "seed.enable" if req.enabled else "seed.disable", req.url, actor=actor.label)
    return {"ok": True}


@router.delete("/admin/seeds", dependencies=[Depends(require_admin)])
def admin_delete_seed(url: str = Query(min_length=8, max_length=2000), actor: AdminActor = Depends(require_admin)):
    with db() as con:
        admin.remove_seed(con, url)
        admin.audit(con, "seed.remove", url, actor=actor.label)
    return {"ok": True}


@router.get("/admin/users", dependencies=[Depends(require_admin)])
def admin_users(limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), q: str = Query(default="", max_length=100)):
    with db() as con:
        return admin.list_users(con, limit=limit, offset=offset, q=q)


@router.post("/admin/users/{user_id}/revoke-sessions", dependencies=[Depends(require_admin)])
def admin_revoke_sessions(user_id: int, actor: AdminActor = Depends(require_admin)):
    with db() as con:
        n = admin.revoke_user_sessions(con, user_id)
        admin.audit(con, "user.revoke_sessions", f"user_id={user_id} sessions={n}", actor=actor.label)
    return {"ok": True, "revoked": n}


@router.post("/admin/users/{user_id}/disable", dependencies=[Depends(require_admin)])
def admin_disable_user(user_id: int, actor: AdminActor = Depends(require_admin)):
    with db() as con:
        n = admin.set_user_disabled(con, user_id, True, actor_user_id=actor.user_id)
        admin.audit(con, "user.disable", f"user_id={user_id} sessions={n}", actor=actor.label)
        return {"ok": True, "revoked": n}


@router.post("/admin/users/{user_id}/enable", dependencies=[Depends(require_admin)])
def admin_enable_user(user_id: int, actor: AdminActor = Depends(require_admin)):
    with db() as con:
        admin.set_user_disabled(con, user_id, False)
        admin.audit(con, "user.enable", f"user_id={user_id}", actor=actor.label)
        return {"ok": True}


@router.post("/admin/users/{user_id}/role", dependencies=[Depends(require_admin)])
def admin_set_role(user_id: int, req: RoleReq, actor: AdminActor = Depends(require_admin)):
    """Appoint or remove an admin. Needs an existing admin (or the admin key); nobody changes their own role."""
    with db() as con:
        role = admin.set_user_role(con, user_id, req.role, actor_user_id=actor.user_id)
        admin.audit(con, "user.role", f"user_id={user_id} role={role}", actor=actor.label)
        return {"ok": True, "role": role}


@router.get("/admin/audit", dependencies=[Depends(require_admin)])
def admin_audit(limit: int = Query(default=100, ge=1, le=500)):
    with db() as con:
        return {"items": admin.list_audit(con, limit)}


@router.get("/admin/metrics", dependencies=[Depends(require_admin_or_bearer)])
def admin_metrics():
    """Prometheus text format. Scrape with `authorization: Bearer <admin key>` or the X-Admin-Key header."""
    with db() as con:
        s = stats(con)
    gauges = {f"mave_{k}": v for k, v in s.items() if isinstance(v, (int, float))}
    return PlainTextResponse(metrics.render(gauges), media_type="text/plain; version=0.0.4")

