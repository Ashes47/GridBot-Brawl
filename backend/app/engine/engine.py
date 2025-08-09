from __future__ import annotations

import random
from typing import Dict, List, Tuple

from .constants import (
    BASE_ATTACK_DMG,
    BOMBER_AOE_DMG,
    BOMBER_SELF_DMG,
    COOLDOWNS,
    Direction,
    SNIPER_DMG,
    MAX_HP,
    Role,
)
from .models import (
    BlinkAction,
    Bot,
    DashAction,
    ExplodeAction,
    GameState,
    MoveAction,
    AttackAction,
    PendingEffects,
    Position,
    ShieldAction,
    SnipeAction,
    ProjectShieldAction,
    HealAction,
    InfectAction,
    SilenceAction,
    MirrorAction,
    DropTrapAction,
    DropWallAction,
    CloneAction,
    YankAction,
    ShoveAction,
    LeapAction,
    ScrambleAction,
    Wall,
    Trap,
    Decoy,
)


class TurnEngine:
    """Runs one simulation tick following the agreed order of phases."""

    def __init__(self, state: GameState, actions: Dict[str, object]):
        """
        :param state: Current game state (will be mutated in-place)
        :param actions: Mapping bot_id -> action instance (subclass of BotAction)
        """
        self.state = state
        self.actions = actions
        self.effects = PendingEffects()
        self.occupancy_bots = self.state.occupied_positions_bots()  # (x,y) -> Bot
        self.blockers = self.state.blockers()  # (x,y) -> kind
        # Track damage dealt this turn per team_id
        self.damage_done_by_team: Dict[str, int] = {}
        # Track damage dealt this turn per bot_id
        self.damage_done_by_bot: Dict[str, int] = {}
        # Event taps for simulation logging
        self.trap_triggers: List[Tuple[int, int]] = []
        # Zone events: list of (type, x, y, bot_id, extra)
        self.zone_events: List[Tuple[str, int, int, str, Dict[str, int]]] = []
        # Zone power-ups: list of (type, x, y, bot_id)
        self.zone_powerups: List[Tuple[str, int, int, str]] = []

    # -------------------------------------------------------
    # Main entry
    # -------------------------------------------------------
    def run(self) -> GameState:
        self._phase_powers()
        self._phase_movement()
        self._phase_attack()
        self._phase_end_of_turn()
        self.state.turn += 1
        return self.state

    # ------------------------ Helpers ----------------------
    def _inc_team_damage(self, team_id: str, amount: int):
        if amount <= 0:
            return
        self.damage_done_by_team[team_id] = self.damage_done_by_team.get(team_id, 0) + amount

    def _inc_bot_damage(self, bot_id: str, amount: int):
        if amount <= 0:
            return
        self.damage_done_by_bot[bot_id] = self.damage_done_by_bot.get(bot_id, 0) + amount

    def _forest_blocks(self, x: int, y: int) -> bool:
        return (x, y) in self.state.terrain and self.state.terrain[(x, y)] == "forest"

    def _clear_los(self, src: Position, dst: Position) -> bool:
        # Blocked by bots/decoys/walls and forest; water does not block
        if src.x == dst.x:
            step = 1 if dst.y > src.y else -1
            for y in range(src.y + step, dst.y, step):
                if (src.x, y) in self.blockers or self._forest_blocks(src.x, y):
                    return False
            return True
        if src.y == dst.y:
            step = 1 if dst.x > src.x else -1
            for x in range(src.x + step, dst.x, step):
                if (x, src.y) in self.blockers or self._forest_blocks(x, src.y):
                    return False
            return True
        # allow diagonal if both row and column are clear at each step (no corner cutting)
        dx = 1 if dst.x > src.x else -1
        dy = 1 if dst.y > src.y else -1
        if abs(dst.x - src.x) == abs(dst.y - src.y):
            cx, cy = src.x, src.y
            for _ in range(abs(dst.x - src.x) - 0):
                # step one
                nx, ny = cx + dx, cy + dy
                # check both orthogonal corners from previous cell
                if (cx + dx, cy) in self.blockers or (cx, cy + dy) in self.blockers or self._forest_blocks(cx + dx, cy) or self._forest_blocks(cx, cy + dy):
                    return False
                # also ensure intermediate diagonal cell is not blocked (we'll land on dst eventually)
                if ((nx, ny) in self.blockers or self._forest_blocks(nx, ny)) and (nx, ny) != (dst.x, dst.y):
                    return False
                cx, cy = nx, ny
            return True
        # other lines (non-straight) unsupported for LoS
        return False

    def _is_enemy(self, a: Bot, b: Bot | None) -> bool:
        return bool(b and b.team_id != a.team_id)

    # -------------------------------------------------------
    # Phase 1 – Powers (prep)
    # -------------------------------------------------------
    def _phase_powers(self):
        for bot in self.state.all_bots():
            action = self.actions.get(bot.id)
            if not action:
                continue
            # Handle only powers here; movement powers get queued for movement
            # Silence blocks power usage
            silenced = bot.silenced_remaining > 0

            # Tank shield
            if isinstance(action, ShieldAction):
                if bot.power_cooldown == 0 and not silenced:
                    bot.shield_pool_remaining = 40
                    bot.shield_expire_turn = self.state.turn + 1
                    bot.power_cooldown = COOLDOWNS[bot.role]
            # Snipe queued
            elif isinstance(action, SnipeAction):
                if bot.power_cooldown == 0 and not silenced:
                    # jammed miss chance 50%
                    if bot.jammed_remaining > 0 and random.random() < 0.5:
                        bot.power_cooldown = COOLDOWNS[bot.role]
                    else:
                        self.effects.snipe_events.append((bot, action.target))
                        bot.power_cooldown = COOLDOWNS[bot.role]
            elif isinstance(action, ExplodeAction):
                if bot.power_cooldown == 0 and not silenced:
                    self.effects.explode_bots.append(bot)
                    bot.power_cooldown = COOLDOWNS[bot.role]
            elif isinstance(action, ProjectShieldAction):
                if bot.power_cooldown == 0 and not silenced:
                    # find ally at target within range 3 (no LoS required)
                    target_bot = self.occupancy_bots.get((action.target.x, action.target.y))
                    if target_bot and target_bot.team_id == bot.team_id and bot.position.distance_chebyshev(action.target) <= 3:
                        target_bot.shield_pool_remaining = 40
                        target_bot.shield_expire_turn = self.state.turn + 1
                        bot.power_cooldown = COOLDOWNS[bot.role]
            elif isinstance(action, HealAction):
                if bot.power_cooldown == 0 and not silenced:
                    target_bot = self.occupancy_bots.get((action.target.x, action.target.y))
                    if target_bot and target_bot.team_id == bot.team_id and target_bot.id != bot.id:
                        if bot.position.distance_chebyshev(action.target) <= 2:
                            target_bot.hp = min(MAX_HP, target_bot.hp + 30)
                            bot.power_cooldown = COOLDOWNS[bot.role]
            elif isinstance(action, InfectAction):
                if bot.power_cooldown == 0 and not silenced:
                    target_bot = self.occupancy_bots.get((action.target.x, action.target.y))
                    if target_bot and self._is_enemy(bot, target_bot):
                        if bot.position.distance_chebyshev(action.target) <= 1 and self._clear_los(bot.position, action.target):
                            target_bot.poison_stacks.append(3)
                            bot.power_cooldown = COOLDOWNS[bot.role]
            elif isinstance(action, SilenceAction):
                if bot.power_cooldown == 0 and not silenced:
                    target_bot = self.occupancy_bots.get((action.target.x, action.target.y))
                    if target_bot and self._is_enemy(bot, target_bot):
                        if bot.position.distance_chebyshev(action.target) <= 3 and self._clear_los(bot.position, action.target):
                            target_bot.silenced_remaining = 2
                            bot.power_cooldown = COOLDOWNS[bot.role]
            elif isinstance(action, MirrorAction):
                if bot.power_cooldown == 0 and not silenced:
                    bot.reflect_ready = True
                    bot.power_cooldown = COOLDOWNS[bot.role]
            elif isinstance(action, DropTrapAction):
                if bot.power_cooldown == 0 and not silenced:
                    # limit 3 per team
                    active = [t for t in self.state.traps if t.team_id == bot.team_id]
                    pos = (bot.position.x, bot.position.y)
                    if len(active) < 3 and all((t.x, t.y) != pos for t in self.state.traps):
                        self.state.traps.append(Trap(x=bot.position.x, y=bot.position.y, team_id=bot.team_id, ttl=5))
                        bot.power_cooldown = COOLDOWNS[bot.role]
            elif isinstance(action, DropWallAction):
                if bot.power_cooldown == 0 and not silenced:
                    # adjacency (8) and empty; limit 3 per team
                    if bot.position.distance_chebyshev(action.target) <= 1:
                        target_xy = (action.target.x, action.target.y)
                        # not out of bounds
                        if action.target.in_bounds(self.state.grid_size):
                            # empty: no blockers and no wall/trap/decoy there
                            occupied = (target_xy in self.blockers) or any((w.x, w.y) == target_xy for w in self.state.walls) or any((d.x, d.y) == target_xy for d in self.state.decoys) or any((t.x, t.y) == target_xy for t in self.state.traps)
                            if not occupied:
                                active = [w for w in self.state.walls if w.team_id == bot.team_id]
                                if len(active) < 3:
                                    self.state.walls.append(Wall(x=action.target.x, y=action.target.y, team_id=bot.team_id, ttl=5))
                                    bot.power_cooldown = COOLDOWNS[bot.role]
            elif isinstance(action, CloneAction):
                if bot.power_cooldown == 0 and not silenced:
                    # limit 1 decoy per caster
                    has_decoy = any(d.owner_bot_id == bot.id for d in self.state.decoys)
                    if not has_decoy:
                        # pick target if provided, else skip
                        tgt = action.target
                        if tgt and bot.position.distance_chebyshev(tgt) <= 1 and tgt.in_bounds(self.state.grid_size):
                            target_xy = (tgt.x, tgt.y)
                            # must be empty of blockers and walls/decoys
                            if target_xy not in self.blockers and not any((w.x, w.y) == target_xy for w in self.state.walls) and not any((d.x, d.y) == target_xy for d in self.state.decoys):
                                self.state.decoys.append(Decoy(x=tgt.x, y=tgt.y, team_id=bot.team_id, owner_bot_id=bot.id, hp=1, ttl=3))
                                bot.power_cooldown = COOLDOWNS[bot.role]
            elif isinstance(action, ScrambleAction):
                if bot.power_cooldown == 0 and not silenced:
                    # radial effect, enemies within radius 3
                    for other in self.state.all_bots():
                        if self._is_enemy(bot, other):
                            if bot.position.distance_chebyshev(other.position) <= 3:
                                other.jammed_remaining = 2
                    bot.power_cooldown = COOLDOWNS[bot.role]
            # Yank/Push resolved in movement
        # refresh references
        self.occupancy_bots = self.state.occupied_positions_bots()
        self.blockers = self.state.blockers()

    # -------------------------------------------------------
    # Phase 2 – Movement
    # -------------------------------------------------------
    def _desired_position(self, bot: Bot) -> Position:
        action = self.actions.get(bot.id)
        silenced = bot.silenced_remaining > 0
        # Blink (power move)
        if isinstance(action, BlinkAction) and bot.power_cooldown == 0 and not silenced:
            empty_tiles = [
                Position(x=x, y=y)
                for x in range(self.state.grid_size)
                for y in range(self.state.grid_size)
                if (x, y) not in self.blockers
            ]
            if empty_tiles:
                bot.power_cooldown = COOLDOWNS[bot.role]
                return random.choice(empty_tiles)
            return bot.position  # no movement if full
        # Dash (power move): move 2 tiles straight if both empty; slowed prohibits
        if isinstance(action, DashAction) and bot.power_cooldown == 0 and not silenced:
            if bot.slowed_remaining > 0:
                return bot.position
            dx, dy = action.direction.delta
            first = (bot.position.x + dx, bot.position.y + dy)
            second = (first[0] + dx, first[1] + dy)
            size = self.state.grid_size
            if (
                0 <= first[0] < size
                and 0 <= first[1] < size
                and 0 <= second[0] < size
                and 0 <= second[1] < size
                and first not in self.blockers
                and second not in self.blockers
                and self.state.terrain.get(first) not in ("water", "swamp")
                and self.state.terrain.get(second) not in ("water", "swamp")
            ):
                bot.power_cooldown = COOLDOWNS[bot.role]
                return Position(x=second[0], y=second[1])
            return bot.position  # failed dash
        # Leap (power move): diagonally 2 tiles over 1 unit/wall; target provided
        if isinstance(action, LeapAction) and bot.power_cooldown == 0 and not silenced:
            if bot.slowed_remaining > 0:
                return bot.position
            tgt = action.target
            if tgt.in_bounds(self.state.grid_size):
                if abs(tgt.x - bot.position.x) == 2 and abs(tgt.y - bot.position.y) == 2:
                    mid = (bot.position.x + (1 if tgt.x > bot.position.x else -1), bot.position.y + (1 if tgt.y > bot.position.y else -1))
                    dest = (tgt.x, tgt.y)
                    # cannot traverse swamp/water; dest cannot be water/swamp; allow jumping over one blocker at mid
                    if (
                        dest not in self.blockers
                        and (mid in self.blockers)
                        and self.state.terrain.get(mid) not in ("swamp", "water")
                        and self.state.terrain.get(dest) not in ("swamp", "water")
                    ):
                        bot.power_cooldown = COOLDOWNS[bot.role]
                        return Position(x=tgt.x, y=tgt.y)
            return bot.position
        # Normal move (compute coords first to avoid invalid Position)
        if isinstance(action, MoveAction):
            # slowed prohibits movement powers and move
            if bot.slowed_remaining > 0:
                return bot.position
            dx, dy = action.direction.delta
            nx, ny = bot.position.x + dx, bot.position.y + dy
            if 0 <= nx < self.state.grid_size and 0 <= ny < self.state.grid_size:
                # water impassable
                if self.state.terrain.get((nx, ny)) == "water":
                    return bot.position
                if (nx, ny) not in self.blockers:
                    # handle ice sliding up to +2 after landing on ice (only for normal move)
                    if self.state.terrain.get((nx, ny)) == "ice":
                        fx, fy = nx, ny
                        steps = 0
                        while steps < 2:
                            tx, ty = fx + dx, fy + dy
                            if not (0 <= tx < self.state.grid_size and 0 <= ty < self.state.grid_size):
                                break
                            if (tx, ty) in self.blockers or self.state.terrain.get((tx, ty)) == "water":
                                break
                            fx, fy = tx, ty
                            steps += 1
                        return Position(x=fx, y=fy)
                    return Position(x=nx, y=ny)
        return bot.position

    def _phase_movement(self):
        desired: Dict[Tuple[int, int], List[Bot]] = {}
        new_positions: Dict[str, Position] = {}

        for bot in self.state.all_bots():
            pos = self._desired_position(bot)
            new_positions[bot.id] = pos
            desired.setdefault((pos.x, pos.y), []).append(bot)

        # resolve collisions for self-moves
        for bot in self.state.all_bots():
            pos_tuple = (new_positions[bot.id].x, new_positions[bot.id].y)
            if len(desired[pos_tuple]) == 1 and (
                pos_tuple not in self.occupancy_bots or self.occupancy_bots.get(pos_tuple) is bot
            ):
                bot.position = new_positions[bot.id]
            # else stays in place
        # update references
        self.occupancy_bots = self.state.occupied_positions_bots()
        self.blockers = self.state.blockers()

        # Resolve Pull (Yank) and Push (Shove) with miss chances
        for bot in self.state.all_bots():
            action = self.actions.get(bot.id)
            silenced = bot.silenced_remaining > 0
            if isinstance(action, YankAction) and bot.power_cooldown == 0 and not silenced:
                # jammed miss chance
                if bot.jammed_remaining > 0 and random.random() < 0.5:
                    bot.power_cooldown = COOLDOWNS[bot.role]
                    continue
                # target must be enemy in LoS within 3; destination is one step closer to bot
                tgt = self.occupancy_bots.get((action.target.x, action.target.y))
                if tgt and self._is_enemy(bot, tgt):
                    if bot.position.distance_chebyshev(action.target) <= 3 and self._clear_los(bot.position, action.target):
                        # compute destination one step toward bot
                        dx = 0 if tgt.position.x == bot.position.x else (-1 if tgt.position.x > bot.position.x else 1)
                        dy = 0 if tgt.position.y == bot.position.y else (-1 if tgt.position.y > bot.position.y else 1)
                        dest = (tgt.position.x - dx, tgt.position.y - dy)
                        if (
                            0 <= dest[0] < self.state.grid_size
                            and 0 <= dest[1] < self.state.grid_size
                            and dest not in self.blockers
                            and self.state.terrain.get(dest) != "water"
                        ):
                            tgt.position = Position(x=dest[0], y=dest[1])
                bot.power_cooldown = COOLDOWNS[bot.role]
            elif isinstance(action, ShoveAction) and bot.power_cooldown == 0 and not silenced:
                # jammed miss chance
                if bot.jammed_remaining > 0 and random.random() < 0.5:
                    bot.power_cooldown = COOLDOWNS[bot.role]
                    continue
                # target must be adjacent enemy; destination one step away in same vector
                tgt = self.occupancy_bots.get((action.target.x, action.target.y))
                if tgt and self._is_enemy(bot, tgt):
                    if bot.position.distance_chebyshev(action.target) <= 1:
                        dx = 0 if tgt.position.x == bot.position.x else (1 if tgt.position.x > bot.position.x else -1)
                        dy = 0 if tgt.position.y == bot.position.y else (1 if tgt.position.y > bot.position.y else -1)
                        dest = (tgt.position.x + dx, tgt.position.y + dy)
                        if (
                            0 <= dest[0] < self.state.grid_size
                            and 0 <= dest[1] < self.state.grid_size
                            and dest not in self.blockers
                            and self.state.terrain.get(dest) != "water"
                        ):
                            tgt.position = Position(x=dest[0], y=dest[1])
                bot.power_cooldown = COOLDOWNS[bot.role]
        # update references again after displacements
        self.occupancy_bots = self.state.occupied_positions_bots()
        self.blockers = self.state.blockers()

        # Trigger traps on entry (including teleports/leaps/pulls/pushes)
        to_remove: List[Trap] = []
        for trap in self.state.traps:
            for b in self.state.all_bots():
                if (b.position.x, b.position.y) == (trap.x, trap.y):
                    # trigger
                    self._trigger_trap(trap, b)
                    to_remove.append(trap)
                    break
        if to_remove:
            self.state.traps = [t for t in self.state.traps if t not in to_remove]

        # Note zone power-ups for bots currently standing on zones (preview before EoT effects)
        self.zone_powerups = []
        for b in self.state.all_bots():
            tile = (b.position.x, b.position.y)
            zs = self.state.zones.get(tile, [])
            for zt in zs:
                # record one per zone type on tile
                self.zone_powerups.append((zt, tile[0], tile[1], b.id))

    def _trigger_trap(self, trap: Trap, entrant: Bot):
        # 20 AoE to all adjacent 3x3 including allies; include entrant as well
        damage_queue: Dict[str, int] = {}
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                x = trap.x + dx
                y = trap.y + dy
                tgt = self.occupancy_bots.get((x, y))
                if tgt:
                    damage_queue[tgt.id] = damage_queue.get(tgt.id, 0) + 20
                    if tgt.team_id != trap.team_id:
                        self._inc_team_damage(trap.team_id, 20)
        for bot in self.state.all_bots():
            dmg = damage_queue.get(bot.id, 0)
            if dmg:
                bot.apply_damage(dmg)
        # collect trigger position for logs
        self.trap_triggers.append((trap.x, trap.y))

    # -------------------------------------------------------
    # Phase 3 – Attacks
    # -------------------------------------------------------
    def _phase_attack(self):
        damage_queue: Dict[str, int] = {}

        # base attacks
        for bot in self.state.all_bots():
            action = self.actions.get(bot.id)
            if isinstance(action, AttackAction):
                # jammed miss chance
                if bot.jammed_remaining > 0 and random.random() < 0.5:
                    continue
                target_pos = bot.position.moved(action.direction)
                tgt = self.occupancy_bots.get((target_pos.x, target_pos.y))
                if tgt and tgt.team_id != bot.team_id:
                    # reflection check
                    if tgt.reflect_ready and self._clear_los(tgt.position, bot.position):
                        # reflect base attack
                        damage_queue[bot.id] = damage_queue.get(bot.id, 0) + (30 if bot.role == Role.BRUISER else BASE_ATTACK_DMG)
                        tgt.reflect_ready = False
                    else:
                        dmg = 30 if bot.role == Role.BRUISER else BASE_ATTACK_DMG
                        damage_queue[tgt.id] = damage_queue.get(tgt.id, 0) + dmg
                        self._inc_team_damage(bot.team_id, dmg)
                        self._inc_bot_damage(bot.id, dmg)

        # snipe events
        for sniper, target in self.effects.snipe_events:
            if not sniper.is_alive():
                continue
            # range 5 (Chebyshev)
            if sniper.position.distance_chebyshev(target) > 5:
                continue
            # straight line only; LoS
            if not (sniper.position.x == target.x or sniper.position.y == target.y):
                continue
            if not self._clear_los(sniper.position, target):
                continue
            tgt_bot = self.occupancy_bots.get((target.x, target.y))
            if tgt_bot and tgt_bot.team_id != sniper.team_id:
                # reflect snipe
                if tgt_bot.reflect_ready and self._clear_los(tgt_bot.position, sniper.position):
                    damage_queue[sniper.id] = damage_queue.get(sniper.id, 0) + SNIPER_DMG
                    tgt_bot.reflect_ready = False
                else:
                    damage_queue[tgt_bot.id] = damage_queue.get(tgt_bot.id, 0) + SNIPER_DMG
                    self._inc_team_damage(sniper.team_id, SNIPER_DMG)
                    self._inc_bot_damage(sniper.id, SNIPER_DMG)

        # explode AoE
        for bomber in self.effects.explode_bots:
            if not bomber.is_alive():
                continue
            # bomber self-damage (do not count to team's damage metrics)
            damage_queue[bomber.id] = damage_queue.get(bomber.id, 0) + BOMBER_SELF_DMG
            # surrounding 3x3
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    x = bomber.position.x + dx
                    y = bomber.position.y + dy
                    tgt = self.occupancy_bots.get((x, y))
                    if tgt:
                        damage_queue[tgt.id] = damage_queue.get(tgt.id, 0) + BOMBER_AOE_DMG
                        if tgt.team_id != bomber.team_id:
                            self._inc_team_damage(bomber.team_id, BOMBER_AOE_DMG)
                            self._inc_bot_damage(bomber.id, BOMBER_AOE_DMG)
        # apply damages
        for bot in self.state.all_bots():
            dmg = damage_queue.get(bot.id, 0)
            if dmg:
                bot.apply_damage(dmg)

    # -------------------------------------------------------
    # Phase 4 – End-of-turn
    # -------------------------------------------------------
    def _phase_end_of_turn(self):
        # decrement structures
        self.state.walls = [Wall(x=w.x, y=w.y, team_id=w.team_id, ttl=w.ttl - 1) for w in self.state.walls if w.ttl - 1 > 0]
        self.state.traps = [Trap(x=t.x, y=t.y, team_id=t.team_id, ttl=t.ttl - 1) for t in self.state.traps if t.ttl - 1 > 0]
        new_decoys: List[Decoy] = []
        for d in self.state.decoys:
            nd = Decoy(x=d.x, y=d.y, team_id=d.team_id, owner_bot_id=d.owner_bot_id, hp=d.hp, ttl=d.ttl - 1)
            if nd.ttl > 0 and nd.hp > 0:
                new_decoys.append(nd)
        self.state.decoys = new_decoys

        for bot in self.state.all_bots():
            # cooldowns
            if bot.power_cooldown > 0:
                bot.power_cooldown -= 1
            # statuses
            if bot.silenced_remaining > 0:
                bot.silenced_remaining -= 1
            if bot.jammed_remaining > 0:
                bot.jammed_remaining -= 1
            if bot.slowed_remaining > 0:
                bot.slowed_remaining -= 1
            # poison ticks (10 per stack)
            if bot.poison_stacks:
                total = 10 * len(bot.poison_stacks)
                bot.apply_damage(total)
                bot.poison_stacks = [n - 1 for n in bot.poison_stacks if n - 1 > 0]
            # shield expire at end of next turn
            if bot.shield_expire_turn is not None and self.state.turn >= bot.shield_expire_turn:
                bot.shield_pool_remaining = 0
                bot.shield_expire_turn = None
        # Zones processing: boost after normal decrement, then heal/damage, then teleport
        for bot in self.state.all_bots():
            tile = (bot.position.x, bot.position.y)
            zones = self.state.zones.get(tile, [])
            if not zones:
                continue
            # Boost
            if "boost" in zones:
                if bot.power_cooldown > 0:
                    bot.power_cooldown = max(0, bot.power_cooldown - 1)
                self.zone_events.append(("boost", tile[0], tile[1], bot.id, {}))
            # Heal/Damage
            if "heal" in zones:
                before = bot.hp
                bot.hp = min(MAX_HP, bot.hp + 20)
                if bot.hp > before:
                    self.zone_events.append(("heal", tile[0], tile[1], bot.id, {}))
            if "damage" in zones:
                bot.apply_damage(10)
                self.zone_events.append(("damage", tile[0], tile[1], bot.id, {}))
        # Teleports applied after all bots processed to avoid chain issues
        teleports: List[Tuple[Bot, Tuple[int, int]]] = []
        for bot in self.state.all_bots():
            tile = (bot.position.x, bot.position.y)
            if "teleport" in self.state.zones.get(tile, []):
                teleports.append((bot, tile))
        if teleports:
            # compute all empty tiles
            empty_tiles = [
                (x, y)
                for x in range(self.state.grid_size)
                for y in range(self.state.grid_size)
                if (x, y) not in self.blockers and self.state.terrain.get((x, y)) != "water"
            ]
            for b, from_tile in teleports:
                if empty_tiles:
                    tx, ty = random.choice(empty_tiles)
                    b.position = Position(x=tx, y=ty)
                    self.zone_events.append(("teleport", from_tile[0], from_tile[1], b.id, {"to_x": tx, "to_y": ty}))
            # refresh references
            self.occupancy_bots = self.state.occupied_positions_bots()
            self.blockers = self.state.blockers()
        # Apply swamp slowed to bots that ended their move on swamp (affects next turn)
        for bot in self.state.all_bots():
            if self.state.terrain.get((bot.position.x, bot.position.y)) == "swamp":
                bot.slowed_remaining = max(bot.slowed_remaining, 1)
        # refresh references
        self.occupancy_bots = self.state.occupied_positions_bots()
        self.blockers = self.state.blockers() 