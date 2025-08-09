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

    def distance_chebyshev(self, other: "Position") -> int:
        return max(abs(self.x - other.x), abs(self.y - other.y))

    class Config:
        frozen = True


class Bot(BaseModel):
    id: str
    role: Role
    position: Position
    hp: int = MAX_HP

    team_id: Optional[str] = None
    # Cooldown
    power_cooldown: int = 0
    # Shield: reduce 75% of next 40 damage; expires end of next turn if unused
    shield_pool_remaining: int = 0  # up to 40
    shield_expire_turn: Optional[int] = None
    # Status effects
    reflect_ready: bool = False
    silenced_remaining: int = 0
    jammed_remaining: int = 0
    poison_stacks: List[int] = []  # each entry is remaining turns (ticks at EoT)
    # Terrain effects
    slowed_remaining: int = 0  # from swamp: prevents Move/Dash/Leap next turn

    def is_alive(self) -> bool:
        return self.hp > 0

    def apply_damage(self, dmg: int):
        # Apply shield reduction up to remaining pool
        if self.shield_pool_remaining > 0 and dmg > 0:
            reducible = min(dmg, self.shield_pool_remaining)
            # 75% reduction on reducible portion => only 25% applied
            reduced_portion = (reducible * 25) // 100
            remainder = dmg - reducible
            dmg = reduced_portion + remainder
            self.shield_pool_remaining -= reducible
            if self.shield_pool_remaining <= 0:
                self.shield_pool_remaining = 0
                self.shield_expire_turn = None
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


# New action types
class ProjectShieldAction(ActionBase):
    target: Position


class HealAction(ActionBase):
    target: Position


class InfectAction(ActionBase):
    target: Position


class SilenceAction(ActionBase):
    target: Position


class MirrorAction(ActionBase):
    pass


class DropTrapAction(ActionBase):
    pass


class DropWallAction(ActionBase):
    target: Position


class CloneAction(ActionBase):
    target: Optional[Position] = None


class YankAction(ActionBase):
    target: Position


class ShoveAction(ActionBase):
    target: Position


class LeapAction(ActionBase):
    target: Position


class ScrambleAction(ActionBase):
    pass


BotAction = Union[
    MoveAction,
    AttackAction,
    SnipeAction,
    ShieldAction,
    ExplodeAction,
    DashAction,
    BlinkAction,
    ProjectShieldAction,
    HealAction,
    InfectAction,
    SilenceAction,
    MirrorAction,
    DropTrapAction,
    DropWallAction,
    CloneAction,
    YankAction,
    ShoveAction,
    LeapAction,
    ScrambleAction,
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


class Wall(BaseModel):
    x: int
    y: int
    team_id: str
    ttl: int


class Trap(BaseModel):
    x: int
    y: int
    team_id: str
    ttl: int


class Decoy(BaseModel):
    x: int
    y: int
    team_id: str
    owner_bot_id: str
    hp: int = 1
    ttl: int = 3


class GameState(BaseModel):
    grid_size: int
    teams: List[Team]
    turn: int = 0
    # Structures
    walls: List[Wall] = []
    traps: List[Trap] = []
    decoys: List[Decoy] = []
    # Map fields
    static_walls: List[Tuple[int, int]] = []  # permanent blockers
    terrain: Dict[Tuple[int, int], str] = {}  # tile -> terrain type
    zones: Dict[Tuple[int, int], List[str]] = {}  # tile -> list of zone types

    def all_bots(self) -> List[Bot]:
        return [b for team in self.teams for b in team.bots if b.is_alive()]

    # ---------------- Engine helpers ----------------
    def occupied_positions_bots(self) -> Dict[Tuple[int, int], Bot]:
        return {(b.position.x, b.position.y): b for b in self.all_bots()}

    def blockers(self) -> Dict[Tuple[int, int], str]:
        # positions that block movement/LoS: bots, decoys, walls, static_walls (LoS only for some terrain handled elsewhere)
        blockers: Dict[Tuple[int, int], str] = {}
        for b in self.all_bots():
            blockers[(b.position.x, b.position.y)] = "bot"
        for d in self.decoys:
            blockers[(d.x, d.y)] = "decoy"
        for w in self.walls:
            blockers[(w.x, w.y)] = "wall"
        for sx, sy in self.static_walls:
            blockers[(sx, sy)] = "wall"
        return blockers

    def bot_by_id(self, bot_id: str) -> Optional[Bot]:
        for b in self.all_bots():
            if b.id == bot_id:
                return b
        return None


class PendingEffects(BaseModel):
    snipe_events: List[Tuple[Bot, Position]] = []  # (sniper, target)
    explode_bots: List[Bot] = []  # bots that will explode in attack phase
    pulls: List[Tuple[Bot, Position]] = []  # (puller, target pos)
    pushes: List[Tuple[Bot, Position]] = []  # (pusher, target pos) 