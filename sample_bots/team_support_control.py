"""Team SupportControl – showcases support and control roles.
Bots:
- Healer: heals lowest HP ally within 2 tiles
- ShieldGiver: projects shield to most-forward ally within 3 tiles
- Puller: yanks visible enemy within 3 tiles (prefers furthest in LoS)
- Pusher: shoves adjacent enemy to open space
- Jammer: scrambles when ≥1 enemies within radius 3
"""
import random
from typing import Dict, Any, List

DIRECTIONS = ["north", "south", "east", "west"]


def manhattan_distance(ax: int, ay: int, bx: int, by: int) -> int:
    return abs(ax - bx) + abs(ay - by)


class Base:
    def __init__(self):
        self.state: Dict[str, Any] = {}

    def random_move(self) -> Dict[str, Any]:
        return {"type": "move", "direction": random.choice(DIRECTIONS)}

    def nearest_visible_enemy(self, obs: Dict[str, Any]) -> Dict[str, Any] | None:
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        if not enemies:
            return None
        return min(enemies, key=lambda e: manhattan_distance(me["x"], me["y"], e["x"], e["y"]))

    def nearest_visible_ally(self, obs: Dict[str, Any]) -> Dict[str, Any] | None:
        me = obs["self"]
        allies = [a for a in obs.get("visible_allies", []) if a["id"] != me["id"]]
        if not allies:
            return None
        return min(allies, key=lambda a: manhattan_distance(me["x"], me["y"], a["x"], a["y"]))


class Healer(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        allies: List[Dict[str, Any]] = obs.get("visible_allies", [])
        low = [a for a in allies if a.get("health", 100) < 100]
        if low:
            target = min(low, key=lambda a: a.get("health", 100))
            if manhattan_distance(me["x"], me["y"], target["x"], target["y"]) <= 2 and me["cooldowns"]["power"] == 0:
                return {"type": "heal", "target": {"x": target["x"], "y": target["y"]}}
        # else drift toward allies
        ally = self.nearest_visible_ally(obs)
        if ally:
            dx = ally["x"] - me["x"]
            dy = ally["y"] - me["y"]
            if abs(dx) > abs(dy):
                return {"type": "move", "direction": "east" if dx > 0 else "west"}
            return {"type": "move", "direction": "south" if dy > 0 else "north"}
        return self.random_move()


class Shield_Giver(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        allies: List[Dict[str, Any]] = obs.get("visible_allies", [])
        if allies and me["cooldowns"]["power"] == 0:
            # pick the ally that is furthest from our baseline (assume advancing is higher y)
            target = max(allies, key=lambda a: a["y"])  # heuristic for "frontline"
            if manhattan_distance(me["x"], me["y"], target["x"], target["y"]) <= 3:
                return {"type": "project_shield", "target": {"x": target["x"], "y": target["y"]}}
        # move toward frontline
        return {"type": "move", "direction": "south"}


class Puller(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        enemies: List[Dict[str, Any]] = obs.get("visible_enemies", [])
        if enemies and me["cooldowns"]["power"] == 0:
            in_range = [e for e in enemies if manhattan_distance(me["x"], me["y"], e["x"], e["y"]) <= 3]
            if in_range:
                # prefer furthest target in range for maximum pull
                target = max(in_range, key=lambda e: manhattan_distance(me["x"], me["y"], e["x"], e["y"]))
                return {"type": "yank", "target": {"x": target["x"], "y": target["y"]}}
        # otherwise advance toward nearest enemy
        enemy = self.nearest_visible_enemy(obs)
        if enemy:
            dx = enemy["x"] - me["x"]
            dy = enemy["y"] - me["y"]
            if abs(dx) > abs(dy):
                return {"type": "move", "direction": "east" if dx > 0 else "west"}
            return {"type": "move", "direction": "south" if dy > 0 else "north"}
        return self.random_move()


class Pusher(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        for e in obs.get("visible_enemies", []):
            if manhattan_distance(me["x"], me["y"], e["x"], e["y"]) == 1 and me["cooldowns"].get("power", 0) == 0:
                return {"type": "shove", "target": {"x": e["x"], "y": e["y"]}}
        # close distance otherwise
        enemy = self.nearest_visible_enemy(obs)
        if enemy:
            dx = enemy["x"] - me["x"]
            dy = enemy["y"] - me["y"]
            if abs(dx) > abs(dy):
                return {"type": "move", "direction": "east" if dx > 0 else "west"}
            return {"type": "move", "direction": "south" if dy > 0 else "north"}
        return self.random_move()


class Jammer(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        if enemies and me["cooldowns"]["power"] == 0:
            # If any enemy within Chebyshev radius 3, use scramble
            for e in enemies:
                if max(abs(e["x"] - me["x"]), abs(e["y"] - me["y"])) <= 3:
                    return {"type": "scramble"}
        # drift toward cluster
        return self.random_move() 