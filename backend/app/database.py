import os
import asyncio
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy import text
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Configure it in your .env file.")

engine = create_async_engine(
    DATABASE_URL, 
    echo=False, 
    future=True,
    pool_size=5,  # Very conservative pool size
    max_overflow=10,  # Minimal overflow to stay well under limits
    pool_pre_ping=True,  # Verify connections before use
    pool_recycle=300,  # Recycle connections every 5 minutes (faster)
    pool_timeout=10,  # Shorter timeout for getting connection
    pool_reset_on_return='commit',  # Reset connections on return
)
AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)

Base = declarative_base()

# Connection availability tracking
_connection_semaphore = asyncio.Semaphore(int(os.getenv("DB_CONNECTION_SEMAPHORE", "10")))
_max_db_connections = int(os.getenv("MAX_DB_CONNECTIONS", "80"))

async def check_db_availability() -> bool:
    """Check if database can accept more connections without hitting limits."""
    try:
        async with _connection_semaphore:
            async with AsyncSessionLocal() as session:
                # Check current connection count
                result = await session.execute(text("""
                    SELECT count(*) as active_connections 
                    FROM pg_stat_activity 
                    WHERE state = 'active' AND datname = current_database()
                """))
                active_connections = result.scalar()
                
                # Check if we're approaching the limit
                return active_connections < _max_db_connections
    except Exception:
        # If we can't check, assume it's not available to be safe
        return False

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async session and commits/rollbacks."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

async def get_session_with_availability_check() -> AsyncGenerator[AsyncSession, None]:
    """Get session only if database has available connections."""
    if not await check_db_availability():
        raise Exception("Database connection limit reached, task will be retried")
    
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise 