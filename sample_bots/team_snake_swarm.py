"""Team SnakeSwarm – leader-follower pathing.
Strategy:
• Scout acts as pathfinder (‘head’). It moves clockwise around the map edge.
• Each following bot (Tank, Sniper, Bomber, Teleporter) tries to occupy the
  previous position of the bot ahead of it – forming a moving snake column.
• Tank shields when enemies adjacent; Bomber explodes when surrounded.
• Sniper shoots straight when LOS.
• Teleporter blinks to tail if cut off.
Demonstrates use of shared history trail.
"""
import random
from collections import deque
from typing import Dict, Any, Deque

DIRS = ["east","south","west","north"]

class SharedTrail:
    trail: Deque[tuple[int,int]] = deque(maxlen=50)  # positions of head per turn

class Base:
    order = []  # overridden
    def __init__(self):
        self.state: Dict[str,Any] = {}

    def follow_trail(self, obs, idx):
        if len(SharedTrail.trail) > idx:
            tx, ty = list(SharedTrail.trail)[-idx-1]
            me = obs["self"]
            if (tx,ty)==(me["x"],me["y"]):
                return None
            dir_ = (
                "east" if tx>me["x"] else "west" if tx<me["x"] else
                "south" if ty>me["y"] else "north")
            return {"type":"move","direction":dir_}
        return None

class Scout(Base):
    def decide(self, obs):
        # head moves clockwise around perimeter
        me=obs["self"]
        turn = obs["turn"]
        dir_=DIRS[turn%4]
        # record trail
        SharedTrail.trail.append((me["x"],me["y"]))
        if me["cooldowns"]["power"]==0 and random.random()<0.3:
            return {"type":"dash","direction":dir_}
        return {"type":"move","direction":dir_}

class Tank(Base):
    def decide(self, obs):
        me=obs["self"]
        move=self.follow_trail(obs,1)
        if move: return move
        if any(abs(e["x"]-me["x"])+abs(e["y"]-me["y"])==1 for e in obs["visible_enemies"]):
            if me["cooldowns"]["power"]==0:
                return {"type":"shield"}
        return {"type":"move","direction":random.choice(DIRS)}

class Sniper(Base):
    def decide(self, obs):
        me=obs["self"]
        move=self.follow_trail(obs,2)
        if move: return move
        for e in obs["visible_enemies"]:
            if (e["x"]==me["x"] or e["y"]==me["y"]) and abs(e["x"]-me["x"])+abs(e["y"]-me["y"])<=5:
                return {"type":"snipe","target":{"x":e["x"],"y":e["y"]}}
        return {"type":"move","direction":random.choice(DIRS)}

class Bomber(Base):
    def decide(self, obs):
        me=obs["self"]
        move=self.follow_trail(obs,3)
        if move: return move
        adj=[e for e in obs["visible_enemies"] if abs(e["x"]-me["x"])<=1 and abs(e["y"]-me["y"])<=1]
        if len(adj)>=2 and me["cooldowns"]["power"]==0:
            return {"type":"explode"}
        return {"type":"move","direction":random.choice(DIRS)}

class Teleporter(Base):
    def decide(self, obs):
        me=obs["self"]
        move=self.follow_trail(obs,4)
        if move: return move
        if not self.state.get("blinked") and me["cooldowns"]["power"]==0:
            self.state["blinked"] = True
            return {"type":"blink"}
        return {"type":"move","direction":random.choice(DIRS)} 