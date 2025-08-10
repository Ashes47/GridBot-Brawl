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
from .db_models import Match, Team, Rating, RatingEvent, MatchQueue
from .rating_service import apply_match_ratings

# We'll bridge to the existing async simulation by running the core logic in a sync wrapper
import asyncio
from .simulation import simulate_match
from itertools import combinations
from datetime import timedelta
import math

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
def run_match(match_id: str, team_ids: List[str], queue_id: str | None = None) -> dict | None:
    """Run a match and update DB rows (sync SQLAlchemy session)."""
    with SessionLocal() as session:  # type: Session
        # mark running and assign map seed/name upfront
        import os, random
        s = os.getenv("FORCE_MAP_SEED")
        seed = int(s) if s and s.isdigit() else random.randint(1, 2**31 - 1)
        map_name = f"seed_map_{seed}"
        # Ensure Match still exists (may have been cleared by reset)
        m_row = session.get(Match, uuid.UUID(match_id))
        if not m_row:
            return None
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
                    return await simulate_match(a_sess, team_ids, seed=seed, match_id=match_id)

            # Validate team ids still exist
            existing_team_ids = {str(tid) for (tid,) in session.execute(select(Team.id)).all()}
            if any(t not in existing_team_ids for t in team_ids):
                session.execute(update(Match).where(Match.id == uuid.UUID(match_id)).values(status="error"))
                session.commit()
                return None

            result = _run_async(_go())

            # Persist ranks_order and ranks_map if present
            update_values = {
                "status": "finished",
                "winner_team_id": uuid.UUID(result["winner_team_id"]) if result["winner_team_id"] else None,
                "log_path": result["log_path"],
                "team_hp": json.dumps(result["team_hp"]),
                "team_damage": json.dumps(result["team_damage"]),
                "map_name": result.get("map_name"),
                "map_seed": int(result.get("map_seed") or 0),
            }
            if "ranks_order" in result:
                update_values["ranks_order"] = [uuid.UUID(tid) for tid in result["ranks_order"]]
            if "ranks_map" in result:
                update_values["ranks_map"] = result["ranks_map"]

            session.execute(
                update(Match)
                .where(Match.id == uuid.UUID(match_id))
                .values(**update_values)
            )
            session.commit()
            # Mark queue item done if provided
            if queue_id:
                try:
                    session.execute(
                        update(MatchQueue)
                        .where(MatchQueue.id == uuid.UUID(queue_id))
                        .values(status="done")
                    )
                    session.commit()
                except Exception:
                    pass
            # Apply TrueSkill rating updates if ranks_map present
            try:
                ranks_map = result.get("ranks_map") if result else None
                if ranks_map:
                    # Maintain team_ids order used for simulation
                    apply_match_ratings(session, session.get(Match, uuid.UUID(match_id)), team_ids, ranks_map)
                    session.commit()
            except Exception as exc:
                # Do not fail the task if rating update fails; log and continue
                import logging
                logging.exception("Rating update failed for match %s", match_id, exc_info=exc)
            return result
        except FileNotFoundError:
            session.execute(
                update(Match).where(Match.id == uuid.UUID(match_id)).values(status="error")
            )
            session.commit()
            if queue_id:
                try:
                    session.execute(
                        update(MatchQueue)
                        .where(MatchQueue.id == uuid.UUID(queue_id))
                        .values(status="failed", last_error="FileNotFoundError")
                    )
                    session.commit()
                except Exception:
                    pass
            return None
        except Exception:
            session.execute(
                update(Match).where(Match.id == uuid.UUID(match_id)).values(status="error")
            )
            session.commit()
            if queue_id:
                try:
                    session.execute(
                        update(MatchQueue)
                        .where(MatchQueue.id == uuid.UUID(queue_id))
                        .values(status="failed", last_error="Exception during run_match")
                    )
                    session.commit()
                except Exception:
                    pass
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


# ------------------------------------------------------------- Match queue & consumer


def enqueue_match(mode: str, team_ids: List[str], priority: str = "normal") -> dict:
    """Insert a MatchQueue row and return its id."""
    import uuid as _uuid
    qid = _uuid.uuid4()
    with SessionLocal() as session:
        mq = MatchQueue(
            id=qid,
            mode=mode,
            team_ids=[_uuid.UUID(t) for t in team_ids],
            priority=priority,
            status="queued",
            created_at=datetime.utcnow(),
            attempts=0,
        )
        session.add(mq)
        session.commit()
    return {"queue_id": str(qid)}


@celery_app.task(name="app.tasks.queue_consumer_once")
def queue_consumer_once() -> dict:
    """Pop one queued item observing per-team per-mode concurrency, create Match row, and enqueue run_match."""
    from sqlalchemy import and_
    processed = 0
    with SessionLocal() as session:
        # Fetch one queued item
        row = (
            session.query(MatchQueue)
            .filter(MatchQueue.status == "queued")
            .order_by(MatchQueue.created_at.asc())
            .first()
        )
        if not row:
            return {"processed": 0}

        team_ids = [str(tid) for tid in row.team_ids]

        # Concurrency guard: at most 1 running per team per mode
        for tid in team_ids:
            exists = (
                session.query(Match)
                .filter(
                    and_(
                        Match.mode == row.mode,
                        Match.status == "running",
                        Match.team_ids.like(f"%{tid}%"),
                    )
                )
                .first()
            )
            if exists:
                # Skip for now; leave queued
                return {"processed": 0}

        # Create Match row
        match_id = uuid.uuid4()
        m = Match(
            id=match_id,
            mode=row.mode,
            team_ids=",".join(sorted(team_ids)),
            status="pending",
            log_path="",
            created_at=datetime.utcnow(),
        )
        session.add(m)
        session.flush()
        # Mark queue running and enqueue simulation
        session.execute(
            update(MatchQueue).where(MatchQueue.id == row.id).values(status="running", attempts=row.attempts + 1)
        )
        session.commit()
        run_match.apply_async(args=[str(match_id), team_ids, str(row.id)], queue="simulation")
        processed = 1
    return {"processed": processed, "match_id": str(match_id), "queue_id": str(row.id)}


# ------------------------------------------------------------- Scheduler helpers


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


def _baseline_ids(session: Session) -> List[str]:
    res = session.execute(select(Team.id).where(Team.name.in_(["Baseline-A","Baseline-B","Baseline-C"])) ).all()
    return [str(r[0]) for r in res]


def _ratings_for_mode(session: Session, mode: str) -> dict[str, tuple[float,float]]:
    rows = session.execute(select(Rating.team_id, Rating.mu, Rating.sigma).where(Rating.mode == mode)).all()
    return {str(tid): (float(mu), float(sigma)) for tid, mu, sigma in rows}


def _recent_pair_blocked(session: Session, mode: str, team_ids: List[str], hours: int) -> bool:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    key = ",".join(sorted(team_ids))
    exists = (
        session.query(Match)
        .filter(Match.mode == mode, Match.team_ids == key, Match.created_at >= cutoff)
        .first()
    )
    return bool(exists)


def _choose_nearby_opponents(mu_map: dict[str, tuple[float,float]], target_id: str, band: float, exclude: set[str]) -> List[str]:
    if target_id not in mu_map:
        return []
    target_mu = mu_map[target_id][0]
    cands = [tid for tid in mu_map.keys() if tid != target_id and tid not in exclude]
    cands.sort(key=lambda tid: abs(mu_map[tid][0] - target_mu))
    nearby = [tid for tid in cands if abs(mu_map[tid][0] - target_mu) <= band]
    return nearby


def _choose_higher_lower(mu_map: dict[str, tuple[float,float]], target_id: str, band: float, factor: float, exclude: set[str]) -> tuple[List[str], List[str]]:
    target_mu = mu_map.get(target_id, (25.0, 8.33))[0]
    cands = [tid for tid in mu_map.keys() if tid != target_id and tid not in exclude]
    higher = [tid for tid in cands if mu_map[tid][0] > target_mu + band and mu_map[tid][0] <= target_mu + factor * band]
    lower = [tid for tid in cands if mu_map[tid][0] < target_mu - band and mu_map[tid][0] >= target_mu - factor * band]
    # sort by distance
    higher.sort(key=lambda tid: mu_map[tid][0] - target_mu)
    lower.sort(key=lambda tid: target_mu - mu_map[tid][0])
    return higher, lower


def _enqueue_duo(session: Session, a: str, b: str, priority: str, repeat_hours: int) -> bool:
    if _recent_pair_blocked(session, "duo", [a,b], repeat_hours):
        return False
    enqueue_match("duo", [a,b], priority)
    return True


@celery_app.task(name="app.tasks.schedule_calibration_for_team")
def schedule_calibration_for_team(team_id: str) -> dict:
    """Calibration per spec (DUO+QUAD). Uses env knobs; enqueues to match_queue."""
    created = {"duo": 0, "quad": 0}
    band_duo = _env_float("BAND_WIDTH_DUO", 3.0)
    band_quad = _env_float("BAND_WIDTH_QUAD", 3.5)
    cal_duo_baseline = _env_int("CAL_DUO_BASELINE", 2)
    cal_duo_near = _env_int("CAL_DUO_NEAR", 4)
    cal_duo_follow = _env_int("CAL_DUO_FOLLOW", 6)
    cal_quad_baseline = _env_int("CAL_QUAD_BASELINE", 2)
    cal_quad_near = _env_int("CAL_QUAD_NEAR", 4)
    cal_quad_follow = _env_int("CAL_QUAD_FOLLOW", 6)
    max_gap = _env_float("MAX_CALIB_GAP", 6.0)
    repeat_hours = _env_int("MAX_REPEAT_WINDOW_HOURS", 24)

    with SessionLocal() as session:
        baselines = _baseline_ids(session)
        ratings_duo = _ratings_for_mode(session, "duo")
        ratings_quad = _ratings_for_mode(session, "quad")

        # Ensure rating rows exist for target team
        if team_id not in ratings_duo:
            session.add(Rating(team_id=uuid.UUID(team_id), mode="duo", mu=25.0, sigma=25.0/3.0))
        if team_id not in ratings_quad:
            session.add(Rating(team_id=uuid.UUID(team_id), mode="quad", mu=25.0, sigma=25.0/3.0))
        session.commit()
        ratings_duo = _ratings_for_mode(session, "duo")
        ratings_quad = _ratings_for_mode(session, "quad")

        # DUO: baseline matches vs first two baselines if enabled
        if _env_bool("BASELINE_ENABLED", True) and cal_duo_baseline > 0 and len(baselines) >= 2:
            for bid in baselines[:2]:
                if _enqueue_duo(session, team_id, bid, "calibration", repeat_hours):
                    created["duo"] += 1

        # DUO: near opponents
        exclude = set([team_id] + baselines)
        near = _choose_nearby_opponents(ratings_duo, team_id, band_duo, exclude)[:cal_duo_near]
        for oid in near:
            if abs(ratings_duo[oid][0] - ratings_duo[team_id][0]) <= max_gap:
                if _enqueue_duo(session, team_id, oid, "calibration", repeat_hours):
                    created["duo"] += 1

        # DUO: follow-ups (skew higher or lower by current mu; adaptive simplification)
        higher, lower = _choose_higher_lower(ratings_duo, team_id, band_duo, 2.0, exclude)
        pool = (higher + near + lower)
        added = 0
        for oid in pool:
            if oid in near:
                continue
            if abs(ratings_duo.get(oid, (25.0,0))[0] - ratings_duo[team_id][0]) > max_gap:
                continue
            if _enqueue_duo(session, team_id, oid, "calibration", repeat_hours):
                created["duo"] += 1
                added += 1
                if added >= cal_duo_follow:
                    break

        # QUAD calibration pods: {team, A, B, C} twice
        if _env_bool("ENABLE_QUAD", False) and _env_bool("BASELINE_ENABLED", True) and len(baselines) >= 3 and cal_quad_baseline > 0:
            pod = [team_id] + baselines[:3]
            for _ in range(min(2, cal_quad_baseline)):
                enqueue_match("quad", pod, "calibration")
                created["quad"] += 1

        # QUAD: near pods: pick 3 opponents by μ around target
        if _env_bool("ENABLE_QUAD", False):
            others = [tid for tid in ratings_quad.keys() if tid not in set([team_id] + baselines)]
            # simple heuristic: pick one slightly higher, one slightly lower, one within band if possible
            higher, lower = _choose_higher_lower(ratings_quad, team_id, band_quad, 2.0, set([team_id] + baselines))
            within = _choose_nearby_opponents(ratings_quad, team_id, band_quad, set([team_id] + baselines))
            def pick3():
                sel = []
                if higher: sel.append(higher[0])
                if lower: sel.append(lower[0])
                for c in within:
                    if c not in sel:
                        sel.append(c); break
                while len(sel) < 3 and others:
                    x = others.pop(0)
                    if x not in sel:
                        sel.append(x)
                return sel if len(sel) == 3 else None
            pods_added = 0
            for _ in range(cal_quad_near):
                sel = pick3()
                if not sel:
                    break
                enqueue_match("quad", [team_id] + sel, "calibration")
                created["quad"] += 1
                pods_added += 1
            # follow-up pods skewed higher or lower (simplified)
            pods_follow = 0
            for trio in [higher[:2] + within[:1], lower[:2] + within[:1]]:
                trio = [x for x in trio if x not in baselines and x != team_id]
                if len(trio) >= 3:
                    enqueue_match("quad", [team_id] + trio[:3], "calibration")
                    created["quad"] += 1
                    pods_follow += 1
                if pods_follow >= cal_quad_follow:
                    break
    return created


@celery_app.task(name="app.tasks.schedule_ongoing")
def schedule_ongoing() -> dict:
    """Ongoing scheduler to keep quotas per day for each team and mode."""
    created = {"duo": 0, "quad": 0}
    repeat_hours = _env_int("MAX_REPEAT_WINDOW_HOURS", 24)
    daily_duo_base = _env_int("DAILY_DUO", 2)
    daily_quad_base = _env_int("DAILY_QUAD", 1)
    sigma_target = _env_float("SIGMA_TARGET", 2.5)
    extra_per_sigma = _env_float("EXTRA_MATCH_PER_SIGMA", 1.0)  # matches per (sigma - target)
    max_daily_extra = _env_int("MAX_DAILY_EXTRA", 3)
    max_daily_duo = _env_int("MAX_DAILY_DUO", daily_duo_base + max_daily_extra)
    max_daily_quad = _env_int("MAX_DAILY_QUAD", daily_quad_base + max_daily_extra)
    band_duo = _env_float("BAND_WIDTH_DUO", 3.0)
    band_quad = _env_float("BAND_WIDTH_QUAD", 3.5)
    with SessionLocal() as session:
        ratings_duo = _ratings_for_mode(session, "duo")
        ratings_quad = _ratings_for_mode(session, "quad")
        all_team_ids = [str(tid) for (tid,) in session.execute(select(Team.id)).all()]
        baselines = set(_baseline_ids(session))
        # Count pending+running today per team/mode
        now = datetime.utcnow()
        start = datetime(now.year, now.month, now.day)
        def count_active(mode: str, tid: str) -> int:
            key_like = f"%{tid}%"
            return (
                session.query(Match)
                .filter(Match.mode == mode, Match.created_at >= start, Match.status.in_(["pending","running"]), Match.team_ids.like(key_like))
                .count()
            )

        def dynamic_daily(mode: str, tid: str) -> int:
            mu_map = ratings_duo if mode == "duo" else ratings_quad
            base = daily_duo_base if mode == "duo" else daily_quad_base
            if tid not in mu_map:
                return base
            sigma = float(mu_map[tid][1])
            extra = 0
            if sigma > sigma_target:
                extra = int(max(0, math.ceil((sigma - sigma_target) * extra_per_sigma)))
            extra = min(extra, max_daily_extra)
            cap = max_daily_duo if mode == "duo" else max_daily_quad
            return min(cap, base + extra)

        # Prioritize high-uncertainty teams by sorting order
        duo_order = sorted([t for t in all_team_ids if t not in baselines], key=lambda tid: ratings_duo.get(tid, (25.0, 8.33))[1], reverse=True)
        quad_order = sorted([t for t in all_team_ids if t not in baselines], key=lambda tid: ratings_quad.get(tid, (25.0, 8.33))[1], reverse=True)

        # DUO sampling per team (high sigma first)
        for t in duo_order:
            if t in baselines:
                continue
            have = count_active("duo", t)
            daily_duo = dynamic_daily("duo", t)
            need = max(0, daily_duo - have)
            if need == 0:
                continue
            mu_map = ratings_duo
            if t not in mu_map:
                continue
            # 70/20/10 split
            near = _choose_nearby_opponents(mu_map, t, band_duo, {t})
            higher, lower = _choose_higher_lower(mu_map, t, band_duo, 2.0, {t})
            pool = []
            pool += near[: max(1, int(need * 0.7))]
            span = (higher[: max(1, int(need * 0.1))] + lower[: max(1, int(need * 0.1))])
            pool += span
            # baseline fill
            if _env_bool("BASELINE_ENABLED", True) and len(pool) < need and baselines:
                pool += list(baselines)[: (need - len(pool))]
            added = 0
            for opp in pool:
                if opp == t:
                    continue
                if _recent_pair_blocked(session, "duo", [t, opp], repeat_hours):
                    continue
                enqueue_match("duo", [t, opp], "normal")
                created["duo"] += 1
                added += 1
                if added >= need:
                    break

        # QUAD sampling per team (simplified): form pods with 3 others
        if _env_bool("ENABLE_QUAD", False):
            for t in quad_order:
                if t in baselines:
                    continue
                have = count_active("quad", t)
                daily_quad = dynamic_daily("quad", t)
                need = max(0, daily_quad - have)
                if need == 0:
                    continue
                mu_map = ratings_quad
                if t not in mu_map:
                    continue
                higher, lower = _choose_higher_lower(mu_map, t, band_quad, 2.0, {t})
                near = _choose_nearby_opponents(mu_map, t, band_quad, {t})
                def pick_trio():
                    sel = []
                    for lst in (near, higher, lower):
                        for x in lst:
                            if x != t and x not in sel and x not in baselines:
                                sel.append(x)
                            if len(sel) == 3:
                                return sel
                    return None
                added = 0
                while added < need:
                    trio = pick_trio()
                    if not trio:
                        break
                    enqueue_match("quad", [t] + trio, "normal")
                    created["quad"] += 1
                    added += 1
    return created


# ------------------------------------------------------------- Ratings maintenance


@celery_app.task(name="app.tasks.inflate_sigma_for_inactive")
def inflate_sigma_for_inactive() -> int:
    """Increase sigma by +0.5 per 7 days of inactivity, capped at initial.
    Returns the number of rows updated.
    """
    from datetime import datetime, timedelta
    MU0 = 25.0
    SIGMA0 = 25.0 / 3.0
    now = datetime.utcnow()
    updated = 0
    with SessionLocal() as session:
        rows = session.query(Rating).all()
        for r in rows:
            last = r.updated_at or now
            delta_days = (now - last).days
            if delta_days >= 7 and r.sigma < SIGMA0:
                # add 0.5 per full 7-day periods
                increments = delta_days // 7
                new_sigma = min(SIGMA0, r.sigma + 0.5 * increments)
                if new_sigma > r.sigma:
                    r.sigma = float(new_sigma)
                    r.updated_at = now
                    updated += 1
        session.commit()
    return updated


@celery_app.task(name="app.tasks.recompute_ratings")
def recompute_ratings() -> dict:
    """Replay all finished matches in chronological order to rebuild ratings and rating_events."""
    from sqlalchemy import delete
    counts = {"ratings": 0, "events": 0, "processed": 0}
    with SessionLocal() as session:
        # wipe ratings and events
        session.execute(delete(RatingEvent))
        session.execute(delete(Rating))
        session.commit()
        # iterate matches
        qs = session.query(Match).filter(Match.status == "finished").order_by(Match.created_at.asc())
        # valid team ids snapshot to avoid FK errors on deleted teams
        valid_team_ids = {str(tid) for (tid,) in session.execute(select(Team.id)).all()}
        for m in qs.all():
            team_ids = m.team_ids.split(",")
            # skip matches involving teams that no longer exist
            if any(tid not in valid_team_ids for tid in team_ids):
                continue
            ranks_map = {}
            try:
                # ranks_map is JSONB; load via orm attribute directly
                ranks_map = m.ranks_map or {}
            except Exception:
                ranks_map = {}
            if not ranks_map:
                # Fallback for duo: winner rank 0, loser 1
                if len(team_ids) == 2 and m.winner_team_id:
                    wid = str(m.winner_team_id)
                    lid = team_ids[0] if team_ids[1] == wid else team_ids[1]
                    ranks_map = {wid: 0, lid: 1}
                else:
                    continue
            apply_match_ratings(session, m, team_ids, ranks_map)
            counts["processed"] += 1
        session.commit()
        counts["ratings"] = session.query(Rating).count()
        counts["events"] = session.query(RatingEvent).count()
    return counts


@celery_app.task(name="app.tasks.reset_and_reenqueue_all")
def reset_and_reenqueue_all() -> dict:
    """Dangerous: clears matches, queue, ratings and re-enqueues calibration for all non-baseline teams."""
    from sqlalchemy import delete
    cleared = {"matches": 0, "queue": 0, "ratings": 0, "events": 0, "teams_enqueued": 0}
    baseline_names = {"baseline", "baseline-a", "baseline-b", "baseline-c"}
    with SessionLocal() as session:
        # Count before delete
        cleared["matches"] = session.query(Match).count()
        cleared["queue"] = session.query(MatchQueue).count()
        cleared["ratings"] = session.query(Rating).count()
        cleared["events"] = session.query(RatingEvent).count()
        # Wipe
        session.execute(delete(MatchQueue))
        session.execute(delete(Match))
        session.execute(delete(RatingEvent))
        session.execute(delete(Rating))
        # Reset calibration progress
        session.execute(update(Team).values(calibration_progress_duo=0, calibration_progress_quad=0))
        session.commit()
        # Enqueue calibration for all non-baseline teams
        rows = session.execute(select(Team.id, Team.name)).all()
        for tid, name in rows:
            if (name or "").lower() in baseline_names:
                continue
            schedule_calibration_for_team.apply_async(args=[str(tid)], queue="simulation")
            cleared["teams_enqueued"] += 1
    return cleared