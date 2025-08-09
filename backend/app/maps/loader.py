from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

from .models import MapSpec


def load_static_maps(directory: str | os.PathLike) -> List[MapSpec]:
    path = Path(directory)
    if not path.exists():
        return []
    maps: List[MapSpec] = []
    for p in path.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            ms = MapSpec(**data)
            if not ms.disabled:
                maps.append(ms)
        except Exception:
            continue
    return maps


