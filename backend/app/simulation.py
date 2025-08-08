import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .db_models import Team as TeamDB, Match
from .engine import (
    Bot,
    Direction,
    GameState,
    GRID_DUO,
    GRID_QUAD,
    Role,
    Team,
    TurnEngine,
    Position,
    TURN_LIMIT,
)


def safe_position_from_dict(target_dict: dict, grid_size: int) -> "Position":
    """
    Safely create a Position from a dictionary, ensuring coordinates are within valid bounds.
    Clamps coordinates to valid range if they're outside bounds.
    Returns a Position with clamped coordinates.
    """
    if not isinstance(target_dict, dict) or "x" not in target_dict or "y" not in target_dict:
        # Return center position as fallback for invalid input
        return Position(x=grid_size // 2, y=grid_size // 2)

    try:
        x = int(target_dict["x"])
        y = int(target_dict["y"])

        # Clamp coordinates to valid bounds (0 <= x,y < grid_size)
        x = max(0, min(x, grid_size - 1))
        y = max(0, min(y, grid_size - 1))

        return Position(x=x, y=y)
    except (ValueError, TypeError, KeyError):
        # Return center position as fallback for invalid input
        return Position(x=grid_size // 2, y=grid_size // 2)


DATA_DIR = Path(os.getenv("DATA_DIR", "./data/matches"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


# default order fallback if roster missing
DEFAULT_ROLE_ORDER = [Role.SNIPER, Role.TANK, Role.BOMBER, Role.SCOUT, Role.TELEPORTER]


async def _load_teams(session: AsyncSession, team_ids: List[str]):
    uuids = []
    for tid in team_ids:
        try:
            uuids.append(uuid.UUID(tid))
        except ValueError:
            raise ValueError(f"Invalid team id: {tid}")
    result = await session.execute(select(TeamDB).where(TeamDB.id.in_(uuids)))
    teams = result.scalars().all()
    if len(teams) != len(team_ids):
        raise ValueError("One or more team ids not found")
    return teams


# ---------------- Spawning helpers ----------------

def _spawn_positions_duo() -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
    top = [(1, 0), (3, 0), (5, 0), (7, 0), (9, 0)]
    bottom = [(1, 9), (3, 9), (5, 9), (7, 9), (9, 9)]
    return top, bottom


def _spawn_positions_quad() -> List[List[Tuple[int, int]]]:
    # quadrants TL, TR, BR, BL
    tl = [(0, 0), (0, 2), (1, 1), (2, 0), (2, 2)]
    tr = [(12, 0), (14, 0), (13, 1), (12, 2), (14, 2)]
    br = [(12, 12), (14, 14), (13, 13), (12, 14), (14, 12)]
    bl = [(0, 12), (0, 14), (1, 13), (2, 12), (2, 14)]
    return [tl, tr, br, bl]


# ---------------- Decision logic using sandbox ----------------

from .sandbox import load_team, safe_decide


class _SandboxManager:
    def __init__(self, team_files: Dict[str, str], bot_class_map: Dict[str, Dict[str, str]]):
        """team_files maps team_id -> bot file path; bot_class_map maps team_id -> {bot_id: class_name}."""
        self.controllers = {
            tid: load_team(tid, path, bot_class_map[tid])
            for tid, path in team_files.items()
        }
        # last_observations maps bot_id -> obs for the most recent decide cycle
        self.last_observations: Dict[str, dict] = {}

    def decide_actions(self, state: GameState) -> Dict[str, object]:
        actions: Dict[str, object] = {}
        obs_map: Dict[str, dict] = {}
        for bot in state.all_bots():
            ctrl = self.controllers.get(bot.team_id)
            if not ctrl:
                continue
            proxy = ctrl.bots.get(bot.id)
            if not proxy:
                continue
            # Build observation per richer schema
            vis_enemies, vis_allies = [], []
            for other in state.all_bots():
                dx = abs(other.position.x - bot.position.x)
                dy = abs(other.position.y - bot.position.y)
                if max(dx, dy) <= 4 and other is not bot:
                    entry = {
                        "id": other.id,
                        "x": other.position.x,
                        "y": other.position.y,
                        "team": other.team_id,
                        "component": other.role.value,
                    }
                    if other.team_id == bot.team_id:
                        vis_allies.append(entry)
                    else:
                        vis_enemies.append(entry)

            # Visible structures within radius 4
            def within4(x, y):
                return max(abs(x - bot.position.x), abs(y - bot.position.y)) <= 4
            visible_walls = [[w.x, w.y] for w in state.walls if within4(w.x, w.y)]
            visible_decoys = [[d.x, d.y, d.team_id] for d in state.decoys if within4(d.x, d.y)]
            visible_traps = [[t.x, t.y, t.team_id] for t in state.traps if t.team_id == bot.team_id and within4(t.x, t.y)]

            obs = {
                "turn": state.turn,
                "map_size": [state.grid_size, state.grid_size],
                "self": {
                    "id": bot.id,
                    "x": bot.position.x,
                    "y": bot.position.y,
                    "health": bot.hp,
                    "component": bot.role.value,
                    "cooldowns": {"power": bot.power_cooldown},
                },
                "visible_enemies": vis_enemies,
                "visible_allies": vis_allies,
                "visible_walls": visible_walls,
                "visible_decoys": visible_decoys,
                "visible_traps": visible_traps,
            }
            obs_map[bot.id] = obs
            action_dict = safe_decide(proxy, obs)
            # translate to Action instances
            from app.engine.models import (
                MoveAction,
                AttackAction,
                DashAction,
                ShieldAction,
                SnipeAction,
                ExplodeAction,
                BlinkAction,
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
            )

            t = action_dict.get("type")
            if t == "move" and "direction" in action_dict:
                actions[bot.id] = MoveAction(direction=Direction(action_dict["direction"]))
            elif t == "attack" and "direction" in action_dict:
                actions[bot.id] = AttackAction(direction=Direction(action_dict["direction"]))
            elif t == "dash" and "direction" in action_dict:
                actions[bot.id] = DashAction(direction=Direction(action_dict["direction"]))
            elif t == "shield":
                actions[bot.id] = ShieldAction()
            elif t == "snipe" and "target" in action_dict:
                actions[bot.id] = SnipeAction(target=safe_position_from_dict(action_dict["target"], state.grid_size))
            elif t == "explode":
                actions[bot.id] = ExplodeAction()
            elif t == "blink":
                actions[bot.id] = BlinkAction()
            elif t == "project_shield" and "target" in action_dict:
                actions[bot.id] = ProjectShieldAction(target=safe_position_from_dict(action_dict["target"], state.grid_size))
            elif t == "heal" and "target" in action_dict:
                actions[bot.id] = HealAction(target=safe_position_from_dict(action_dict["target"], state.grid_size))
            elif t == "infect" and "target" in action_dict:
                actions[bot.id] = InfectAction(target=safe_position_from_dict(action_dict["target"], state.grid_size))
            elif t == "silence" and "target" in action_dict:
                actions[bot.id] = SilenceAction(target=safe_position_from_dict(action_dict["target"], state.grid_size))
            elif t == "mirror":
                actions[bot.id] = MirrorAction()
            elif t == "drop_trap":
                actions[bot.id] = DropTrapAction()
            elif t == "drop_wall" and "target" in action_dict:
                actions[bot.id] = DropWallAction(target=safe_position_from_dict(action_dict["target"], state.grid_size))
            elif t == "clone":
                # optional target; if provided, parse, else leave None and engine will treat as no-op
                pos = action_dict.get("target")
                actions[bot.id] = CloneAction(target=(safe_position_from_dict(pos, state.grid_size) if isinstance(pos, dict) else None))
            elif t == "yank" and "target" in action_dict:
                actions[bot.id] = YankAction(target=safe_position_from_dict(action_dict["target"], state.grid_size))
            elif t == "shove" and "target" in action_dict:
                actions[bot.id] = ShoveAction(target=safe_position_from_dict(action_dict["target"], state.grid_size))
            elif t == "leap" and "target" in action_dict:
                actions[bot.id] = LeapAction(target=safe_position_from_dict(action_dict["target"], state.grid_size))
            elif t == "scramble":
                actions[bot.id] = ScrambleAction()
            # else: unknown or malformed -> idle
        self.last_observations = obs_map
        return actions


# ---------------- Main simulation ----------------


async def simulate_match(session: AsyncSession, team_ids: List[str]):
    mode = "duo" if len(team_ids) == 2 else "quad" if len(team_ids) == 4 else None
    if not mode:
        raise ValueError("Only 2 or 4 teams supported")

    teams_db = await _load_teams(session, team_ids)

    # Build GameState
    if mode == "duo":
        grid_size = GRID_DUO
        pos_lists = _spawn_positions_duo()
    else:
        grid_size = GRID_QUAD
        pos_lists = _spawn_positions_quad()

    engine_teams: List[Team] = []
    team_files: Dict[str, str] = {}
    bot_class_map: Dict[str, Dict[str, str]] = {}

    # helper to map roster strings to Role
    def role_from_str(s: str) -> Role:
        return Role(s)

    for idx, team_db in enumerate(teams_db):
        positions = pos_lists[idx]
        bots: List[Bot] = []
        roster_list = json.loads(team_db.roster) if team_db.roster else [r.value for r in DEFAULT_ROLE_ORDER]
        # ensure 5 entries
        if len(roster_list) != 5:
            roster_list = roster_list[:5] if len(roster_list) > 5 else roster_list + [r.value for r in DEFAULT_ROLE_ORDER][: (5 - len(roster_list))]
        class_name_map: Dict[str, str] = {}
        for i, comp_key in enumerate(roster_list):
            role = role_from_str(comp_key)
            pos = positions[i]
            role_code = {
                Role.SNIPER: "sn",
                Role.TANK: "ta",
                Role.BOMBER: "bo",
                Role.SCOUT: "sc",
                Role.TELEPORTER: "te",
                Role.POISONER: "po",
                Role.TRAP_SETTER: "ts",
                Role.HEALER: "he",
                Role.SHIELD_GIVER: "sg",
                Role.PULLER: "pu",
                Role.BRUISER: "br",
                Role.JAMMER: "ja",
                Role.REFLECTOR: "re",
                Role.WALL_BUILDER: "wb",
                Role.PUSHER: "ps",
                Role.DECOY_CASTER: "dc",
                Role.LEAPER: "le",
                Role.SILENCER: "si",
            }[role]
            bot_id = f"{team_db.id.hex[:6]}-{role_code}"
            bots.append(
                Bot(
                    id=bot_id,
                    role=role,
                    position=Position(x=pos[0], y=pos[1]),
                    team_id=str(team_db.id),
                )
            )
            # class name mapping: from comp_key to expected class name in user file (PascalCase with underscores retained per spec)
            class_name = {
                "tank": "Tank",
                "sniper": "Sniper",
                "bomber": "Bomber",
                "scout": "Scout",
                "teleporter": "Teleporter",
                "poisoner": "Poisoner",
                "trap_setter": "Trap_Setter",
                "healer": "Healer",
                "shield_giver": "Shield_Giver",
                "puller": "Puller",
                "bruiser": "Bruiser",
                "jammer": "Jammer",
                "reflector": "Reflector",
                "wall_builder": "Wall_Builder",
                "pusher": "Pusher",
                "decoy_caster": "Decoy_Caster",
                "leaper": "Leaper",
                "silencer": "Silencer",
            }[comp_key]
            class_name_map[bot_id] = class_name
        engine_teams.append(Team(id=str(team_db.id), bots=bots))
        team_files[str(team_db.id)] = team_db.code_path
        bot_class_map[str(team_db.id)] = class_name_map

    state = GameState(grid_size=grid_size, teams=engine_teams)

    log: List[dict] = []
    turn = 0
    winner_team_id: str | None = None
    sandbox = _SandboxManager(team_files, bot_class_map)
    damage_done_total: Dict[str, int] = {tid: 0 for tid in team_ids}
    damage_by_bot_total: Dict[str, int] = {}

    while turn < TURN_LIMIT and not winner_team_id:
        actions = sandbox.decide_actions(state)

        def _serialize(act):
            t = act.__class__.__name__.lower()
            if t.endswith('action'):
                t = t[:-6]
            d = {"type": t}
            if hasattr(act, 'direction'):
                d["direction"] = act.direction.value
            if hasattr(act, 'target'):
                d["target"] = [act.target.x, act.target.y]
            return d

        actions_serial = {bid: _serialize(a) for bid, a in actions.items()}
        # snapshot before running for event derivation
        prev_alive_ids = {b.id for b in state.all_bots()}
        prev_hp_map = {b.id: b.hp for b in state.all_bots()}
        prev_positions = {b.id: (b.position.x, b.position.y) for b in state.all_bots()}
        prev_occupancy = { (b.position.x, b.position.y): b.id for b in state.all_bots() }

        engine = TurnEngine(state, actions)
        state = engine.run()
        # accumulate damage for this turn
        for tid, amount in getattr(engine, 'damage_done_by_team', {}).items():
            if tid in damage_done_total:
                damage_done_total[tid] += amount
        for bid, amount in getattr(engine, 'damage_done_by_bot', {}).items():
            damage_by_bot_total[bid] = damage_by_bot_total.get(bid, 0) + amount

        # derive simple events
        events: list[str] = []
        # movement events (exclude dash since it has dedicated event below)
        for bid, (px, py) in prev_positions.items():
            curr_b = state.bot_by_id(bid)
            if not curr_b:
                continue
            cx, cy = curr_b.position.x, curr_b.position.y
            if (cx, cy) != (px, py):
                dx, dy = cx - px, cy - py
                dir_name = None
                if (dx, dy) == (1, 0): dir_name = 'east'
                elif (dx, dy) == (-1, 0): dir_name = 'west'
                elif (dx, dy) == (0, 1): dir_name = 'south'
                elif (dx, dy) == (0, -1): dir_name = 'north'
                if dir_name and actions_serial.get(bid, {}).get('type') != 'dash':
                    events.append(f"✅ {bid} moved {dir_name}")
        for bid, ad in actions_serial.items():
            t = ad.get('type')
            if t == 'shield':
                events.append(f"🛡 {bid} used shield")
            elif t == 'project_shield':
                events.append(f"🧲 {bid} projected shield")
            elif t == 'heal':
                events.append(f"🧬 {bid} healed an ally")
            elif t == 'infect':
                events.append(f"💉 {bid} infected a target")
            elif t == 'silence':
                events.append(f"🔇 {bid} silenced a target")
            elif t == 'mirror':
                events.append(f"🪞 {bid} readied reflect")
            elif t == 'drop_trap':
                events.append(f"🪤 {bid} planted a trap")
            elif t == 'drop_wall':
                events.append(f"🧱 {bid} built a wall")
            elif t == 'clone':
                events.append(f"🐾 {bid} summoned a decoy")
            elif t == 'scramble':
                events.append(f"🛰 {bid} scrambled nearby enemies")
            elif t == 'yank':
                events.append(f"🌀 {bid} yanked an enemy")
            elif t == 'shove':
                events.append(f"🌀→ {bid} shoved an enemy")
            elif t == 'leap':
                events.append(f"🐸 {bid} leaped")
            elif t == 'snipe':
                # resolve target id from prev occupancy if possible
                tgt_xy = ad.get('target')
                if isinstance(tgt_xy, list) and len(tgt_xy) == 2:
                    tgt_id = prev_occupancy.get((tgt_xy[0], tgt_xy[1]))
                    if tgt_id:
                        events.append(f"🎯 {bid} sniped {tgt_id} (40 dmg)")
                    else:
                        events.append(f"🎯 {bid} sniped {tgt_xy}")
                else:
                    events.append(f"🎯 {bid} sniped")
            elif t == 'explode':
                # count enemies in 3x3 around bomber at prev positions
                bx, by = prev_positions.get(bid, (None, None))
                hit = 0
                if bx is not None:
                    for dx in (-1,0,1):
                        for dy in (-1,0,1):
                            if dx==0 and dy==0: continue
                            tid = prev_occupancy.get((bx+dx, by+dy))
                            if tid:
                                # enemy if team differs by id prefix
                                if tid.split('-')[0] != bid.split('-')[0]:
                                    hit += 1
                events.append(f"💣 {bid} exploded (hit {hit} enemies)")
            elif t == 'dash':
                events.append(f"🏃 {bid} dashed {ad.get('direction')}")
            elif t == 'blink':
                events.append(f"✨ {bid} blinked")
            elif t == 'attack':
                events.append(f"⚔️ {bid} attacked {ad.get('direction')}")
        # eliminations
        curr_alive_ids = {b.id for b in state.all_bots()}
        for gone in (prev_alive_ids - curr_alive_ids):
            events.append(f"☠️ {gone} eliminated")

        # per-bot cooldowns and shields
        cooldowns = {b.id: b.power_cooldown for b in state.all_bots()}
        shields = {b.id: b.shield_pool_remaining for b in state.all_bots()}

        # collect snapshot
        # include simple structure overlays and trap triggers
        structures = {
            "walls": [[w.x, w.y, w.team_id] for w in state.walls],
            "traps": [[t.x, t.y, t.team_id] for t in state.traps],
            "decoys": [[d.x, d.y, d.team_id] for d in state.decoys],
        }
        trap_trigs = getattr(engine, 'trap_triggers', [])
        if trap_trigs:
            for x,y in trap_trigs:
                events.append(f"🪤 Trap triggered at [{x},{y}]")
        log.append(
            {
                "turn": turn,
                "actions": actions_serial,
                "positions": {b.id: [b.position.x, b.position.y] for b in state.all_bots()},
                "hp": {b.id: b.hp for b in state.all_bots()},
                "observations": sandbox.last_observations,
                "cooldowns": cooldowns,
                "shields": shields,
                "structures": structures,
                "events": events,
            }
        )
        # check win condition
        alive_by_team: Dict[str, List[Bot]] = {}
        for b in state.all_bots():
            alive_by_team.setdefault(b.team_id, []).append(b)
        if len(alive_by_team) == 1:
            winner_team_id = next(iter(alive_by_team))
        turn += 1

    # timeout tiebreaker
    if not winner_team_id:
        team_hp = {
            team_id: sum(b.hp for b in bots) for team_id, bots in alive_by_team.items()
        }
        max_hp = max(team_hp.values())
        winners = [tid for tid, hp in team_hp.items() if hp == max_hp]
        if len(winners) == 1:
            winner_team_id = winners[0]

    # Compute final hp per team
    final_hp = {tid: 0 for tid in team_ids}
    for b in state.all_bots():
        final_hp[b.team_id] += b.hp

    # Write log file
    match_dir = DATA_DIR
    match_dir.mkdir(parents=True, exist_ok=True)
    log_filename = f"{uuid.uuid4()}.json"
    log_path = match_dir / log_filename
    # append simple summary frame for viewer end panel
    summary = {
        "summary": True,
        "damage_by_bot": damage_by_bot_total,
    }
    log_with_tail = log + [summary]
    log_path.write_text(json.dumps(log_with_tail), encoding="utf-8")

    return {
        "winner_team_id": winner_team_id,
        "turns": turn,
        "log_path": str(log_path),
        "team_hp": final_hp,
        "team_damage": damage_done_total,
        "damage_by_bot": damage_by_bot_total,
    }


async def run_match_job(session_factory, match_id: str, team_ids: List[str]):
    """Background task entrypoint: run simulation and update Match row."""
    from sqlalchemy import update
    result = None
    async with session_factory() as session:
        # mark running
        await session.execute(update(Match).where(Match.id == uuid.UUID(match_id)).values(status="running"))
        await session.commit()
        try:
            result = await simulate_match(session, team_ids)
            await session.execute(
                update(Match)
                .where(Match.id == uuid.UUID(match_id))
                .values(
                    status="finished",
                    winner_team_id=uuid.UUID(result["winner_team_id"]) if result["winner_team_id"] else None,
                    log_path=result["log_path"],
                    team_hp=json.dumps(result["team_hp"]),
                    team_damage=json.dumps(result["team_damage"]),
                )
            )
        except FileNotFoundError:
            await session.execute(
                update(Match).where(Match.id == uuid.UUID(match_id)).values(status="error")
            )
        except Exception as exc:
            await session.execute(
                update(Match).where(Match.id == uuid.UUID(match_id)).values(status="error")
            )
            import logging; logging.exception("Match %s failed", match_id, exc_info=exc)
        await session.commit()
    return result 