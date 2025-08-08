"""Team AreaControl – tries to control centre and focus fire on lowest HP enemy.
Advanced behaviour:
• All bots move toward central 3×3 zone; Scout dashes first.
• Shared target selection: lowest-HP enemy seen; stored in class variable so bots coordinate.
• Sniper snipes target if line-of-sight ≤5 else moves to get LOS.
• Tank shields allies by body-blocking; shields when power ready and enemies in range.
• Bomber waits until target is adjacent then explodes.
• Teleporter blinks to opposite side of target to sandwich.
Note: Uses class-level shared_state for cooperation.
"""
import random
from typing import Dict, Any, List, ClassVar

CENTRE = [(4,4),(5,4),(4,5),(5,5)]  # for 10×10
DIRS = ["north","south","east","west"]

def dir_to(me, tgt):
    if abs(tgt["x"]-me["x"]) > abs(tgt["y"]-me["y"]):
        return "east" if tgt["x"]>me["x"] else "west"
    return "south" if tgt["y"]>me["y"] else "north"

class Shared:
    target: ClassVar[Dict[str,Any]|None] = None

class Base:
    def __init__(self):
        self.state: Dict[str,Any] = {}

    def pick_target(self, obs):
        if obs["visible_enemies"]:
            Shared.target = min(obs["visible_enemies"], key=lambda e:e.get("health",100))
        return Shared.target

    def move_toward(self, me, pt):
        dx = pt[0]-me["x"]
        dy = pt[1]-me["y"]
        if abs(dx)>abs(dy):
            return {"type":"move","direction":"east" if dx>0 else "west"}
        return {"type":"move","direction":"south" if dy>0 else "north"}

class Sniper(Base):
    def decide(self, obs):
        me = obs["self"]
        tgt=self.pick_target(obs)
        if tgt and (tgt["x"]==me["x"] or tgt["y"]==me["y"]) and abs(tgt["x"]-me["x"])+abs(tgt["y"]-me["y"])<=5:
            return {"type":"snipe","target":{"x":tgt["x"],"y":tgt["y"]}}
        # move to get LOS by aligning x first
        if tgt:
            if tgt["x"]!=me["x"]:
                return {"type":"move","direction":"east" if tgt["x"]>me["x"] else "west"}
        return self.move_toward(me, random.choice(CENTRE))

class Tank(Base):
    def decide(self, obs):
        me=obs["self"]
        tgt=self.pick_target(obs)
        if me["cooldowns"]["power"]==0 and tgt and abs(tgt["x"]-me["x"])+abs(tgt["y"]-me["y"])<=2:
            return {"type":"shield"}
        if tgt and abs(tgt["x"]-me["x"])+abs(tgt["y"]-me["y"])==1:
            return {"type":"attack","direction":dir_to(me,tgt)}
        return self.move_toward(me, random.choice(CENTRE))

class Bomber(Base):
    def decide(self, obs):
        me=obs["self"]
        tgt=self.pick_target(obs)
        if tgt and abs(tgt["x"]-me["x"])<=1 and abs(tgt["y"]-me["y"])<=1 and me["cooldowns"]["power"]==0:
            return {"type":"explode"}
        if tgt:
            return {"type":"move","direction":dir_to(me,tgt)}
        return self.move_toward(me, random.choice(CENTRE))

class Scout(Base):
    def decide(self, obs):
        me=obs["self"]
        # dash toward centre if far
        if max(abs(me["x"]-5),abs(me["y"]-5))>3 and me["cooldowns"]["power"]==0:
            return {"type":"dash","direction":dir_to(me,{"x":5,"y":5})}
        return {"type":"move","direction":random.choice(DIRS)}

class Teleporter(Base):
    def decide(self, obs):
        me=obs["self"]
        tgt=self.pick_target(obs)
        if tgt and not self.state.get("blinked"):
            self.state["blinked"]=True
            return {"type":"blink"}
        return {"type":"move","direction":random.choice(DIRS)} 