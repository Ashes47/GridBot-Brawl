class _Base:
    def __init__(self, direction="east"):
        self.direction = direction
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        # attack adjacent if any
        def dir_to(e):
            if e["x"] == me["x"] and e["y"] == me["y"]-1: return "north"
            if e["x"] == me["x"] and e["y"] == me["y"]+1: return "south"
            if e["y"] == me["y"] and e["x"] == me["x"]-1: return "west"
            if e["y"] == me["y"] and e["x"] == me["x"]+1: return "east"
            return None
        for e in enemies:
            d = dir_to(e)
            if d:
                return {"type": "attack", "direction": d}
        # otherwise move forward
        return {"type": "move", "direction": self.direction}

class Sniper(_Base):
    pass
class Tank(_Base):
    pass
class Bomber(_Base):
    pass
class Scout(_Base):
    pass
class Teleporter(_Base):
    pass 