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

    @staticmethod
    def add(p, q):
        return (p[0] + q[0], p[1] + q[1])

    @staticmethod
    def clamp(x, lo, hi):
        return max(lo, min(hi, x))


class _Team:
    """Stateless team tactics helpers computed per-observation."""

    # Higher number means higher priority to kill
    ROLE_FOCUS_SCORE = {
        # High value disruptors / damage
        "healer": 9,
        "silencer": 9,
        "sniper": 8,
        "bomber": 8,
        "puller": 7,
        "pusher": 7,
        "jammer": 6,
        "poisoner": 6,
        # Utility
        "shield_giver": 5,
        "reflector": 4,
        "trap_setter": 4,
        "decoy_caster": 3,
        "wall_builder": 3,
        # Frontline / mobility
        "bruiser": 5,
        "tank": 4,
        "leaper": 4,
        "scout": 3,
        "teleporter": 3,
    }

    @staticmethod
    def allies(obs):
        return obs.get("visible_allies", [])

    @staticmethod
    def enemies(obs):
        return obs.get("visible_enemies", [])

    @staticmethod
    def team_centroid(obs):
        pts = [(obs["self"]["x"], obs["self"]["y"])] + [(a["x"], a["y"]) for a in _Team.allies(obs)]
        if not pts:
            return (obs["self"]["x"], obs["self"]["y"])
        sx = sum(p[0] for p in pts) / len(pts)
        sy = sum(p[1] for p in pts) / len(pts)
        return (sx, sy)

    @staticmethod
    def enemy_cluster_center(obs):
        enemies = _Team.enemies(obs)
        if not enemies:
            return None
        sx = sum(e["x"] for e in enemies) / len(enemies)
        sy = sum(e["y"] for e in enemies) / len(enemies)
        return (sx, sy)

    @staticmethod
    def choose_focus_enemy(obs):
        """Choose a shared focus target: highest priority, then closest to team centroid."""
        enemies = _Team.enemies(obs)
        if not enemies:
            return None
        cx, cy = _Team.team_centroid(obs)
        def score(e):
            role_score = _Team.ROLE_FOCUS_SCORE.get(e.get("component", ""), 0)
            dist = _Helpers.manhattan((cx, cy), (e["x"], e["y"]))
            # prioritize higher role_score and nearer distance
            return (-role_score, dist, e["x"], e["y"])  # stable last keys
        enemies_sorted = sorted(enemies, key=score)
        return enemies_sorted[0]

    @staticmethod
    def count_adjacent_to(x, y, enemies):
        c = 0
        for e in enemies:
            if _Helpers.manhattan((x, y), (e["x"], e["y"])) == 1:
                c += 1
        return c

    @staticmethod
    def avoid_stack_direction(me, desired_dir, allies):
        """If an ally is already in desired tile, try alternate axis to reduce collisions."""
        dxdy = {
            "north": (0, -1),
            "south": (0, 1),
            "east": (1, 0),
            "west": (-1, 0),
        }.get(desired_dir, (0, 0))
        nx, ny = me["x"] + dxdy[0], me["y"] + dxdy[1]
        for a in allies:
            if a["x"] == nx and a["y"] == ny:
                # pick perpendicular
                if desired_dir in ("north", "south"):
                    return "east" if nx <= a["x"] else "west"
                else:
                    return "south" if ny <= a["y"] else "north"
        return desired_dir


class _Base:
    def _adjacent_enemies(self, me, enemies):
        ax, ay = me["x"], me["y"]
        adj = []
        for e in enemies:
            if _Helpers.manhattan((ax, ay), (e["x"], e["y"])) == 1:
                adj.append(e)
        return adj

    def _has_straight_los_guess(self, me, e, visibles):
        # Rough LoS guard using visible blockers in radius 4: bots (allies/enemies) + decoys + walls
        if not (me["x"] == e["x"] or me["y"] == e["y"]):
            return False
        # build blockers from visible allies/enemies and visible_decoys/walls
        blockers = set((b["x"], b["y"]) for b in visibles.get("enemies", []))
        blockers |= set((b["x"], b["y"]) for b in visibles.get("allies", []))
        blockers |= set((w[0], w[1]) for w in visibles.get("walls", []))
        blockers |= set((d[0], d[1]) for d in visibles.get("decoys", []))
        # trace
        if me["x"] == e["x"]:
            step = 1 if e["y"] > me["y"] else -1
            for y in range(me["y"] + step, e["y"], step):
                if (me["x"], y) in blockers:
                    return False
            return True
        if me["y"] == e["y"]:
            step = 1 if e["x"] > me["x"] else -1
            for x in range(me["x"] + step, e["x"], step):
                if (x, me["y"]) in blockers:
                    return False
            return True
        return False

    def _move_towards_focus(self, obs):
        me = obs["self"]
        target = _Team.choose_focus_enemy(obs)
        if target:
            dir_ = _Helpers.dir_from_to(me["x"], me["y"], target["x"], target["y"])
            dir_ = _Team.avoid_stack_direction(me, dir_, obs.get("visible_allies", []))
            return {"type": "move", "direction": dir_}
        # idle patrol toward enemy cluster or bounce horizontally
        center = _Team.enemy_cluster_center(obs)
        if center:
            dir_ = _Helpers.dir_from_to(me["x"], me["y"], int(center[0]), int(center[1]))
            return {"type": "move", "direction": dir_}
        # fallback sweep
        # sweep along row: toward map edge then back
        w, h = obs.get("map_size", [10, 10])
        return {"type": "move", "direction": "east" if me["x"] < w - 2 else "west"}


class Sniper(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        allies = obs.get("visible_allies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        # If adjacent threat, kite back if possible; otherwise attack
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            threat = adj[0]
            # Try to step away along opposite cardinal
            away = _Helpers.dir_cardinal(threat["x"], threat["y"], me["x"], me["y"])  # from threat to me
            # try avoid stacking
            away = _Team.avoid_stack_direction(me, away, allies)
            return {"type": "move", "direction": away}

        # Coordinated snipe: straight line within 5, LoS guess using visibles
        if cooldown == 0 and enemies:
            me_pos = (me["x"], me["y"])
            visibles = {
                "enemies": enemies,
                "allies": allies,
                "walls": obs.get("visible_walls", []),
                "decoys": obs.get("visible_decoys", []),
            }
            # prefer focus target if snipeable
            focus = _Team.choose_focus_enemy(obs)
            line_targets = []
            for e in enemies:
                if _Helpers.chebyshev(me_pos, (e["x"], e["y"])) <= 5 and self._has_straight_los_guess(me, e, visibles):
                    line_targets.append(e)
            if focus and focus in line_targets:
                return {"type": "snipe", "target": {"x": focus["x"], "y": focus["y"]}}
            if line_targets:
                # shoot closest by chebyshev
                tgt = sorted(line_targets, key=lambda e: _Helpers.chebyshev(me_pos, (e["x"], e["y"])))[0]
                return {"type": "snipe", "target": {"x": tgt["x"], "y": tgt["y"]}}

        # Move to align with team focus on row/column
        focus = _Team.choose_focus_enemy(obs)
        if focus:
            if focus["x"] != me["x"]:
                dir_ = "east" if focus["x"] > me["x"] else "west"
            else:
                dir_ = "south" if focus["y"] > me["y"] else "north"
            dir_ = _Team.avoid_stack_direction(me, dir_, allies)
            return {"type": "move", "direction": dir_}
        return self._move_towards_focus(obs)


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
            # Attack the focus target if adjacent else first
            focus = _Team.choose_focus_enemy(obs)
            e = next((x for x in adj if focus and x["id"] == focus["id"]), adj[0])
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], e["x"], e["y"])
            return {"type": "attack", "direction": d or "north"}

        return self._move_towards_focus(obs)


class Bomber(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        hp = me.get("health", 100)

        adj = self._adjacent_enemies(me, enemies)
        if cooldown == 0:
            # explode if it hits 2+ enemies, or trade if low HP with any enemy adjacent
            if len(adj) >= 2 or (hp <= 30 and len(adj) >= 1):
                return {"type": "explode"}

        if adj:
            # otherwise attack to secure kill
            e = adj[0]
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], e["x"], e["y"])
            return {"type": "attack", "direction": d or "north"}

        # Move slightly ahead of tank toward focus
        return self._move_towards_focus(obs)


class Scout(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        allies = obs.get("visible_allies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        adj = self._adjacent_enemies(me, enemies)
        if adj:
            threat = adj[0]
            if cooldown == 0:
                # dash away to kite in opposite cardinal
                away = _Helpers.dir_cardinal(threat["x"], threat["y"], me["x"], me["y"])  # from threat to me
                away = _Team.avoid_stack_direction(me, away, allies)
                return {"type": "dash", "direction": away}
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], threat["x"], threat["y"])
            return {"type": "attack", "direction": d or "north"}

        # dash toward team focus
        focus = _Team.choose_focus_enemy(obs)
        if focus:
            if cooldown == 0:
                dir_ = _Helpers.dir_from_to(me["x"], me["y"], focus["x"], focus["y"])  # longer stride
                dir_ = _Team.avoid_stack_direction(me, dir_, allies)
                return {"type": "dash", "direction": dir_}
        return self._move_towards_focus(obs)


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
        return self._move_towards_focus(obs)


# ---------------- Additional advanced roles (for synergy) ----------------

class Healer(_Base):
    def decide(self, obs):
        me = obs["self"]
        allies = obs.get("visible_allies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        # We do not have ally HP in obs; project shield/ heal heuristically to front-most ally within 2 if off CD
        if cooldown == 0 and allies:
            # Pick ally closest to enemy cluster center and within range 2
            center = _Team.enemy_cluster_center(obs)
            if center:
                candidates = sorted(allies, key=lambda a: _Helpers.manhattan((a[0], a[1]), (int(center[0]), int(center[1]))))
            else:
                candidates = allies
            for a in candidates:
                if _Helpers.chebyshev((me["x"], me["y"]), (a["x"], a["y"])) <= 2:
                    return {"type": "heal", "target": {"x": a["x"], "y": a["y"]}}
        # default: move with team focus
        return self._move_towards_focus(obs)


class Shield_Giver(_Base):
    def decide(self, obs):
        me = obs["self"]
        allies = obs.get("visible_allies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        if cooldown == 0 and allies:
            # shield frontliner (closest to enemy center) within range 3
            center = _Team.enemy_cluster_center(obs)
            priorities = sorted(allies, key=lambda a: _Helpers.manhattan((a["x"], a["y"]), (int(center[0]), int(center[1]))))
            for a in priorities:
                if _Helpers.chebyshev((me["x"], me["y"]), (a["x"], a["y"])) <= 3:
                    return {"type": "project_shield", "target": {"x": a["x"], "y": a["y"]}}
        return self._move_towards_focus(obs)


class Silencer(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        if cooldown == 0 and enemies:
            focus = _Team.choose_focus_enemy(obs)
            # Use if within 3 and straight LoS guess
            if focus and _Helpers.chebyshev((me["x"], me["y"]), (focus["x"], focus["y"])) <= 3:
                return {"type": "silence", "target": {"x": focus["x"], "y": focus["y"]}}
        # fight or move
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": d or "north"}
        return self._move_towards_focus(obs)


class Poisoner(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        if cooldown == 0 and enemies:
            focus = _Team.choose_focus_enemy(obs)
            if focus and _Helpers.chebyshev((me["x"], me["y"]), (focus["x"], focus["y"])) <= 1:
                return {"type": "infect", "target": {"x": focus["x"], "y": focus["y"]}}
        # normal fight/move
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": d or "north"}
        return self._move_towards_focus(obs)


class Jammer(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        if cooldown == 0 and enemies:
            # if any enemy within radius 3, scramble
            if any(_Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) <= 3 for e in enemies):
                return {"type": "scramble"}
        # otherwise fight/move
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": d or "north"}
        return self._move_towards_focus(obs)


class Reflector(_Base):
    def decide(self, obs):
        me = obs["self"]
        cooldown = me.get("cooldowns", {}).get("power", 0)
        enemies = obs.get("visible_enemies", [])
        # ready reflect when enemies are nearby or sniper threats exist
        if cooldown == 0 and enemies:
            high_threat = any(e.get("component") in ("sniper", "bomber") for e in enemies)
            close = any(_Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) <= 4 for e in enemies)
            if high_threat or close:
                return {"type": "mirror"}
        # attack or move
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": d or "north"}
        return self._move_towards_focus(obs)


class Trap_Setter(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        # Plant trap if enemies likely to traverse adjacent area (when any enemy within 2)
        if cooldown == 0 and any(_Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) <= 2 for e in enemies):
            return {"type": "drop_trap"}
        # attack or move
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": d or "north"}
        return self._move_towards_focus(obs)


class Wall_Builder(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        if cooldown == 0 and enemies:
            # Build a wall one step toward nearest enemy to create cover
            focus = _Team.choose_focus_enemy(obs)
            if focus:
                dir_ = _Helpers.dir_cardinal(me["x"], me["y"], focus["x"], focus["y"])
                dx, dy = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}[dir_]
                tx, ty = me["x"] + dx, me["y"] + dy
                w, h = obs.get("map_size", [10, 10])
                if 0 <= tx < w and 0 <= ty < h:
                    return {"type": "drop_wall", "target": {"x": tx, "y": ty}}
        # fight/move
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": d or "north"}
        return self._move_towards_focus(obs)


class Decoy_Caster(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        if cooldown == 0 and enemies:
            # place decoy between us and focus target if adjacent tile free (engine will validate)
            focus = _Team.choose_focus_enemy(obs)
            if focus:
                dir_ = _Helpers.dir_cardinal(me["x"], me["y"], focus["x"], focus["y"])
                dx, dy = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}[dir_]
                tx, ty = me["x"] + dx, me["y"] + dy
                return {"type": "clone", "target": {"x": tx, "y": ty}}
        # fallback fight/move
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": d or "north"}
        return self._move_towards_focus(obs)


class Puller(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        if cooldown == 0 and enemies:
            # pull focus target if within 3 and line
            focus = _Team.choose_focus_enemy(obs)
            if focus and _Helpers.chebyshev((me["x"], me["y"]), (focus["x"], focus["y"])) <= 3:
                return {"type": "yank", "target": {"x": focus["x"], "y": focus["y"]}}
        # otherwise fight/move
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": d or "north"}
        return self._move_towards_focus(obs)


class Pusher(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        adj = self._adjacent_enemies(me, enemies)
        if cooldown == 0 and adj:
            # shove first adjacent to scatter enemy lines
            e = adj[0]
            return {"type": "shove", "target": {"x": e["x"], "y": e["y"]}}
        if adj:
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": d or "north"}
        return self._move_towards_focus(obs)


class Leaper(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        if cooldown == 0 and enemies:
            # attempt diagonal leap 2 tiles to flank near focus target
            focus = _Team.choose_focus_enemy(obs)
            if focus:
                # pick diagonal landing two steps that reduces distance
                candidates = [
                    (me["x"] + 2, me["y"] + 2),
                    (me["x"] + 2, me["y"] - 2),
                    (me["x"] - 2, me["y"] + 2),
                    (me["x"] - 2, me["y"] - 2),
                ]
                w, h = obs.get("map_size", [10, 10])
                best = None
                best_dist = 10**9
                for (tx, ty) in candidates:
                    if 0 <= tx < w and 0 <= ty < h:
                        d = _Helpers.manhattan((tx, ty), (focus["x"], focus["y"]))
                        if d < best_dist:
                            best_dist = d
                            best = (tx, ty)
                if best is not None:
                    return {"type": "leap", "target": {"x": best[0], "y": best[1]}}
        # normal fight/move
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": d or "north"}
        return self._move_towards_focus(obs)


class Bruiser(_Base):
    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            focus = _Team.choose_focus_enemy(obs)
            e = next((x for x in adj if focus and x["id"] == focus["id"]), adj[0])
            d = _Helpers.dir_to_adjacent(me["x"], me["y"], e["x"], e["y"])
            return {"type": "attack", "direction": d or "north"}
        return self._move_towards_focus(obs) 