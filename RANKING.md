# Ranking and Scheduling (Developer Deep Dive)

This document explains our TrueSkill setup, match ranking, scheduling, storage, and how the system scales.

## TrueSkill configuration

- Independent ratings per team per mode (`duo`, `quad`).
- Canonical parameters:
  - `mu0 = 25.0`
  - `sigma0 = 25.0 / 3.0 ≈ 8.333`
  - `beta = 4.166`
  - `tau = 0.083`
  - `draw_probability = 0.0`
- Leaderboard score: conservative skill = `mu - 3*sigma` (a safety‑first lower bound).
- Library: `trueskill==0.4.5`. Updates via environment‑bound `rate()` per match.

### Update forms
- DUO: two single‑member teams; call `env.rate([[A],[B]], ranks=[0,1] or [1,0])`.
- QUAD: four single‑member teams; call `env.rate([[A],[B],[C],[D]], ranks=[ra, rb, rc, rd])` where ranks use competition ranking (ties share the same integer).

## Match ranking (engine → TrueSkill)

- QUAD rank order:
  1. Survivors outrank eliminated.
  2. Among survivors: higher total team HP; tie → higher total team damage; tie → deterministic by match id.
  3. Among eliminated on the same turn: later death is better; tie → higher total team damage; tie → deterministic by match id.
- We persist:
  - `ranks_order`: ordered list of team_ids (winner/1st first) for easy display.
  - `ranks_map`: `{ team_id -> rank_int }` (authoritative for ties and TrueSkill updates).

## Data model (storage)

- `ratings(team_id, mode, mu, sigma, updated_at)` — current snapshot per mode.
- `rating_events(id, match_id, team_id, mode, mu_before, sigma_before, mu_after, sigma_after, created_at)` — audit/backfill trail.
- `matches(id, mode, team_ids, status, winner_team_id, map_name, map_seed, ranks_order, ranks_map, team_hp, team_damage, created_at)`.
- `match_queue(id, mode, team_ids[], priority, status, attempts, last_error, created_at)`.

## Scheduling

### Calibration (on submit/update)
- DUO (≈12): 2 vs baselines (A,B) + ~10 vs real teams (near band, then adaptive follow‑ups). Avoid μ gaps > `MAX_CALIB_GAP`.
- QUAD (≈12): 2 baseline pods `{team,A,B,C}` + ~10 pods vs real teams (near pod composition, then follow‑ups).
- Tunables: `BAND_WIDTH_DUO`, `BAND_WIDTH_QUAD`, `CAL_*`, `MAX_CALIB_GAP`, `BASELINE_ENABLED`.

### Ongoing (keep board fresh)
- Per‑team quotas per day: `DAILY_DUO`, `DAILY_QUAD`.
- Sampling per team: 70% near band, 20% above/below, 10% baselines (if enabled).
- Repeat suppression window: `MAX_REPEAT_WINDOW_HOURS`. Pairwise caps can be layered.
- Per‑team per‑mode concurrency: at most one running DUO and one running QUAD at a time.
- Dynamic extras: until a team’s uncertainty `sigma` ≤ `SIGMA_TARGET`, the scheduler grants extra matches per day computed as `ceil((sigma - SIGMA_TARGET) * EXTRA_MATCH_PER_SIGMA)`, capped by `MAX_DAILY_EXTRA` and per‑mode caps `MAX_DAILY_DUO` / `MAX_DAILY_QUAD`.

## Queue and workers

- `enqueue_match(mode, team_ids, priority)` inserts a row into `match_queue`.
- `queue_consumer_once` pops an eligible row (respects concurrency), creates a `matches` row, marks queue `running`, and enqueues `run_match(queue_id=...)`.
- `run_match` executes the simulation, persists map/logs/ranks, marks queue `done/failed`, and applies TrueSkill via `apply_match_ratings(...)`.
- Periodic jobs (Celery beat):
  - `queue_consumer_once` every few seconds.
  - `schedule_ongoing` hourly.
  - `inflate_sigma_for_inactive` daily (00:00 UTC).

## Baselines

- Baseline‑A/B/C: seeded once with pinned UUIDs; fixed rosters (configurable). Rated but hidden from public leaderboard unless `BASELINES_VISIBLE=true`.
- Used to calibrate new teams and for occasional drift checks.

## Scaling to 500+ teams

- Calibration: ~12 matches/team/mode ⇒ ~6,000 DUO + ~6,000 QUAD initial (some overlap expected across teams/opponents).
- Ongoing/day (example with `DAILY_DUO=3`, `DAILY_QUAD=2`):
  - DUO ≈ (N × DAILY_DUO) / 2 ⇒ ~750 with N=500.
  - QUAD ≈ (N × DAILY_QUAD) / 4 ⇒ ~250 with N=500.
- Scale horizontally: add Celery workers (increase `--concurrency` / replica count). Keep per‑team concurrency limits to avoid thrash.
- DB: indexes on `matches(mode,created_at)`, `matches(mode,status)`, `match_queue(status,created_at)`, `ratings(mode,team_id)`.
- Redis: use separate DB indices for Celery broker/results vs cache.

## Admin & maintenance

- Recompute ratings: `POST /leaderboard/re-evaluate` with `X-Admin-Token` — replays history to rebuild `ratings` and `rating_events`.
- Sigma inflation for inactivity: +0.5 σ each 7 days without matches, capped at initial σ.
- Submit‑for‑evaluation rate limit: `RATE_LIMIT_SUBMIT_EVAL_SECONDS` (per team).

## Failure and backoff

- `match_queue.attempts` increments on each try; beat frequency provides lightweight backoff. Terminal policies can be added (e.g., after N failures mark `failed`).
- Failed matches never change ratings.

## Future improvements

- Alembic migrations for enum types and schema evolution.
- Incremental leaderboard cache (Redis) per mode; event‑driven invalidation.
- Richer QUAD pod selection: diversity by μ band and recent opponent history.
