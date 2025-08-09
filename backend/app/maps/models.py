from __future__ import annotations

from typing import List, Literal, Optional, Tuple
from pydantic import BaseModel, Field, validator


TerrainType = Literal["forest", "swamp", "ice", "water"]
ZoneType = Literal["heal", "damage", "boost", "teleport"]


class TerrainCell(BaseModel):
    type: TerrainType
    pos: Tuple[int, int]


class ZoneCell(BaseModel):
    type: ZoneType
    pos: Tuple[int, int]


class MapSpec(BaseModel):
    name: str
    size: dict  # {"duo": [w,h], "quad": [w,h]} or {"both": [w,h]}
    seed: Optional[int] = None
    static_walls: List[Tuple[int, int]] = Field(default_factory=list)
    terrain: List[TerrainCell] = Field(default_factory=list)
    zones: List[ZoneCell] = Field(default_factory=list)
    spawn_positions: dict | None = None
    dynamic_rules: dict = Field(default_factory=dict)
    disabled: bool = False

    @validator("terrain", each_item=True, pre=True)
    def _terrain_tuple(cls, v):
        if isinstance(v, dict) and "pos" in v and isinstance(v.get("pos"), list):
            v["pos"] = tuple(v["pos"])  # type: ignore
        return v

    @validator("zones", each_item=True, pre=True)
    def _zones_tuple(cls, v):
        if isinstance(v, dict) and "pos" in v and isinstance(v.get("pos"), list):
            v["pos"] = tuple(v["pos"])  # type: ignore
        return v

    def grid_size_for_mode(self, mode: str, default_duo: int = 10, default_quad: int = 15) -> int:
        if isinstance(self.size, dict):
            if mode in self.size and isinstance(self.size[mode], list):
                wh = self.size[mode]
                return int(wh[0])
            # fallback to single size
            if "both" in self.size and isinstance(self.size["both"], list):
                return int(self.size["both"][0])
        return default_duo if mode == "duo" else default_quad


