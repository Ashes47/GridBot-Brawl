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
        self.occupancy = self.state.occupied_positions()  # (x,y) -> Bot
        # Track damage dealt this turn per team_id
        self.damage_done_by_team: Dict[str, int] = {}
        # Track damage dealt this turn per bot_id
        self.damage_done_by_bot: Dict[str, int] = {}

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

    # -------------------------------------------------------
    # Phase 1 – Powers (prep)
    # -------------------------------------------------------
    def _phase_powers(self):
        for bot in self.state.all_bots():
            action = self.actions.get(bot.id)
            if not action:
                continue
            # skip if action is not a power
            if isinstance(action, ShieldAction):
                if bot.power_cooldown == 0:
                    bot.shield_remaining = 3
                    bot.power_cooldown = COOLDOWNS[bot.role]
            elif isinstance(action, SnipeAction):
                if bot.power_cooldown == 0:
                    self.effects.snipe_events.append((bot, action.target))
                    bot.power_cooldown = COOLDOWNS[bot.role]
            elif isinstance(action, ExplodeAction):
                if bot.power_cooldown == 0:
                    self.effects.explode_bots.append(bot)
                    bot.power_cooldown = COOLDOWNS[bot.role]
            # Dash / Blink handled in movement phase

    # -------------------------------------------------------
    # Phase 2 – Movement
    # -------------------------------------------------------
    def _desired_position(self, bot: Bot) -> Position:
        action = self.actions.get(bot.id)
        # Blink
        if isinstance(action, BlinkAction) and bot.power_cooldown == 0:
            empty_tiles = [
                Position(x=x, y=y)
                for x in range(self.state.grid_size)
                for y in range(self.state.grid_size)
                if (x, y) not in self.occupancy
            ]
            if empty_tiles:
                bot.power_cooldown = COOLDOWNS[bot.role]
                return random.choice(empty_tiles)
            return bot.position  # no movement if full
        # Dash
        if isinstance(action, DashAction) and bot.power_cooldown == 0:
            dx, dy = action.direction.delta
            first = (bot.position.x + dx, bot.position.y + dy)
            second = (first[0] + dx, first[1] + dy)
            size = self.state.grid_size
            if (
                0 <= first[0] < size
                and 0 <= first[1] < size
                and 0 <= second[0] < size
                and 0 <= second[1] < size
                and first not in self.occupancy
                and second not in self.occupancy
            ):
                bot.power_cooldown = COOLDOWNS[bot.role]
                return Position(x=second[0], y=second[1])
            return bot.position  # failed dash
        # Normal move (compute coords first to avoid invalid Position)
        if isinstance(action, MoveAction):
            dx, dy = action.direction.delta
            nx, ny = bot.position.x + dx, bot.position.y + dy
            if 0 <= nx < self.state.grid_size and 0 <= ny < self.state.grid_size:
                return Position(x=nx, y=ny)
        return bot.position

    def _phase_movement(self):
        desired: Dict[Tuple[int, int], List[Bot]] = {}
        new_positions: Dict[str, Position] = {}

        for bot in self.state.all_bots():
            pos = self._desired_position(bot)
            new_positions[bot.id] = pos
            desired.setdefault((pos.x, pos.y), []).append(bot)

        # resolve collisions
        for bot in self.state.all_bots():
            pos_tuple = (new_positions[bot.id].x, new_positions[bot.id].y)
            # if collision (multiple bots want same) or destination occupied by another staying bot
            if len(desired[pos_tuple]) == 1 and (
                pos_tuple not in self.occupancy or self.occupancy[ pos_tuple ] is bot
            ):
                bot.position = new_positions[bot.id]
            # else stays in place
        # update occupancy
        self.occupancy = self.state.occupied_positions()

    # -------------------------------------------------------
    # Phase 3 – Attacks
    # -------------------------------------------------------
    def _inc_team_damage(self, team_id: str, amount: int):
        if amount <= 0:
            return
        self.damage_done_by_team[team_id] = self.damage_done_by_team.get(team_id, 0) + amount

    def _inc_bot_damage(self, bot_id: str, amount: int):
        if amount <= 0:
            return
        self.damage_done_by_bot[bot_id] = self.damage_done_by_bot.get(bot_id, 0) + amount

    def _phase_attack(self):
        damage_queue: Dict[str, int] = {}

        # base attacks and dash etc.
        for bot in self.state.all_bots():
            action = self.actions.get(bot.id)
            if isinstance(action, AttackAction):
                target_pos = bot.position.moved(action.direction)
                tgt = self.occupancy.get((target_pos.x, target_pos.y))
                if tgt and tgt.team_id != bot.team_id:
                    damage_queue[tgt.id] = damage_queue.get(tgt.id, 0) + BASE_ATTACK_DMG
                    self._inc_team_damage(bot.team_id, BASE_ATTACK_DMG)
                    self._inc_bot_damage(bot.id, BASE_ATTACK_DMG)

        # snipe events
        for sniper, target in self.effects.snipe_events:
            if not sniper.is_alive():
                continue
            if sniper.position.distance_manhattan(target) > 5:
                continue
            if sniper.position.x != target.x and sniper.position.y != target.y:
                continue  # not straight line
            if not self._clear_los(sniper.position, target):
                continue
            tgt_bot = self.occupancy.get((target.x, target.y))
            if tgt_bot and tgt_bot.team_id != sniper.team_id:
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
                    tgt = self.occupancy.get((x, y))
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

    def _clear_los(self, src: Position, dst: Position) -> bool:
        # assume src.x == dst.x or src.y == dst.y (straight)
        if src.x == dst.x:
            step = 1 if dst.y > src.y else -1
            for y in range(src.y + step, dst.y, step):
                if (src.x, y) in self.occupancy:
                    return False
        else:
            step = 1 if dst.x > src.x else -1
            for x in range(src.x + step, dst.x, step):
                if (x, src.y) in self.occupancy:
                    return False
        return True

    # -------------------------------------------------------
    # Phase 4 – End-of-turn
    # -------------------------------------------------------
    def _phase_end_of_turn(self):
        for bot in self.state.all_bots():
            # cooldowns
            if bot.power_cooldown > 0:
                bot.power_cooldown -= 1
            # shield duration
            if bot.shield_remaining > 0:
                bot.shield_remaining -= 1
        # Remove dead handled naturally via is_alive() checks 