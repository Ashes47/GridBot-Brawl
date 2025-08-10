"""Team Grandmaster-Control — an epic, tournament-ready bot set.

Implements all 18 component classes with coordinated, map-aware tactics inspired by
guide.html, maps.html, and bots.html. Designed to be a strong baseline for competitive play.

Recommended 5-bot roster examples:
- Control Setup: Sniper + Wall_Builder + Trap_Setter + Puller + Silencer
- Balanced Core: Tank + Sniper + Healer + Scout + Bomber
- Disrupt Rush: Bruiser + Scout + Bomber + Pusher + Jammer

Each class exposes decide(self, obs) -> action dict, compatible with the engine.
"""

import random


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

    @staticmethod
    def get_direction_coords(direction):
        """Convert direction string to coordinate delta."""
        return {
            "north": (0, -1),
            "south": (0, 1),
            "east": (1, 0),
            "west": (-1, 0),
        }.get(direction, (0, 0))

    @staticmethod
    def is_straight_line(ax, ay, bx, by):
        """Check if two points are on the same row, column, or diagonal."""
        dx, dy = abs(bx - ax), abs(by - ay)
        return dx == 0 or dy == 0 or dx == dy

    @staticmethod
    def count_enemies_in_range(pos, enemies, max_range):
        """Count enemies within range of position."""
        return sum(1 for e in enemies if _Helpers.chebyshev(pos, (e["x"], e["y"])) <= max_range)


class _Team:
    """Stateless team tactics helpers computed per-observation."""

    # Higher number means higher priority to kill (based on bots.html strategy analysis)
    ROLE_FOCUS_SCORE = {
        # Highest priority: force multipliers and key damage dealers
        "healer": 10,
        "shield_giver": 9,
        "silencer": 9,
        "sniper": 8,

        # High priority: disruptors and area control
        "puller": 8,
        "jammer": 7,
        "bomber": 7,
        "poisoner": 6,

        # Medium priority: utility and control
        "trap_setter": 5,
        "wall_builder": 5,
        "reflector": 5,
        "pusher": 4,
        "decoy_caster": 4,

        # Lower priority: frontline and mobility
        "bruiser": 4,
        "tank": 3,
        "leaper": 3,
        "scout": 3,
        "teleporter": 2,
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

    def _has_los_guess(self, me, e, visibles):
        # LoS guess using visible blockers in radius 4: bots + decoys + (static) walls and forest.
        # Matches engine diagonal rule (no corner cutting) approximately.
        sx, sy = me["x"], me["y"]
        dx, dy = e["x"], e["y"]
        blockers = set((b["x"], b["y"]) for b in visibles.get("enemies", []))
        blockers |= set((b["x"], b["y"]) for b in visibles.get("allies", []))
        blockers |= set((w[0], w[1]) for w in visibles.get("walls", []))
        blockers |= set((w[0], w[1]) for w in visibles.get("static_walls", []))
        blockers |= set((d[0], d[1]) for d in visibles.get("decoys", []))
        forest = set((t[0], t[1]) for t in visibles.get("terrain", []) if len(t) >= 3 and t[2] == "forest")

        def is_blocked(x, y):
            return (x, y) in blockers or (x, y) in forest

        if sx == dx:
            step = 1 if dy > sy else -1
            for y in range(sy + step, dy, step):
                if is_blocked(sx, y):
                    return False
            return True
        if sy == dy:
            step = 1 if dx > sx else -1
            for x in range(sx + step, dx, step):
                if is_blocked(x, sy):
                    return False
            return True
        # diagonal if equal deltas: enforce corner checks each step
        if abs(dx - sx) == abs(dy - sy):
            stepx = 1 if dx > sx else -1
            stepy = 1 if dy > sy else -1
            cx, cy = sx, sy
            for _ in range(abs(dx - sx)):
                # check orthogonal corners from current cell towards next
                if is_blocked(cx + stepx, cy) or is_blocked(cx, cy + stepy):
                    return False
                # move diagonally
                nx, ny = cx + stepx, cy + stepy
                if (nx, ny) != (dx, dy) and is_blocked(nx, ny):
                    return False
                cx, cy = nx, ny
            return True
        return False

    def _dir_to_dxdy(self, direction):
        return {
            "north": (0, -1),
            "south": (0, 1),
            "east": (1, 0),
            "west": (-1, 0),
        }.get(direction, (0, 0))

    def _collect_vis(self, obs):
        # Returns (walls, decoys, allies, terrain_at, zones_at, traps, static_walls)
        walls = set((w[0], w[1]) for w in obs.get("visible_walls", []) or [])
        static_walls = set((w[0], w[1]) for w in obs.get("visible_static_walls", []) or [])
        all_walls = walls | static_walls
        decoys = set((d[0], d[1]) for d in obs.get("visible_decoys", []) or [])
        allies = set((a["x"], a["y"]) for a in obs.get("visible_allies", []) or [])
        terrain_at = {}
        for t in obs.get("visible_terrain", []) or []:
            if len(t) >= 3:
                terrain_at[(t[0], t[1])] = t[2]
        zones_at = {}
        for z in obs.get("visible_zones", []) or []:
            if len(z) >= 3:
                zones_at[(z[0], z[1])] = list(z[2]) if isinstance(z[2], list) else [z[2]]
        traps = set((t[0], t[1]) for t in obs.get("visible_traps", []) or [])
        return all_walls, decoys, allies, terrain_at, zones_at, traps, static_walls

    def _tile_penalty(self, nx, ny, me, obs, for_dash=False):
        """
        Enhanced tile penalty calculation based on maps.html terrain mechanics.
        Lower penalty is better for movement decisions.
        """
        w, h = obs.get("map_size", [10, 10])
        if nx < 0 or ny < 0 or nx >= w or ny >= h:
            return 10_000

        walls, decoys, allies, terrain_at, zones_at, traps, _ = self._collect_vis(obs)

        # Completely impassable
        if (nx, ny) in walls or (nx, ny) in decoys:
            return 5_000

        # Strong avoidance of ally stacking
        if (nx, ny) in allies:
            return 400

        penalty = 0
        hp = me.get("health", 100)
        cooldown = me.get("cooldowns", {}).get("power", 0)
        enemies = obs.get("visible_enemies", [])

        # Terrain penalties based on maps.html mechanics
        terrain = terrain_at.get((nx, ny))
        if terrain == "water":
            penalty += 2_000  # Completely impassable
        elif terrain == "swamp":
            # Ending turn on swamp applies Slowed status (can't Move, Dash, Leap next turn)
            penalty += 150 if for_dash else 80
        elif terrain == "ice":
            # Normal move slides up to 2 extra tiles - can be dangerous or helpful
            penalty += 30  # Slight penalty due to unpredictability
        elif terrain == "forest":
            # Blocks LoS - good for cover from snipers and ranged attacks
            if enemies and any(e.get("component") in ("sniper", "bomber") for e in enemies):
                penalty -= 15  # Bonus for cover against ranged threats
            else:
                penalty -= 5   # Small general bonus for LoS breaking

        # Zone effects (applied at end of turn)
        zones = zones_at.get((nx, ny), [])
        if "damage" in zones:
            # -10 HP at end of turn (reduced by shields)
            penalty += 300 if hp <= 30 else 150
        if "heal" in zones:
            # +20 HP at end of turn (capped at 100)
            if hp <= 70:
                penalty -= 100
            elif hp <= 90:
                penalty -= 50
        if "boost" in zones:
            # Extra cooldown reduction - valuable for power-dependent bots
            if cooldown > 0:
                penalty -= 80
            else:
                penalty -= 20  # Still useful for future
        if "teleport" in zones:
            # Random teleport - high risk/reward
            if hp <= 25:
                penalty -= 30  # Desperate escape option
            else:
                penalty += 40  # Generally avoid unpredictability

        # Avoid stepping on own traps
        if (nx, ny) in traps:
            penalty += 200

        return penalty

    def _choose_best_direction(self, me, desired_dir, obs):
        # Evaluate desired and alternatives; pick lowest penalty
        dirs = [desired_dir]
        if desired_dir in ("north", "south"):
            dirs += ["east", "west", ("south" if desired_dir == "north" else "north")]
        else:
            dirs += ["north", "south", ("west" if desired_dir == "east" else "east")]
        best = None
        best_score = 10_000
        allies = obs.get("visible_allies", [])
        for d in dirs:
            d2 = _Team.avoid_stack_direction(me, d, allies)
            dx, dy = self._dir_to_dxdy(d2)
            nx, ny = me["x"] + dx, me["y"] + dy
            score = self._tile_penalty(nx, ny, me, obs)
            if score < best_score:
                best_score = score
                best = d2
        return best or desired_dir

    def _choose_best_dash(self, me, desired_dir, obs):
        # Dash is a 2-step in a cardinal direction; avoid hazardous paths
        candidates = ["north", "south", "east", "west"]
        # prioritize desired first
        ordered = [desired_dir] + [d for d in candidates if d != desired_dir]
        best = None
        best_score = 10_000
        for d in ordered:
            dx, dy = self._dir_to_dxdy(d)
            step1 = (me["x"] + dx, me["y"] + dy)
            step2 = (me["x"] + 2 * dx, me["y"] + 2 * dy)
            p1 = self._tile_penalty(step1[0], step1[1], me, obs, for_dash=True)
            p2 = self._tile_penalty(step2[0], step2[1], me, obs, for_dash=True)
            score = p1 + p2
            if p1 >= 2000 or p2 >= 2000:
                score += 5000
            if score < best_score:
                best_score = score
                best = d
        return best or desired_dir

    def _best_direction_towards(self, me, gx, gy, obs):
        # Choose direction that trades off hazard vs distance-to-goal
        alpha = 3  # weight for distance reduction
        best = None
        best_score = 10_000
        for d in ("north", "south", "east", "west"):
            dx, dy = self._dir_to_dxdy(d)
            nx, ny = me["x"] + dx, me["y"] + dy
            pen = self._tile_penalty(nx, ny, me, obs)
            dist = _Helpers.manhattan((nx, ny), (gx, gy))
            score = pen + alpha * dist
            if score < best_score:
                best_score = score
                best = d
        return best or "north"

    def _choose_zone_target(self, me, obs):
        # Return best zone tile (x,y) to head toward given context, or None
        _, _, _, terrain_at, zones_at, _, _ = self._collect_vis(obs)
        hp = me.get("health", 100)
        cooldown = me.get("cooldowns", {}).get("power", 0)
        candidates = []
        for (x, y), zlist in zones_at.items():
            if "damage" in zlist:
                continue
            desirability = 0
            if "heal" in zlist and hp <= 70:
                desirability += 8
            if "boost" in zlist and cooldown > 0:
                desirability += 5
            if "teleport" in zlist:
                desirability += 1
            if desirability <= 0:
                continue
            candidates.append(((x, y), desirability))
        if not candidates:
            return None
        # pick by desirability minus distance
        best = None
        best_score = -10_000
        for (x, y), desirability in candidates:
            dist = _Helpers.manhattan((me["x"], me["y"]), (x, y))
            score = desirability * 10 - dist
            if score > best_score:
                best_score = score
                best = (x, y)
        return best

    def _move_towards_focus(self, obs):
        me = obs["self"]
        # Zone opportunism first
        zone_goal = self._choose_zone_target(me, obs)
        if zone_goal:
            bx, by = zone_goal
            dir_ = self._best_direction_towards(me, bx, by, obs)
            return {"type": "move", "direction": dir_}
        target = _Team.choose_focus_enemy(obs)
        if target:
            dir_ = self._best_direction_towards(me, target["x"], target["y"], obs)
            return {"type": "move", "direction": dir_}
        # idle patrol toward enemy cluster or bounce horizontally
        center = _Team.enemy_cluster_center(obs)
        if center:
            dir_ = self._best_direction_towards(me, int(center[0]), int(center[1]), obs)
            return {"type": "move", "direction": dir_}
        # fallback sweep
        w, h = obs.get("map_size", [10, 10])
        dir_ = self._best_direction_towards(me, w - 1 if me["x"] < w - 2 else 0, me["y"], obs)
        return {"type": "move", "direction": dir_}


class Sniper(_Base):
    """
    🎯 Elite damage dealer requiring straight-line positioning.
    40 damage per shot, range 5, requires LoS. Can 3-shot most targets.
    Strategy: Backline positioning with clear sightlines, kite when pressured.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        allies = obs.get("visible_allies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        # If adjacent threat, prioritize kiting over attacking
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            threat = adj[0]
            # Kite away using cardinal direction, maintaining distance
            away_dir = _Helpers.dir_cardinal(threat["x"], threat["y"], me["x"], me["y"])
            safe_dir = self._choose_best_direction(me, away_dir, obs)
            return {"type": "move", "direction": safe_dir}

        # Snipe priority
        if cooldown == 0 and enemies:
            me_pos = (me["x"], me["y"]) 
            visibles = {
                "enemies": enemies,
                "allies": allies,
                "walls": obs.get("visible_walls", []),
                "static_walls": obs.get("visible_static_walls", []),
                "decoys": obs.get("visible_decoys", []),
                "terrain": obs.get("visible_terrain", []),
            }

            # Find all valid snipe targets (range 5, straight line, LoS)
            valid_targets = []
            for e in enemies:
                pos = (e["x"], e["y"])
                distance = _Helpers.chebyshev(me_pos, pos)
                if distance <= 5 and _Helpers.is_straight_line(me["x"], me["y"], e["x"], e["y"]):
                    if self._has_los_guess(me, e, visibles):
                        valid_targets.append(e)

            if valid_targets:
                # Prioritize focus target if available
                focus = _Team.choose_focus_enemy(obs)
                if focus and focus in valid_targets:
                    return {"type": "snipe", "target": {"x": focus["x"], "y": focus["y"]}}

                # Otherwise prioritize by role importance then distance
                def snipe_priority(target):
                    role_score = _Team.ROLE_FOCUS_SCORE.get(target.get("component", ""), 0)
                    distance = _Helpers.chebyshev(me_pos, (target["x"], target["y"]))
                    return (-role_score, distance)  # higher role score and closer is better

                best_target = min(valid_targets, key=snipe_priority)
                return {"type": "snipe", "target": {"x": best_target["x"], "y": best_target["y"]}}

        # Positioning
        focus = _Team.choose_focus_enemy(obs)
        if focus:
            direction = self._best_direction_towards(me, focus["x"], focus["y"], obs)
            return {"type": "move", "direction": direction}

        return self._move_towards_focus(obs)


class Tank(_Base):
    """
    🛡️ Premier frontliner and damage sponge.
    Shield reduces 75% of next 40 damage (expires end of next turn).
    Strategy: Position aggressively, use shield reactively when overwhelmed.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        allies = obs.get("visible_allies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        hp = me.get("health", 100)

        adj = self._adjacent_enemies(me, enemies)

        # Enhanced shield timing
        nearby_enemies = _Helpers.count_enemies_in_range((me["x"], me["y"]), enemies, 2)
        critical_danger = len(adj) >= 2 or hp <= 60 or nearby_enemies >= 3

        if cooldown == 0 and critical_danger:
            return {"type": "shield"}

        # Attack adjacent
        if adj:
            focus = _Team.choose_focus_enemy(obs)
            target = next((e for e in adj if focus and e["id"] == focus["id"]), adj[0])
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], target["x"], target["y"])
            return {"type": "attack", "direction": direction or "north"}

        # Advance toward focus
        focus = _Team.choose_focus_enemy(obs)
        if focus:
            healer_range = any(
                a.get("component") == "healer" and _Helpers.chebyshev((me["x"], me["y"]), (a["x"], a["y"])) <= 3
                for a in allies
            )
            if healer_range or hp > 70:
                direction = self._best_direction_towards(me, focus["x"], focus["y"], obs)
                return {"type": "move", "direction": direction}

        return self._move_towards_focus(obs)


class Bomber(_Base):
    """
    💣 High-risk, high-reward AoE specialist.
    Explosion deals 30 AoE damage (3×3) + 10 self-damage.
    Strategy: Dive enemy clusters for 2+ target explosions, trade when low HP.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        hp = me.get("health", 100)

        adj = self._adjacent_enemies(me, enemies)

        if cooldown == 0:
            explosion_targets = [e for e in enemies if _Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) <= 1]
            should_explode = len(explosion_targets) >= 2 or (hp <= 30 and len(explosion_targets) >= 1)
            if not should_explode and hp <= 40 and explosion_targets:
                focus = _Team.choose_focus_enemy(obs)
                if focus and focus in explosion_targets:
                    priority = _Team.ROLE_FOCUS_SCORE.get(focus.get("component", ""), 0)
                    if priority >= 7:
                        should_explode = True
            if should_explode:
                return {"type": "explode"}

        if adj:
            focus = _Team.choose_focus_enemy(obs)
            target = next((e for e in adj if focus and e["id"] == focus["id"]), adj[0])
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], target["x"], target["y"])
            return {"type": "attack", "direction": direction or "north"}

        if enemies:
            # seek clusters
            best_cluster_pos = None
            max_cluster_value = 0
            for e in enemies:
                nearby_enemies = [other for other in enemies if _Helpers.chebyshev((e["x"], e["y"]), (other["x"], other["y"])) <= 2]
                if len(nearby_enemies) >= 2:
                    cluster_value = sum(_Team.ROLE_FOCUS_SCORE.get(enemy.get("component", ""), 1) for enemy in nearby_enemies)
                    if cluster_value > max_cluster_value:
                        max_cluster_value = cluster_value
                        best_cluster_pos = (e["x"], e["y"])
            if best_cluster_pos:
                direction = self._best_direction_towards(me, best_cluster_pos[0], best_cluster_pos[1], obs)
                return {"type": "move", "direction": direction}
            focus = _Team.choose_focus_enemy(obs)
            if focus:
                direction = self._best_direction_towards(me, focus["x"], focus["y"], obs)
                return {"type": "move", "direction": direction}

        return self._move_towards_focus(obs)


class Scout(_Base):
    """
    🏃 Mobile skirmisher with hit-and-run tactics.
    Dash moves 2 tiles straight if both tiles empty. Cooldown 2.
    Strategy: Use dash for engage/disengage, kite slower enemies, pick off isolated targets.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        adj = self._adjacent_enemies(me, enemies)
        if adj:
            threat = adj[0]
            if cooldown == 0:
                escape_dir = _Helpers.dir_cardinal(threat["x"], threat["y"], me["x"], me["y"])
                safe_dash = self._choose_best_dash(me, escape_dir, obs)
                return {"type": "dash", "direction": safe_dash}
            else:
                direction = _Helpers.dir_to_adjacent(me["x"], me["y"], threat["x"], threat["y"])
                return {"type": "attack", "direction": direction or "north"}

        if cooldown == 0 and enemies:
            isolated_targets = []
            for e in enemies:
                nearby_allies = sum(1 for other in enemies if other != e and _Helpers.chebyshev((e["x"], e["y"]), (other["x"], other["y"])) <= 2)
                if nearby_allies == 0:
                    isolated_targets.append(e)
            if isolated_targets:
                def isolation_priority(target):
                    role_score = _Team.ROLE_FOCUS_SCORE.get(target.get("component", ""), 0)
                    distance = _Helpers.chebyshev((me["x"], me["y"]), (target["x"], target["y"]))
                    return (-role_score, distance)

                best_target = min(isolated_targets, key=isolation_priority)
                dash_dir = _Helpers.dir_from_to(me["x"], me["y"], best_target["x"], best_target["y"])
                safe_dash = self._choose_best_dash(me, dash_dir, obs)
                return {"type": "dash", "direction": safe_dash}

            focus = _Team.choose_focus_enemy(obs)
            if focus:
                dash_dir = _Helpers.dir_from_to(me["x"], me["y"], focus["x"], focus["y"])
                safe_dash = self._choose_best_dash(me, dash_dir, obs)
                return {"type": "dash", "direction": safe_dash}

        focus = _Team.choose_focus_enemy(obs)
        if focus:
            team_center = _Team.team_centroid(obs)
            team_to_focus_x = focus["x"] - team_center[0]
            team_to_focus_y = focus["y"] - team_center[1]
            flank_options = [
                (focus["x"] + team_to_focus_y, focus["y"] - team_to_focus_x),
                (focus["x"] - team_to_focus_y, focus["y"] + team_to_focus_x),
            ]
            w, h = obs.get("map_size", [10, 10])
            valid_flanks = [(x, y) for x, y in flank_options if 0 <= x < w and 0 <= y < h]
            if valid_flanks:
                best_flank = min(valid_flanks, key=lambda pos: _Helpers.manhattan((me["x"], me["y"]), pos))
                direction = self._best_direction_towards(me, best_flank[0], best_flank[1], obs)
                return {"type": "move", "direction": direction}

        return self._move_towards_focus(obs)


class Teleporter(_Base):
    """
    ✨ Defensive repositioning specialist.
    Blink teleports to random empty tile. Cooldown 4.
    Strategy: Blink when overwhelmed, re-enter fights from unexpected angles.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        hp = me.get("health", 100)

        adj = self._adjacent_enemies(me, enemies)
        nearby_enemies = _Helpers.count_enemies_in_range((me["x"], me["y"]), enemies, 2)
        panic_conditions = len(adj) >= 2 or hp <= 35 or nearby_enemies >= 4
        if cooldown == 0 and panic_conditions:
            return {"type": "blink"}

        if adj and len(adj) == 1:
            target = adj[0]
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], target["x"], target["y"])
            return {"type": "attack", "direction": direction or "north"}

        if enemies and hp > 50:
            high_value_targets = [e for e in enemies if _Team.ROLE_FOCUS_SCORE.get(e.get("component", ""), 0) >= 6]
            if high_value_targets:
                best_target = max(high_value_targets, key=lambda e: _Team.ROLE_FOCUS_SCORE.get(e.get("component", ""), 0))
                direction = self._best_direction_towards(me, best_target["x"], best_target["y"], obs)
                return {"type": "move", "direction": direction}

        return self._move_towards_focus(obs)


class Healer(_Base):
    """
    🧬 Sustain enabler allowing prolonged fights.
    Heals 30 HP within 2-tile range (no LoS required). Cooldown 3.
    Strategy: Heal frontliners, prioritize keeping tanks/bruisers alive.
    """

    def decide(self, obs):
        me = obs["self"]
        allies = obs.get("visible_allies", [])
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        if cooldown == 0 and allies:
            def heal_priority(ally):
                ally_pos = (ally["x"], ally["y"])
                min_enemy_dist = min((_Helpers.chebyshev(ally_pos, (e["x"], e["y"])) for e in enemies), default=10)
                role_priority = {
                    "tank": 10,
                    "bruiser": 9,
                    "bomber": 7,
                    "scout": 6,
                    "sniper": 5,
                    "teleporter": 4,
                    "poisoner": 4,
                    "shield_giver": 3,
                    "healer": 2,
                    "silencer": 2,
                    "puller": 4,
                    "jammer": 3,
                    "reflector": 2,
                    "trap_setter": 3,
                    "wall_builder": 2,
                    "pusher": 2,
                    "decoy_caster": 1,
                    "leaper": 4,
                }.get(ally.get("component", ""), 1)
                heal_distance = _Helpers.chebyshev((me["x"], me["y"]), ally_pos)
                return (min_enemy_dist, -role_priority, heal_distance)

            valid_targets = [a for a in allies if _Helpers.chebyshev((me["x"], me["y"]), (a["x"], a["y"])) <= 2]
            if valid_targets:
                best_target = min(valid_targets, key=heal_priority)
                return {"type": "heal", "target": {"x": best_target["x"], "y": best_target["y"]}}

        if allies and enemies:
            enemy_center = _Team.enemy_cluster_center(obs)
            if enemy_center:
                frontline_allies = sorted(allies, key=lambda a: _Helpers.manhattan((a["x"], a["y"]), (int(enemy_center[0]), int(enemy_center[1]))))[:2]
                if frontline_allies:
                    target_ally = frontline_allies[0]
                    safe_distance = 4
                    too_close_to_enemies = any(_Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) < safe_distance for e in enemies)
                    if too_close_to_enemies:
                        retreat_dirs = []
                        for direction in ["north", "south", "east", "west"]:
                            dx, dy = _Helpers.get_direction_coords(direction)
                            new_pos = (me["x"] + dx, me["y"] + dy)
                            enemy_distances = [_Helpers.chebyshev(new_pos, (e["x"], e["y"])) for e in enemies]
                            min_enemy_dist = min(enemy_distances) if enemy_distances else 10
                            heal_dist = _Helpers.chebyshev(new_pos, (target_ally["x"], target_ally["y"]))
                            if min_enemy_dist > safe_distance - 1 and heal_dist <= 2:
                                retreat_dirs.append((direction, min_enemy_dist))
                        if retreat_dirs:
                            best_retreat = max(retreat_dirs, key=lambda x: x[1])[0]
                            return {"type": "move", "direction": best_retreat}
                    direction = self._best_direction_towards(me, target_ally["x"], target_ally["y"], obs)
                    return {"type": "move", "direction": direction}

        return self._move_towards_focus(obs)


class Shield_Giver(_Base):
    """
    🧲 Proactive protection specialist.
    Projects shields within 3-tile range before engagements. Cooldown 2.
    Strategy: Enable risky plays, protect glass cannons and frontliners.
    """

    def decide(self, obs):
        me = obs["self"]
        allies = obs.get("visible_allies", [])
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        if cooldown == 0 and allies:
            def shield_priority(ally):
                ally_pos = (ally["x"], ally["y"]) 
                min_enemy_dist = min((_Helpers.chebyshev(ally_pos, (e["x"], e["y"])) for e in enemies), default=10)
                role_priority = {
                    "sniper": 10,
                    "bomber": 9,
                    "tank": 8,
                    "bruiser": 7,
                    "healer": 6,
                    "scout": 5,
                    "teleporter": 4,
                    "poisoner": 4,
                }.get(ally.get("component", ""), 1)
                shield_distance = _Helpers.chebyshev((me["x"], me["y"]), ally_pos)
                return (min_enemy_dist, -role_priority, shield_distance)

            valid_targets = [a for a in allies if _Helpers.chebyshev((me["x"], me["y"]), (a["x"], a["y"])) <= 3]
            if valid_targets:
                best_target = min(valid_targets, key=shield_priority)
                return {"type": "project_shield", "target": {"x": best_target["x"], "y": best_target["y"]}}

        if allies and enemies:
            enemy_center = _Team.enemy_cluster_center(obs)
            if enemy_center:
                direction = self._best_direction_towards(me, int(enemy_center[0]), int(enemy_center[1]), obs)
                return {"type": "move", "direction": direction}

        return self._move_towards_focus(obs)


class Silencer(_Base):
    """
    🔇 Power disruption specialist.
    Silences enemy powers for 2 turns within 3-tile LoS. Cooldown 3.
    Strategy: Silence before key moments, target defensive powers and supports.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        if cooldown == 0 and enemies:
            silence_priorities = {
                "healer": 10,
                "shield_giver": 9,
                "tank": 8,
                "reflector": 7,
                "jammer": 6,
                "bomber": 6,
                "teleporter": 5,
                "sniper": 5,
                "trap_setter": 4,
                "wall_builder": 4,
            }
            visibles = {
                "enemies": enemies,
                "allies": obs.get("visible_allies", []),
                "walls": obs.get("visible_walls", []),
                "static_walls": obs.get("visible_static_walls", []),
                "decoys": obs.get("visible_decoys", []),
                "terrain": obs.get("visible_terrain", []),
            }
            valid_targets = []
            for e in enemies:
                distance = _Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"]))
                if distance <= 3 and self._has_los_guess(me, e, visibles):
                    priority = silence_priorities.get(e.get("component", ""), 0)
                    if priority > 0:
                        valid_targets.append((e, priority, distance))
            if valid_targets:
                best_target = max(valid_targets, key=lambda x: (x[1], -x[2]))[0]
                return {"type": "silence", "target": {"x": best_target["x"], "y": best_target["y"]}}

        adj = self._adjacent_enemies(me, enemies)
        if adj:
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": direction or "north"}

        return self._move_towards_focus(obs)


class Poisoner(_Base):
    """
    💉 DoT specialist requiring close positioning.
    Infects targets at range 1, applies 10 DoT for 3 turns (stacks). Cooldown 3.
    Strategy: Tag high-priority targets, stack poison on tanks, synergize with control.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        if cooldown == 0 and enemies:
            adjacent_enemies = [e for e in enemies if _Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) <= 1]
            if adjacent_enemies:
                def poison_priority(enemy):
                    role_priority = _Team.ROLE_FOCUS_SCORE.get(enemy.get("component", ""), 0)
                    if enemy.get("component") == "tank":
                        role_priority += 3
                    return role_priority

                best_target = max(adjacent_enemies, key=poison_priority)
                return {"type": "infect", "target": {"x": best_target["x"], "y": best_target["y"]}}

        adj = self._adjacent_enemies(me, enemies)
        if adj:
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": direction or "north"}

        focus = _Team.choose_focus_enemy(obs)
        if focus:
            distance = _Helpers.chebyshev((me["x"], me["y"]), (focus["x"], focus["y"]))
            if distance > 1:
                direction = self._best_direction_towards(me, focus["x"], focus["y"], obs)
                return {"type": "move", "direction": direction}

        return self._move_towards_focus(obs)


class Jammer(_Base):
    """
    🛰 Disruption specialist affecting 3-tile radius.
    Scrambles enemies for 2 turns (50% miss chance on powers). Cooldown 3.
    Strategy: Scramble before enemy power spikes, reduce reliability.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        if cooldown == 0 and enemies:
            affected_enemies = [e for e in enemies if _Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) <= 3]
            should_scramble = len(affected_enemies) >= 2
            if not should_scramble and affected_enemies:
                high_value = any(_Team.ROLE_FOCUS_SCORE.get(e.get("component", ""), 0) >= 7 for e in affected_enemies)
                should_scramble = high_value
            if should_scramble:
                return {"type": "scramble"}

        adj = self._adjacent_enemies(me, enemies)
        if adj:
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": direction or "north"}

        return self._move_towards_focus(obs)


class Reflector(_Base):
    """
    🪞 Defensive specialist punishing predictable attacks.
    Mirrors next attack back to attacker if in LoS. Cooldown 4.
    Strategy: Mirror before likely snipes/attacks, reflect damage back.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        if cooldown == 0 and enemies:
            high_threats = [e for e in enemies if e.get("component") in ("sniper", "bomber")]
            close_enemies = [e for e in enemies if _Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) <= 4]
            should_mirror = len(high_threats) > 0 or len(close_enemies) >= 2
            if should_mirror:
                return {"type": "mirror"}

        adj = self._adjacent_enemies(me, enemies)
        if adj:
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": direction or "north"}

        return self._move_towards_focus(obs)


class Trap_Setter(_Base):
    """
    🪤 Area control specialist.
    Drops trap on current tile (20 AoE on trigger, lasts 5 turns, max 3). Cooldown 3.
    Strategy: Plant on key paths, objectives, chokepoints.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        if cooldown == 0:
            nearby_enemies = [e for e in enemies if _Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) <= 3]
            should_trap = len(nearby_enemies) >= 1
            if should_trap:
                return {"type": "drop_trap"}

        adj = self._adjacent_enemies(me, enemies)
        if adj:
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": direction or "north"}

        return self._move_towards_focus(obs)


class Wall_Builder(_Base):
    """
    🧱 Terrain control specialist.
    Places adjacent walls (lasts 5 turns, max 3). Cooldown 3.
    Strategy: Build cover, funnel enemies, protect key positions.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        if cooldown == 0 and enemies:
            focus = _Team.choose_focus_enemy(obs)
            if focus:
                direction = _Helpers.dir_cardinal(me["x"], me["y"], focus["x"], focus["y"])
                dx, dy = _Helpers.get_direction_coords(direction)
                wall_x, wall_y = me["x"] + dx, me["y"] + dy
                w, h = obs.get("map_size", [10, 10])
                if 0 <= wall_x < w and 0 <= wall_y < h:
                    return {"type": "drop_wall", "target": {"x": wall_x, "y": wall_y}}

        adj = self._adjacent_enemies(me, enemies)
        if adj:
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": direction or "north"}

        return self._move_towards_focus(obs)


class Pusher(_Base):
    """
    🌀→ Defensive positioning specialist.
    Pushes adjacent enemies 1 tile away. Cooldown 2.
    Strategy: Push threats away from backline, disrupt formations.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        adj = self._adjacent_enemies(me, enemies)
        if cooldown == 0 and adj:
            target = adj[0]
            return {"type": "shove", "target": {"x": target["x"], "y": target["y"]}}

        if adj:
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": direction or "north"}

        return self._move_towards_focus(obs)


class Decoy_Caster(_Base):
    """
    🐾 LoS denial specialist.
    Spawns 1 HP decoy adjacent (blocks LoS/movement, lasts 3 turns). Cooldown 4.
    Strategy: Block important sightlines, buy time, disrupt targeting.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        if cooldown == 0 and enemies:
            focus = _Team.choose_focus_enemy(obs)
            if focus:
                direction = _Helpers.dir_cardinal(me["x"], me["y"], focus["x"], focus["y"])
                dx, dy = _Helpers.get_direction_coords(direction)
                decoy_x, decoy_y = me["x"] + dx, me["y"] + dy
                return {"type": "clone", "target": {"x": decoy_x, "y": decoy_y}}

        adj = self._adjacent_enemies(me, enemies)
        if adj:
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": direction or "north"}

        return self._move_towards_focus(obs)


class Puller(_Base):
    """
    🌀 Formation disruptor.
    Pulls target 1 tile closer (LoS ≤3). Cooldown 3.
    Strategy: Pull priority targets into team or traps.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        if cooldown == 0 and enemies:
            focus = _Team.choose_focus_enemy(obs)
            if focus:
                distance = _Helpers.chebyshev((me["x"], me["y"]), (focus["x"], focus["y"]))
                if distance <= 3:
                    return {"type": "yank", "target": {"x": focus["x"], "y": focus["y"]}}

        adj = self._adjacent_enemies(me, enemies)
        if adj:
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": direction or "north"}

        return self._move_towards_focus(obs)


class Leaper(_Base):
    """
    🐸 Flanking specialist with diagonal mobility.
    Leaps diagonally 2 tiles over blocker. Cooldown 2.
    Strategy: Bypass frontlines, reach enemy backline.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        if cooldown == 0 and enemies:
            focus = _Team.choose_focus_enemy(obs)
            if focus:
                candidates = [
                    (me["x"] + 2, me["y"] + 2),
                    (me["x"] + 2, me["y"] - 2),
                    (me["x"] - 2, me["y"] + 2),
                    (me["x"] - 2, me["y"] - 2),
                ]
                w, h = obs.get("map_size", [10, 10])
                walls, decoys, _, terrain_at, _, _, _ = self._collect_vis(obs)
                valid_leaps = []
                for (lx, ly) in candidates:
                    if 0 <= lx < w and 0 <= ly < h:
                        if (lx, ly) not in walls and (lx, ly) not in decoys:
                            if terrain_at.get((lx, ly)) != "water":
                                distance_to_focus = _Helpers.manhattan((lx, ly), (focus["x"], focus["y"]))
                                valid_leaps.append(((lx, ly), distance_to_focus))
                if valid_leaps:
                    best_leap = min(valid_leaps, key=lambda x: x[1])[0]
                    return {"type": "leap", "target": {"x": best_leap[0], "y": best_leap[1]}}

        adj = self._adjacent_enemies(me, enemies)
        if adj:
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": direction or "north"}

        return self._move_towards_focus(obs)


class Bruiser(_Base):
    """
    🦾 Enhanced frontline fighter.
    30 base attack damage (vs normal 20). No power - passive advantage.
    Strategy: Trade favorably, pressure enemy frontline.
    """

    def decide(self, obs):
        me = obs["self"]
        enemies = obs.get("visible_enemies", [])

        adj = self._adjacent_enemies(me, enemies)
        if adj:
            focus = _Team.choose_focus_enemy(obs)
            target = next((e for e in adj if focus and e["id"] == focus["id"]), adj[0])
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], target["x"], target["y"])
            return {"type": "attack", "direction": direction or "north"}

        return self._move_towards_focus(obs)


