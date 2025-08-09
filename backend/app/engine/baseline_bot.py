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
        "healer": 10,        # Sustain enabler - must be eliminated first
        "shield_giver": 9,   # Proactive protection - high value target
        "silencer": 9,       # Power disruption specialist
        "sniper": 8,         # Elite damage dealer with 40 damage
        
        # High priority: disruptors and area control
        "puller": 8,         # Formation disruptor
        "jammer": 7,         # Disruption specialist
        "bomber": 7,         # High-risk, high-reward AoE
        "poisoner": 6,       # DoT specialist, dangerous over time
        
        # Medium priority: utility and control
        "trap_setter": 5,    # Area control specialist
        "wall_builder": 5,   # Terrain control
        "reflector": 5,      # Defensive specialist
        "pusher": 4,         # Defensive positioning
        "decoy_caster": 4,   # LoS denial
        
        # Lower priority: frontline and mobility (harder to kill, less immediate threat)
        "bruiser": 4,        # Enhanced frontline fighter (30 damage but tanky)
        "tank": 3,           # Damage sponge (hardest to kill)
        "leaper": 3,         # Flanking specialist
        "scout": 3,          # Mobile skirmisher
        "teleporter": 2,     # Defensive repositioning (slippery target)
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
            # discourage if water or heavy swamp (already penalized later), just store
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
        # sweep along row: toward map edge then back
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

        # Snipe priority: focus target > high-value targets > closest valid target
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
                    return (-role_score, distance)  # Higher role score and closer is better
                
                best_target = min(valid_targets, key=snipe_priority)
                return {"type": "snipe", "target": {"x": best_target["x"], "y": best_target["y"]}}

        # Positioning: align on row/column with priority targets while maintaining distance
        focus = _Team.choose_focus_enemy(obs)
        if focus:
            # Try to align on same row/column as focus while staying back
            focus_x, focus_y = focus["x"], focus["y"]
            
            # Prefer staying 3+ tiles away from all enemies for safety
            min_safe_distance = 3
            too_close = any(_Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) < min_safe_distance for e in enemies)
            
            if too_close:
                # Retreat while trying to maintain alignment
                direction = self._best_direction_towards(me, focus_x, focus_y, obs)
                return {"type": "move", "direction": direction}
            else:
                # Move to better alignment position
                direction = self._best_direction_towards(me, focus_x, focus_y, obs)
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
        
        # Enhanced shield timing: 2+ enemies adjacent OR low HP OR multiple enemies in range 2
        nearby_enemies = _Helpers.count_enemies_in_range((me["x"], me["y"]), enemies, 2)
        critical_danger = len(adj) >= 2 or hp <= 60 or nearby_enemies >= 3
        
        # Shield timing: reactive when committed to fight or critically threatened
        if cooldown == 0 and critical_danger:
            return {"type": "shield"}

        # Attack adjacent enemies, prioritizing focus target
        if adj:
            focus = _Team.choose_focus_enemy(obs)
            target = next((e for e in adj if focus and e["id"] == focus["id"]), adj[0])
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], target["x"], target["y"])
            return {"type": "attack", "direction": direction or "north"}

        # Aggressive frontline positioning - lead engagements toward focus
        focus = _Team.choose_focus_enemy(obs)
        if focus:
            # Stay within support range of healers/shield givers while advancing
            healer_range = any(
                a.get("component") == "healer" and _Helpers.chebyshev((me["x"], me["y"]), (a["x"], a["y"])) <= 3 
                for a in allies
            )
            
            # More aggressive if healer support available
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
        allies = obs.get("visible_allies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)
        hp = me.get("health", 100)

        adj = self._adjacent_enemies(me, enemies)
        
        if cooldown == 0:
            # Count potential explosion targets in 3x3 area centered on self
            explosion_targets = []
            for e in enemies:
                if _Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) <= 1:
                    explosion_targets.append(e)
            
            # Explode if: 2+ enemies in blast radius OR trading when low HP
            should_explode = len(explosion_targets) >= 2 or (hp <= 30 and len(explosion_targets) >= 1)
            
            # Also explode if focusing high-value single target and low HP
            if not should_explode and hp <= 40 and explosion_targets:
                focus = _Team.choose_focus_enemy(obs)
                if focus and focus in explosion_targets:
                    priority = _Team.ROLE_FOCUS_SCORE.get(focus.get("component", ""), 0)
                    if priority >= 7:  # High-value target worth trading for
                        should_explode = True
            
            if should_explode:
                return {"type": "explode"}

        # Attack adjacent enemies when not exploding
        if adj:
            focus = _Team.choose_focus_enemy(obs)
            target = next((e for e in adj if focus and e["id"] == focus["id"]), adj[0])
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], target["x"], target["y"])
            return {"type": "attack", "direction": direction or "north"}

        # Aggressive positioning: look for opportunities to get close to enemy groups
        if enemies:
            # Find enemy clusters (2+ enemies within range 2 of each other)
            best_cluster_pos = None
            max_cluster_value = 0
            
            for e in enemies:
                nearby_enemies = [
                    other for other in enemies 
                    if _Helpers.chebyshev((e["x"], e["y"]), (other["x"], other["y"])) <= 2
                ]
                if len(nearby_enemies) >= 2:
                    cluster_value = sum(_Team.ROLE_FOCUS_SCORE.get(enemy.get("component", ""), 1) for enemy in nearby_enemies)
                    if cluster_value > max_cluster_value:
                        max_cluster_value = cluster_value
                        best_cluster_pos = (e["x"], e["y"])
            
            # Move toward best cluster or focus target
            if best_cluster_pos:
                direction = self._best_direction_towards(me, best_cluster_pos[0], best_cluster_pos[1], obs)
                return {"type": "move", "direction": direction}
            
            # Fallback: move toward focus target
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
        allies = obs.get("visible_allies", [])
        cooldown = me.get("cooldowns", {}).get("power", 0)

        adj = self._adjacent_enemies(me, enemies)
        
        # Hit-and-run: escape when adjacent to enemies
        if adj:
            threat = adj[0]
            if cooldown == 0:
                # Dash away to kite - prioritize staying mobile
                escape_dir = _Helpers.dir_cardinal(threat["x"], threat["y"], me["x"], me["y"])
                safe_dash = self._choose_best_dash(me, escape_dir, obs)
                return {"type": "dash", "direction": safe_dash}
            else:
                # Attack if can't dash away
                direction = _Helpers.dir_to_adjacent(me["x"], me["y"], threat["x"], threat["y"])
                return {"type": "attack", "direction": direction or "north"}

        # Aggressive engagement: dash toward isolated or high-value targets
        if cooldown == 0 and enemies:
            # Look for isolated targets (no allies within 2 tiles)
            isolated_targets = []
            for e in enemies:
                nearby_allies = sum(1 for other in enemies if other != e and _Helpers.chebyshev((e["x"], e["y"]), (other["x"], other["y"])) <= 2)
                if nearby_allies == 0:
                    isolated_targets.append(e)
            
            # Prioritize isolated high-value targets
            if isolated_targets:
                def isolation_priority(target):
                    role_score = _Team.ROLE_FOCUS_SCORE.get(target.get("component", ""), 0)
                    distance = _Helpers.chebyshev((me["x"], me["y"]), (target["x"], target["y"]))
                    return (-role_score, distance)
                
                best_target = min(isolated_targets, key=isolation_priority)
                dash_dir = _Helpers.dir_from_to(me["x"], me["y"], best_target["x"], best_target["y"])
                safe_dash = self._choose_best_dash(me, dash_dir, obs)
                return {"type": "dash", "direction": safe_dash}
            
            # Fallback: dash toward focus target for team coordination
            focus = _Team.choose_focus_enemy(obs)
            if focus:
                dash_dir = _Helpers.dir_from_to(me["x"], me["y"], focus["x"], focus["y"])
                safe_dash = self._choose_best_dash(me, dash_dir, obs)
                return {"type": "dash", "direction": safe_dash}

        # Mobile positioning: stay on flanks, ready to engage
        focus = _Team.choose_focus_enemy(obs)
        if focus:
            # Move to flank position - try to approach from different angle than main team
            team_center = _Team.team_centroid(obs)
            
            # Calculate flanking position (perpendicular to team->focus vector)
            team_to_focus_x = focus["x"] - team_center[0]
            team_to_focus_y = focus["y"] - team_center[1]
            
            # Perpendicular flank positions
            flank_options = [
                (focus["x"] + team_to_focus_y, focus["y"] - team_to_focus_x),  # 90 degrees
                (focus["x"] - team_to_focus_y, focus["y"] + team_to_focus_x),  # -90 degrees
            ]
            
            # Choose closest valid flank position
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
        
        # Defensive blink triggers: multiple adjacent enemies OR low HP OR overwhelmed
        nearby_enemies = _Helpers.count_enemies_in_range((me["x"], me["y"]), enemies, 2)
        panic_conditions = len(adj) >= 2 or hp <= 35 or nearby_enemies >= 4
        
        if cooldown == 0 and panic_conditions:
            return {"type": "blink"}

        # Aggressive 1v1 combat when safe
        if adj and len(adj) == 1:
            target = adj[0]
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], target["x"], target["y"])
            return {"type": "attack", "direction": direction or "north"}

        # Aggressive positioning: threaten enemy backline when not in danger
        if enemies and hp > 50:
            # Look for enemy supports/backline units to threaten
            high_value_targets = [
                e for e in enemies 
                if _Team.ROLE_FOCUS_SCORE.get(e.get("component", ""), 0) >= 6
            ]
            
            if high_value_targets:
                # Move toward highest priority target we can threaten
                best_target = max(high_value_targets, key=lambda e: _Team.ROLE_FOCUS_SCORE.get(e.get("component", ""), 0))
                direction = self._best_direction_towards(me, best_target["x"], best_target["y"], obs)
                return {"type": "move", "direction": direction}

        return self._move_towards_focus(obs)


# ---------------- Additional advanced roles (for synergy) ----------------

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
        
        # Priority healing: frontline allies closest to danger
        if cooldown == 0 and allies:
            # Prioritize allies by: proximity to enemies > role importance > distance from healer
            def heal_priority(ally):
                ally_pos = (ally["x"], ally["y"])
                
                # Distance to closest enemy (lower is higher priority)
                min_enemy_dist = min(
                    (_Helpers.chebyshev(ally_pos, (e["x"], e["y"])) for e in enemies),
                    default=10
                )
                
                # Role priority for healing (tanks/bruisers get priority)
                role_priority = {
                    "tank": 10, "bruiser": 9, "bomber": 7, "scout": 6,
                    "sniper": 5, "teleporter": 4, "poisoner": 4,
                    "shield_giver": 3, "healer": 2, "silencer": 2,
                    "puller": 4, "jammer": 3, "reflector": 2,
                    "trap_setter": 3, "wall_builder": 2, "pusher": 2,
                    "decoy_caster": 1, "leaper": 4
                }.get(ally.get("component", ""), 1)
                
                # Distance from healer
                heal_distance = _Helpers.chebyshev((me["x"], me["y"]), ally_pos)
                
                # Lower values = higher priority
                return (min_enemy_dist, -role_priority, heal_distance)
            
            # Find allies within heal range (2 tiles)
            valid_targets = [a for a in allies if _Helpers.chebyshev((me["x"], me["y"]), (a["x"], a["y"])) <= 2]
            
            if valid_targets:
                best_target = min(valid_targets, key=heal_priority)
                return {"type": "heal", "target": {"x": best_target["x"], "y": best_target["y"]}}

        # Positioning: stay 2 tiles from frontline, prioritize safety
        if allies:
            # Find frontline allies (closest to enemies)
            if enemies:
                enemy_center = _Team.enemy_cluster_center(obs)
                if enemy_center:
                    frontline_allies = sorted(
                        allies, 
                        key=lambda a: _Helpers.manhattan((a["x"], a["y"]), (int(enemy_center[0]), int(enemy_center[1])))
                    )[:2]  # Top 2 frontline allies
                    
                    if frontline_allies:
                        # Position to be in range of frontline but safe from enemies
                        target_ally = frontline_allies[0]
                        
                        # Stay back from enemies while maintaining heal range
                        safe_distance = 4  # Stay 4+ tiles from enemies if possible
                        too_close_to_enemies = any(
                            _Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) < safe_distance 
                            for e in enemies
                        )
                        
                        if too_close_to_enemies:
                            # Retreat while maintaining heal range
                            retreat_dirs = []
                            for direction in ["north", "south", "east", "west"]:
                                dx, dy = _Helpers.get_direction_coords(direction)
                                new_pos = (me["x"] + dx, me["y"] + dy)
                                
                                # Check if position is safer and maintains heal range
                                enemy_distances = [_Helpers.chebyshev(new_pos, (e["x"], e["y"])) for e in enemies]
                                min_enemy_dist = min(enemy_distances) if enemy_distances else 10
                                heal_dist = _Helpers.chebyshev(new_pos, (target_ally["x"], target_ally["y"]))
                                
                                if min_enemy_dist > safe_distance - 1 and heal_dist <= 2:
                                    retreat_dirs.append((direction, min_enemy_dist))
                            
                            if retreat_dirs:
                                best_retreat = max(retreat_dirs, key=lambda x: x[1])[0]
                                return {"type": "move", "direction": best_retreat}
                        
                        # Move to maintain heal range of frontline
                        direction = self._best_direction_towards(me, target_ally["x"], target_ally["y"], obs)
                        return {"type": "move", "direction": direction}

        # Fallback: stay with team
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
            # Priority: allies about to engage or under threat
            def shield_priority(ally):
                ally_pos = (ally["x"], ally["y"])
                
                # Distance to closest enemy (lower = higher priority)
                min_enemy_dist = min(
                    (_Helpers.chebyshev(ally_pos, (e["x"], e["y"])) for e in enemies),
                    default=10
                )
                
                # Role priority (high-value targets and frontliners)
                role_priority = {
                    "sniper": 10,      # Glass cannon protection
                    "bomber": 9,       # Enable risky dives
                    "tank": 8,         # Double defense
                    "bruiser": 7,      # Sustain trades
                    "healer": 6,       # Protect force multiplier
                    "scout": 5,        # Enable aggressive plays
                    "teleporter": 4,   # Secondary protection
                    "poisoner": 4,     # Close-range protection
                }.get(ally.get("component", ""), 1)
                
                # Distance from shield giver
                shield_distance = _Helpers.chebyshev((me["x"], me["y"]), ally_pos)
                
                # Lower = higher priority
                return (min_enemy_dist, -role_priority, shield_distance)
            
            # Find allies within shield range (3 tiles)
            valid_targets = [a for a in allies if _Helpers.chebyshev((me["x"], me["y"]), (a["x"], a["y"])) <= 3]
            
            if valid_targets:
                best_target = min(valid_targets, key=shield_priority)
                return {"type": "project_shield", "target": {"x": best_target["x"], "y": best_target["y"]}}

        # Position in safe backline with 3-tile range to key allies
        if allies and enemies:
            enemy_center = _Team.enemy_cluster_center(obs)
            if enemy_center:
                # Stay back from enemies while maintaining shield range
                safe_distance = 5
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
            # Priority targets for silencing (defensive powers and supports)
            silence_priorities = {
                "healer": 10,       # Shut down sustain
                "shield_giver": 9,  # Prevent protection  
                "tank": 8,          # Block shield timing
                "reflector": 7,     # Disable reflects
                "jammer": 6,        # Counter-disruption
                "bomber": 6,        # Prevent explosion timing
                "teleporter": 5,    # Block escape
                "sniper": 5,        # Stop high damage
                "trap_setter": 4,   # Prevent trap placement
                "wall_builder": 4,  # Block terrain control
            }
            
            # Find valid silence targets (LoS required, range 3)
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
                    if priority > 0:  # Only silence high-value targets
                        valid_targets.append((e, priority, distance))
            
            if valid_targets:
                # Sort by priority then distance
                best_target = max(valid_targets, key=lambda x: (x[1], -x[2]))[0]
                return {"type": "silence", "target": {"x": best_target["x"], "y": best_target["y"]}}

        # Combat when not silencing
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
            # Find enemies in range 1 (adjacent + LoS)
            adjacent_enemies = [e for e in enemies if _Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) <= 1]
            
            if adjacent_enemies:
                # Priority: high-value targets > tanks (to stack DoT)
                def poison_priority(enemy):
                    role_priority = _Team.ROLE_FOCUS_SCORE.get(enemy.get("component", ""), 0)
                    
                    # Special bonus for tanks - poison stacking is effective
                    if enemy.get("component") == "tank":
                        role_priority += 3
                    
                    return role_priority
                
                best_target = max(adjacent_enemies, key=poison_priority)
                return {"type": "infect", "target": {"x": best_target["x"], "y": best_target["y"]}}

        # Aggressive positioning behind frontline
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], adj[0]["x"], adj[0]["y"])
            return {"type": "attack", "direction": direction or "north"}
        
        # Move to get in range-1 of priority targets
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
            # Count enemies in scramble radius (3 tiles)
            affected_enemies = [e for e in enemies if _Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) <= 3]
            
            # Scramble if affecting 2+ enemies or high-value single target
            should_scramble = len(affected_enemies) >= 2
            
            if not should_scramble and affected_enemies:
                high_value = any(
                    _Team.ROLE_FOCUS_SCORE.get(e.get("component", ""), 0) >= 7
                    for e in affected_enemies
                )
                should_scramble = high_value
            
            if should_scramble:
                return {"type": "scramble"}

        # Combat positioning
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
            # Mirror when facing high-threat enemies or about to be focused
            high_threats = [e for e in enemies if e.get("component") in ("sniper", "bomber")]
            close_enemies = [e for e in enemies if _Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) <= 4]
            
            should_mirror = len(high_threats) > 0 or len(close_enemies) >= 2
            
            if should_mirror:
                return {"type": "mirror"}

        # Combat
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
            # Plant trap if enemies likely to traverse this area
            nearby_enemies = [e for e in enemies if _Helpers.chebyshev((me["x"], me["y"]), (e["x"], e["y"])) <= 3]
            
            # Also consider if we're on a likely path (near objectives or chokepoints)
            should_trap = len(nearby_enemies) >= 1
            
            if should_trap:
                return {"type": "drop_trap"}

        # Combat
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
                # Build wall toward enemy to create cover or block advances
                direction = _Helpers.dir_cardinal(me["x"], me["y"], focus["x"], focus["y"])
                dx, dy = _Helpers.get_direction_coords(direction)
                wall_x, wall_y = me["x"] + dx, me["y"] + dy
                
                w, h = obs.get("map_size", [10, 10])
                if 0 <= wall_x < w and 0 <= wall_y < h:
                    return {"type": "drop_wall", "target": {"x": wall_x, "y": wall_y}}

        # Combat
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
            # Push first adjacent enemy to scatter formations
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
                # Place decoy between us and focus to block LoS
                direction = _Helpers.dir_cardinal(me["x"], me["y"], focus["x"], focus["y"])
                dx, dy = _Helpers.get_direction_coords(direction)
                decoy_x, decoy_y = me["x"] + dx, me["y"] + dy
                return {"type": "clone", "target": {"x": decoy_x, "y": decoy_y}}

        # Combat
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

        # Combat
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
                # Find diagonal leap positions that get closer to focus
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
                        # Check landing isn't blocked
                        if (lx, ly) not in walls and (lx, ly) not in decoys:
                            if terrain_at.get((lx, ly)) != "water":
                                distance_to_focus = _Helpers.manhattan((lx, ly), (focus["x"], focus["y"]))
                                valid_leaps.append(((lx, ly), distance_to_focus))
                
                if valid_leaps:
                    best_leap = min(valid_leaps, key=lambda x: x[1])[0]
                    return {"type": "leap", "target": {"x": best_leap[0], "y": best_leap[1]}}

        # Combat
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
        
        # Aggressive combat with enhanced damage
        adj = self._adjacent_enemies(me, enemies)
        if adj:
            focus = _Team.choose_focus_enemy(obs)
            target = next((e for e in adj if focus and e["id"] == focus["id"]), adj[0])
            direction = _Helpers.dir_to_adjacent(me["x"], me["y"], target["x"], target["y"])
            return {"type": "attack", "direction": direction or "north"}
        
        return self._move_towards_focus(obs)


 