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

# Bot roles (expanded)
class Role(str, Enum):
    # Damage
    SNIPER = "sniper"
    BOMBER = "bomber"
    BRUISER = "bruiser"
    POISONER = "poisoner"
    # Defense
    TANK = "tank"
    SHIELD_GIVER = "shield_giver"
    REFLECTOR = "reflector"
    # Support
    HEALER = "healer"
    PULLER = "puller"
    SILENCER = "silencer"
    # Mobility
    SCOUT = "scout"
    TELEPORTER = "teleporter"
    LEAPER = "leaper"
    # Control / Utility / Disruptor
    TRAP_SETTER = "trap_setter"
    PUSHER = "pusher"
    JAMMER = "jammer"
    WALL_BUILDER = "wall_builder"
    DECOY_CASTER = "decoy_caster"

# Power cooldowns (canonical v1.0)
COOLDOWNS: Dict[Role, int] = {
    Role.SNIPER: 3,
    Role.BOMBER: 3,
    Role.BRUISER: 0,  # passive
    Role.POISONER: 3,

    Role.TANK: 3,
    Role.SHIELD_GIVER: 2,
    Role.REFLECTOR: 4,

    Role.HEALER: 3,
    Role.PULLER: 3,
    Role.SILENCER: 3,

    Role.SCOUT: 2,
    Role.TELEPORTER: 4,
    Role.LEAPER: 2,

    Role.TRAP_SETTER: 3,
    Role.PUSHER: 2,
    Role.JAMMER: 3,
    Role.WALL_BUILDER: 3,
    Role.DECOY_CASTER: 4,
}

MAX_HP = 100
BASE_ATTACK_DMG = 20
SNIPER_DMG = 40
BOMBER_AOE_DMG = 30
BOMBER_SELF_DMG = 10
VISION_RADIUS = 4
TURN_LIMIT = 100 