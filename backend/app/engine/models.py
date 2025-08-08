from __future__ import annotations

import random
from typing import Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, Field, validator

from .constants import (
    BASE_ATTACK_DMG,
    BOMBER_AOE_DMG,
    BOMBER_SELF_DMG,
    COOLDOWNS,
    Direction,
    MAX_HP,
    Role,
)


class Position(BaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)

    def in_bounds(self, size: int) -> bool:
        return 0 <= self.x < size and 0 <= self.y < size

    def moved(self, direction: Direction) -> "Position":
        dx, dy = direction.delta
        return Position(x=self.x + dx, y=self.y + dy)

    def distance_manhattan(self, other: "Position") -> int:
        return abs(self.x - other.x) + abs(self.y - other.y)

    class Config:
        frozen = True


class Bot(BaseModel):
    id: str
    role: Role
    position: Position
    hp: int = MAX_HP

    team_id: Optional[str] = None
    # Power state
    power_cooldown: int = 0  # turns remaining until power usable
    shield_remaining: int = 0  # turns of 50% damage reduction

    def is_alive(self) -> bool:
        return self.hp > 0

    def apply_damage(self, dmg: int):
        if self.shield_remaining > 0:
            dmg = dmg // 2  # 50% reduction, rounded down
        self.hp = max(0, self.hp - dmg)

    class Config:
        validate_assignment = True


class ActionBase(BaseModel):
    pass


class MoveAction(ActionBase):
    direction: Direction


class AttackAction(ActionBase):
    direction: Direction


class SnipeAction(ActionBase):
    target: Position


class ShieldAction(ActionBase):
    pass


class ExplodeAction(ActionBase):
    pass


class DashAction(ActionBase):
    direction: Direction


class BlinkAction(ActionBase):
    pass


BotAction = Union[
    MoveAction,
    AttackAction,
    SnipeAction,
    ShieldAction,
    ExplodeAction,
    DashAction,
    BlinkAction,
]


class Team(BaseModel):
    id: str
    bots: List[Bot]

    @validator("bots", each_item=True)
    def _assign_team(cls, bot: Bot, values):
        team_id = values.get("id")
        if team_id:
            bot.team_id = team_id
        return bot

    def alive_bots(self) -> List[Bot]:
        return [b for b in self.bots if b.is_alive()]


class GameState(BaseModel):
    grid_size: int
    teams: List[Team]
    turn: int = 0

    def all_bots(self) -> List[Bot]:
        return [b for team in self.teams for b in team.bots if b.is_alive()]

    # ---------------- Engine helpers ----------------
    def occupied_positions(self) -> Dict[Tuple[int, int], Bot]:
        return {(b.position.x, b.position.y): b for b in self.all_bots()}

    def bot_by_id(self, bot_id: str) -> Optional[Bot]:
        for b in self.all_bots():
            if b.id == bot_id:
                return b
        return None


class PendingEffects(BaseModel):
    snipe_events: List[Tuple[Bot, Position]] = []  # (sniper, target)
    explode_bots: List[Bot] = []  # bots that will explode in attack phase 