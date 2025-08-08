import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

sync_database_url_env = os.getenv("SYNC_DATABASE_URL")
database_url_env = os.getenv("DATABASE_URL")

if sync_database_url_env:
    SYNC_DATABASE_URL = sync_database_url_env
elif database_url_env:
    SYNC_DATABASE_URL = database_url_env.replace("+asyncpg", "")
else:
    raise RuntimeError(
        "SYNC_DATABASE_URL is not set and DATABASE_URL is unavailable. Configure one in your .env file."
    )

# Pool tuned for workers
engine = create_engine(SYNC_DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False) 