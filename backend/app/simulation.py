import json
import os
import random
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
from .maps import generate_map, load_rules


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
    # Default 10x10 grid fallback: evenly spaced on first and last rows
    top = [(0, 0), (2, 0), (4, 0), (6, 0), (8, 0)]
    bottom = [(0, 9), (2, 9), (4, 9), (6, 9), (8, 9)]
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
            # include static walls as separate list
            visible_static_walls = [[sx, sy] for (sx, sy) in state.static_walls if within4(sx, sy)]
            visible_decoys = [[d.x, d.y, d.team_id] for d in state.decoys if within4(d.x, d.y)]
            visible_traps = [[t.x, t.y, t.team_id] for t in state.traps if t.team_id == bot.team_id and within4(t.x, t.y)]
            # Visible terrain and zones (type included)
            visible_terrain = [[x, y, t] for (x, y), t in state.terrain.items() if within4(x, y)]
            visible_zones = [[x, y, zs] for (x, y), zs in state.zones.items() if within4(x, y)]

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
                "visible_static_walls": visible_static_walls,
                "visible_decoys": visible_decoys,
                "visible_traps": visible_traps,
                "visible_terrain": visible_terrain,
                "visible_zones": visible_zones,
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


async def simulate_match(session: AsyncSession, team_ids: List[str], seed: int | None = None, match_id: str | None = None):
    mode = "duo" if len(team_ids) == 2 else "quad" if len(team_ids) == 4 else None
    if not mode:
        raise ValueError("Only 2 or 4 teams supported")

    teams_db = await _load_teams(session, team_ids)

    # Assign map seed and generate map from rules
    seed = int(seed) if seed is not None else random.randint(1, 2**31 - 1)
    rules = load_rules()
    ms = generate_map(mode, seed, rules)
    grid_size = ms.grid_size_for_mode(mode, GRID_DUO, GRID_QUAD)
    # Build spawn positions list in engine format (list per team index)
    pos_lists: List[List[Tuple[int, int]]]
    if ms.spawn_positions:
        # Accept both nested (by mode) and flat schemas
        sp = None
        if isinstance(ms.spawn_positions, dict) and mode in ms.spawn_positions and isinstance(ms.spawn_positions[mode], dict):
            sp = ms.spawn_positions[mode]
        elif isinstance(ms.spawn_positions, dict) and "teamA" in ms.spawn_positions:
            sp = ms.spawn_positions  # flat
        if mode == "duo" and sp:
            a, b = _spawn_positions_duo()
            pos_lists = [sp.get("teamA", a), sp.get("teamB", b)]
        elif mode == "quad" and sp:
            pos_lists = [sp.get("teamA"), sp.get("teamB"), sp.get("teamC"), sp.get("teamD")]
        else:
            pos_lists = _spawn_positions_duo() if mode == "duo" else _spawn_positions_quad()
    else:
        pos_lists = _spawn_positions_duo() if mode == "duo" else _spawn_positions_quad()

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

    # Terrain/zones maps
    terrain_map = {tuple(t.pos): t.type for t in ms.terrain}
    zones_map: Dict[Tuple[int, int], List[str]] = {}
    for z in ms.zones:
        zones_map.setdefault(tuple(z.pos), []).append(z.type)

    state = GameState(
        grid_size=grid_size,
        teams=engine_teams,
        static_walls=[tuple(p) for p in ms.static_walls],
        terrain=terrain_map,
        zones=zones_map,
    )

    log: List[dict] = []
    turn = 0
    winner_team_id: str | None = None
    sandbox = _SandboxManager(team_files, bot_class_map)
    damage_done_total: Dict[str, int] = {tid: 0 for tid in team_ids}
    damage_by_bot_total: Dict[str, int] = {}
    # Track elimination turn for each team (None if survived to end)
    team_elimination_turn: Dict[str, int | None] = {tid: None for tid in team_ids}

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
                # resolve target id from prev occupancy if possible
                tgt_xy = ad.get('target')
                if isinstance(tgt_xy, list) and len(tgt_xy) == 2:
                    tgt_id = prev_occupancy.get((tgt_xy[0], tgt_xy[1]))
                    if tgt_id:
                        events.append(f"💉 {bid} infected {tgt_id}")
                    else:
                        events.append(f"💉 {bid} infected {tgt_xy}")
                else:
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
        # zone power-up preview events (on tiles bots are standing on after movement)
        for pu in getattr(engine, 'zone_powerups', []) or []:
            zt, zx, zy, bid = pu
            friendly = friendlyName = None
            try:
                team_prefix = bid.split('-')[0]
                role_code = bid.split('-')[1]
                team_name = next((t['name'] for t in teams_meta if t['prefix'] == team_prefix), team_prefix)
                role_name = { 'sn':'Sniper','ta':'Tank','bo':'Bomber','sc':'Scout','te':'Teleporter','po':'Poisoner','ts':'Trap Setter','he':'Healer','sg':'Shield Giver','pu':'Puller','br':'Bruiser','ja':'Jammer','re':'Reflector','wb':'Wall Builder','ps':'Pusher','dc':'Decoy Caster','le':'Leaper','si':'Silencer'}.get(role_code, role_code)
                friendly = f"{team_name} {role_name}"
            except Exception:
                friendly = bid
            icon = {'heal':'💚','damage':'💀','boost':'⚡','teleport':'✨'}.get(zt, '🟩')
            events.append(f"{icon} Zone ready: {zt} at [{zx},{zy}] for {friendly}")

        # zone events to drive VFX/SFX in viewer
        for ze in getattr(engine, 'zone_events', []) or []:
            zt, zx, zy, bid, extra = ze
            # friendly bot name
            try:
                team_prefix = bid.split('-')[0]
                role_code = bid.split('-')[1]
                team_name = next((t['name'] for t in teams_meta if t['prefix'] == team_prefix), team_prefix)
                role_name = { 'sn':'Sniper','ta':'Tank','bo':'Bomber','sc':'Scout','te':'Teleporter','po':'Poisoner','ts':'Trap Setter','he':'Healer','sg':'Shield Giver','pu':'Puller','br':'Bruiser','ja':'Jammer','re':'Reflector','wb':'Wall Builder','ps':'Pusher','dc':'Decoy Caster','le':'Leaper','si':'Silencer'}.get(role_code, role_code)
                friendly = f"{team_name} {role_name}"
            except Exception:
                friendly = bid
            if zt == 'heal':
                events.append(f"💚 Zone heal at [{zx},{zy}] for {friendly}")
            elif zt == 'damage':
                events.append(f"💀 Zone damage at [{zx},{zy}] to {friendly}")
            elif zt == 'boost':
                events.append(f"⚡ Zone boost at [{zx},{zy}] for {friendly}")
            elif zt == 'teleport':
                tx, ty = extra.get('to_x'), extra.get('to_y')
                events.append(f"✨ Teleport at [{zx},{zy}] moved {friendly} to [{tx},{ty}]")
        log.append(
            {
                "turn": turn,
                "actions": actions_serial,
                "positions": {b.id: [b.position.x, b.position.y] for b in state.all_bots()},
                "hp": {b.id: b.hp for b in state.all_bots()},
                "observations": sandbox.last_observations,
                "cooldowns": cooldowns,
                "shields": shields,
                # expose poison stack counts for frontend VFX/UI
                "poison": {b.id: len(b.poison_stacks) for b in state.all_bots()},
                "structures": structures,
                "events": events,
            }
        )
        # check win condition and update elimination turns
        alive_by_team: Dict[str, List[Bot]] = {}
        for b in state.all_bots():
            alive_by_team.setdefault(b.team_id, []).append(b)
        # Record elimination turn when a team reaches 0 alive for the first time
        for tid in team_ids:
            if team_elimination_turn[tid] is None and tid not in alive_by_team:
                team_elimination_turn[tid] = turn
        if len(alive_by_team) == 1:
            winner_team_id = next(iter(alive_by_team))
        turn += 1

    # timeout tiebreaker (no draws): determine winner via HP, then damage, then deterministic random
    if not winner_team_id:
        # Compute alive_by_team from final state
        alive_by_team = {}
        for b in state.all_bots():
            alive_by_team.setdefault(b.team_id, []).append(b)
        team_hp_now = {team_id: sum(b.hp for b in bots) for team_id, bots in alive_by_team.items()}
        if team_hp_now:
            max_hp = max(team_hp_now.values())
            candidates = [tid for tid, hp in team_hp_now.items() if hp == max_hp]
            if len(candidates) == 1:
                winner_team_id = candidates[0]
            else:
                # tie on HP: use total damage dealt
                max_dmg = max(damage_done_total.get(tid, 0) for tid in candidates)
                dmg_candidates = [tid for tid in candidates if damage_done_total.get(tid, 0) == max_dmg]
                if len(dmg_candidates) == 1:
                    winner_team_id = dmg_candidates[0]
                else:
                    # deterministic random using match seed
                    import random as _rand
                    _rng = _rand.Random(int(ms.seed) if ms.seed is not None else 0)
                    _rng.shuffle(dmg_candidates)
                    winner_team_id = dmg_candidates[0]

    # Compute final hp per team
    final_hp = {tid: 0 for tid in team_ids}
    for b in state.all_bots():
        final_hp[b.team_id] += b.hp

    # Write log file (include map header at root)
    match_dir = DATA_DIR
    match_dir.mkdir(parents=True, exist_ok=True)
    log_filename = f"{uuid.uuid4()}.json"
    log_path = match_dir / log_filename
    # append simple summary frame for viewer end panel
    summary = {
        "summary": True,
        "damage_by_bot": damage_by_bot_total,
    }
    map_header = {
        "map": {
            "name": ms.name,
            "seed": ms.seed,
            "size": ms.size,
            "spawn_positions": getattr(ms, 'spawn_positions', None),
            "static_walls": ms.static_walls,
            "terrain": [{"type": t.type, "pos": list(t.pos)} for t in ms.terrain],
            "zones": [{"type": z.type, "pos": list(z.pos)} for z in ms.zones],
        }
    }
    log_with_tail = [map_header] + log + [summary]
    log_path.write_text(json.dumps(log_with_tail), encoding="utf-8")

    # Build ranks per spec
    # Survivors first (no elimination turn), then eliminated by later death better
    survivors = [tid for tid in team_ids if team_elimination_turn.get(tid) is None]
    eliminated = [tid for tid in team_ids if team_elimination_turn.get(tid) is not None]

    # Sort helpers with deterministic randomness based on match_id when provided (fallback: map seed)
    import random as _rand, hashlib as _hash
    if match_id is not None:
        try:
            _seed_base = int(_hash.sha256(match_id.encode("utf-8")).hexdigest()[:8], 16)
        except Exception:
            _seed_base = int(ms.seed) if ms.seed is not None else 0
    else:
        _seed_base = int(ms.seed) if ms.seed is not None else 0

    def sort_survivors(tids: List[str]) -> List[str]:
        # group by hp, damage for potential equal ranks
        def key(tid: str):
            return (-final_hp.get(tid, 0), -damage_done_total.get(tid, 0))
        tids_sorted = sorted(tids, key=key)
        # break display-order ties deterministically
        i = 0
        out: List[str] = []
        while i < len(tids_sorted):
            j = i + 1
            while j < len(tids_sorted) and key(tids_sorted[j]) == key(tids_sorted[i]):
                j += 1
            group = tids_sorted[i:j]
            rng = _rand.Random((_seed_base + len(group) + i) & 0xFFFFFFFF)
            rng.shuffle(group)
            out.extend(group)
            i = j
        return out

    def sort_eliminated(tids: List[str]) -> List[str]:
        def key(tid: str):
            return (-(team_elimination_turn.get(tid) or -1), -damage_done_total.get(tid, 0))
        tids_sorted = sorted(tids, key=key)
        i = 0
        out: List[str] = []
        while i < len(tids_sorted):
            j = i + 1
            while j < len(tids_sorted) and key(tids_sorted[j]) == key(tids_sorted[i]):
                j += 1
            group = tids_sorted[i:j]
            rng = _rand.Random((_seed_base + 1000 + len(group) + i) & 0xFFFFFFFF)
            rng.shuffle(group)
            out.extend(group)
            i = j
        return out

    survivors_sorted = sort_survivors(survivors)
    eliminated_sorted = sort_eliminated(eliminated)
    ranks_order: List[str] = survivors_sorted + eliminated_sorted

    # Build ranks_map with competition ranking (equal metrics => equal rank)
    ranks_map: Dict[str, int] = {}
    current_rank = 0

    def assign_group(group: List[str]):
        nonlocal current_rank
        for tid in group:
            ranks_map[tid] = current_rank
        current_rank += len(group)

    # Survivors groups by (hp, damage)
    def group_by_key(tids: List[str], key_func):
        if not tids:
            return []
        groups: List[List[str]] = []
        i = 0
        sorted_tids = sorted(tids, key=key_func)
        while i < len(sorted_tids):
            j = i + 1
            while j < len(sorted_tids) and key_func(sorted_tids[j]) == key_func(sorted_tids[i]):
                j += 1
            groups.append(sorted_tids[i:j])
            i = j
        return groups

    for grp in group_by_key(survivors_sorted, lambda tid: (-final_hp.get(tid, 0), -damage_done_total.get(tid, 0))):
        assign_group(grp)
    # Eliminated groups by (elim_turn, damage)
    for grp in group_by_key(eliminated_sorted, lambda tid: (team_elimination_turn.get(tid), -damage_done_total.get(tid, 0))):
        assign_group(grp)

    return {
        "winner_team_id": winner_team_id,
        "turns": turn,
        "log_path": str(log_path),
        "team_hp": final_hp,
        "team_damage": damage_done_total,
        "damage_by_bot": damage_by_bot_total,
        "map_name": ms.name,
        "map_seed": int(ms.seed) if ms.seed is not None else None,
        "ranks_order": ranks_order,
        "ranks_map": ranks_map,
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