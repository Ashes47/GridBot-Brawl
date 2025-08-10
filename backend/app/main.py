import os
from fastapi import FastAPI
# CORS middleware
from fastapi.middleware.cors import CORSMiddleware

# Router imports
from .api.team import router as team_router
from .api.simulate import router as simulate_router
from .api.match import router as match_router
from .api.leaderboard import router as leaderboard_router
from .api.metadata import router as metadata_router
from .api.admin import router as admin_router
from .api.admin import baselines_seed


app = FastAPI(title="GridBot Brawl API")

# Allow frontend (localhost:8080) to call APIs during dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Create tables at startup (simple approach for early development)
from .database import engine, Base


@app.on_event("startup")
async def _startup():
    reset_flag = os.getenv("RESET_DB", "false").lower() in ("1", "true", "yes")
    async with engine.begin() as conn:
        if reset_flag:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        # Create helpful indexes (Postgres only). Ignore failures if not supported.
        try:
            from sqlalchemy import text
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_matches_mode_created ON matches(mode, created_at)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_matches_mode_status ON matches(mode, status)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_match_queue_status_created ON match_queue(status, created_at)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ratings_mode_team ON ratings(mode, team_id)"))
        except Exception:
            import logging
            logging.exception("Index creation failed (non-fatal)")
    # Optional: seed baselines automatically if enabled
    if os.getenv("SEED_BASELINES_ON_START", "false").lower() in ("1","true","yes"):
        # Call seeding using the same app context; admin token is not required internally
        try:
            from .database import AsyncSessionLocal
            async with AsyncSessionLocal() as s:
                # bypass token check by calling underlying logic in-process
                await baselines_seed(x_admin_token=os.getenv("ADMIN_TOKEN"), session=s)
        except Exception:
            # ignore seeding errors on boot, log only
            import logging
            logging.exception("Baseline seeding failed during startup")



app.include_router(team_router)
app.include_router(simulate_router)
app.include_router(match_router)
app.include_router(leaderboard_router)
app.include_router(metadata_router)
app.include_router(admin_router)


@app.get("/health")
async def health_check():
    """Simple endpoint to verify that the service is up."""
    return {"status": "ok"} 