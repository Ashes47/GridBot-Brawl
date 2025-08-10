from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple
import uuid

from sqlalchemy.orm import Session

from .db_models import Match, Rating, RatingEvent

try:
    from trueskill import TrueSkill, Rating as TSRating, rate
except Exception as exc:  # pragma: no cover - allow import to fail in environments without deps yet
    TrueSkill = None  # type: ignore
    TSRating = None  # type: ignore
    rate = None  # type: ignore


# Canonical TrueSkill env parameters
MU0 = 25.0
SIGMA0 = 25.0 / 3.0
BETA = 4.166
TAU = 0.083
DRAW_PROB = 0.0


def _get_env():
    if TrueSkill is None:
        raise RuntimeError("trueskill is not installed. Add trueskill to requirements and install.")
    # Create a deterministic environment per process
    return TrueSkill(mu=MU0, sigma=SIGMA0, beta=BETA, tau=TAU, draw_probability=DRAW_PROB)


def _get_or_create_rating(session: Session, team_id: str, mode: str) -> Rating:
    tid = uuid.UUID(team_id)
    r: Rating | None = session.get(Rating, (tid, mode))
    if r is None:
        r = Rating(team_id=tid, mode=mode, mu=MU0, sigma=SIGMA0)
        session.add(r)
        session.flush()
    return r


def _ratings_to_ts(ratings: List[Rating]) -> List[TSRating]:
    return [TSRating(mu=r.mu, sigma=r.sigma) for r in ratings]


def _persist_updates(
    session: Session,
    match_id: str,
    mode: str,
    before: Dict[str, Tuple[float, float]],
    after: Dict[str, Tuple[float, float]],
):
    for team_id, (mu_before, sigma_before) in before.items():
        mu_after, sigma_after = after[team_id]
        session.add(
            RatingEvent(
                match_id=match_id,
                team_id=team_id,
                mode=mode,
                mu_before=mu_before,
                sigma_before=sigma_before,
                mu_after=mu_after,
                sigma_after=sigma_after,
            )
        )


def apply_match_ratings(session: Session, match: Match, team_ids: List[str], ranks_map: Dict[str, int]) -> None:
    """Update ratings using TrueSkill given a finished match.
    - team_ids: ordered list of team ids corresponding to the order passed to rate()
    - ranks_map: mapping team_id -> rank (equal ranks allowed)
    """
    env = _get_env()
    # fetch or create ratings in team_ids order
    current: List[Rating] = []
    before_map: Dict[str, Tuple[float, float]] = {}
    for tid in team_ids:
        r = _get_or_create_rating(session, tid, match.mode)
        current.append(r)
        before_map[tid] = (r.mu, r.sigma)

    ts_list = _ratings_to_ts(current)
    # Build team structure: one player per team
    teams_for_rate = [[ts] for ts in ts_list]
    ranks_for_rate = [int(ranks_map.get(tid, 0)) for tid in team_ids]

    # Compute updates
    # Use environment-bound rate to apply configured parameters
    updated_nested = _get_env().rate(teams_for_rate, ranks=ranks_for_rate)
    updated: List[TSRating] = [t[0] for t in updated_nested]

    after_map: Dict[str, Tuple[float, float]] = {}
    for idx, tid in enumerate(team_ids):
        new = updated[idx]
        current[idx].mu = float(new.mu)
        current[idx].sigma = float(new.sigma)
        after_map[tid] = (float(new.mu), float(new.sigma))

    _persist_updates(session, str(match.id), match.mode, before_map, after_map)
    session.flush()


