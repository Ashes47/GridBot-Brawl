# Bot Code Battle (GridBot Brawl)

A fast, sandboxed bot-battler where teams upload a single Python file that defines 5 components (classes). The backend runs tournaments, stores results, and exposes a simple viewer-friendly API. A static frontend lets you sign up, upload code, run matches, and watch replays.

## Repository layout
- `backend/`: FastAPI app, Celery workers, simulation engine
- `frontend/`: Static HTML/CSS frontend served by Nginx
- `sample_bots/`: Example teams you can battle locally
- `data/teams`: Uploaded team code will be stored here (host volume)
- `data/matches`: Match logs (JSON) will be written here (host volume)

## Prerequisites
- Docker and Docker Compose
- Or for local dev: Python 3.11+, PostgreSQL 15+, Redis 7+

## One-time setup
1. Create required data directories on your host:
   - `mkdir -p data/teams data/matches`
2. Create a `.env` file in the repo root (same folder as `docker-compose.yml`). Example:

```
# Postgres
POSTGRES_DB=botbrawl
POSTGRES_USER=botuser
POSTGRES_PASSWORD=botpassword

# SQLAlchemy (asyncpg)
DATABASE_URL=postgresql+asyncpg://botuser:botpassword@db:5432/botbrawl

# Redis (with password)
REDIS_PASSWORD=changeme
CELERY_BROKER_URL=redis://:changeme@redis:6379/0
CELERY_RESULT_BACKEND=redis://:changeme@redis:6379/0

# Admin + Flower
ADMIN_PASSWORD=adminchangeme
FLOWER_USER=admin
FLOWER_PASSWORD=adminchangeme
```

Notes:
- The `docker-compose.yml` expects these variables via `.env`.
- The backend also reads:
  - `DATABASE_URL`, `SYNC_DATABASE_URL`
  - `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `REDIS_URL`
  - Admin: `ADMIN_TOKEN` (for admin APIs), `ADMIN_PASSWORD` (legacy team admin only)
  - Feature flags: `ENABLE_DUO`, `ENABLE_QUAD`, `BASELINE_ENABLED`, `BASELINES_VISIBLE`
  - Scheduler: `MAX_REPEAT_WINDOW_HOURS`, `DAILY_DUO`, `DAILY_QUAD`, `BAND_WIDTH_DUO`, `BAND_WIDTH_QUAD`, calibration knobs
  - Dynamic quotas: `SIGMA_TARGET`, `EXTRA_MATCH_PER_SIGMA`, `MAX_DAILY_EXTRA`, `MAX_DAILY_DUO`, `MAX_DAILY_QUAD`
  - Rate limits: `RATE_LIMIT_SUBMIT_EVAL_SECONDS`
  - Boot: `RESET_DB`, `SEED_BASELINES_ON_START`, `ENABLE_CELERY_BEAT`
  - Maps: `MAP_RULES_PATH`, `FORCE_MAP_SEED`
  - If you change `POSTGRES_*`, keep `DATABASE_URL` in sync.

## Run with Docker
```
docker compose up --build
```
Services and URLs:
- Backend API: `http://localhost:8000` (health: `GET /health`)
- Frontend: `http://localhost:8080`
- Celery Flower: `http://localhost:5555` (login with `FLOWER_USER` / `FLOWER_PASSWORD`)

Volumes (host ↔ container):
- `./data/teams` ↔ `/app/data/teams`
- `./data/matches` ↔ `/app/data/matches`

Make sure the host `data/teams` and `data/matches` exist and are writable before starting.

## Local development (without Docker)
1. Start dependencies:
   - PostgreSQL: create DB `botbrawl` and user `botuser` with password `botpassword` (or your own, then adjust `DATABASE_URL`).
   - Redis: start a local Redis and set a password if you want to match the Docker setup.
2. Create and activate venv, install deps:
```
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```
3. Required env vars (example):
```
export DATABASE_URL=postgresql+asyncpg://botuser:botpassword@localhost:5432/botbrawl
export CELERY_BROKER_URL=redis://localhost:6379/0
export CELERY_RESULT_BACKEND=redis://localhost:6379/0
export ADMIN_PASSWORD=adminchangeme
```
4. Create data directories:
```
mkdir -p data/teams data/matches
```
5. Run services:
```
# API
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --app-dir backend

# Celery workers (in separate shells)
celery -A app.celery_app.celery_app worker -Q baseline --concurrency 2 -P threads -n baseline@%h --workdir backend
celery -A app.celery_app.celery_app worker -Q simulation --concurrency 4 -P threads -n simulation@%h --workdir backend

# Flower (optional)
celery -A app.celery_app.celery_app flower --basic_auth=${FLOWER_USER:-admin}:${FLOWER_PASSWORD:-adminchangeme} --workdir backend
```

Tip: The FastAPI app auto-creates tables at startup.

## Using the app
- Open the frontend at `http://localhost:8080` to create a team, upload your bot file, and start matches.
- Sample bots live in `sample_bots/` if you want to inspect patterns.

### Minimal API quickstart
- Create a team (multipart form): `POST /teams/` with fields `name`, `members` (comma-separated), `password`, optional `roster` (JSON array of 5 components), optional `bot_file`.
- Run duo simulation: `POST /simulate/duo` with body `{ "team_ids": ["<id1>", "<id2>"] }`.
- Get match info: `GET /matches/{match_id}`.
- Download match log JSON: `GET /matches/{match_id}/log`.
- Leaderboard: `GET /leaderboard?mode=duo|quad` (returns TrueSkill ratings: μ, σ, conservative score (μ-3σ), recent W-L, recent form).
- Baseline test: `POST /simulate/vs_baseline` with `{ "team_id": "<id>", "baseline_roster": ["sniper",...]} (optional)`.

### Components catalog
- `GET /metadata/components` returns the canonical list of components (roles) and cooldowns that your bot can implement.

## Local head-to-head (no DB/Redis required)
You can pit two bot files locally using the included CLI. This runs a quick, isolated simulation and writes a JSON log next to your terminal.
```
python run_match.py sample_bots/team_snake_swarm.py sample_bots/team_sniper_turtle.py
```
Output shows the winner and the path to the log file (e.g., `local_match_<uuid>.json`).

## Important directories to create
- `data/teams`: where uploaded team code is stored (by team id). Must exist and be writable.
- `data/matches`: where simulation logs are written. Must exist and be writable.

If these folders are missing or unwritable, uploads and match logs will fail.

## Troubleshooting
- Can’t connect to DB in Docker: ensure `.env` values for `POSTGRES_*` match `DATABASE_URL` and containers can reach `db` host.
- Celery tasks not running: confirm Redis is reachable (`CELERY_BROKER_URL`) and workers are up for queues `baseline` and `simulation`.
- Log download 404: check the file exists under `data/matches/` and container paths match host volumes.
- CORS during local dev: the API allows `http://localhost:8080` by default.

## Ranking and Scheduling (for developers)

We use TrueSkill (per mode) and a match scheduler to keep the board fresh and fair. See `RANKING.md` for deep details.

- Ratings per team per mode: `(mu, sigma)` initialized to `mu=25.0`, `sigma≈8.333`.
- Leaderboard score: conservative skill `mu - 3*sigma`.
- Updates: after each finished match using `trueskill.rate` (DUO as two teams; QUAD with ranks).
- QUAD ranking policy: survivors rank above eliminated; among survivors, higher total HP breaks ties, then total damage; remaining ties resolved deterministically by match id.
- Baselines A/B/C are rated but hidden publicly by default (`BASELINES_VISIBLE=false`), used for calibration and drift checks.

Scheduling overview:
- On submit-for-evaluation, we enqueue a calibration batch:
  - DUO: baselines + nearby opponents; adaptive follow-ups within bands and gap caps
  - QUAD (if enabled): baseline pods + nearby pods
- Ongoing scheduler targets daily quotas per team per mode with banded sampling (70/20/10 near/above-below/baseline) and repeat suppression.
- Per-team per-mode concurrency enforced; queue consumers run periodically (via Celery beat).

Maintenance:
- Sigma inflation: inactive teams gain +0.5 σ per 7 days, capped at initial σ.
- Admin can recompute ratings by replaying match history: `POST /leaderboard/re-evaluate` with `X-Admin-Token`.

## Scaling to 500+ teams

- Periodic queue consumer (Celery beat) processes matches continuously.
- Ongoing quotas: DUO≈(teams×DAILY_DUO)/2 per day; QUAD≈(teams×DAILY_QUAD)/4 per day.
- Per‑team per‑mode concurrency guard prevents overload on hot teams.
- Baselines available to fill when the pool is small or uneven.
- Redis + Celery recommended with separate DB indices for cache vs workers.
- DB indexes applied at startup to keep queries fast.

## Map system

- Procedural seed maps are generated per match using `maps/map_rules.json`. The assigned seed and map name are stored on the `matches` row and embedded in the log header.
- Static maps can be dropped into `maps/` as JSON matching the MapSpec schema; disabled by default via `"disabled": true`.
- Optional envs:
  - `MAP_RULES_PATH` to point to a custom rules file
  - `FORCE_MAP_SEED` to force a specific seed for QA
