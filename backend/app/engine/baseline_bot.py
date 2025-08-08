class _Helpers:
    @staticmethod
    def manhattan(a, b):
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    @staticmethod
    def chebyshev(a, b):
        return max(abs(a[0] - b[0]), abs(a[1] - b[1]))

    @staticmethod
    def dir_from_to(ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        if abs(dx) >= abs(dy):
            return "east" if dx > 0 else ("west" if dx < 0 else ("south" if dy > 0 else "north"))
        else:
            return "south" if dy > 0 else ("north" if dy < 0 else ("east" if dx > 0 else "west"))

    @staticmethod
    def dir_cardinal(ax, ay, bx, by):
        # strict cardinal direction toward target
        if bx == ax:
            return "south" if by > ay else "north"
        if by == ay:
            return "east" if bx > ax else "west"
        # fall back to the dominant axis
        return _Helpers.dir_from_to(ax, ay, bx, by)

    @staticmethod
    def dir_to_adjacent(ax, ay, ex, ey):
        if ex == ax and ey == ay - 1:
            return "north"
        if ex == ax and ey == ay + 1:
            return "south"
        if ey == ay and ex == ax - 1:
            return "west"
        if ey == ay and ex == ax + 1:
            return "east"
        return None


class _Base:
    def __init__(self):
        # simple memory that can be used across turns
        self.last_target_id = None
        self.patrol_dir = "east"

    def _choose_nearest_enemy(self, me, enemies):
        if not enemies:
            return None
        me_pos = (me["x"], me["y"])
        enemies_sorted = sorted(enemies, key=lambda e: _Helpers.manhattan(me_pos, (e["x"], e["y"])))
        return enemies_sorted[0]

    def _adjacent_enemies(self, me, enemies):
        ax, ay = me["x"], me["y"]
        adj = []
        for e in enemies:
            if _Helpers.manhattan((ax, ay), (e["x"], e["y"])) == 1:
                adj.append(e)
        return adj

    def _has_line_of_sight(self, me, e, max_range=5):
        # No walls in current engine; line-of-sight for straight lines within range
        if me["x"] == e["x"]:
            dy = abs(me["y"] - e["y"])
            return dy <= max_range
        if me["y"] == e["y"]:
            dx = abs(me["x"] - e["x"])
            return dx <= max_range
        return False

    def _move_towards(self, me, target):
        return {"type": "move", "direction": _Helpers.dir_from_to(me["x"], me["y"], target["x"], target["y"]) }

    def _kite_from(self, me, threat):
        # move away from threat
        dx = me["x"] - threat["x"]
        dy = me["y"] - threat["y"]
        dir_ = "east" if dx > 0 else ("west" if dx < 0 else ("south" if dy > 0 else "north"))
        return {"type": "move", "direction": dir_}

    def _default_patrol(self, me, map_size):
        # simple back-and-forth horizontal patrol around center line
        w, h = map_size
        x, y = me["x"], me["y"]
        if self.patrol_dir == "east" and x >= w - 2:
            self.patrol_dir = "west"
        elif self.patrol_dir == "west" and x <= 1:
            self.patrol_dir = "east"
        return {"type": "move", "direction": self.patrol_dir}

    def decide(self, obs):
        # Fallback behavior: attack if adjacent else move toward nearest enemy or patrol
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            # hit the lowest HP adjacent if we know HP (we don't), just pick first
            e = adj[0]
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], e["x"], e["y"])
            if d:
                return {"type": "attack", "direction": d}
        target = self._choose_nearest_enemy(me, enemies)
        if target:
            return self._move_towards(me, target)
        return self._default_patrol(me, obs.get("map_size", [10, 10]))


class Sniper(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        # If adjacent threat, back up or attack
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            # Prefer moving away; if cannot determine safe, attack
            threat = adj[0]
            # If we are cornered (heuristic), just attack
            if cooldown == 0 and len(enemies) >= 1:
                # quick micro backstep
                return self._kite_from(me, threat)
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], threat["x"], threat["y"])
            return {"type": "attack", "direction": d or "north"}

        # Snipe straight line up to 5 tiles if ready
        if cooldown == 0:
            me_pos = (me["x"], me["y"])
            line_targets = [e for e in enemies if self._has_line_of_sight(me, e, max_range=5)]
            if line_targets:
                # shoot the closest by chebyshev
                tgt = sorted(line_targets, key=lambda e: _Helpers.chebyshev(me_pos, (e["x"], e["y"])))[0]
                return {"type": "snipe", "target": {"x": tgt["x"], "y": tgt["y"]}}

        # Otherwise move to get alignment with nearest enemy
        target = self._choose_nearest_enemy(me, enemies)
        if target:
            # try to align rows/cols
            if target["x"] != me["x"]:
                dir_ = "east" if target["x"] > me["x"] else "west"
                return {"type": "move", "direction": dir_}
            else:
                dir_ = "south" if target["y"] > me["y"] else "north"
                return {"type": "move", "direction": dir_}
        return self._default_patrol(me, obs.get("map_size", [10, 10]))


class Tank(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        hp = me.get("health", 100)

        adj = self._adjacent_enemies(me, enemies)
        danger = len(adj) >= 2 or hp <= 60
        if cooldown == 0 and danger:
            return {"type": "shield"}

        if adj:
            # attack the first adjacent enemy
            e = adj[0]
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], e["x"], e["y"])
            return {"type": "attack", "direction": d or "north"}

        # push towards nearest enemy
        target = self._choose_nearest_enemy(me, enemies)
        if target:
            return self._move_towards(me, target)
        return self._default_patrol(me, obs.get("map_size", [10, 10]))


class Bomber(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        hp = me.get("health", 100)

        adj = self._adjacent_enemies(me, enemies)
        if cooldown == 0:
            # explode if it hits 2+ enemies, or trade if low HP with any enemy adjacent
            if len(adj) >= 2 or (hp <= 25 and len(adj) >= 1):
                return {"type": "explode"}

        if adj:
            # otherwise normal attack
            e = adj[0]
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], e["x"], e["y"])
            return {"type": "attack", "direction": d or "north"}

        target = self._choose_nearest_enemy(me, enemies)
        if target:
            return self._move_towards(me, target)
        return self._default_patrol(me, obs.get("map_size", [10, 10]))


class Scout(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        adj = self._adjacent_enemies(me, enemies)
        if adj:
            # kite away if possible, else attack
            threat = adj[0]
            if cooldown == 0:
                # dash out in the opposite cardinal direction
                away = self._kite_from(me, threat)["direction"]
                return {"type": "dash", "direction": away}
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], threat["x"], threat["y"])
            return {"type": "attack", "direction": d or "north"}

        # dash towards nearest enemy to close in quickly
        target = self._choose_nearest_enemy(me, enemies)
        if target:
            if cooldown == 0:
                dir_ = _Helpers.dir_from_to(me["x"], me["y"], target["x"], target["y"])  # longer stride
                return {"type": "dash", "direction": dir_}
            return self._move_towards(me, target)
        return self._default_patrol(me, obs.get("map_size", [10, 10]))


class Teleporter(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        hp = me.get("health", 100)

        adj = self._adjacent_enemies(me, enemies)
        # panic blink if in danger
        if cooldown == 0 and (len(adj) >= 2 or hp <= 35):
            return {"type": "blink"}

        if adj:
            # attack 1v1 when safe
            e = adj[0]
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], e["x"], e["y"])
            return {"type": "attack", "direction": d or "north"}

        # otherwise reposition toward nearest enemy
        target = self._choose_nearest_enemy(me, enemies)
        if target:
            return self._move_towards(me, target)
        return self._default_patrol(me, obs.get("map_size", [10, 10])) 