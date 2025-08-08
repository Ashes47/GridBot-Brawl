import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


SYNC_DATABASE_URL = os.getenv(
    "SYNC_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql+asyncpg://botuser:botpassword@db:5432/botbrawl").replace(
        "+asyncpg", ""
    ),
)

# Pool tuned for workers
engine = create_engine(SYNC_DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False) 