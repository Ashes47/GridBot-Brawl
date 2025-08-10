import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, Integer, Float
from sqlalchemy.dialects.postgresql import UUID, ENUM, ARRAY, JSONB
from sqlalchemy.orm import relationship

from .database import Base


class Team(Base):
    __tablename__ = "teams"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(100), nullable=False, unique=True)
    code_path = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    members = relationship("Member", back_populates="team", cascade="all, delete-orphan")
    password_hash = Column(String(200), nullable=False)
    roster = Column(Text, nullable=True)  # JSON-encoded list of 5 component strings
    # Calibration progress counters per mode (0-12)
    calibration_progress_duo = Column(Integer, nullable=False, default=0)
    calibration_progress_quad = Column(Integer, nullable=False, default=0)


class Member(Base):
    __tablename__ = "members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"))

    team = relationship("Team", back_populates="members")


class Match(Base):
    __tablename__ = "matches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    # Replace with ENUM via Alembic; keep definition here for create_all compatibility
    mode = Column(String(10), nullable=False)  # duo or quad
    team_ids = Column(Text, nullable=False)  # comma-separated uuid strings for simplicity
    winner_team_id = Column(UUID(as_uuid=True), nullable=True)
    log_path = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(20), nullable=False, default="pending")
    team_hp = Column(Text, nullable=True)  # JSON-encoded {team_id: hp}
    team_damage = Column(Text, nullable=True)  # JSON-encoded {team_id: dmg} 
    # Map metadata
    map_name = Column(String(100), nullable=True)
    # store as integer for deterministic generator seed
    map_seed = Column(Integer, nullable=True)
    # Rankings per match
    ranks_order = Column(ARRAY(UUID(as_uuid=True)), nullable=True)
    ranks_map = Column(JSONB, nullable=True)  # {team_id(str): rank(int)}


# ---------------- Ratings & Events -----------------

# ENUMs (actual Postgres ENUMs will be created by Alembic migrations)
ModeEnum = ENUM("duo", "quad", name="mode_enum", create_type=False)
QueuePriorityEnum = ENUM("calibration", "normal", name="queue_priority_enum", create_type=False)
QueueStatusEnum = ENUM("queued", "running", "done", "failed", name="queue_status_enum", create_type=False)


class Rating(Base):
    __tablename__ = "ratings"

    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    mode = Column(String(10), primary_key=True)  # will be migrated to ENUM
    mu = Column(Float, nullable=False)
    sigma = Column(Float, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class RatingEvent(Base):
    __tablename__ = "rating_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_id = Column(UUID(as_uuid=True), ForeignKey("matches.id", ondelete="CASCADE"), nullable=False)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    mode = Column(String(10), nullable=False)  # will be ENUM via migration
    mu_before = Column(Float, nullable=False)
    sigma_before = Column(Float, nullable=False)
    mu_after = Column(Float, nullable=False)
    sigma_after = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------- Match Queue -----------------

class MatchQueue(Base):
    __tablename__ = "match_queue"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mode = Column(String(10), nullable=False)  # ENUM via migration
    team_ids = Column(ARRAY(UUID(as_uuid=True)), nullable=False)
    priority = Column(String(20), nullable=False, default="normal")  # ENUM via migration
    status = Column(String(20), nullable=False, default="queued")  # ENUM via migration
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)