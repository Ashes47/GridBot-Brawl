# Scaling Guide

This guide explains how to scale Bot Code Battle operationally as team counts grow (e.g., 500+ teams), including throughput estimates and tuning knobs.

## Architecture recap
- FastAPI backend (match creation, APIs)
- Celery workers (simulation and scheduling)
- Redis (broker/results + cache)
- Postgres (matches, ratings, queue)
- Periodic jobs (Celery beat): queue consumer, ongoing scheduler, sigma inflation

## Throughput and load
- Calibration (one-time per submit/update): ~12 matches per team per mode
  - 500 teams ⇒ ~6,000 DUO + ~6,000 QUAD calibration matches (some overlap expected)
- Ongoing (per day) with defaults DAILY_DUO=3, DAILY_QUAD=2
  - DUO ≈ (N × DAILY_DUO) / 2 ⇒ ~750 matches/day for N=500
  - QUAD ≈ (N × DAILY_QUAD) / 4 ⇒ ~250 matches/day for N=500

Simulation runtime depends on map size, team code complexity, and CPU. Empirically target 0.5–2.0s per match on a modern vCPU. Tune concurrency accordingly.

## Docker Compose scaling
- worker-simulation: set concurrency via env `WORKER_SIM_CONCURRENCY` (per container)
- Horizontal scale: `docker compose up -d --scale worker-simulation=K` to run K simulation workers
- worker-beat: single instance is sufficient
- CPU/memory: ensure enough cores for concurrency (avoid CPU starvation)

Example capacity planning (rough):
- If one worker container at concurrency=8 completes ~4 matches/sec sustained, daily capacity ≈ 345k. Realistic sustained is much lower (contention, I/O). Start with:
  - 2–4 worker-simulation containers × concurrency 4–8 each
  - Monitor and increase if queue grows

## Scheduler and queue
- Beat runs `queue_consumer_once` every 5s by default; increase frequency or parallelize with more workers if the queued backlog grows
- Per-team per-mode concurrency is enforced by the consumer to prevent thrash
- Use baselines to fill when pool is sparse
- Dynamic daily quotas: until a team’s uncertainty σ ≤ `SIGMA_TARGET`, the scheduler grants extra matches per day: `extra = ceil((σ - SIGMA_TARGET) * EXTRA_MATCH_PER_SIGMA)`, capped by `MAX_DAILY_EXTRA` and `MAX_DAILY_*` per mode.

## Database
- Indexes are created at startup:
  - matches(mode, created_at), matches(mode, status)
  - match_queue(status, created_at)
  - ratings(mode, team_id)
- Consider increasing Postgres shared buffers and max connections for higher concurrency

## Redis
- Separate DB indices for Celery vs cache (already configured)
- If worker volume grows, consider a dedicated Redis instance and network locality

## Observability and ops
- Celery Flower to monitor queue depth and task runtimes
- Export metrics (queue depth, match runtime, rating latency) if running behind a reverse proxy/sidecar metrics exporter
- Admin APIs:
  - `/admin/scheduler/ongoing` to force an ongoing scheduling cycle
  - `/admin/scheduler/consume_once` to pull one queue item immediately
  - `/leaderboard/re-evaluate` to replay and rebuild ratings

## Practical tuning checklist
- Bump `WORKER_SIM_CONCURRENCY` to 4–8
- Scale `worker-simulation` replicas to 2–4 initially
- Keep `ENABLE_CELERY_BEAT=true` for automated scheduling
- Ensure CPUs scale with concurrency (vCPU per 1–2 concurrent matches)
- Persist volumes (`data/teams`, `data/matches`), monitor disk throughput
- Back up Postgres regularly; vacuum as needed under heavy write load

## SLA considerations
- New submit-to-first-match: 1–5 minutes under moderate load with beat frequency at 5s
- Leaderboard freshness: updated on match completion; W-L recent form follows soon after

## Cost controls
- Reduce DAILY_* quotas or beat frequency during off-hours
- Lower concurrency or scale down worker replicas
- Disable QUAD temporarily with `ENABLE_QUAD=false` if capacity is constrained
