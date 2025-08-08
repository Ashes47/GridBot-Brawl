from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..db_models import Match, Team
from ..tasks import run_match, run_baseline_test
import uuid
from datetime import datetime
from pathlib import Path
import os

router = APIRouter(prefix="/simulate", tags=["simulate"])


class SimulateRequest(BaseModel):
    team_ids: List[str] = Field(..., min_items=2, max_items=4)


@router.post("/duo")
async def simulate_duo(req: SimulateRequest, session: AsyncSession = Depends(get_session)):
    if len(req.team_ids) != 2:
        raise HTTPException(status_code=400, detail="duo requires exactly 2 team_ids")

    match_id = uuid.uuid4()
    match = Match(
        id=match_id,
        mode="duo",
        team_ids=",".join(req.team_ids),
        status="pending",
        log_path="",
        created_at=datetime.utcnow(),
    )
    session.add(match)
    await session.flush()

    # enqueue Celery task to simulation queue
    run_match.apply_async(args=[str(match_id), req.team_ids], queue="simulation")

    return {"match_id": str(match_id), "status": "pending"}


@router.post("/quad")
async def simulate_quad(req: SimulateRequest, session: AsyncSession = Depends(get_session)):
    if len(req.team_ids) != 4:
        raise HTTPException(status_code=400, detail="quad requires exactly 4 team_ids")

    match_id = uuid.uuid4()
    match = Match(
        id=match_id,
        mode="quad",
        team_ids=",".join(req.team_ids),
        status="pending",
        log_path="",
        created_at=datetime.utcnow(),
    )
    session.add(match)
    await session.flush()

    run_match.apply_async(args=[str(match_id), req.team_ids], queue="simulation")

    return {"match_id": str(match_id), "status": "pending"}


@router.post("/vs_baseline")
async def simulate_vs_baseline(team_id: str, session: AsyncSession = Depends(get_session)):
    """Create a duo match of given team vs built-in baseline bot.
    Returns a match_id to view in viewer.
    """
    # Ensure team exists
    from sqlalchemy import select as _select
    res = await session.execute(_select(Team).where(Team.id == uuid.UUID(team_id)))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="team not found")


    match_id = uuid.uuid4()
    # Let Celery create the baseline match and return id

    # let Celery task create baseline team and match consistently
    res = run_baseline_test.apply_async(args=[team_id], queue="baseline")
    return {"match_id": res.get(timeout=10).get("match_id"), "status": "pending"}
