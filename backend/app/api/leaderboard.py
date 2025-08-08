from typing import Dict, List, Tuple

from fastapi import APIRouter, Depends, Form, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_session
from ..db_models import Match, Team
import os

from ..tasks import schedule_full_evaluation

ADMIN_SECRET = os.getenv("ADMIN_PASSWORD")

router = APIRouter(prefix="/leaderboard", tags=["leaderboard"])


class TeamEntry(BaseModel):
    team_id: str
    name: str
    wins: int
    draws: int
    losses: int
    matches: int
    points: int
    hp_total: int


class LeaderboardResponse(BaseModel):
    teams: List[TeamEntry]
    pending_matches: int
    running_matches: int


async def _get_baseline_ids(session: AsyncSession) -> List[str]:
    res = await session.execute(select(Team).where(Team.name == "Baseline"))
    return [str(t.id) for t in res.scalars().all()]


async def _aggregate(session: AsyncSession, mode: str) -> LeaderboardResponse:
    # Identify baseline teams
    baseline_ids = set(await _get_baseline_ids(session))

    # Count pending and running matches (excluding baseline)
    pend_res = await session.execute(select(Match).where(Match.mode == mode, Match.status == "pending"))
    run_res = await session.execute(select(Match).where(Match.mode == mode, Match.status == "running"))
    pending_all = [m for m in pend_res.scalars().all()]
    running_all = [m for m in run_res.scalars().all()]
    def _exclude_baseline(ms: List[Match]) -> int:
        c = 0
        for m in ms:
            ids = set(m.team_ids.split(","))
            if not ids.intersection(baseline_ids):
                c += 1
        return c
    pending_matches = _exclude_baseline(pending_all)
    running_matches = _exclude_baseline(running_all)

    # Finished matches excluding baseline
    result = await session.execute(select(Match).where(Match.mode == mode, Match.status == "finished"))
    matches_all: List[Match] = result.scalars().all()
    matches: List[Match] = []
    for m in matches_all:
        ids = set(m.team_ids.split(","))
        if not ids.intersection(baseline_ids):
            matches.append(m)

    wins: Dict[str, int] = {}
    draws: Dict[str, int] = {}
    matches_played: Dict[str, int] = {}

    for m in matches:
        team_ids = m.team_ids.split(",")
        for tid in team_ids:
            matches_played[tid] = matches_played.get(tid, 0) + 1
        if m.winner_team_id:
            wid = str(m.winner_team_id)
            wins[wid] = wins.get(wid, 0) + 1
        else:
            for tid in team_ids:
                draws[tid] = draws.get(tid, 0) + 1

    team_ids_all = list(matches_played.keys())
    if not team_ids_all:
        return LeaderboardResponse(
            teams=[],
            pending_matches=pending_matches,
            running_matches=running_matches,
        )

    t_res = await session.execute(select(Team).where(Team.id.in_(team_ids_all)))
    teams_db: List[Team] = t_res.scalars().all()
    name_map = {str(t.id): t.name for t in teams_db}

    leaderboard: List[TeamEntry] = []
    for tid in team_ids_all:
        hp_total = 0
        # sum hp from matches
        for m in matches:
            if tid in m.team_ids.split(",") and m.team_hp:
                import json as _j
                hp_total += _j.loads(m.team_hp).get(tid, 0)

        leaderboard.append(
            TeamEntry(
                team_id=tid,
                name=name_map.get(tid, "<unknown>"),
                wins=wins.get(tid, 0),
                draws=draws.get(tid, 0),
                losses=matches_played[tid] - wins.get(tid, 0),
                matches=matches_played[tid],
                points=wins.get(tid,0)*3 + draws.get(tid,0)*1,
                hp_total=hp_total,
            )
        )
    leaderboard.sort(key=lambda e: (-e.points, -e.hp_total))
    return LeaderboardResponse(
        teams=leaderboard,
        pending_matches=pending_matches,
        running_matches=running_matches,
    )


@router.post("/re-evaluate", status_code=202)
async def re_evaluate_matches(
    admin_password: str = Form(...),
    session: AsyncSession = Depends(get_session)
):
    if admin_password != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin password")
    
    # Clear existing matches
    await session.execute(Match.__table__.delete())
    await session.commit()

    # Enqueue full evaluation scheduling
    schedule_full_evaluation.apply_async(queue="simulation")
    return {"status": "queued"}


@router.get("/duo", response_model=LeaderboardResponse)
async def leaderboard_duo(session: AsyncSession = Depends(get_session)):
    return await _aggregate(session, "duo")


@router.get("/quad", response_model=LeaderboardResponse)
async def leaderboard_quad(session: AsyncSession = Depends(get_session)):
    return await _aggregate(session, "quad") 