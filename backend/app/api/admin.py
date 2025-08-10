import os
import uuid
from typing import List

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_session
from ..db_models import Match, Team
from ..sync_database import SessionLocal
from ..db_models import Team as TeamDB
from ..tasks import (
    recompute_ratings,
    schedule_ongoing,
    queue_consumer_once,
    schedule_calibration_for_team,
    inflate_sigma_for_inactive,
)


router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(token: str | None) -> None:
    expected = os.getenv("ADMIN_TOKEN")
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="admin token required")


@router.post("/leaderboard/rebuild")
async def rebuild_leaderboard(x_admin_token: str | None = Header(None), session: AsyncSession = Depends(get_session)):
    require_admin(x_admin_token)
    # Event-driven cache will handle rebuild; this is a placeholder for future cache recompute
    return {"status": "ok"}


@router.post("/ratings/recompute")
async def ratings_recompute(x_admin_token: str | None = Header(None), session: AsyncSession = Depends(get_session)):
    require_admin(x_admin_token)
    out = recompute_ratings.apply_async(queue="simulation")
    return {"status": "queued", "task_id": out.id}


@router.post("/scheduler/ongoing")
async def run_ongoing_scheduler(x_admin_token: str | None = Header(None)):
    require_admin(x_admin_token)
    out = schedule_ongoing.apply_async(queue="simulation")
    return {"status": "queued", "task_id": out.id}


@router.post("/scheduler/consume_once")
async def run_queue_consumer_once(x_admin_token: str | None = Header(None)):
    require_admin(x_admin_token)
    out = queue_consumer_once.apply_async(queue="simulation")
    return {"status": "queued", "task_id": out.id}


@router.post("/scheduler/calibrate/{team_id}")
async def run_calibration_for_team(team_id: str, x_admin_token: str | None = Header(None)):
    require_admin(x_admin_token)
    out = schedule_calibration_for_team.apply_async(args=[team_id], queue="simulation")
    return {"status": "queued", "task_id": out.id}


@router.post("/ratings/inflate_sigma")
async def run_inflate_sigma(x_admin_token: str | None = Header(None)):
    require_admin(x_admin_token)
    out = inflate_sigma_for_inactive.apply_async(queue="simulation")
    return {"status": "queued", "task_id": out.id}


@router.post("/baselines/seed")
async def baselines_seed(x_admin_token: str | None = Header(None), session: AsyncSession = Depends(get_session)):
    require_admin(x_admin_token)
    # Seed baseline teams with pinned UUIDs and rosters
    PINNED = [
        ("8b8c2f20-7a2f-4a64-8f40-9a0e28c8a5f3", "Baseline-A", ["tank","sniper","scout","healer","trap_setter"]),
        ("1c0a4f2e-4f91-4c8c-86a8-cedf2b219a45", "Baseline-B", ["tank","bomber","teleporter","shield_giver","puller"]),
        ("f9c2d4e3-9a70-4315-a2c0-2e77c5a3d6b8", "Baseline-C", ["bruiser","poisoner","jammer","wall_builder","leaper"]),
    ]
    from pathlib import Path
    base_code = (Path(__file__).resolve().parents[1] / "engine" / "baseline_bot.py").read_text(encoding="utf-8")
    data_dir = Path(os.getenv("DATA_DIR", "./data/teams"))
    data_dir.mkdir(parents=True, exist_ok=True)

    created = 0
    with SessionLocal() as s:  # sync for simplicity
        for tid_str, name, roster in PINNED:
            tid = uuid.UUID(tid_str)
            t = s.get(TeamDB, tid)
            team_dir = data_dir / tid_str
            team_dir.mkdir(parents=True, exist_ok=True)
            bot_path = team_dir / "bot.py"
            bot_path.write_text(base_code, encoding="utf-8")
            roster_json = __import__("json").dumps(roster)
            if not t:
                t = TeamDB(id=tid, name=name, code_path=str(bot_path), password_hash="!", roster=roster_json)
                s.add(t)
                created += 1
            else:
                t.name = name
                t.code_path = str(bot_path)
                t.roster = roster_json
        s.commit()
    return {"status": "ok", "created": created}


