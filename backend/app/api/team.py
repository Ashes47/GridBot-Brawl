import os
import uuid
import ast
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy import text
import json as _json
import shutil
from passlib.hash import bcrypt

from ..database import get_session
from ..db_models import Member, Team, AppSetting
from ..db_models import Match, MatchQueue, Rating, RatingEvent
from ..tasks import schedule_calibration_for_team
import os
from redis import asyncio as aioredis
from datetime import datetime

router = APIRouter(prefix="/teams", tags=["teams"])

# Directory where uploaded bot files are stored
DATA_DIR = Path(os.getenv("DATA_DIR", "./data/teams"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE_BYTES = 256 * 1024  # 256 KB limit

# mapping from component key to expected class name in code
COMPONENT_CLASS_NAME = {
    "tank": "Tank",
    "sniper": "Sniper",
    "bomber": "Bomber",
    "scout": "Scout",
    "teleporter": "Teleporter",
    "poisoner": "Poisoner",
    "trap_setter": "Trap_Setter",
    "healer": "Healer",
    "shield_giver": "Shield_Giver",
    "puller": "Puller",
    "bruiser": "Bruiser",
    "jammer": "Jammer",
    "reflector": "Reflector",
    "wall_builder": "Wall_Builder",
    "pusher": "Pusher",
    "decoy_caster": "Decoy_Caster",
    "leaper": "Leaper",
    "silencer": "Silencer",
}

# reverse map for class-name → component key
CLASS_NAME_TO_COMPONENT = {v: k for k, v in COMPONENT_CLASS_NAME.items()}

# canonical role values to validate roster
CANONICAL_COMPONENTS = set(COMPONENT_CLASS_NAME.keys())


def _validate_code_contains_classes(code: str, class_names: List[str]) -> None:
    if len(code.encode("utf-8")) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File too large")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise HTTPException(status_code=400, detail=f"Syntax error: {exc}") from exc
    classes = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    missing = [c for c in class_names if c not in classes]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required classes for roster: {', '.join(missing)}")


def _extract_class_names_in_order(code: str) -> List[str]:
    """Return top-level class names in source order. Also enforces file size and syntax validity."""
    if len(code.encode("utf-8")) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File too large")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise HTTPException(status_code=400, detail=f"Syntax error: {exc}") from exc
    return [node.name for node in getattr(tree, 'body', []) if isinstance(node, ast.ClassDef)]


# ---------------- Rate limiting helpers ----------------

async def _check_submit_eval_rate_limit(team_id: str) -> int | None:
    """Returns remaining seconds to wait if limited, else None. Uses RATE_LIMIT_SUBMIT_EVAL_SECONDS only."""
    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    client = aioredis.from_url(url, decode_responses=True)
    secs = int(os.getenv("RATE_LIMIT_SUBMIT_EVAL_SECONDS"))
    ttl = max(1, secs)
    key = f"rate:submit_eval:{team_id}"
    try:
        ok = await client.set(key, "1", nx=True, ex=ttl)
        if ok:
            return None
        rem = await client.ttl(key)
        return int(rem if rem and rem > 0 else ttl)
    finally:
        await client.close()


def _validate_roster_json(roster_json: str) -> List[str]:
    try:
        roster = _json.loads(roster_json or "[]")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid roster JSON")
    if not isinstance(roster, list):
        raise HTTPException(status_code=400, detail="Roster must be a JSON array")
    if len(roster) != 5:
        raise HTTPException(status_code=400, detail="Roster must contain exactly 5 components")
    lowered = [str(x).lower() for x in roster]
    if len(set(lowered)) != 5:
        raise HTTPException(status_code=400, detail="Roster must contain 5 unique components")
    for r in lowered:
        if r not in CANONICAL_COMPONENTS:
            raise HTTPException(status_code=400, detail=f"Invalid component: {r}")
    return lowered


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_team(
    name: str = Form(..., max_length=100),
    members: str = Form(..., description="Comma-separated member names"),
    password: str = Form(..., min_length=6),
    roster: str = Form(None, description="JSON array of 5 component strings (optional if bot_file provided)"),
    bot_file: UploadFile | None = File(None),
    session: AsyncSession = Depends(get_session),
):
    """Register a team. If no roster provided but a bot file is uploaded, auto-detect the first 5 valid component classes found in the file."""
    # Check if signup is enabled (default True if setting absent)
    res = await session.execute(select(AppSetting).where(AppSetting.key == "signup_enabled"))
    row = res.scalar_one_or_none()
    enabled = True
    if row is not None:
        val = (row.value or "").strip().lower()
        enabled = val in ("1", "true", "yes", "on")
    if not enabled:
        raise HTTPException(status_code=403, detail="Signup is currently disabled by admin")
    # ensure unique name
    existing = await session.execute(select(Team).where(Team.name == name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Team name already taken")

    code_str: str | None = None
    if bot_file is not None:
        code_bytes = await bot_file.read()
        try:
            code_str = code_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="File must be UTF-8 text")

    # Determine roster list
    roster_list: List[str]
    if roster not in (None, ""):
        roster_list = _validate_roster_json(roster)
    else:
        if code_str is not None:
            class_order = _extract_class_names_in_order(code_str)
            auto: List[str] = []
            seen: set[str] = set()
            for cls in class_order:
                key = CLASS_NAME_TO_COMPONENT.get(cls)
                if key and key not in seen:
                    auto.append(key)
                    seen.add(key)
                    if len(auto) == 5:
                        break
            if len(auto) < 5:
                raise HTTPException(status_code=400, detail="Could not auto-detect 5 components from uploaded code. Define 5 component classes or select a roster manually.")
            roster_list = auto
        else:
            # No code uploaded, default to first 5 canonical components
            roster_list = list(COMPONENT_CLASS_NAME.keys())[:5]

    required_classes = [COMPONENT_CLASS_NAME[k] for k in roster_list]

    if code_str is None:
        # Minimal safe placeholder bot implementing all classes to ease future roster changes
        code_lines = []
        for cls in COMPONENT_CLASS_NAME.values():
            code_lines.append(
                "class " + cls + ":\n    def decide(self, obs):\n        return {\"type\": \"move\", \"direction\": \"north\"}\n"
            )
        code_str = "\n".join(code_lines)
    else:
        # Validate uploaded code contains required classes
        _validate_code_contains_classes(code_str, required_classes)

    # Persist file on disk
    team_id = uuid.uuid4()
    team_dir = DATA_DIR / str(team_id)
    team_dir.mkdir(parents=True, exist_ok=True)
    file_path = team_dir / "bot.py"
    file_path.write_text(code_str, encoding="utf-8")

    # Persist DB rows
    team = Team(id=team_id, name=name, code_path=str(file_path), password_hash=bcrypt.hash(password), roster=_json.dumps(roster_list))
    member_objs: List[Member] = [Member(name=m.strip()) for m in members.split(",") if m.strip()]
    if len(member_objs) == 0:
        raise HTTPException(status_code=400, detail="At least one member required")
    team.members.extend(member_objs)

    session.add(team)
    await session.flush()

    return {
        "id": str(team.id),
        "name": team.name,
        "members": [m.name for m in team.members],
        "roster": roster_list,
    }


@router.put("/{team_id}")
async def update_team(
    team_id: str,
    name: str = Form(None),
    members: str = Form(None),
    password: str = Form(...),
    roster: str = Form(None, description="JSON array of 5 component strings"),
    bot_file: UploadFile = File(None),
    session: AsyncSession = Depends(get_session),
):
    try:
        tid = uuid.UUID(team_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid team id")

    # fetch team
    result = await session.execute(select(Team).where(Team.id == tid))
    team: Team | None = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    from passlib.hash import bcrypt

    # verify password
    if not bcrypt.verify(password, team.password_hash):
        raise HTTPException(status_code=403, detail="Invalid password")

    # if roster provided, validate values and optionally code support
    roster_list: Optional[List[str]] = None
    if roster is not None:
        roster_list = _validate_roster_json(roster)

    # handle bot file update
    if bot_file is not None:
        code_bytes = await bot_file.read()
        code_str = code_bytes.decode("utf-8", errors="replace")
        # Determine which roster to validate against (incoming or existing)
        roster_for_validation = roster_list if roster_list is not None else (_json.loads(team.roster) if team.roster else [])
        required = [COMPONENT_CLASS_NAME[k] for k in roster_for_validation] if roster_for_validation else []
        if required:
            _validate_code_contains_classes(code_str, required)
        # overwrite existing file
        file_path = Path(team.code_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(code_str, encoding="utf-8")

    # update roster only (no code): validate existing file contains necessary classes
    if roster_list is not None and bot_file is None:
        try:
            code_on_disk = Path(team.code_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            raise HTTPException(status_code=400, detail="Team code file missing on server")
        required = [COMPONENT_CLASS_NAME[k] for k in roster_list]
        _validate_code_contains_classes(code_on_disk, required)
        team.roster = _json.dumps(roster_list)

    # update name / members
    if name:
        team.name = name
    if members is not None:
        team.members.clear()
        team.members.extend([Member(name=m.strip()) for m in members.split(",") if m.strip()])

    await session.flush()

    # delete matches involving team (to be re-scheduled)
    from ..db_models import Match
    await session.execute(
        Match.__table__.delete().where(Match.team_ids.like(f"%{team_id}%"))
    )

    return {"status": "updated"}

# ------------------------------------------------------------ Admin password reset


import os

ADMIN_SECRET = os.getenv("ADMIN_PASSWORD")


@router.post("/{team_id}/reset_password")
async def reset_password(
    team_id: str,
    admin_password: str = Form(...),
    new_password: str = Form(..., min_length=6),
    session: AsyncSession = Depends(get_session),
):
    if admin_password != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin password")

    try:
        tid = uuid.UUID(team_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid team id")

    result = await session.execute(select(Team).where(Team.id == tid))
    team: Team | None = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    team.password_hash = bcrypt.hash(new_password)
    await session.flush()
    return {"status": "password_reset"}


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    team_id: str,
    admin_password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Delete a team and its code, requires admin password."""
    if admin_password != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin password")

    try:
        tid = uuid.UUID(team_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid team id")

    # Fetch team
    result = await session.execute(select(Team).where(Team.id == tid))
    team: Team | None = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    # Collect match logs for cleanup and delete DB rows related to this team
    # 1) Collect matches (ids and log paths)
    rows = await session.execute(select(Match.id, Match.log_path).where(Match.team_ids.like(f"%{team_id}%")))
    match_rows = rows.all()
    # 2) Delete queue entries involving this team
    await session.execute(MatchQueue.__table__.delete().where(MatchQueue.team_ids.contains([tid])))
    # 3) Delete matches
    await session.execute(Match.__table__.delete().where(Match.team_ids.like(f"%{team_id}%")))
    # 4) Delete rating events and ratings for this team
    await session.execute(RatingEvent.__table__.delete().where(RatingEvent.team_id == tid))
    await session.execute(Rating.__table__.delete().where(Rating.team_id == tid))
    # 5) Delete the team row
    await session.delete(team)
    await session.flush()

    # Delete team code directory from disk
    team_dir = DATA_DIR / str(team_id)
    if team_dir.exists():
        shutil.rmtree(team_dir)
    # Delete match logs from data/matches
    try:
        from pathlib import Path as _Path
        base = _Path(os.getenv("DATA_DIR", "./data/matches"))
        for mid, log_path in match_rows:
            try:
                p = _Path(log_path or "")
                if p.is_absolute() and p.exists():
                    p.unlink(missing_ok=True)
                else:
                    cand = base / (p.name if p.name else str(p))
                    if cand.exists():
                        cand.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass

    return


@router.get("/{team_id}")
async def get_team(team_id: str, session: AsyncSession = Depends(get_session)):
    """Retrieve a team record by ID."""
    try:
        tid = uuid.UUID(team_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid team id")

    from sqlalchemy.orm import selectinload
    result = await session.execute(select(Team).options(selectinload(Team.members)).where(Team.id == tid))
    team: Optional[Team] = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    return {
        "id": str(team.id),
        "name": team.name,
        "created_at": team.created_at,
        "members": [m.name for m in team.members],
        "roster": (_json.loads(team.roster) if team.roster else None),
        "status": getattr(team, 'status', 'pending'),  # Include team status
        "last_validated": getattr(team, 'last_validated', None),
        "last_error": getattr(team, 'last_error', None),
    }


# List teams endpoint


@router.get("/", response_model=list[dict])
async def list_teams(search: str | None = None, limit: int = 100, session: AsyncSession = Depends(get_session)):
    """Return a list of recent teams (default limit 100)."""
    from sqlalchemy import func
    query = (
        select(Team.id, Team.name, Team.created_at, Team.status, func.count(Member.id).label("mc"))
        .select_from(Team)
        .outerjoin(Member)
        .group_by(Team.id, Team.status)
        .order_by(Team.created_at.desc())
    )
    # Exclude baselines from directory when BASELINES_VISIBLE=false
    show_baselines = os.getenv("BASELINES_VISIBLE", "false").lower() in ("1","true","yes")
    if not show_baselines:
        baseline_names = ["Baseline", "Baseline-A", "Baseline-B", "Baseline-C"]
        query = query.where(~Team.name.in_(baseline_names))
    if search:
        query = query.where(Team.name.ilike(f"%{search}%"))
    result = await session.execute(query.limit(limit))
    rows = result.all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "created_at": r.created_at,
            "member_count": r.mc,
            "status": getattr(r, 'status', 'pending'),
            "last_validated": getattr(r, 'last_validated', None),
            "last_error": getattr(r, 'last_error', None),
        }
        for r in rows
    ]

# public teams page helper same as list


# ------------------------------------------------------------ Matches for a team


@router.get("/{team_id}/matches")
async def team_matches(team_id: str, session: AsyncSession = Depends(get_session)):
    try:
        tid = uuid.UUID(team_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid team id")

    from ..db_models import Match, Team as TeamDB

    rows = await session.execute(select(Match).where(Match.team_ids.like(f"%{tid}%"), Match.status == "finished"))
    matches = list(rows.scalars().all())

    # Build a single map of team_id -> name for all involved teams
    all_ids: set[str] = set()
    for m in matches:
        all_ids.update(m.team_ids.split(","))
    id_to_name: dict[str, str] = {}
    if all_ids:
        # cast to UUIDs that parse
        valid_uuids = []
        for s in all_ids:
            try:
                valid_uuids.append(uuid.UUID(s))
            except Exception:
                continue
        if valid_uuids:
            t_res = await session.execute(select(TeamDB).where(TeamDB.id.in_(valid_uuids)))
            for t in t_res.scalars().all():
                id_to_name[str(t.id)] = t.name

    data = []
    for m in matches:
        ids = m.team_ids.split(",")
        outcome = "draw"
        if m.winner_team_id:
            outcome = "win" if str(m.winner_team_id) == team_id else "loss"
        opponents = [i for i in ids if i != team_id]
        hp_map = _json.loads(m.team_hp) if m.team_hp else {}
        dmg_map = _json.loads(m.team_damage) if getattr(m, 'team_damage', None) else {}
        is_baseline = any(id_to_name.get(op, "").lower() == "baseline" for op in opponents)
        data.append(
            {
                "match_id": str(m.id),
                "mode": m.mode,
                "opponents": opponents,
                "opponent_names": [id_to_name.get(op, op) for op in opponents],
                "outcome": outcome,
                "hp_left": hp_map.get(team_id, 0),
                "damage_done": dmg_map.get(team_id, 0),
                "is_baseline": is_baseline,
                "map_name": m.map_name,
                "map_seed": m.map_seed,
                "ranks_order": [str(t) for t in (m.ranks_order or [])],
                "ranks_map": m.ranks_map,
                "trueskill_delta": None,
            }
        )
    return data


@router.get("/{team_id}/queue_status")
async def team_queue_status(team_id: str, session: AsyncSession = Depends(get_session)):
    """Return counts of pending and running matches for this team since midnight UTC."""
    try:
        tid = uuid.UUID(team_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid team id")

    from ..db_models import Match, MatchQueue
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    pend = await session.execute(select(Match).where(Match.team_ids.like(f"%{tid}%"), Match.status == "pending", Match.created_at >= today))
    run = await session.execute(select(Match).where(Match.team_ids.like(f"%{tid}%"), Match.status == "running", Match.created_at >= today))
    queued = await session.execute(select(MatchQueue).where(MatchQueue.team_ids.any(tid), MatchQueue.status == "queued"))
    return {"pending": len([*pend.scalars().all()]), "running": len([*run.scalars().all()]), "queued": len([*queued.scalars().all()])}


@router.post("/{team_id}/submit_for_evaluation")
async def submit_for_evaluation(team_id: str, password: str = Form(...), session: AsyncSession = Depends(get_session)):
    """Submit a team for leaderboard evaluation: schedule duo and quad matches via Celery."""
    try:
        tid = uuid.UUID(team_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid team id")

    result = await session.execute(select(Team).where(Team.id == tid))
    team: Team | None = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    from passlib.hash import bcrypt
    if not bcrypt.verify(password, team.password_hash):
        raise HTTPException(status_code=403, detail="Invalid password")

    # Rate-limit submit for evaluation by team (configurable hours)
    limited_secs = await _check_submit_eval_rate_limit(team_id)
    if limited_secs is not None:
        from fastapi import Response
        # send 429 with Retry-After
        raise HTTPException(status_code=429, detail=f"Submit for evaluation allowed again in {limited_secs} seconds")

    # enqueue calibration scheduling (DUO-first, QUAD if enabled)
    schedule_calibration_for_team.apply_async(args=[team_id], queue="simulation")

    return {"status": "queued"}
