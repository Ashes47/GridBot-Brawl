import asyncio
import os
from itertools import combinations
from typing import List, Set, FrozenSet
from uuid import uuid4
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import AsyncSessionLocal
from .db_models import Match, Team
from .simulation import run_match_job

CHECK_INTERVAL = 5  # seconds
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_SIMULATIONS", 4))
MAX_NEW_DUO_PER_CYCLE = 50
MAX_NEW_QUAD_PER_CYCLE = 10


async def run_match_with_semaphore(semaphore, session_factory, match_id, teams):
    async with semaphore:
        await run_match_job(session_factory, match_id, teams)


async def scheduler_loop():
    """Background task: ensure all team combinations have matches."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    while True:
        try:
            async with AsyncSessionLocal() as session:
                # Load teams and exclude the special Baseline team from auto-scheduling
                res = await session.execute(select(Team.id, Team.name))
                team_rows = res.all()
                team_ids: List[str] = [str(tid) for tid, name in team_rows if (name or "").lower() != "baseline"]

                # More efficient check for existing matches
                rows = await session.execute(
                    select(Match.team_ids, Match.mode).where(
                        Match.status.in_(["pending", "running", "finished"])
                    )
                )
                existing_duos: Set[FrozenSet[str]] = set()
                existing_quads: Set[FrozenSet[str]] = set()
                for m in rows:
                    ids = frozenset(m.team_ids.split(","))
                    if m.mode == "duo":
                        existing_duos.add(ids)
                    else:  # quad
                        existing_quads.add(ids)

                # Schedule Duo matches
                new_duos = 0
                for ta, tb in combinations(team_ids, 2):
                    if new_duos >= MAX_NEW_DUO_PER_CYCLE:
                        break
                    if frozenset([ta, tb]) in existing_duos:
                        continue

                    match_id = uuid4()
                    match = Match(
                        id=match_id,
                        mode="duo",
                        team_ids=",".join([ta, tb]),
                        status="pending",
                        log_path="",
                        created_at=datetime.utcnow(),
                    )
                    session.add(match)
                    await session.flush()
                    asyncio.create_task(
                        run_match_with_semaphore(semaphore, AsyncSessionLocal, str(match_id), [ta, tb])
                    )
                    new_duos += 1
                await session.commit()

                # Schedule up to N quad combinations each cycle
                new_quads = 0
                for quad_tuple in combinations(team_ids, 4):
                    if new_quads >= MAX_NEW_QUAD_PER_CYCLE:
                        break
                    ids_set = frozenset(quad_tuple)
                    if ids_set in existing_quads:
                        continue
                    
                    match_id = uuid4()
                    match = Match(
                        id=match_id,
                        mode="quad",
                        team_ids=",".join(quad_tuple),
                        status="pending",
                        log_path="",
                        created_at=datetime.utcnow(),
                    )
                    session.add(match)
                    await session.flush()
                    asyncio.create_task(
                        run_match_with_semaphore(
                            semaphore, AsyncSessionLocal, str(match_id), list(quad_tuple)
                        )
                    )
                    new_quads += 1
                await session.commit()

        except Exception as exc:
            import logging
            logging.exception("Scheduler cycle failed", exc_info=exc)
        await asyncio.sleep(CHECK_INTERVAL) 