from typing import List

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..simulation import run_match_job
from ..db_models import Match, Team
from ..database import AsyncSessionLocal
import uuid
from datetime import datetime
from pathlib import Path
import os

router = APIRouter(prefix="/simulate", tags=["simulate"])


class SimulateRequest(BaseModel):
    team_ids: List[str] = Field(..., min_items=2, max_items=4)


@router.post("/duo")
async def simulate_duo(req: SimulateRequest, background: BackgroundTasks, session: AsyncSession = Depends(get_session)):
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

    # schedule background task
    background.add_task(run_match_job, AsyncSessionLocal, str(match_id), req.team_ids)

    return {"match_id": str(match_id), "status": "pending"}


@router.post("/quad")
async def simulate_quad(req: SimulateRequest, background: BackgroundTasks, session: AsyncSession = Depends(get_session)):
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

    background.add_task(run_match_job, AsyncSessionLocal, str(match_id), req.team_ids)

    return {"match_id": str(match_id), "status": "pending"}


@router.post("/vs_baseline")
async def simulate_vs_baseline(team_id: str, background: BackgroundTasks, session: AsyncSession = Depends(get_session)):
    """Create a duo match of given team vs built-in baseline bot.
    Returns a match_id to view in viewer.
    """
    # Ensure team exists
    from sqlalchemy import select as _select
    res = await session.execute(_select(Team).where(Team.id == uuid.UUID(team_id)))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="team not found")

    # Write baseline bot to a temp path accessible to sandbox
    base_dir = Path(os.getenv("DATA_DIR", "./data/teams")) / "baseline"
    base_dir.mkdir(parents=True, exist_ok=True)
    bot_path = base_dir / "bot.py"
    # Copy from app/engine/baseline_bot.py if not exists
    if not bot_path.exists():
        src = Path(__file__).resolve().parent.parent / "engine" / "baseline_bot.py"
        bot_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    # Create or reuse a Team row for baseline
    name = "Baseline"
    # Try find existing
    existing = await session.execute(_select(Team).where(Team.name == name))
    baseline_team = existing.scalar_one_or_none()
    if not baseline_team:
        baseline_team = Team(name=name, code_path=str(bot_path), password_hash="!")
        session.add(baseline_team)
        await session.flush()

    match_id = uuid.uuid4()
    match = Match(
        id=match_id,
        mode="duo",
        team_ids=",".join([team_id, str(baseline_team.id)]),
        status="pending",
        log_path="",
        created_at=datetime.utcnow(),
    )
    session.add(match)
    await session.flush()

    # schedule background task
    background.add_task(run_match_job, AsyncSessionLocal, str(match_id), [team_id, str(baseline_team.id)])

    return {"match_id": str(match_id), "status": "pending"}
