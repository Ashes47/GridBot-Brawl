"""Team ZergRush – relentless forward assault.
Each bot moves toward nearest enemy, focusing fire.
Teleporter blinks behind enemy lines once.
"""
import random
from typing import Dict, Any

DIRECTIONS = ["north","south","east","west"]

class Base:
    def __init__(self):
        self.state: Dict[str,Any] = {}

    def decide(self, obs):
        return {"type":"move","direction":random.choice(DIRECTIONS)}

    def dir_to(self, me, tgt):
        if abs(tgt["x"]-me["x"]) > abs(tgt["y"]-me["y"]):
            return "east" if tgt["x"]>me["x"] else "west"
        else:
            return "south" if tgt["y"]>me["y"] else "north"

class Sniper(Base):
    def decide(self, obs):
        me=obs["self"]
        # try snipe furthest visible enemy
        if obs["visible_enemies"]:
            e=max(obs["visible_enemies"], key=lambda e:abs(e["x"]-me["x"])+abs(e["y"]-me["y"]))
            if e["x"]==me["x"] or e["y"]==me["y"]:
                return {"type":"snipe","target":{"x":e["x"],"y":e["y"]}}
        return super().decide(obs)

class Tank(Base):
    def decide(self, obs):
        me=obs["self"]
        if me["cooldowns"]["power"]==0:
            return {"type":"shield"}
        if obs["visible_enemies"]:
            tgt=obs["visible_enemies"][0]
            if abs(tgt["x"]-me["x"])+abs(tgt["y"]-me["y"])==1:
                return {"type":"attack","direction":self.dir_to(me,tgt)}
        return super().decide(obs)

class Bomber(Base):
    def decide(self, obs):
        me=obs["self"]
        adj=[e for e in obs["visible_enemies"] if abs(e["x"]-me["x"])<=1 and abs(e["y"]-me["y"])<=1]
        if len(adj)>=1 and me["cooldowns"]["power"]==0:
            return {"type":"explode"}
        return super().decide(obs)

class Scout(Base):
    pass  # simple random move inherits

class Teleporter(Base):
    def decide(self, obs):
        if not self.state.get("blinked") and obs["visible_enemies"]:
            self.state["blinked"]=True
            return {"type":"blink"}
        return super().decide(obs) 