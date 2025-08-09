from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import List

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .celery_app import celery_app
from .sync_database import SessionLocal
from .db_models import Match, Team

# We'll bridge to the existing async simulation by running the core logic in a sync wrapper
import asyncio
from .simulation import simulate_match
from itertools import combinations

# Create async engine/sessionmaker per worker thread to avoid cross-event-loop locks
import threading
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from .database import DATABASE_URL

_thread_local = threading.local()

def get_async_sessionmaker():
    sm = getattr(_thread_local, "async_sessionmaker", None)
    if sm is None:
        engine = create_async_engine(DATABASE_URL, echo=False, future=True)
        sm = async_sessionmaker(bind=engine, expire_on_commit=False)
        _thread_local.async_sessionmaker = sm
        _thread_local.async_engine = engine
    return sm


def _run_async(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_running():
        # In Celery workers this should not happen; fallback
        return asyncio.run(coro)
    return loop.run_until_complete(coro)


@celery_app.task(name="app.tasks.run_match")
def run_match(match_id: str, team_ids: List[str]) -> dict | None:
    """Run a match and update DB rows (sync SQLAlchemy session)."""
    with SessionLocal() as session:  # type: Session
        # mark running and assign map seed/name upfront
        import os, random
        s = os.getenv("FORCE_MAP_SEED")
        seed = int(s) if s and s.isdigit() else random.randint(1, 2**31 - 1)
        map_name = f"seed_map_{seed}"
        session.execute(
            update(Match)
            .where(Match.id == uuid.UUID(match_id))
            .values(status="running", map_seed=seed, map_name=map_name)
        )
        session.commit()
        try:
            async def _go():
                AsyncSessionMaker = get_async_sessionmaker()
                async with AsyncSessionMaker() as a_sess:
                    return await simulate_match(a_sess, team_ids, seed=seed)

            result = _run_async(_go())

            session.execute(
                update(Match)
                .where(Match.id == uuid.UUID(match_id))
                .values(
                    status="finished",
                    winner_team_id=uuid.UUID(result["winner_team_id"]) if result["winner_team_id"] else None,
                    log_path=result["log_path"],
                    team_hp=json.dumps(result["team_hp"]),
                    team_damage=json.dumps(result["team_damage"]),
                    map_name=result.get("map_name"),
                    map_seed=int(result.get("map_seed") or 0),
                )
            )
            session.commit()
            return result
        except FileNotFoundError:
            session.execute(
                update(Match).where(Match.id == uuid.UUID(match_id)).values(status="error")
            )
            session.commit()
            return None
        except Exception:
            session.execute(
                update(Match).where(Match.id == uuid.UUID(match_id)).values(status="error")
            )
            session.commit()
            raise


@celery_app.task(name="app.tasks.run_baseline_test")
def run_baseline_test(team_id: str, baseline_roster: List[str] | None = None) -> dict:
    """Create baseline team if needed, create Match, and enqueue run_match on baseline queue.
    If baseline_roster provided, temporarily set baseline Team.roster to those 5 roles for this match.
    Returns {"match_id": str}.
    """
    from pathlib import Path

    with SessionLocal() as session:
        # Ensure team exists
        try:
            tid = uuid.UUID(team_id)
        except ValueError:
            raise ValueError("Invalid team id")
        t = session.get(Team, tid)
        if not t:
            raise ValueError("team not found")

        # Write baseline bot to a temp path accessible to sandbox
        base_dir = Path(os.getenv("DATA_DIR", "./data/teams")) / "baseline"
        base_dir.mkdir(parents=True, exist_ok=True)
        bot_path = base_dir / "bot.py"
        # Always refresh baseline code to pick up latest changes
        src = Path(__file__).resolve().parent / "engine" / "baseline_bot.py"
        bot_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

        # Create or reuse Team row for baseline
        name = "Baseline"
        baseline_team = session.execute(select(Team).where(Team.name == name)).scalar_one_or_none()
        if not baseline_team:
            baseline_team = Team(name=name, code_path=str(bot_path), password_hash="!")
            session.add(baseline_team)
            session.flush()
        else:
            # Ensure code path up to date
            baseline_team.code_path = str(bot_path)

        # Optionally set roster
        if baseline_roster and len(baseline_roster) == 5:
            baseline_team.roster = json.dumps(baseline_roster)
        else:
            baseline_team.roster = None  # fallback to default

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
        session.flush()
        session.commit()

    # enqueue run_match
    run_match.apply_async(args=[str(match_id), [team_id, str(baseline_team.id)]], queue="baseline")
    return {"match_id": str(match_id)}


@celery_app.task(name="app.tasks.schedule_evaluation_for_team")
def schedule_evaluation_for_team(team_id: str) -> dict:
    """Schedule duo and quad matches involving this team vs all others.
    Returns counts of matches created. Actual execution delegated to run_match tasks.
    """
    created = {"duo": 0, "quad": 0}
    with SessionLocal() as session:
        # Fetch all teams except Baseline
        rows = session.execute(select(Team.id, Team.name)).all()
        team_ids = [str(tid) for tid, name in rows if (name or "").lower() != "baseline"]
        if team_id not in team_ids:
            team_ids.append(team_id)
        # dedupe
        team_ids = list(dict.fromkeys(team_ids))

        # create duo against all others
        for other in team_ids:
            if other == team_id:
                continue
            ids = sorted([team_id, other])
            existing = session.execute(select(Match).where(Match.mode == "duo", Match.team_ids == ",".join(ids))).scalar_one_or_none()
            if existing:
                continue
            match_id = uuid.uuid4()
            m = Match(
                id=match_id,
                mode="duo",
                team_ids=",".join(ids),
                status="pending",
                log_path="",
                created_at=datetime.utcnow(),
            )
            session.add(m)
            session.flush()
            run_match.apply_async(args=[str(match_id), ids], queue="simulation")
            created["duo"] += 1

        # create quads that include this team where possible (pick any 3 others)
        others = [t for t in team_ids if t != team_id]
        # simple strategy: chunk by 3
        for i in range(0, len(others), 3):
            chunk = others[i:i+3]
            if len(chunk) < 3:
                break
            ids = [team_id] + chunk
            key = ",".join(sorted(ids))
            existing = session.execute(select(Match).where(Match.mode == "quad", Match.team_ids == key)).scalar_one_or_none()
            if existing:
                continue
            match_id = uuid.uuid4()
            m = Match(
                id=match_id,
                mode="quad",
                team_ids=key,
                status="pending",
                log_path="",
                created_at=datetime.utcnow(),
            )
            session.add(m)
            session.flush()
            run_match.apply_async(args=[str(match_id), ids], queue="simulation")
            created["quad"] += 1
        session.commit()
    return created


@celery_app.task(name="app.tasks.schedule_full_evaluation")
def schedule_full_evaluation() -> dict:
    """Schedule duo and quad matches across all teams (excluding Baseline).
    Returns counts of matches created.
    """
    created = {"duo": 0, "quad": 0}
    with SessionLocal() as session:
        rows = session.execute(select(Team.id, Team.name)).all()
        team_ids = [str(tid) for tid, name in rows if (name or "").lower() != "baseline"]

        # Duo combinations
        for a, b in combinations(team_ids, 2):
            ids = sorted([a, b])
            key = ",".join(ids)
            existing = session.execute(
                select(Match).where(Match.mode == "duo", Match.team_ids == key)
            ).scalar_one_or_none()
            if existing:
                continue
            match_id = uuid.uuid4()
            m = Match(
                id=match_id,
                mode="duo",
                team_ids=key,
                status="pending",
                log_path="",
                created_at=datetime.utcnow(),
            )
            session.add(m)
            session.flush()
            run_match.apply_async(args=[str(match_id), ids], queue="simulation")
            created["duo"] += 1

        # Quad combinations
        for q in combinations(team_ids, 4):
            ids = list(q)
            key = ",".join(sorted(ids))
            existing = session.execute(
                select(Match).where(Match.mode == "quad", Match.team_ids == key)
            ).scalar_one_or_none()
            if existing:
                continue
            match_id = uuid.uuid4()
            m = Match(
                id=match_id,
                mode="quad",
                team_ids=key,
                status="pending",
                log_path="",
                created_at=datetime.utcnow(),
            )
            session.add(m)
            session.flush()
            run_match.apply_async(args=[str(match_id), ids], queue="simulation")
            created["quad"] += 1
        session.commit()
    return created 