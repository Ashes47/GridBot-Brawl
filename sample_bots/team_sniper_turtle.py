"""Team Sniper-Turtle – coordinated tank shield + sniper fire.
Logic:
• Tank parks in center and keeps shield up every cooldown.
• Sniper stays behind tank; if LoS fire else reposition.
• Bomber guards flank and explodes when ≥2 enemies adjacent.
• Scout circles perimeter to reveal map and bait.
• Teleporter emergency-blinks when low HP then returns to help.
"""
import random
from typing import Dict, Any, List

DIRECTIONS = ["north", "south", "east", "west"]
CENTER = (5, 5)  # for 10×10; okay enough also for 15×15 (rough centre)


def towards(a: int, b: int) -> str:
    if a < b:
        return "east"
    elif a > b:
        return "west"
    return "north"  # default, shouldn’t happen


class Base:
    def __init__(self):
        self.state: Dict[str, Any] = {}

    def rand_move(self):
        return {"type": "move", "direction": random.choice(DIRECTIONS)}


class Tank(Base):
    def decide(self, obs):
        self_pos = obs["self"]
        # move to centre once
        if not self.state.get("parked"):
            dx = CENTER[0] - self_pos["x"]
            if dx != 0:
                return {"type": "move", "direction": "east" if dx > 0 else "west"}
            dy = CENTER[1] - self_pos["y"]
            if dy != 0:
                return {"type": "move", "direction": "south" if dy > 0 else "north"}
            self.state["parked"] = True
        # shield whenever off cooldown
        if self_pos["cooldowns"]["power"] == 0:
            return {"type": "shield"}
        # attack adjacent enemy
        for e in obs["visible_enemies"]:
            if abs(e["x"]-self_pos["x"]) + abs(e["y"]-self_pos["y"]) == 1:
                dir_ = (
                    "north" if e["y"] < self_pos["y"] else "south" if e["y"] > self_pos["y"] else
                    "west" if e["x"] < self_pos["x"] else "east")
                return {"type": "attack", "direction": dir_}
        return self.rand_move()


class Sniper(Base):
    def decide(self, obs):
        me = obs["self"]
        # prioritise straight-line snipe
        for e in obs["visible_enemies"]:
            if e["x"] == me["x"] and abs(e["y"]-me["y"]) <= 5:
                return {"type": "snipe", "target": {"x": me["x"], "y": e["y"]}}
            if e["y"] == me["y"] and abs(e["x"]-me["x"]) <= 5:
                return {"type": "snipe", "target": {"x": e["x"], "y": me["y"]}}
        # reposition behind tank (assumed id T1-Ta) centre-aligned
        dir_ = towards(me["x"], CENTER[0])
        return {"type": "move", "direction": dir_}


class Bomber(Base):
    def decide(self, obs):
        me = obs["self"]
        adj = [e for e in obs["visible_enemies"] if abs(e["x"]-me["x"])<=1 and abs(e["y"]-me["y"])<=1]
        if len(adj) >= 2:
            return {"type": "explode"}
        # patrol near tank
        dir_ = random.choice(["north","south"])
        return {"type": "move", "direction": dir_}


class Scout(Base):
    def decide(self, obs):
        # perimeter run clockwise
        seq = ["east","south","west","west","north","north","east"]
        step = self.state.get("step", 0)
        self.state["step"] = (step+1)%len(seq)
        if random.random()<0.3:
            return {"type": "dash", "direction": seq[step]}
        return {"type": "move", "direction": seq[step]}


class Teleporter(Base):
    def decide(self, obs):
        if obs["self"]["health"] < 40 and not self.state.get("blinked"):
            self.state["blinked"] = True
            return {"type": "blink"}
        # orbit around tank
        return self.rand_move() 