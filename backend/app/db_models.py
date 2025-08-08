import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
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


class Member(Base):
    __tablename__ = "members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"))

    team = relationship("Team", back_populates="members")


class Match(Base):
    __tablename__ = "matches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    mode = Column(String(10), nullable=False)  # duo or quad
    team_ids = Column(Text, nullable=False)  # comma-separated uuid strings for simplicity
    winner_team_id = Column(UUID(as_uuid=True), nullable=True)
    log_path = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(20), nullable=False, default="pending")
    team_hp = Column(Text, nullable=True)  # JSON-encoded {team_id: hp}
    team_damage = Column(Text, nullable=True)  # JSON-encoded {team_id: dmg} 