"""Team TricksAndTraps – demonstrates tactical gadgets and deception.
Bots:
- TrapSetter: drops traps on own tile when enemies nearby
- DecoyCaster: spawns 1HP decoy adjacent to mislead and block LoS
- Reflector: uses mirror when ranged attackers are visible
- WallBuilder: places walls to create cover or blockage
- Poisoner: infects adjacent target when safe
"""
import random
from typing import Dict, Any, List, Tuple

DIRS = ["north", "south", "east", "west"]


def manhattan(ax: int, ay: int, bx: int, by: int) -> int:
    return abs(ax - bx) + abs(ay - by)


class Base:
    def __init__(self):
        self.state: Dict[str, Any] = {}

    def rand_move(self) -> Dict[str, Any]:
        return {"type": "move", "direction": random.choice(DIRS)}


class Trap_Setter(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        # Drop a trap if an enemy within 2 tiles and we have charges
        if enemies and me["cooldowns"]["power"] == 0:
            if any(manhattan(me["x"], me["y"], e["x"], e["y"]) <= 2 for e in enemies):
                # Keep total traps under cap implicitly handled by engine
                return {"type": "drop_trap"}
        # inch toward center to seed traps
        return {"type": "move", "direction": "south"}


class Decoy_Caster(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        # prefer to place decoy when enemies visible and we have none active
        if me["cooldowns"]["power"] == 0 and obs.get("visible_enemies"):
            # try to place decoy ahead toward enemies by heuristic
            e = obs["visible_enemies"][0]
            dx = e["x"] - me["x"]
            dy = e["y"] - me["y"]
            if abs(dx) > abs(dy):
                return {"type": "clone", "target": {"x": me["x"] + (1 if dx > 0 else -1), "y": me["y"]}}
            else:
                return {"type": "clone", "target": {"x": me["x"], "y": me["y"] + (1 if dy > 0 else -1)}}
        return self.rand_move()


class Reflector(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        # If any enemy is aligned in straight line (potential ranged), mirror
        for e in obs.get("visible_enemies", []):
            if (e["x"] == me["x"] or e["y"] == me["y"]) and me["cooldowns"]["power"] == 0:
                return {"type": "mirror"}
        return self.rand_move()


class Wall_Builder(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        # Place wall adjacent toward nearest enemy if possible
        enemies = obs.get("visible_enemies", [])
        if enemies and me["cooldowns"]["power"] == 0:
            e = enemies[0]
            dx = 1 if e["x"] > me["x"] else -1 if e["x"] < me["x"] else 0
            dy = 1 if e["y"] > me["y"] else -1 if e["y"] < me["y"] else 0
            tx = me["x"] + (dx if dx != 0 else 0)
            ty = me["y"] + (dy if dy != 0 else 0)
            if dx != 0 or dy != 0:
                return {"type": "drop_wall", "target": {"x": tx, "y": ty}}
        # otherwise seed cover while advancing
        return {"type": "move", "direction": "south"}


class Poisoner(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        if enemies and me["cooldowns"]["power"] == 0:
            # prefer lowest HP adjacent target
            adj = [e for e in enemies if manhattan(me["x"], me["y"], e["x"], e["y"]) == 1]
            if adj:
                target = min(adj, key=lambda e: e.get("health", 100))
                return {"type": "infect", "target": {"x": target["x"], "y": target["y"]}}
        # otherwise close distance
        if enemies:
            e = enemies[0]
            dx = e["x"] - me["x"]
            dy = e["y"] - me["y"]
            if abs(dx) > abs(dy):
                return {"type": "move", "direction": "east" if dx > 0 else "west"}
            return {"type": "move", "direction": "south" if dy > 0 else "north"}
        return self.rand_move() 