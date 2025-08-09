from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple
from pathlib import Path

from .models import MapSpec, TerrainCell, ZoneCell


@dataclass
class Rules:
    grid_size: Dict[str, List[int]]
    wall_density: float
    forest_density: float
    swamp_density: float
    ice_density: float
    water_density: float
    zone_counts: Dict[str, int]
    spawn_margin: int
    ensure_path_between_spawns: bool


def load_rules(path: str | os.PathLike | None = None) -> Rules:
    # Resolve rules path robustly across different working directories
    if path:
        candidates = [Path(path)]
    else:
        env_path = os.getenv("MAP_RULES_PATH")
        candidates = []
        if env_path:
            candidates.append(Path(env_path))
        # repo root /maps relative to this file: backend/app/maps/ -> repo_root/maps/map_rules.json
        try:
            repo_root = Path(__file__).resolve().parents[3]
            candidates.append(repo_root / "maps" / "map_rules.json")
        except Exception:
            pass
        # fallback to CWD
        candidates.append(Path.cwd() / "maps" / "map_rules.json")
        candidates.append(Path("maps/map_rules.json"))
    rules_path = next((p for p in candidates if p and p.exists()), None)
    if not rules_path:
        raise FileNotFoundError("map_rules.json not found; set MAP_RULES_PATH or place file under /maps")
    data = json.loads(rules_path.read_text(encoding="utf-8"))
    return Rules(
        grid_size=data.get("grid_size", {"duo": [10, 10], "quad": [15, 15]}),
        wall_density=float(data.get("wall_density", 0.08)),
        forest_density=float(data.get("forest_density", 0.05)),
        swamp_density=float(data.get("swamp_density", 0.03)),
        ice_density=float(data.get("ice_density", 0.02)),
        water_density=float(data.get("water_density", 0.00)),
        zone_counts=data.get("zone_counts", {"heal": 2, "damage": 2, "boost": 1, "teleport": 1}),
        spawn_margin=int(data.get("spawn_margin", 2)),
        ensure_path_between_spawns=bool(data.get("ensure_path_between_spawns", True)),
    )


def _neighbors4(x: int, y: int, n: int) -> List[Tuple[int, int]]:
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < n and 0 <= ny < n:
            yield nx, ny


def _mask_from_positions(positions: List[Tuple[int, int]], n: int) -> List[List[bool]]:
    m = [[False] * n for _ in range(n)]
    for x, y in positions:
        if 0 <= x < n and 0 <= y < n:
            m[y][x] = True
    return m


def _flood_fill_passable(n: int, blocked: Set[Tuple[int, int]], start: Tuple[int, int]) -> Set[Tuple[int, int]]:
    seen: Set[Tuple[int, int]] = set()
    stack = [start]
    while stack:
        x, y = stack.pop()
        if (x, y) in seen or (x, y) in blocked:
            continue
        seen.add((x, y))
        for nx, ny in _neighbors4(x, y, n):
            if (nx, ny) not in seen and (nx, ny) not in blocked:
                stack.append((nx, ny))
    return seen


def _choose_spawns(n: int, mode: str, rng: random.Random, margin: int) -> Dict[str, List[Tuple[int, int]]]:
    if mode == "duo":
        count = 5
        top = [(i, 0) for i in range(0, n, max(1, n // count))][:count]
        bottom = [(i, n - 1) for i in range(0, n, max(1, n // count))][:count]
        return {"teamA": top, "teamB": bottom}
    else:
        # quad: pick corners
        def block(cx, cy):
            return [(cx + dx, cy + dy) for dx in (0, 1, 2) for dy in (0, 1, 2)]

        return {
            "teamA": block(0, 0)[:5],
            "teamB": block(n - 3, 0)[:5],
            "teamC": block(0, n - 3)[:5],
            "teamD": block(n - 3, n - 3)[:5],
        }


def generate_map(mode: str, seed: int, rules: Rules) -> MapSpec:
    rng = random.Random(int(seed))
    size_list = rules.grid_size.get(mode) or rules.grid_size.get("duo")
    n = int(size_list[0]) if isinstance(size_list, list) else (10 if mode == "duo" else 15)

    # Start with empty
    static_walls: Set[Tuple[int, int]] = set()
    terrain_at: Dict[Tuple[int, int], str] = {}
    zones: List[ZoneCell] = []

    total = n * n
    def pick_unique_positions(k: int, forbidden: Set[Tuple[int, int]]) -> List[Tuple[int, int]]:
        candidates = [(x, y) for x in range(n) for y in range(n) if (x, y) not in forbidden]
        rng.shuffle(candidates)
        return candidates[:k]

    # Place walls by density (avoid margins near borders to ease pathing a bit)
    wall_target = int(total * rules.wall_density)
    forbidden: Set[Tuple[int, int]] = set()
    picks = pick_unique_positions(wall_target, forbidden)
    static_walls.update(picks)

    # Terrain densities (mutually exclusive)
    def place_terrain(kind: str, density: float):
        k = int(total * density)
        forb = set(static_walls) | set(terrain_at.keys())
        for pos in pick_unique_positions(k, forb):
            terrain_at[pos] = kind

    place_terrain("forest", rules.forest_density)
    place_terrain("swamp", rules.swamp_density)
    place_terrain("ice", rules.ice_density)
    if rules.water_density > 0:
        place_terrain("water", rules.water_density)

    # Spawns then refine walls to ensure paths if requested
    spawns = _choose_spawns(n, mode, rng, rules.spawn_margin)
    spawn_cells = {tuple(p) for team in spawns.values() for p in team}
    # Ensure no walls on spawn cells
    static_walls = {p for p in static_walls if p not in spawn_cells}
    # Enforce spawn margin: clear walls within margin around spawns and avoid placing zones later
    def within_margin_cells(points: Set[Tuple[int, int]], m: int) -> Set[Tuple[int, int]]:
        out: Set[Tuple[int, int]] = set()
        for (sx, sy) in points:
            for dx in range(-m, m + 1):
                for dy in range(-m, m + 1):
                    if abs(dx) + abs(dy) <= m:
                        x, y = sx + dx, sy + dy
                        if 0 <= x < n and 0 <= y < n:
                            out.add((x, y))
        return out
    margin_cells = within_margin_cells(spawn_cells, rules.spawn_margin)
    static_walls = {p for p in static_walls if p not in margin_cells}

    if rules.ensure_path_between_spawns and mode == "duo":
        # ensure a path between average spawn positions of A and B ignoring forest and ice/swamp; walls block
        def centroid(points: List[Tuple[int, int]]):
            sx = sum(x for x, _ in points) / len(points)
            sy = sum(y for _, y in points) / len(points)
            return int(round(sx)), int(round(sy))

        a_c = centroid(spawns["teamA"]) ; b_c = centroid(spawns["teamB"])
        blocked = set(static_walls)
        reach = _flood_fill_passable(n, blocked, a_c)
        if b_c not in reach:
            # carve a simple Manhattan corridor: clear walls along x then y
            x0, y0 = a_c; x1, y1 = b_c
            cx, cy = x0, y0
            while cx != x1:
                cx += 1 if x1 > cx else -1
                static_walls.discard((cx, cy))
            while cy != y1:
                cy += 1 if y1 > cy else -1
                static_walls.discard((cx, cy))

    # Zones: avoid walls and water
    forb_for_zones = set(static_walls) | set(margin_cells) | {pos for pos, t in terrain_at.items() if t == "water"}
    def add_zone(kind: str, count: int):
        nonlocal zones
        for pos in pick_unique_positions(count, forb_for_zones):
            zones.append(ZoneCell(type=kind, pos=pos))
            forb_for_zones.add(pos)

    zc = rules.zone_counts
    add_zone("heal", int(zc.get("heal", 0)))
    add_zone("damage", int(zc.get("damage", 0)))
    add_zone("boost", int(zc.get("boost", 0)))
    add_zone("teleport", int(zc.get("teleport", 0)))

    terrain_cells = [TerrainCell(type=t, pos=pos) for pos, t in terrain_at.items()]

    return MapSpec(
        name=f"seed_map_{seed}",
        size={mode: [n, n]},
        seed=int(seed),
        static_walls=sorted(list(static_walls)),
        terrain=terrain_cells,
        zones=zones,
        spawn_positions={mode: spawns},
        dynamic_rules={
            "trap_visible_to_allies": True,
            "wall_blocks_movement": True,
            "wall_blocks_los": True,
            "forest_blocks_los": True,
            "swamp_slow": True,
            "ice_sliding": True,
            "zones_apply_on_step": False,
        },
    )


