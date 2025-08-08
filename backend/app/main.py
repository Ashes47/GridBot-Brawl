from fastapi import FastAPI
# CORS middleware
from fastapi.middleware.cors import CORSMiddleware

# Router imports
from .api.team import router as team_router
from .api.simulate import router as simulate_router
from .api.match import router as match_router
from .api.leaderboard import router as leaderboard_router


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
from .scheduler import scheduler_loop


@app.on_event("startup")
async def _startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # launch background scheduler
    import asyncio
    asyncio.create_task(scheduler_loop())


app.include_router(team_router)
app.include_router(simulate_router)
app.include_router(match_router)
app.include_router(leaderboard_router)


@app.get("/health")
async def health_check():
    """Simple endpoint to verify that the service is up."""
    return {"status": "ok"} 