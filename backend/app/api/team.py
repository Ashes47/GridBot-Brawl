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

from ..database import get_session
from ..db_models import Member, Team

router = APIRouter(prefix="/teams", tags=["teams"])

# Directory where uploaded bot files are stored
DATA_DIR = Path(os.getenv("DATA_DIR", "./data/teams"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

REQUIRED_BOT_CLASSES = {"Sniper", "Tank", "Bomber", "Scout", "Teleporter"}
MAX_FILE_SIZE_BYTES = 32 * 1024  # 32 KB limit


def _validate_bot_file(code: str) -> None:
    """Parse the Python code and check for required bot class names."""
    if len(code.encode("utf-8")) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File too large")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise HTTPException(status_code=400, detail=f"Syntax error: {exc}") from exc

    class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    missing = REQUIRED_BOT_CLASSES - class_names
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required classes: {', '.join(sorted(missing))}",
        )


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_team(
    name: str = Form(..., max_length=100),
    members: str = Form(..., description="Comma-separated member names"),
    password: str = Form(..., min_length=6),
    bot_file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    """Register a team and upload its bot code."""
    # ensure unique name
    existing = await session.execute(select(Team).where(Team.name == name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Team name already taken")

    # Read file content
    code_bytes = await bot_file.read()
    try:
        code_str = code_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 text")

    _validate_bot_file(code_str)

    # Persist file on disk
    team_id = uuid.uuid4()
    team_dir = DATA_DIR / str(team_id)
    team_dir.mkdir(parents=True, exist_ok=True)
    file_path = team_dir / "bot.py"
    file_path.write_text(code_str, encoding="utf-8")

    # Persist DB rows
    from passlib.hash import bcrypt
    team = Team(id=team_id, name=name, code_path=str(file_path), password_hash=bcrypt.hash(password))
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
    }


# ------------------------------------------------------------ Update team code / name / members


@router.put("/{team_id}")
async def update_team(
    team_id: str,
    name: str = Form(None),
    members: str = Form(None),
    password: str = Form(...),
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

    # handle bot file update
    if bot_file is not None:
        code_bytes = await bot_file.read()
        code_str = code_bytes.decode("utf-8", errors="replace")
        _validate_bot_file(code_str)

        # overwrite existing file
        file_path = Path(team.code_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(code_str, encoding="utf-8")

    # update name / members
    if name:
        team.name = name
    if members is not None:
        # clear old members
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

    # Delete from DB
    await session.delete(team)
    await session.flush()

    # Delete code from disk
    team_dir = DATA_DIR / str(team_id)
    if team_dir.exists():
        shutil.rmtree(team_dir)

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
    }


# List teams endpoint


@router.get("/", response_model=list[dict])
async def list_teams(search: str | None = None, limit: int = 100, session: AsyncSession = Depends(get_session)):
    """Return a list of recent teams (default limit 100)."""
    from sqlalchemy import func
    query = (
        select(Team.id, Team.name, Team.created_at, func.count(Member.id).label("mc"))
        .select_from(Team)
        .outerjoin(Member)
        .group_by(Team.id)
        .order_by(Team.created_at.desc())
    )
    # exclude Baseline from directory
    query = query.where(Team.name != "Baseline")
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
            }
        )
    return data
