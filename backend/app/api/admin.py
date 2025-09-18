import os
import uuid
from typing import List

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_session
from ..db_models import Match, Team, AppSetting, Member
from ..sync_database import SessionLocal
from ..db_models import Team as TeamDB
from ..tasks import (
    recompute_ratings,
    schedule_ongoing,
    queue_consumer_once,
    schedule_calibration_for_team,
    inflate_sigma_for_inactive,
    reset_and_reenqueue_all,
    reset_and_reenqueue_team,
)
from passlib.hash import bcrypt


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


@router.post("/scheduler/reset_team/{team_id}")
async def reset_team(team_id: str, x_admin_token: str | None = Header(None)):
    require_admin(x_admin_token)
    out = reset_and_reenqueue_team.apply_async(args=[team_id], queue="simulation")
    return {"status": "queued", "task_id": out.id}


@router.post("/leaderboard/re-evaluate")
async def leaderboard_re_evaluate_admin(x_admin_token: str | None = Header(None)):
    """Reset all matches/queue/ratings and re-enqueue calibration for all teams (admin). Also cleans data/matches."""
    require_admin(x_admin_token)
    out = reset_and_reenqueue_all.apply_async(queue="simulation")
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



@router.post("/settings/signup")
async def set_signup_enabled(enabled: bool = Form(...), x_admin_token: str | None = Header(None), session: AsyncSession = Depends(get_session)):
    """Enable/disable public signup."""
    require_admin(x_admin_token)
    val = "true" if enabled else "false"
    existing = await session.execute(select(AppSetting).where(AppSetting.key == "signup_enabled"))
    row = existing.scalar_one_or_none()
    if row:
        row.value = val
    else:
        s = AppSetting(key="signup_enabled", value=val)
        session.add(s)
    await session.flush()
    return {"status": "ok", "signup_enabled": (val == "true")}


@router.post("/bulk_signup")
async def bulk_signup(
    x_admin_token: str | None = Header(None),
    csv_file: UploadFile = File(None),
    csv_path: str = Form(None),
    default_roster: str = Form(None, description="JSON array of 5 components. Optional if bot files will be uploaded later."),
    session: AsyncSession = Depends(get_session),
):
    """Mass register teams from a CSV. Accepts an uploaded CSV or a server path.

    Expected columns: Team Name, Member Emails (comma-separated), Team Password.
    Supports the registration.csv schema:
    - Timestamp, Email address, Team Name, Team Leader Name, Team Leader Email, 
      Member 1-3 Emails, Team Password
    If columns differ, we attempt to infer by header names.
    """
    require_admin(x_admin_token)
    import csv, io, json
    from pathlib import Path

    content = None
    if csv_file is not None:
        content = (await csv_file.read()).decode("utf-8")
    elif csv_path:
        p = Path(csv_path)
        if not p.exists():
            raise HTTPException(status_code=400, detail="CSV path not found")
        content = p.read_text(encoding="utf-8")
    elif os.getenv("REGISTRATION_CSV"):
        # fallback to env var path
        p = Path(os.getenv("REGISTRATION_CSV"))
        if p.exists():
            content = p.read_text(encoding="utf-8")
    if not content:
        raise HTTPException(status_code=400, detail="No CSV provided")

    # parse roster if provided
    roster_list = None
    if default_roster:
        try:
            roster_list = json.loads(default_roster)
            if not isinstance(roster_list, list) or len(roster_list) != 5:
                raise ValueError()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid default_roster; must be JSON array of 5 components")

    reader = csv.DictReader(io.StringIO(content))
    created = 0
    skipped = 0
    results: list[dict] = []

    for row in reader:
        try:
            # Normalize keys
            norm = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }

            # Team name: prefer explicit 'team name'; avoid 'team leader name'
            name = None
            if "team name" in norm and norm["team name"]:
                name = norm["team name"]
            else:
                # Fuzzy search: key contains both 'team' and 'name' but not 'leader' or 'password'
                for k, v in norm.items():
                    if "team" in k and "name" in k and "leader" not in k and "password" not in k and v:
                        name = v
                        break

            # Password column
            password = None
            for k, v in norm.items():
                if "password" in k and v:
                    password = v
                    break

            # Members: collect any emails across relevant columns
            member_vals: list[str] = []
            for k, v in norm.items():
                if not v:
                    continue
                kk = k.replace(" ", "")
                if "email" in k and ("member" in k or "leader" in k or k == "email address" or kk == "emailaddress"):
                    member_vals.append(v)
            members = ",".join([m for m in dict.fromkeys(member_vals)])  # de-duplicate, preserve order

            if not name or not password:
                skipped += 1
                results.append({"name": name or "", "status": "skipped", "detail": "missing team name or password"})
                continue

            # ensure unique
            ex = await session.execute(select(Team).where(Team.name == name))
            if ex.scalar_one_or_none():
                skipped += 1
                results.append({"name": name, "status": "exists"})
                continue

            # Create team
            t_id = uuid.uuid4()
            from pathlib import Path as _P
            data_dir = _P(os.getenv("DATA_DIR", "./data/teams")) / str(t_id)
            data_dir.mkdir(parents=True, exist_ok=True)
            bot_path = data_dir / "bot.py"
            # minimal placeholder code; real upload can replace later
            code_str = "\n".join([
                "class Tank:\n    def decide(self, obs):\n        return {\"type\": \"move\", \"direction\": \"north\"}",
                "class Sniper:\n    def decide(self, obs):\n        return {\"type\": \"move\", \"direction\": \"north\"}",
                "class Bomber:\n    def decide(self, obs):\n        return {\"type\": \"move\", \"direction\": \"north\"}",
                "class Scout:\n    def decide(self, obs):\n        return {\"type\": \"move\", \"direction\": \"north\"}",
                "class Teleporter:\n    def decide(self, obs):\n        return {\"type\": \"move\", \"direction\": \"north\"}",
            ])
            bot_path.write_text(code_str, encoding="utf-8")

            roster_json = __import__("json").dumps(roster_list or ["tank","sniper","bomber","scout","teleporter"])
            team = Team(id=t_id, name=name, code_path=str(bot_path), password_hash=bcrypt.hash(password), roster=roster_json)
            team.members.extend([Member(name=m.strip()) for m in members.split(",") if m.strip()])
            session.add(team)
            created += 1
            results.append({"name": name, "status": "created"})
        except Exception as e:
            skipped += 1
            results.append({"name": "", "status": "error", "detail": str(e)})

    await session.flush()
    return {"status": "ok", "created": created, "skipped": skipped, "results": results}


@router.post("/validate-team/{team_id}")
async def validate_team(
    team_id: str,
    x_admin_token: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
):
    """Validate a specific team's code against baseline."""
    if not x_admin_token or x_admin_token != os.getenv("ADMIN_TOKEN"):
        raise HTTPException(status_code=403, detail="admin token required")
    
    from ..tasks import validate_team_code
    task = validate_team_code.apply_async(args=[team_id], queue="baseline")
    return {"status": "queued", "task_id": task.id}


@router.post("/validate-all-teams")
async def validate_all_teams(
    x_admin_token: str | None = Header(None),
):
    """Validate all teams against baselines."""
    if not x_admin_token or x_admin_token != os.getenv("ADMIN_TOKEN"):
        raise HTTPException(status_code=403, detail="admin token required")
    
    from ..tasks import validate_all_teams
    task = validate_all_teams.apply_async(queue="baseline")
    return {"status": "queued", "task_id": task.id}


@router.get("/team-status")
async def get_team_status(
    session: AsyncSession = Depends(get_session),
):
    """Get status summary of all teams."""
    from sqlalchemy import func
    
    # Count teams by status
    status_counts = await session.execute(
        select(Team.status, func.count(Team.id))
        .group_by(Team.status)
    )
    status_summary = {status: count for status, count in status_counts.all()}
    
    # Get recent validation results
    recent_teams = await session.execute(
        select(Team.id, Team.name, Team.status)
        .order_by(Team.created_at.desc())
        .limit(20)
    )
    
    teams = []
    for team_id, name, status in recent_teams.all():
        teams.append({
            "id": str(team_id),
            "name": name,
            "status": status
        })
    
    return {
        "status_summary": status_summary,
        "recent_teams": teams
    }
