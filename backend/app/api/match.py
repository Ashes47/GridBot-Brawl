import json
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_session
from ..db_models import Match, Team
import os

router = APIRouter(prefix="/matches", tags=["matches"])


@router.get("/{match_id}")
async def get_match(match_id: str, session: AsyncSession = Depends(get_session)):
    try:
        mid = uuid.UUID(match_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid match id")

    result = await session.execute(select(Match).where(Match.id == mid))
    match: Optional[Match] = result.scalar_one_or_none()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    team_ids = match.team_ids.split(",")
    # fetch team names
    uuids = []
    for t in team_ids:
        try:
            uuids.append(uuid.UUID(t))
        except Exception:
            continue
    t_res = await session.execute(select(Team).where(Team.id.in_(uuids)))
    teams = t_res.scalars().all()
    id_to_name = {str(t.id): t.name for t in teams}

    teams_meta = [
        {
            "id": tid,
            "name": id_to_name.get(tid, "<unknown>"),
            "prefix": tid.replace("-", "").lower()[:6],
        }
        for tid in team_ids
    ]

    return {
        "id": str(match.id),
        "mode": match.mode,
        "team_ids": team_ids,
        "teams": teams_meta,
        "winner_team_id": str(match.winner_team_id) if match.winner_team_id else None,
        "created_at": match.created_at,
        "log_path": match.log_path,
        "status": match.status,
        "map_name": match.map_name,
        "map_seed": match.map_seed,
    }


@router.get("/{match_id}/log")
async def download_log(match_id: str, session: AsyncSession = Depends(get_session)):
    try:
        mid = uuid.UUID(match_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid match id")

    result = await session.execute(select(Match).where(Match.id == mid))
    match: Optional[Match] = result.scalar_one_or_none()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    # Try stored path first
    raw = match.log_path or ""
    p = Path(raw)
    if p.is_absolute() and p.exists():
        final_path = p
    else:
        # Rebuild from DATA_DIR and filename
        data_dir = Path(os.getenv("DATA_DIR", "./data/matches"))
        candidate = data_dir / (p.name if p.name else raw)
        if candidate.exists():
            final_path = candidate
        else:
            # Also try interpreting stored path as relative under /app
            alt = Path("/app") / raw
            if alt.exists():
                final_path = alt
            else:
                raise HTTPException(status_code=404, detail="Log file missing")

    return FileResponse(final_path, media_type="application/json", filename=f"{match_id}.json")


@router.get("/")
async def list_matches(limit: int = 50, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Match).order_by(Match.created_at.desc()).limit(limit))
    matches: List[Match] = result.scalars().all()
    return [
        {
            "id": str(m.id),
            "mode": m.mode,
            "winner_team_id": str(m.winner_team_id) if m.winner_team_id else None,
            "created_at": m.created_at,
            "status": m.status,
            "map_name": m.map_name,
            "map_seed": m.map_seed,
        }
        for m in matches
    ]