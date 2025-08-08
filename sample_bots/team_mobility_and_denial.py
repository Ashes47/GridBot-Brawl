"""Team MobilityDenial – mobility plus power denial and frontline pressure.
Bots:
- Leaper: diagonal jumps to reposition and flank
- Silencer: silences high-value enemy in range
- Bruiser: heavy base attacker that dashes when possible
- Tank: anchors frontline, shields when pressured
- Teleporter: backline repositioning tool
"""
import random
from typing import Dict, Any, List

DIRS = ["north", "south", "east", "west"]


def manhattan(ax: int, ay: int, bx: int, by: int) -> int:
    return abs(ax - bx) + abs(ay - by)


class Base:
    def __init__(self):
        self.state: Dict[str, Any] = {}

    def rand_move(self) -> Dict[str, Any]:
        return {"type": "move", "direction": random.choice(DIRS)}

    def step_toward(self, me: Dict[str, Any], tx: int, ty: int) -> Dict[str, Any]:
        dx = tx - me["x"]
        dy = ty - me["y"]
        if abs(dx) > abs(dy):
            return {"type": "move", "direction": "east" if dx > 0 else "west"}
        return {"type": "move", "direction": "south" if dy > 0 else "north"}


class Leaper(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        if me["cooldowns"].get("power", 0) == 0 and enemies:
            # try jump diagonally closer toward first visible enemy
            e = enemies[0]
            dx = 1 if e["x"] > me["x"] else -1
            dy = 1 if e["y"] > me["y"] else -1
            return {"type": "leap", "target": {"x": me["x"] + 2 * dx, "y": me["y"] + 2 * dy}}
        # else advance
        if enemies:
            e = enemies[0]
            return self.step_toward(me, e["x"], e["y"])
        return self.rand_move()


class Silencer(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        if me["cooldowns"]["power"] == 0 and enemies:
            # prefer enemy with visible component key if present, else first
            # range <=3 required; pick closest within 3
            in_range = [e for e in enemies if max(abs(e["x"] - me["x"]), abs(e["y"] - me["y"])) <= 3]
            if in_range:
                target = min(in_range, key=lambda e: manhattan(me["x"], me["y"], e["x"], e["y"]))
                return {"type": "silence", "target": {"x": target["x"], "y": target["y"]}}
        # move toward enemies
        if enemies:
            e = enemies[0]
            return self.step_toward(me, e["x"], e["y"])
        return self.rand_move()


class Bruiser(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        # If adjacent enemy, attack
        for e in obs.get("visible_enemies", []):
            if manhattan(me["x"], me["y"], e["x"], e["y"]) == 1:
                # choose direction toward enemy
                dx = e["x"] - me["x"]
                dy = e["y"] - me["y"]
                if abs(dx) > abs(dy):
                    return {"type": "attack", "direction": "east" if dx > 0 else "west"}
                return {"type": "attack", "direction": "south" if dy > 0 else "north"}
        # otherwise dash forward if clear lanes are likely
        if me["cooldowns"].get("power", 0) == 0 and random.random() < 0.3:
            # use dash if engine interprets Bruiser dash or fallback move
            return {"type": "dash", "direction": random.choice(DIRS)}
        enemies = obs.get("visible_enemies", [])
        if enemies:
            e = enemies[0]
            return self.step_toward(me, e["x"], e["y"])
        return self.rand_move()


class Tank(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        if me["cooldowns"]["power"] == 0:
            # shield preemptively when enemies visible
            if obs.get("visible_enemies"):
                return {"type": "shield"}
        # push toward mid
        return {"type": "move", "direction": "south"}


class Teleporter(Base):
    def decide(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        me = obs["self"]
        # if low health, blink to reset position
        if me.get("health", 100) < 50 and not self.state.get("blinked"):
            self.state["blinked"] = True
            return {"type": "blink"}
        enemies = obs.get("visible_enemies", [])
        if enemies:
            e = enemies[0]
            return self.step_toward(me, e["x"], e["y"])
        return self.rand_move() 