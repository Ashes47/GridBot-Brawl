import sys
import uuid
import json
from pathlib import Path
from typing import Dict, List, Tuple

from backend.app.engine import (
    Bot,
    Position,
    Team,
    GameState,
    TurnEngine,
    GRID_DUO,
    Role,
    Direction,
)
from backend.app.sandbox import load_team, safe_decide
import multiprocessing as mp

# For local testing, keep a default roster of 5 classic roles
ROLE_ORDER = [Role.SNIPER, Role.TANK, Role.BOMBER, Role.SCOUT, Role.TELEPORTER]


def spawn_positions_duo() -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
    top = [(1, 0), (3, 0), (5, 0), (7, 0), (9, 0)]
    bottom = [(1, 9), (3, 9), (5, 9), (7, 9), (9, 9)]
    return top, bottom


def build_game_state(file_a: Path, file_b: Path) -> Tuple[GameState, Dict[str, dict]]:
    positions_a, positions_b = spawn_positions_duo()
    teams: List[Team] = []
    team_files: Dict[str, str] = {}
    bot_class_map: Dict[str, Dict[str, str]] = {}

    def role_code(role: Role) -> str:
        return {
            Role.SNIPER: "sn",
            Role.TANK: "ta",
            Role.BOMBER: "bo",
            Role.SCOUT: "sc",
            Role.TELEPORTER: "te",
        }[role]

    def class_name_for(role: Role) -> str:
        return {
            Role.SNIPER: "Sniper",
            Role.TANK: "Tank",
            Role.BOMBER: "Bomber",
            Role.SCOUT: "Scout",
            Role.TELEPORTER: "Teleporter",
        }[role]

    for idx, (team_file, positions) in enumerate([(file_a, positions_a), (file_b, positions_b)]):
        team_id = f"T{idx+1}"
        bots: List[Bot] = []
        class_map: Dict[str, str] = {}
        for i, role in enumerate(ROLE_ORDER):
            pos = positions[i]
            code = role_code(role)
            bot_id = f"{team_id}-{code}"
            bots.append(
                Bot(
                    id=bot_id,
                    role=role,
                    position=Position(x=pos[0], y=pos[1]),
                    team_id=team_id,
                )
            )
            class_map[bot_id] = class_name_for(role)
        teams.append(Team(id=team_id, bots=bots))
        team_files[team_id] = str(team_file)
        bot_class_map[team_id] = class_map

    state = GameState(grid_size=GRID_DUO, teams=teams)
    return state, {"files": team_files, "class_map": bot_class_map}


class SandboxManager:
    def __init__(self, team_files: Dict[str, str], bot_class_map: Dict[str, Dict[str, str]]):
        self.ctrls = {
            tid: load_team(tid, path, bot_class_map[tid])
            for tid, path in team_files.items()
        }

    def decide(self, state: GameState) -> Tuple[Dict[str, object], Dict[str, object]]:
        from backend.app.engine.models import (
            MoveAction,
            AttackAction,
            DashAction,
            ShieldAction,
            SnipeAction,
            ExplodeAction,
            BlinkAction,
        )
        actions: Dict[str, object] = {}
        raw_actions: Dict[str, object] = {}
        for bot in state.all_bots():
            proxy = self.ctrls[bot.team_id].bots[bot.id]
            # build observation similar to backend schema (simplified)
            vis_enemies, vis_allies = [], []
            for other in state.all_bots():
                if other is bot:
                    continue
                dx = abs(other.position.x - bot.position.x)
                dy = abs(other.position.y - bot.position.y)
                if max(dx,dy)<=4:
                    entry={"id":other.id,"x":other.position.x,"y":other.position.y,"team":other.team_id,"component":other.role.value}
                    (vis_allies if other.team_id==bot.team_id else vis_enemies).append(entry)

            obs = {
                "turn": state.turn,
                "map_size": [state.grid_size,state.grid_size],
                "self": {"id":bot.id,"x": bot.position.x, "y": bot.position.y, "health": bot.hp, "component": bot.role.value, "cooldowns":{"power": bot.power_cooldown}},
                "visible_enemies": vis_enemies,
                "visible_allies": vis_allies,
            }
            act = safe_decide(proxy, obs)
            t = act.get("type")
            if t == "move" and "direction" in act:
                actions[bot.id] = MoveAction(direction=Direction(act["direction"]))
                raw_actions[bot.id] = {"type": "move", "direction": act["direction"]}
            elif t == "attack" and "direction" in act:
                actions[bot.id] = AttackAction(direction=Direction(act["direction"]))
                raw_actions[bot.id] = {"type": "attack", "direction": act["direction"]}
            elif t == "dash" and "direction" in act:
                actions[bot.id] = DashAction(direction=Direction(act["direction"]))
                raw_actions[bot.id] = {"type": "dash", "direction": act["direction"]}
            elif t == "shield":
                actions[bot.id] = ShieldAction()
                raw_actions[bot.id] = {"type": "shield"}
            elif t == "snipe" and "target" in act:
                actions[bot.id] = SnipeAction(target=Position(**act["target"]))
                raw_actions[bot.id] = {"type": "snipe", "target": act["target"]}
            elif t == "explode":
                actions[bot.id] = ExplodeAction()
                raw_actions[bot.id] = {"type": "explode"}
            elif t == "blink":
                actions[bot.id] = BlinkAction()
                raw_actions[bot.id] = {"type": "blink"}
        return actions, raw_actions


def run_match(file_a: str, file_b: str):
    state, mapping = build_game_state(Path(file_a), Path(file_b))
    sandbox = SandboxManager(mapping["files"], mapping["class_map"])

    log = []
    turn = 0
    winner = None
    while turn < 100 and not winner:
        actions, raw_actions = sandbox.decide(state)
        state = TurnEngine(state, actions).run()
        log.append(
            {
                "turn": turn,
                "actions": raw_actions,
                "positions": {b.id: [b.position.x, b.position.y] for b in state.all_bots()},
                "hp": {b.id: b.hp for b in state.all_bots()},
            }
        )
        alive_by_team = {}
        for b in state.all_bots():
            alive_by_team.setdefault(b.team_id, []).append(b)
        if len(alive_by_team) == 1:
            winner = next(iter(alive_by_team))
        turn += 1

    if not winner:
        winner = "draw"

    log_path = Path(f"local_match_{uuid.uuid4()}.json")
    log_path.write_text(json.dumps(log), encoding="utf-8")
    print(f"Winner: {winner}\nTurns: {turn}\nLog: {log_path}")


if __name__ == "__main__":
    try:
        mp.set_start_method("fork")
    except RuntimeError:
        pass
    if len(sys.argv) != 3:
        print("Usage: python run_match.py team1.py team2.py")
        sys.exit(1)
    run_match(sys.argv[1], sys.argv[2])
