from __future__ import annotations

from enum import Enum
from typing import Dict, Tuple

# Grid support
GRID_DUO = 10  # size for 1-vs-1 matches
GRID_QUAD = 15  # size for 4-team matches

# Directions are orthogonal only
class Direction(str, Enum):
    NORTH = "north"
    SOUTH = "south"
    EAST = "east"
    WEST = "west"

    @property
    def delta(self) -> Tuple[int, int]:
        return {
            Direction.NORTH: (0, -1),
            Direction.SOUTH: (0, 1),
            Direction.EAST: (1, 0),
            Direction.WEST: (-1, 0),
        }[self]

# Bot roles
class Role(str, Enum):
    SNIPER = "sniper"
    TANK = "tank"
    BOMBER = "bomber"
    SCOUT = "scout"
    TELEPORTER = "teleporter"

# Power cooldowns
COOLDOWNS: Dict[Role, int] = {
    Role.SNIPER: 3,
    Role.TANK: 5,
    Role.BOMBER: 4,
    Role.SCOUT: 2,
    Role.TELEPORTER: 5,
}

MAX_HP = 100
BASE_ATTACK_DMG = 20
SNIPER_DMG = 40
BOMBER_AOE_DMG = 30
BOMBER_SELF_DMG = 10
VISION_RADIUS = 4
TURN_LIMIT = 100 