from typing import Dict, List

import os
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_session
from ..db_models import Match, Team, Rating, MatchQueue
from ..tasks import recompute_ratings, reset_and_reenqueue_all


router = APIRouter(prefix="/leaderboard", tags=["leaderboard"])


class TeamRow(BaseModel):
    team_id: str
    name: str
    mu: float
    sigma: float
    conservative: float
    wl_last50: Dict[str, int]
    recent10: str
    match_ids: List[str]


class LeaderboardResponse(BaseModel):
    teams: List[TeamRow]


async def _baseline_ids(session: AsyncSession) -> set[str]:
    # Include both pinned A/B/C and legacy "Baseline" team created by baseline tests
    names = ["Baseline", "Baseline-A", "Baseline-B", "Baseline-C"]
    res = await session.execute(select(Team).where(Team.name.in_(names)))
    return {str(t.id) for t in res.scalars().all()}


@router.get("")
async def leaderboard(
    mode: str,
    include_baselines: bool = False,
    x_admin_token: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
):
    if mode not in ("duo", "quad"):
        raise HTTPException(status_code=400, detail="mode must be 'duo' or 'quad'")

    # Visibility policy
    baselines_visible = os.getenv("BASELINES_VISIBLE", "false").lower() in ("1", "true", "yes")
    admin_ok = x_admin_token and x_admin_token == os.getenv("ADMIN_TOKEN")
    show_baselines = include_baselines and bool(admin_ok)
    if baselines_visible:
        show_baselines = True

    # Load ratings for this mode
    res = await session.execute(select(Rating, Team).join(Team, Team.id == Rating.team_id).where(Rating.mode == mode, Team.status == "valid"))
    rows = res.all()

    baseline_ids = await _baseline_ids(session)
    entries: List[TeamRow] = []

    # Prepare recent matches for WL and recent form
    matches_res = await session.execute(select(Match).where(Match.mode == mode, Match.status == "finished").order_by(Match.created_at.desc()))
    matches_all: List[Match] = matches_res.scalars().all()

    # Index matches per team (limit to last ~200 per team to bound work)
    per_team_matches: Dict[str, List[Match]] = {}
    for m in matches_all:
        tids = m.team_ids.split(",")
        for tid in tids:
            bucket = per_team_matches.setdefault(tid, [])
            if len(bucket) < 200:
                bucket.append(m)

    def recent_stats(tid: str) -> tuple[Dict[str, int], str, List[str]]:
        ms = per_team_matches.get(tid, [])
        wl = {"wins": 0, "losses": 0}
        form = []
        match_ids = []
        for m in ms[:50]:
            outcome = None
            if m.winner_team_id:
                outcome = "W" if str(m.winner_team_id) == tid else "L"
            else:
                outcome = "L"  # draws not expected
            if outcome == "W":
                wl["wins"] += 1
            else:
                wl["losses"] += 1
            if len(form) < 10:
                form.append(outcome)
            if len(match_ids) < 20:
                match_ids.append(str(m.id))
        return wl, " ".join(form), match_ids

    for r, t in rows:
        tid_str = str(t.id)
        if tid_str in baseline_ids and not show_baselines:
            continue
        conservative = float(r.mu - 3.0 * r.sigma)
        wl, form, mids = recent_stats(tid_str)
        entries.append(
            TeamRow(
                team_id=tid_str,
                name=t.name,
                mu=float(r.mu),
                sigma=float(r.sigma),
                conservative=conservative,
                wl_last50=wl,
                recent10=form,
                match_ids=mids,
            )
        )

    # Sort by conservative skill desc
    entries.sort(key=lambda e: (-e.conservative, e.name.lower()))
    return {"teams": entries}


@router.get("/pending_running")
async def pending_running(mode: str, session: AsyncSession = Depends(get_session)):
    if mode not in ("duo", "quad"):
        raise HTTPException(status_code=400, detail="mode must be 'duo' or 'quad'")
    pend_res = await session.execute(select(Match).where(Match.mode == mode, Match.status == "pending"))
    run_res = await session.execute(select(Match).where(Match.mode == mode, Match.status == "running"))
    q_res = await session.execute(select(MatchQueue).where(MatchQueue.mode == mode, MatchQueue.status == "queued"))
    return {
        "pending": len([*pend_res.scalars().all()]),
        "running": len([*run_res.scalars().all()]),
        "queued": len([*q_res.scalars().all()]),
    }


@router.post("/re-compute")
async def leaderboard_re_compute(
    x_admin_token: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
):
    # Require admin token and trigger ratings recompute (replay matches)
    if not x_admin_token or x_admin_token != os.getenv("ADMIN_TOKEN"):
        raise HTTPException(status_code=403, detail="admin token required")
    task = recompute_ratings.apply_async(queue="simulation")
    return {"status": "queued", "task_id": task.id}


@router.post("/re-evaluate")
async def leaderboard_re_evaluate(
    x_admin_token: str | None = Header(None),
):
    # Dangerous: resets matches/queue/ratings, then enqueues calibration for all teams
    if not x_admin_token or x_admin_token != os.getenv("ADMIN_TOKEN"):
        raise HTTPException(status_code=403, detail="admin token required")
    task = reset_and_reenqueue_all.apply_async(queue="simulation")
    return {"status": "queued", "task_id": task.id}

